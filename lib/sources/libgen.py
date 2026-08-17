"""LibGen source adapter using libgen-api-enhanced library.

LibGen is used as a fallback when Anna's Archive quota is exhausted
or unavailable. LibGen has no quota limits but requires rate limiting
to avoid being blocked.
"""

import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import requests
from bs4 import BeautifulSoup

from .base import SourceAdapter
from .config import SourceConfig
from .models import DownloadResult, SourceType, UnifiedBookResult

# CRITICAL: Import is libgen_api_enhanced, NOT libgen_api
from libgen_api_enhanced import LibgenSearch
from libgen_api_enhanced import search_request as _lge_search_request

logger = logging.getLogger("zlibrary.sources")

# libgen.li serves its default-nginx stub (HTTP 200, ~640 bytes, no results
# table) to blocklisted tool User-Agents — python-requests' default, python-httpx's
# default, and curl among them — while any identifying UA gets the real page
# (measured 2026-08-17, issue #124). An honest self-identifying UA is admitted,
# so no browser string is needed. Used by BOTH the search path (via the shim
# below) and the download path (ads.php serves the same stub to blocked UAs).
USER_AGENT = "zlibrary-mcp (+https://github.com/rookslog/zlibrary-mcp)"


class SourceParseError(Exception):
    """A source served a page the adapter could not parse.

    Distinct from an empty result: the page did not contain the structure
    results live in, so reporting "no matches" would be false (#124).
    """


class _RequestsWithUA:
    """Stand-in for the `requests` module inside libgen-api-enhanced.

    libgen-api-enhanced 1.3 calls the module-level ``requests.get`` with no
    headers hook, which sends the blocklisted default UA (see USER_AGENT note
    above). This shim is swapped in for the ``requests`` reference inside its
    ``search_request`` module: same ``.get``/``.exceptions`` surface, the
    identifying UA added, and the last search response recorded so
    ``LibgenAdapter.search`` can tell "no matches" from "served a page with no
    results table".
    """

    exceptions = requests.exceptions

    def __init__(self) -> None:
        self.last_response: Optional[requests.Response] = None

    def get(self, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("User-Agent", USER_AGENT)
        response = requests.get(url, headers=headers, **kwargs)
        self.last_response = response
        return response


_search_requests = _RequestsWithUA()
_lge_search_request.requests = _search_requests

# Mirrors tried in order when resolving a download. Different mirrors hand off
# to different CDN nodes (cdn3/cdn4/... .booksdl.lc) and those nodes fail
# independently — 2026-08-10, libgen.li -> cdn4 failed TLS while vg/la -> cdn3
# served real bytes. Failing over between mirrors therefore also routes around
# a dead CDN node, which is why this list exists rather than a single default.
FALLBACK_MIRRORS = ("li", "vg", "la")


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
        """Search for books matching query.

        Uses libgen-api-enhanced LibgenSearch.search_title() wrapped in
        asyncio.to_thread() to avoid blocking the event loop.

        Args:
            query: Search string (title, author, ISBN, etc.)
            **kwargs: Ignored (for interface compatibility)

        Returns:
            List of UnifiedBookResult with source=LIBGEN
        """
        await self._rate_limit()
        _search_requests.last_response = None

        def _search_sync():
            s = LibgenSearch(mirror=self.mirror)
            return s.search_title(query)

        results = await asyncio.to_thread(_search_sync)

        if not results:
            # A genuinely-empty search still renders the (empty) results
            # table (verified 2026-08-17), so a page WITHOUT the table means
            # the mirror served something else entirely — a UA-block stub or
            # a layout change — and "no results" would be a false report.
            page = _search_requests.last_response
            if page is not None and "tablelibgen" not in page.text:
                title_match = re.search(r"<title>([^<]*)</title>", page.text, re.I)
                page_title = title_match.group(1).strip() if title_match else ""
                raise SourceParseError(
                    f"LibGen search page had no results table (HTTP "
                    f"{page.status_code}, {len(page.text)} bytes, title "
                    f"{page_title!r}) — parse failure, not an empty result"
                )
            return []

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
        try:
            response = await client.get(url, headers={"Range": "bytes=0-2047"})
        except Exception as exc:
            return False, type(exc).__name__

        if "/ads.php" in str(response.url):
            return False, "key expired (bounced to ads.php)"
        if response.status_code not in (200, 206):
            return False, f"HTTP {response.status_code}"
        if "text/html" in response.headers.get("content-type", ""):
            return False, "served HTML, not a file"
        if not response.content[:4] == b"%PDF" and len(response.content) < 512:
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
            ValueError: If no mirror yields a download key
        """
        attempts = []

        # The identifying UA matters here too: ads.php serves the same
        # UA-blocklist stub to python-httpx's default UA (measured
        # 2026-08-17, #124), which surfaces as "no GET link" on every mirror.
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            for mirror in self._mirror_candidates():
                await self._rate_limit()
                try:
                    key = await self._resolve_key(client, mirror, md5)
                except Exception as exc:  # network, TLS, HTTP error
                    attempts.append(f"{mirror}: {type(exc).__name__}")
                    logger.warning(f"LibGen mirror {mirror} failed for {md5}: {exc}")
                    continue

                if not key:
                    attempts.append(f"{mirror}: no GET link")
                    continue

                url = f"https://libgen.{mirror}/get.php?md5={md5}&key={key}"
                ok, detail = await self._serves_bytes(client, url)
                if not ok:
                    attempts.append(f"{mirror}: {detail}")
                    logger.warning(
                        f"LibGen mirror {mirror} resolved but cannot serve {md5}: {detail}"
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

        raise ValueError(
            f"No LibGen mirror could resolve a download for {md5} "
            f"(tried {', '.join(attempts)})"
        )

    async def close(self) -> None:
        """Clean up resources.

        LibgenSearch doesn't maintain persistent connections, and
        get_download_url scopes its client to the call, so this is a no-op.
        """
        pass
