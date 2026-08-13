"""LibGen source adapter using libgen-api-enhanced library.

LibGen is used as a fallback when Anna's Archive quota is exhausted
or unavailable. LibGen has no quota limits but requires rate limiting
to avoid being blocked.
"""

import asyncio
import logging
import time
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import SourceAdapter
from .config import SourceConfig
from .errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderUnreachableError,
    SourceError,
)
from .models import DownloadResult, SourceType, UnifiedBookResult
from .net import (
    bounded_await,
    build_timeout,
    classify_httpx_error,
    classify_requests_error,
    probe_host,
    run_bounded,
)

# CRITICAL: Import is libgen_api_enhanced, NOT libgen_api
from libgen_api_enhanced import LibgenSearch

logger = logging.getLogger("zlibrary.sources")

PROVIDER = "libgen"

# Mirrors tried in order when resolving a download. Different mirrors hand off
# to different CDN nodes (cdn3/cdn4/... .booksdl.lc) and those nodes fail
# independently — 2026-08-10, libgen.li -> cdn4 failed TLS while vg/la -> cdn3
# served real bytes. Failing over between mirrors therefore also routes around
# a dead CDN node, which is why this list exists rather than a single default.
FALLBACK_MIRRORS = ("li", "vg", "la")


def mirror_host(mirror: str) -> str:
    """Hostname for a mirror suffix, matching what LibgenSearch builds."""
    return f"libgen.{mirror}"


class LibgenAdapter(SourceAdapter):
    """LibGen source adapter.

    Wraps the synchronous libgen-api-enhanced library with async interface.
    Implements rate limiting to avoid being blocked by LibGen servers.

    Attributes:
        MIN_REQUEST_INTERVAL: Minimum seconds between requests (2.0)
    """

    MIN_REQUEST_INTERVAL = 2.0  # seconds between requests

    def __init__(self, config: SourceConfig):
        """Initialize adapter with configuration.

        Args:
            config: SourceConfig with libgen_mirror setting
        """
        self.config = config
        self.mirror = config.libgen_mirror
        self._last_request = 0.0

    async def _rate_limit(self) -> None:
        """Enforce rate limiting between requests.

        Sleeps if the last request was less than MIN_REQUEST_INTERVAL ago.
        """
        elapsed = time.time() - self._last_request
        if elapsed < self.MIN_REQUEST_INTERVAL:
            await asyncio.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.time()

    async def search(self, query: str, **kwargs) -> List[UnifiedBookResult]:
        """Search for books matching query, failing over between mirrors.

        Two hazards are handled here that the previous implementation was not
        bounded against:

        - `libgen_api_enhanced` issues `requests.get(...)` with **no timeout**
          (search_request.py:177), so a mirror that drops SYNs blocks forever.
          It runs under `run_bounded` on a daemon thread: the await is capped
          at `config.total_timeout` and an abandoned call cannot outlive the
          process. Under `asyncio.to_thread` it did exactly that — three
          orphaned bridge processes, the oldest 9h10m old, on 2026-08-11.
        - Search used only the configured mirror while `get_download_url`
          already walked `_mirror_candidates()`. It now walks the same list, so
          one dead mirror no longer means no results.

        Args:
            query: Search string (title, author, ISBN, etc.)
            **kwargs: Ignored (for interface compatibility)

        Returns:
            List of UnifiedBookResult with source=LIBGEN

        Raises:
            AllSourcesFailedError: If no mirror could complete the search
        """
        failures = []

        for mirror in self._mirror_candidates():
            host = mirror_host(mirror)
            try:
                await self._preflight(mirror)
            except ProviderUnreachableError as exc:
                logger.warning("LibGen mirror %s unreachable: %s", mirror, exc)
                failures.append(exc)
                continue

            await self._rate_limit()

            def _search_sync(mirror=mirror):
                return LibgenSearch(mirror=mirror).search_title(query)

            try:
                results = await run_bounded(
                    _search_sync,
                    self.config.total_timeout,
                    provider=PROVIDER,
                    host=host,
                    operation="search",
                )
            except Exception as exc:
                failure = self._as_source_error(exc, host)
                logger.warning("LibGen search failed on %s: %s", mirror, failure)
                failures.append(failure)
                continue

            return self._to_unified(results or [])

        raise AllSourcesFailedError(f"LibGen search for {query!r}", failures)

    def _to_unified(self, results) -> List[UnifiedBookResult]:
        """Convert libgen-api-enhanced books into UnifiedBookResult."""
        return [
            UnifiedBookResult(
                md5=getattr(book, "md5", "") or "",
                title=getattr(book, "title", "") or "",
                author=getattr(book, "author", "") or "",
                year=str(getattr(book, "year", "") or ""),
                extension=getattr(book, "extension", "") or "",
                size=getattr(book, "size", "") or "",
                source=SourceType.LIBGEN,
                # Deliberately empty: the only URL the search response can
                # offer is a .onion link needing Tor, and a clearnet key is
                # short-lived (see get_download_url), so resolving one per
                # search hit would waste a request per result and expire
                # before use. Callers resolve on demand via get_download_url.
                download_url="",
                extra={
                    "id": getattr(book, "id", ""),
                    "language": getattr(book, "language", ""),
                    "pages": getattr(book, "pages", ""),
                },
            )
            for book in results
        ]

    def _mirror_candidates(self) -> List[str]:
        """Mirrors to try, configured one first, without duplicates."""
        return [self.mirror] + [m for m in FALLBACK_MIRRORS if m != self.mirror]

    async def _preflight(self, mirror: str) -> None:
        """Fail fast if a mirror is not reachable.

        This matters more for LibGen than for Anna's: once the third-party
        search call starts it cannot be interrupted, only abandoned. Probing
        first means an unroutable mirror (libgen.is resolves to
        193.218.118.42 but drops every SYN, measured 2026-08-11) costs one
        bounded probe and never enters that call.

        Args:
            mirror: Mirror suffix, e.g. 'li'

        Raises:
            ProviderUnreachableError: If the mirror does not resolve or connect
        """
        if not self.config.preflight_enabled:
            return
        await probe_host(
            PROVIDER,
            mirror_host(mirror),
            timeout=self.config.preflight_timeout,
        )

    def _as_source_error(self, exc: BaseException, host: str) -> Exception:
        """Convert a failure into a provider-attributed error.

        Handles both httpx exceptions (our own `get_download_url` requests) and
        the `requests`-based exceptions that surface from the LibGen library.

        Args:
            exc: Exception raised while talking to a mirror
            host: Mirror hostname for attribution

        Returns:
            An already-attributed error unchanged, otherwise a
            ProviderResponseError or ProviderUnreachableError.
        """
        if isinstance(exc, SourceError):
            return exc
        if isinstance(exc, httpx.HTTPError):
            try:
                host = exc.request.url.host or host
            except RuntimeError:
                # Some callers construct an httpx error without attaching a
                # request. Retain the mirror origin as the safe attribution.
                pass
            reason, detail = classify_httpx_error(exc)
        else:
            reason, detail = classify_requests_error(exc)
        if reason in ("http_error", "protocol_error"):
            return ProviderResponseError(PROVIDER, host, detail, reason=reason)
        return ProviderUnreachableError(PROVIDER, host, detail, reason=reason)

    async def _resolve_key(
        self, client: httpx.AsyncClient, mirror: str, md5: str
    ) -> Optional[str]:
        """Scrape the one-time CDN key out of a mirror's ads.php page.

        `ads.php?md5=<md5>` is addressable directly from the hash — no search
        is involved — and carries a single `GET` anchor whose href holds the
        key. Returns None if the page has no usable key.
        """
        url = f"https://libgen.{mirror}/ads.php?md5={md5}"
        response = await client.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a"):
            if anchor.get_text(strip=True).upper() != "GET":
                continue
            href = anchor.get("href")
            if not href:
                continue
            params = parse_qs(urlparse(urljoin(url, href)).query)
            key = (params.get("key") or [None])[0]
            if key:
                return key
        return None

    async def _serves_bytes(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[bool, str]:
        """Check that a resolved URL actually delivers a file.

        A mirror can hand out a valid key while the CDN node it redirects to
        is dead, so resolving is not evidence of downloadability (measured
        2026-08-10: libgen.li resolved fine but its cdn4 node failed TLS).
        This asks for 2KB and inspects what comes back.

        Two non-obvious failure shapes are treated as failure, not success:
        an expired key silently redirects back to `/ads.php`, and a dead node
        can answer 200 with an HTML error page.
        """
        # Transport exceptions must reach the normal classifier. Returning
        # their class name as a semantic non-byte result reclassified DNS/TLS
        # and timeout failures as protocol_error at the mirror aggregate.
        inspected = bytearray()
        async with client.stream(
            "GET", url, headers={"Range": "bytes=0-2047"}
        ) as response:
            if "/ads.php" in str(response.url):
                return False, "key expired (bounced to ads.php)"
            if response.status_code >= 400:
                response.raise_for_status()
            if response.status_code not in (200, 206):
                return False, f"HTTP {response.status_code}"
            if "text/html" in response.headers.get("content-type", ""):
                return False, "served HTML, not a file"

            async for chunk in response.aiter_bytes():
                inspected.extend(chunk[: 2048 - len(inspected)])
                if len(inspected) >= 2048:
                    break

            if not inspected[:4] == b"%PDF" and len(inspected) < 512:
                return False, "response too small to be a file"

            cdn = urlparse(str(response.url)).hostname or "?"
            return True, cdn

    async def get_download_url(self, md5: str) -> DownloadResult:
        """Resolve a clearnet download URL for a book by MD5 hash.

        Walks `_mirror_candidates()` and returns the first mirror that yields a
        key. The previous implementation looked the book up with
        `search_title(md5)`, but LibGen's title index contains no md5 strings,
        so that returned nothing and this method raised for every input — it
        had never worked against the live service (the unit suite mocks
        LibgenSearch, so it stayed green).

        The returned URL is short-lived: the key expires in well under 2.5
        hours, after which `get.php` silently 307s back to `/ads.php` instead
        of erroring. Resolve immediately before downloading; never cache.

        Args:
            md5: MD5 hash identifying the book

        Returns:
            DownloadResult with URL and no quota_info (LibGen has no quota)

        Raises:
            AllSourcesFailedError: If no mirror yields a working download URL
        """
        failures: List[SourceError] = []

        async with httpx.AsyncClient(
            timeout=build_timeout(self.config), follow_redirects=True
        ) as client:
            for mirror in self._mirror_candidates():
                try:
                    await self._preflight(mirror)
                except ProviderUnreachableError as exc:
                    failures.append(exc)
                    logger.warning(f"LibGen mirror {mirror} unreachable: {exc}")
                    continue

                await self._rate_limit()

                async def resolve_attempt() -> tuple[Optional[str], str]:
                    """Resolve and validate one mirror under one total budget."""
                    key = await self._resolve_key(client, mirror, md5)
                    if not key:
                        return None, "no GET link"

                    candidate = f"https://libgen.{mirror}/get.php?md5={md5}&key={key}"
                    ok, detail = await self._serves_bytes(client, candidate)
                    return (candidate if ok else None), detail

                try:
                    url, detail = await bounded_await(
                        resolve_attempt(),
                        self.config.total_timeout,
                        provider=PROVIDER,
                        host=mirror_host(mirror),
                        operation="download resolution",
                    )
                except Exception as exc:  # network, TLS, HTTP error
                    failure = self._as_source_error(exc, mirror_host(mirror))
                    failures.append(failure)
                    logger.warning(
                        f"LibGen mirror {mirror} failed for {md5}: {failure}"
                    )
                    continue

                if not url:
                    failures.append(
                        ProviderResponseError(
                            PROVIDER,
                            mirror_host(mirror),
                            detail,
                            reason="protocol_error",
                        )
                    )
                    logger.warning(
                        f"LibGen mirror {mirror} could not serve {md5}: {detail}"
                    )
                    continue

                logger.info(
                    f"LibGen download resolved on mirror {mirror} via {detail} for {md5}"
                )
                return DownloadResult(
                    url=url,
                    source=SourceType.LIBGEN,
                    quota_info=None,  # LibGen has no quota
                )

        raise AllSourcesFailedError("download", failures)

    async def close(self) -> None:
        """Clean up resources.

        LibgenSearch doesn't maintain persistent connections, and
        get_download_url scopes its client to the call, so this is a no-op.
        """
        pass
