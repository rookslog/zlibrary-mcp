"""LibGen source adapter using libgen-api-enhanced library.

LibGen is used as a fallback when Anna's Archive quota is exhausted
or unavailable. LibGen has no quota limits but requires rate limiting
to avoid being blocked.
"""

import asyncio
import logging
import re
import threading
import time
from typing import AsyncIterator, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import requests
from bs4 import BeautifulSoup

from .base import SourceAdapter
from .config import DEFAULT_LIBGEN_USER_AGENT, SourceConfig
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
from libgen_api_enhanced import search_request as _lge_search_request

logger = logging.getLogger("zlibrary.sources")

PROVIDER = "libgen"

# libgen.li serves its default-nginx stub (HTTP 200, ~640 bytes, no results
# table) to blocklisted tool User-Agents while an admitted UA gets the real
# page. Used by BOTH the search path (via the shim below) and the download path
# (ads.php serves the same stub to blocked UAs).
#
# The blocklist widened on 2026-08-23 to include the honest self-identifying
# string that #124 had established as admitted, taking search AND ads.php down
# together (#141). The policy, its default and the measurements now live on
# `SourceConfig.libgen_user_agent` so a further widening is an env change
# rather than a release; see lib/sources/config.py.
#
# Kept as a module constant for backwards compatibility — scripts/check_upstream.py
# imports it — but every request reads the config so `LIBGEN_USER_AGENT` takes
# effect without a reimport.
USER_AGENT = DEFAULT_LIBGEN_USER_AGENT


def get_user_agent(config: Optional[SourceConfig] = None) -> str:
    """UA for every LibGen request, honouring `LIBGEN_USER_AGENT`.

    Pass `config` wherever the caller holds one — an adapter constructed as
    `LibgenAdapter(SourceConfig(libgen_user_agent=...))` must send that UA, not
    re-read the environment behind its caller's back (Codex on #146). Only the
    module-level search shim, which has no config to hold, falls through to the
    environment.

    Read per request rather than captured at import: `get_source_config` is
    deliberately uncached so the environment can change under a running
    process, and a UA pinned at import would silently ignore the override
    that exists to answer the next blocklist widening.
    """
    if config is not None:
        return config.libgen_user_agent or DEFAULT_LIBGEN_USER_AGENT
    try:
        from .config import get_source_config  # noqa: PLC0415

        return get_source_config().libgen_user_agent or DEFAULT_LIBGEN_USER_AGENT
    except Exception:  # noqa: BLE001 - a config fault must not remove the UA
        return DEFAULT_LIBGEN_USER_AGENT


# Used only when the configuration cannot be read at all. Any real config
# supplies its own budgets; this exists so a broken environment still gets a
# finite timeout rather than none (the failure the shim default prevents).
SHIM_FALLBACK_TIMEOUT = 30.0


def _shim_default_timeout():
    """Default `requests` timeout for library calls that omit one.

    Derived from the same `SourceConfig` the adapter uses, so raising
    `BOOK_SOURCE_READ_TIMEOUT` / `BOOK_SOURCE_TOTAL_TIMEOUT` for a slow
    network actually reaches the LibGen search path — a hard-coded 30s
    silently capped it and defeated supported tuning (Codex on #133).

    Shaped like `net.build_timeout`: a (connect, read) pair with the same two
    config fields behind it. Both are clamped to `total_timeout`, because the
    call runs under `run_bounded(..., config.total_timeout)` — a per-request
    budget above the wall-clock budget could never be reached and would only
    misreport which deadline fired.

    Returns:
        (connect, read) seconds, or SHIM_FALLBACK_TIMEOUT if config is unreadable
    """
    try:
        # Imported at call time, not module scope: the value must follow
        # environment changes (get_source_config is deliberately uncached).
        from .config import get_source_config  # noqa: PLC0415

        config = get_source_config()
        total = float(config.total_timeout)
        return (
            min(float(config.connect_timeout), total),
            min(float(config.read_timeout), total),
        )
    except Exception:  # noqa: BLE001 - a config fault must not remove the bound
        logger.warning(
            "LibGen shim could not read the source config; "
            "falling back to a %.0fs request timeout",
            SHIM_FALLBACK_TIMEOUT,
        )
        return SHIM_FALLBACK_TIMEOUT


class _RequestsWithUA:
    """Stand-in for the `requests` module inside libgen-api-enhanced.

    libgen-api-enhanced 1.3 calls the module-level ``requests.get`` with no
    headers hook, which sends the blocklisted default UA (see USER_AGENT note
    above). This shim is swapped in for the ``requests`` reference inside its
    ``search_request`` module: same ``.get``/``.exceptions`` surface, the
    identifying UA added, and the last search response recorded so
    ``LibgenAdapter.search`` can tell "no matches" from "served a page with no
    results table".

    ``last_response`` and ``user_agent`` are **thread-local**. The search runs
    under `net.run_bounded`, which abandons its daemon thread when the budget
    elapses; an abandoned `requests.get` that completes minutes later would
    otherwise overwrite the slot a *later* mirror attempt is about to read,
    and the adapter would attribute one mirror's stub page to another. Each
    bounded attempt gets its own thread, so each gets its own slot.

    ``user_agent`` rides the same mechanism so an adapter's *own*
    `SourceConfig` reaches the search path. This shim is a module singleton
    swapped into third-party code, so it cannot hold a config of its own;
    `LibgenAdapter.search` sets the slot inside the bounded thread that is
    about to use it. Unset, it falls back to the environment.
    """

    exceptions = requests.exceptions

    def __init__(self) -> None:
        self._state = threading.local()

    @property
    def last_response(self) -> Optional[requests.Response]:
        return getattr(self._state, "last_response", None)

    @last_response.setter
    def last_response(self, response: Optional[requests.Response]) -> None:
        self._state.last_response = response

    @property
    def user_agent(self) -> Optional[str]:
        return getattr(self._state, "user_agent", None)

    @user_agent.setter
    def user_agent(self, value: Optional[str]) -> None:
        self._state.user_agent = value

    def get(self, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("User-Agent", self.user_agent or get_user_agent())
        # libgen-api-enhanced omits the timeout on some calls; a mirror that
        # accepts the connection and never responds would otherwise hold the
        # search thread past every outer budget (Codex on #128).
        kwargs.setdefault("timeout", _shim_default_timeout())
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

# A usable search row carries a real md5; anything else (empty, ISBN,
# citation text from a column-shifted article row) cannot be downloaded.
_MD5_HEX_RE = re.compile(r"[0-9a-fA-F]{32}")


def mirror_host(mirror: str) -> str:
    """Hostname for a mirror suffix, matching what LibgenSearch builds."""
    return f"libgen.{mirror}"


def _nginx_stub(text: str) -> bool:
    """True when a body is nginx's default page rather than LibGen's.

    This is what a blocklisted User-Agent receives: HTTP 200, ~640 bytes,
    `<title>Welcome to nginx!</title>`. It is not an error status and not an
    empty result, so nothing upstream distinguishes it from a real outage or a
    catalogue miss unless it is classified here (#141).

    Matched on the title rather than the byte count: the length is incidental
    and the same block on a different vhost may pad differently, while the
    default-page title is the thing that identifies it.

    Scoped to the title element specifically, not a body-wide substring: a
    real LibGen record whose own title or description contains the phrase
    would otherwise be classified as a block, and `_resolve_key` would discard
    a page carrying a perfectly good GET anchor (Codex on #146).
    """
    match = re.search(r"<title>([^<]*)</title>", text, re.I)
    return bool(match and "welcome to nginx" in match.group(1).lower())


def _blocked_ua_detail(context: str, config: Optional[SourceConfig] = None) -> str:
    """Detail text naming the UA a mirror refused, and how to change it."""
    return (
        f"{context} served nginx's default stub for User-Agent "
        f"{get_user_agent(config)!r} — this UA is blocklisted, which is NOT an "
        f"outage and NOT an empty catalogue. Set LIBGEN_USER_AGENT to a "
        f"string the mirror admits (#141)."
    )


def _unparseable_search_page(
    page, config: Optional[SourceConfig] = None
) -> Optional[str]:
    """Detail text when a zero-result page is a parse failure, else None.

    A genuinely-empty search still renders the (empty) results table (verified
    2026-08-17), so a page WITHOUT `tablelibgen` means the mirror served
    something else entirely — a UA-block stub or a layout change — and "no
    results" would be a false report (#124).

    This is #124's `SourceParseError` folded into the #106 error taxonomy: the
    caller raises it as a `ProviderResponseError`/`protocol_error` attributed
    to the mirror host, so one stubbed mirror fails over to the next instead
    of aborting the whole search. The status, byte count, and page title are
    retained because they are what distinguishes a stub from a redesign.

    Args:
        page: The recorded `requests.Response`, or None if nothing was fetched

    Returns:
        A detail string for the error envelope, or None if the page is fine
        (or absent — a fully-mocked search records no page and must stay an
        ordinary empty result).
    """
    if page is None:
        return None
    text = getattr(page, "text", "") or ""
    if "tablelibgen" in text:
        return None
    status = getattr(page, "status_code", "?")
    title_match = re.search(r"<title>([^<]*)</title>", text, re.I)
    page_title = title_match.group(1).strip() if title_match else ""
    if _nginx_stub(text):
        # #124's diagnostics are retained verbatim alongside the new naming:
        # title, status and byte count are what distinguish a stub from a
        # redesign, and dropping them would trade one diagnosis for another.
        return (
            f"{_blocked_ua_detail('search page', config)} "
            f"[HTTP {status}, {len(text)} bytes, title {page_title!r}]"
        )
    return (
        f"search page had no results table (HTTP "
        f"{status}, {len(text)} bytes, title "
        f"{page_title!r}) — parse failure, not an empty result"
    )


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

        A mirror that answers with a page carrying no results table is a third
        hazard (#124): it is a parse failure, not an empty result, and it is
        recorded as a typed per-mirror failure so the walk continues.

        Args:
            query: Search string (title, author, ISBN, etc.)
            **kwargs: Ignored (for interface compatibility)

        Returns:
            List of UnifiedBookResult with source=LIBGEN

        Raises:
            AllSourcesFailedError: If no mirror could complete the search
        """
        failures: List[SourceError] = []

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
                # The fetched page is read back on the SAME thread that
                # fetched it — `_search_requests.last_response` is
                # thread-local precisely so an abandoned earlier attempt
                # cannot supply it.
                _search_requests.last_response = None
                # Set on the same thread that is about to issue the request,
                # so this adapter's configured UA reaches the search path
                # rather than the shim re-reading the environment.
                _search_requests.user_agent = get_user_agent(self.config)
                # search_default covers title+author+series+publisher —
                # search_title made author-bearing queries return nothing
                # and got swamped on generic titles (#134).
                results = LibgenSearch(mirror=mirror).search_default(query)
                return results, _search_requests.last_response

            try:
                results, page = await run_bounded(
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

            if not results:
                unparseable = _unparseable_search_page(page, self.config)
                if unparseable:
                    failure = ProviderResponseError(
                        PROVIDER, host, unparseable, reason="protocol_error"
                    )
                    logger.warning("LibGen search unusable on %s: %s", mirror, failure)
                    failures.append(failure)
                    continue

            return self._to_unified(results or [])

        raise AllSourcesFailedError(f"LibGen search for {query!r}", failures)

    def _to_unified(self, results) -> List[UnifiedBookResult]:
        """Convert libgen-api-enhanced books into UnifiedBookResult.

        Rows whose md5 is not a full 32-hex digest are dropped:
        journal-article rows come back column-shifted from the parser
        (empty md5, or another column's value — an ISBN, a citation — in
        the md5 field, #132), and anything short of a real md5 cannot be
        resolved by ads.php. Same rule as the production canary's
        usable-row check. The full per-object-type parse is #132's job;
        this filter is the minimal slice that keeps #134's wider search
        results usable.
        """
        dropped = sum(
            1
            for book in results
            if not _MD5_HEX_RE.fullmatch(getattr(book, "md5", "") or "")
        )
        if dropped:
            logger.debug(
                "LibGen search: dropped %d row(s) without a valid 32-hex md5",
                dropped,
            )
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
            if _MD5_HEX_RE.fullmatch(getattr(book, "md5", "") or "")
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
    ) -> tuple[Optional[str], str]:
        """Scrape the one-time CDN key out of a mirror's ads.php page.

        `ads.php?md5=<md5>` is addressable directly from the hash — no search
        is involved — and carries a single `GET` anchor whose href holds the
        key.

        Returns:
            (key, detail). `key` is None when the page carries no usable key;
            `detail` says why, distinguishing a blocklisted UA from real
            markup drift. Those two produce identical symptoms — HTTP 200 with
            no `GET` anchor — and reporting the block as "DOM drift" sent an
            operator looking for a parser bug that did not exist (#141).
        """
        url = f"https://libgen.{mirror}/ads.php?md5={md5}"
        response = await client.get(url)
        response.raise_for_status()

        if _nginx_stub(response.text):
            return None, _blocked_ua_detail("ads.php", self.config)

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
                return key, ""
        return None, (
            "ads.php returned HTTP 200 with no key-bearing GET link "
            "(DOM drift — the resolver scrapes this anchor for the CDN key)"
        )

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

    async def iter_download_candidates(self, md5: str) -> AsyncIterator[DownloadResult]:
        """Yield one working clearnet candidate per unique mirror.

        Walks `_mirror_candidates()` and yields each mirror that resolves and
        serves probe bytes, resuming at the next mirror when the consumer asks
        again. The previous implementation looked the book up with
        `search_title(md5)`, but LibGen's title index contains no md5 strings,
        so that returned nothing and this method raised for every input — it
        had never worked against the live service (the unit suite mocks
        LibgenSearch, so it stayed green).

        The returned URL is short-lived: the key expires in well under 2.5
        hours, after which `get.php` silently 307s back to `/ads.php` instead
        of erroring. Resolve immediately before downloading; never cache.

        Args:
            md5: MD5 hash identifying the book

        Raises:
            AllSourcesFailedError: With the ordered mirror-resolution failures
                after all candidates have been consumed
        """
        failures: List[SourceError] = []

        # The identifying UA matters here too: ads.php serves the same
        # UA-blocklist stub to python-httpx's default UA (measured
        # 2026-08-17, #124), which surfaces as "no GET link" on every mirror.
        # The timeout stays on the configured per-phase budget rather than a
        # bare 30s — bounding these calls is what #106 exists for.
        async with httpx.AsyncClient(
            timeout=build_timeout(self.config),
            follow_redirects=True,
            headers={"User-Agent": get_user_agent(self.config)},
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
                    key, key_detail = await self._resolve_key(client, mirror, md5)
                    if not key:
                        return None, key_detail

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
                yield DownloadResult(
                    url=url,
                    source=SourceType.LIBGEN,
                    quota_info=None,  # LibGen has no quota
                )

        if failures:
            raise AllSourcesFailedError("download resolution", failures)

    async def get_download_url(self, md5: str) -> DownloadResult:
        """Return the first candidate for compatibility with existing callers."""
        candidates = self.iter_download_candidates(md5)
        try:
            return await anext(candidates)
        finally:
            await candidates.aclose()

    async def close(self) -> None:
        """Clean up resources.

        LibgenSearch doesn't maintain persistent connections, and
        get_download_url scopes its client to the call, so this is a no-op.
        """
        pass
