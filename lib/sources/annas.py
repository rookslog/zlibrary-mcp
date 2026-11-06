"""Anna's Archive source adapter.

Provides search and fast download functionality for Anna's Archive.
Search uses HTML scraping, downloads use the fast download API.

Key decisions:
- ANNAS-DOMAIN-INDEX-1: Use domain_index=1 for fast download API
  (domain_index=0 has SSL errors)
- ANNAS-SCRAPE-SEARCH: Search via HTML scraping (no search API exists)
"""

import asyncio
import logging
import re
import time
from typing import Dict, List, NoReturn, Optional
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import SourceAdapter
from .config import ANNAS_TRUSTED_HOSTS, SourceConfig
from .errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
    SourceError,
)
from .models import DownloadResult, QuotaInfo, SourceType, UnifiedBookResult
from .net import (
    WalkDeadline,
    bounded_await,
    build_timeout,
    classify_httpx_error,
    port_of,
    probe_host,
    proxy_in_use,
)

PROVIDER = "annas"


def _raise_keyed_error(error: SourceError) -> NoReturn:
    """Do not retain the keyed request, including in an active caller handler."""
    try:
        raise error from None
    finally:
        # `from None` only suppresses display; raising still sets __context__.
        error.__context__ = None


def _valid_download_url(url: str) -> bool:
    """Validate components without rewriting a provider's signed URL text."""
    if url != url.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in url):
        return False
    try:
        parts = urlsplit(url)
        # Reading .port validates numeric syntax and the 0..65535 range.
        parts.port
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


# Each Anna's result renders a metadata strip of "·"-separated segments, e.g.
#   English [en] · PDF · 5.6MB · 2008 · 📕 Book (fiction) · 🚀/lgli/nexusstc/zlib
# Segments are OPTIONAL and ORDER IS NOT GUARANTEED — year is absent from about
# 9% of results (measured over 100 results across two queries). The strip is
# therefore matched by pattern, never by index: positional parsing silently
# shifts year into extension on the records that omit it.
_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s*[KMGT]B$", re.IGNORECASE)
# Any plausible four-digit year, deliberately including pre-1500 ones. This is a
# scholarly-text tool: incunabula and early-modern imprints carry dates like
# 1492, and a 15xx-and-later floor silently dropped a year that was present
# upstream. Widening is safe because size carries a unit suffix, content type
# carries parentheses, and extension must start with a letter — so no other
# segment can be mistaken for a bare four-digit number.
_YEAR_RE = re.compile(r"^(?:1[0-9]\d{2}|20\d{2})$")
# Extension must START with a letter. A bare "[A-Z0-9]{2,5}" also matches a
# four-digit year, which is exactly the corruption this parser exists to avoid.
_EXT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")
_LANG_RE = re.compile(r"\[([a-z]{2,3})\]")


def _parse_metadata_strip(strip: str) -> Dict[str, str]:
    """Pattern-match the '·'-separated metadata strip on an Anna's result.

    Args:
        strip: Raw strip text

    Returns:
        Dict with any of: language, extension, size, year, content_type,
        provenance. Absent segments are simply omitted.
    """
    parsed: Dict[str, str] = {}
    for raw in strip.split("·"):
        seg = raw.replace("\U0001f680", "").strip()  # 🚀 prefixes provenance
        if not seg:
            continue
        if "year" not in parsed and _YEAR_RE.match(seg):
            parsed["year"] = seg
        elif "size" not in parsed and _SIZE_RE.match(seg):
            parsed["size"] = seg.replace(" ", "")
        elif "language" not in parsed and _LANG_RE.search(seg):
            parsed["language"] = _LANG_RE.search(seg).group(1)
        elif "extension" not in parsed and _EXT_RE.match(seg):
            parsed["extension"] = seg.lower()
        elif "content_type" not in parsed and "(" in seg:
            parsed["content_type"] = seg
        elif "provenance" not in parsed and ("/" in seg or seg in ("zlib", "upload")):
            parsed["provenance"] = seg.strip("/")
    return parsed


def _find_metadata_strip(card: Tag) -> Optional[str]:
    """Locate the metadata strip within a result card.

    Identified by content rather than by class name: Anna's uses generated
    Tailwind classes that change between deploys, whereas a "·"-separated run
    of recognisable segments is a stable shape.

    Recognition must not hinge on any single segment. Requiring a size segment
    discarded the whole strip for records that omit it — losing the language,
    extension, year and content type that were present — which contradicted the
    parser's own every-segment-is-optional contract.
    """
    for text in card.find_all(string=lambda t: t and "·" in t):
        candidate = text.strip()
        for part in candidate.split("·"):
            seg = part.replace("\U0001f680", "").strip()
            if _SIZE_RE.match(seg) or _EXT_RE.match(seg) or _LANG_RE.search(seg):
                return candidate
    return None


class QuotaExhaustedError(Exception):
    """Raised when Anna's Archive download quota is exhausted."""

    pass


def _attempt_budget(config, deadline: Optional[WalkDeadline]) -> float:
    """One attempt's budget, clamped to whatever the shared walk has left.

    Without the clamp a provider late in an `auto` walk starts a fresh
    45-second attempt on a walk that has 3 seconds left, and the bridge is
    killed mid-request rather than returning the failures it had collected
    (#152). With no deadline — an adapter used directly — the configured
    budget stands.
    """
    if deadline is None:
        return config.total_timeout
    return min(config.total_timeout, max(0.0, deadline.remaining()))


class AnnasArchiveAdapter(SourceAdapter):
    """Anna's Archive adapter implementing SourceAdapter interface.

    Provides:
    - search(query) -> List[UnifiedBookResult] via HTML scraping
    - get_download_url(md5) -> DownloadResult via fast download API

    Configuration:
    - annas_base_url: Base URL for Anna's Archive (default: https://annas-archive.gl)
    - annas_secret_key: Member key for search sign-in and fast downloads

    Security: the secret key is only ever attached to hosts in
    config.ANNAS_TRUSTED_HOSTS. Anna's Archive domains lapse and get re-registered
    by squatters (annas-archive.li is a Trellian parking page as of 2026-03), and
    the fast-download API sends the key as a URL query parameter — sending it to
    an unverified host would disclose it to whoever now controls the domain.
    Anonymous search is not restricted; member sign-in reuses this guard.
    """

    def __init__(self, config: SourceConfig):
        """Initialize adapter with configuration.

        Args:
            config: SourceConfig with Anna's Archive settings
        """
        self.config = config
        self.base_url = config.annas_base_url.rstrip("/")
        self.secret_key = config.annas_secret_key
        self.host = (urlsplit(self.base_url).hostname or "").lower()
        # The probe must target what the request targets. A base URL may name
        # a non-default port (a local mirror, a test double), and probing 443
        # regardless would report a reachable host as dead.
        self.port = port_of(self.base_url)
        self.scheme = urlsplit(self.base_url).scheme or "https"
        self._client: Optional[httpx.AsyncClient] = None
        self._member_lock = asyncio.Lock()
        self._member_generation = 0
        self._had_member_session = False
        self._member_state = "none"
        self._member_retry_at = 0.0
        # httpcore DEBUG logs include raw Set-Cookie response headers. Keep
        # transport chatter out of diagnostics while a member owner is alive.
        self._transport_filter = lambda record: False
        self._transport_loggers = []
        if self.secret_key:
            for name in (
                "httpx",
                "httpcore.connection",
                "httpcore.http11",
                "httpcore.http2",
                "httpcore.proxy",
                "httpcore.socks",
            ):
                logger = logging.getLogger(name)
                logger.addFilter(self._transport_filter)
                self._transport_loggers.append(logger)

    def _guard_key_host(self) -> None:
        """Shared credential destination guard for sign-in and fast download."""
        host = (urlsplit(self.base_url).hostname or "").lower()
        if host not in ANNAS_TRUSTED_HOSTS:
            _raise_keyed_error(
                ProviderConfigurationError(
                    PROVIDER,
                    host,
                    f"Refusing to send ANNAS_SECRET_KEY to unverified host '{host}'. "
                    f"The fast-download API passes the key as a URL parameter, and "
                    f"lapsed Anna's Archive domains get re-registered by squatters "
                    f"(annas-archive.li is now a parking page). Set ANNAS_BASE_URL "
                    f"to a known Anna's Archive domain "
                    f"({', '.join(sorted(ANNAS_TRUSTED_HOSTS))}) or update "
                    f"ANNAS_TRUSTED_HOSTS in lib/sources/config.py if the project "
                    f"has moved to a new domain you have verified yourself.",
                )
            )

    def _has_member_cookie(self, client) -> bool:
        request = httpx.Request("GET", f"{self.base_url}/search")
        client.cookies.set_cookie_header(request)
        return bool(
            re.search(
                r"(?:^|;\s*)aa_account_id2=[^;]+", request.headers.get("cookie", "")
            )
        )

    def member_status(self) -> Dict:
        """Local coarse state only; never expose credential or cookie data."""
        if self._member_state == "active" and self._client is not None:
            if not self._has_member_cookie(self._client):
                self._member_state = "none"
        return {
            "member_key_configured": bool(self.secret_key),
            "member_session": self._member_state,
        }

    def _reject_member(self, client) -> None:
        client.cookies = httpx.Cookies()
        self._member_state = "rejected"
        # Same fixed-cooldown style as the provider circuit breaker.
        self._member_retry_at = time.monotonic() + 60.0

    async def _signin(self, client, refused_generation=None) -> None:
        if not self.secret_key:
            return
        async with self._member_lock:
            if refused_generation is not None:
                if refused_generation != self._member_generation:
                    return  # Another search already refreshed this generation.
                client.cookies = httpx.Cookies()
                self._member_state = "none"
            if self.member_status()["member_session"] == "active":
                return
            if time.monotonic() < self._member_retry_at:
                return
            try:
                self._guard_key_host()
                response = await client.post(
                    f"{self.base_url}/account/",
                    data={"key": self.secret_key},
                    follow_redirects=False,
                )
                if not isinstance(response, httpx.Response):
                    raise TypeError("Invalid sign-in response")
                # Never follow a credential-bearing redirect, even 307/308.
                location = response.headers.get("location")
                foreign = (
                    location
                    and httpx.URL(str(response.url)).join(location).host != self.host
                )
                accepted = (
                    200 <= response.status_code < 400
                    and not foreign
                    and self._has_member_cookie(client)
                    and any(
                        c.name == "aa_account_id2" and c.value and not c.is_expired()
                        for c in response.cookies.jar
                    )
                )
            except asyncio.CancelledError:
                # A timed-out/cancelled sign-in must not become a per-search retry.
                self._member_generation += 1
                self._reject_member(client)
                raise
            except Exception:
                # Do not log, chain or retain the failed keyed request/body.
                accepted = False
            self._member_generation += 1
            if accepted:
                self._member_state = "active"
                self._had_member_session = True
                self._member_retry_at = 0.0
            else:
                self._reject_member(client)

    @staticmethod
    def _is_check(response) -> bool:
        text = response.text
        if not isinstance(text, str):
            return False
        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for script in soup(["script", "style"]):
            script.decompose()
        return bool(
            re.search(r"ddos[- ]guard", title, re.I)
            or re.search(
                r"checking your browser|browser verification",
                soup.get_text(" ", strip=True),
                re.I,
            )
        )

    async def _search_page(self, client, url):
        await self._signin(client)
        for attempt in range(2):
            generation = self._member_generation
            active = self._member_state == "active"
            refusal = False
            try:
                response = await self._fetch(client, url)
                refusal = self._is_check(response)
                if not refusal:
                    if self._had_member_session:
                        secrets = [self.secret_key] + [
                            c.value for c in client.cookies.jar
                        ]
                        if any(
                            token and token in response.text
                            for secret in secrets
                            for token in (
                                secret,
                                quote(secret, safe=""),
                                quote_plus(secret, safe=""),
                            )
                        ):
                            _raise_keyed_error(
                                ProviderResponseError(
                                    PROVIDER,
                                    self.host,
                                    "Credential echo omitted",
                                    reason="protocol_error",
                                )
                            )
                    return response
            except httpx.HTTPStatusError as exc:
                if not active or exc.response.status_code not in (401, 403):
                    raise
                refusal = True
            if refusal and active and attempt == 0:
                await self._signin(client, generation)
                continue
            if active and generation == self._member_generation:
                self._reject_member(client)
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    "Browser verification or member session refused",
                    reason="http_error",
                )
            )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client.

        Returns:
            Configured httpx.AsyncClient instance, with connect and read
            budgets taken from config rather than a hard-coded constant.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=build_timeout(self.config), follow_redirects=True
            )
        return self._client

    async def _preflight(self, deadline: Optional[WalkDeadline] = None) -> None:
        """Fail fast if the configured Anna's host is not reachable.

        Anna's domains lapse (annas-archive.org and .se are NXDOMAIN as of
        2026-08-11), so "this domain no longer exists" is a routine outcome and
        deserves a one-probe answer naming the host, not a full request budget
        spent on a name that cannot resolve.
        """
        timeout = self.config.preflight_timeout
        if deadline is not None:
            preflight_runs = self.config.preflight_enabled and not proxy_in_use(
                f"{self.scheme}://{self.host}:{self.port}"
            )
            minimum = (timeout if preflight_runs else 0.0) + 1.0
            timeout = deadline.reserve(timeout, minimum=minimum)
            if timeout is None:
                raise ProviderTimeoutError(
                    PROVIDER,
                    self.host,
                    "walk budget exhausted before preflight",
                    reason="walk_budget_exhausted",
                )
        if not self.config.preflight_enabled or not self.host:
            return
        await probe_host(
            PROVIDER,
            self.host,
            port=self.port,
            timeout=timeout,
            scheme=self.scheme,
        )

    def _as_source_error(self, exc: BaseException) -> Exception:
        """Convert a transport failure into a provider-attributed error.

        Args:
            exc: Exception raised by an httpx call

        Returns:
            The original exception if it is already attributed, otherwise a
            ProviderUnreachableError (transport) or ProviderResponseError
            (the host answered but the answer was unusable).
        """
        if isinstance(exc, SourceError):
            return exc
        reason, detail = classify_httpx_error(exc)
        if reason in ("http_error", "protocol_error"):
            return ProviderResponseError(PROVIDER, self.host, detail, reason=reason)
        return ProviderUnreachableError(PROVIDER, self.host, detail, reason=reason)

    async def _fetch(self, client, url: str, params: Optional[Dict] = None):
        """GET a URL and raise on an error status, as one awaitable.

        Exists so the status check sits *inside* the wall-clock budget rather
        than after it.
        """
        if self._member_state == "active" and not params:
            # A session cookie must never leave this origin via a redirect.
            response = await client.get(url, follow_redirects=False)
        else:
            response = (
                await client.get(url, params=params)
                if params
                else await client.get(url)
            )
        response.raise_for_status()
        return response

    async def search(
        self,
        query: str,
        deadline: Optional[WalkDeadline] = None,
        **kwargs,
    ) -> List[UnifiedBookResult]:
        """Search Anna's Archive for books.

        Scrapes HTML from /search?q={query} and extracts MD5 hashes
        using selector: a[href^='/md5/']

        Args:
            query: Search query string
            deadline: Wall-clock ceiling shared with the rest of the router's
                walk. Anna's is one attempt, but in an `auto` search it runs
                alongside LibGen's mirror walk and they spend the same clock —
                which is the whole point of a shared deadline (#152).
            **kwargs: Additional search options (unused)

        Returns:
            List of UnifiedBookResult with source=ANNAS_ARCHIVE

        Raises:
            ProviderUnreachableError: If the host does not resolve or connect
            ProviderResponseError: If it answers with an HTTP or protocol error
        """
        await self._preflight(deadline)

        client = await self._get_client()
        url = f"{self.base_url}/search?q={quote(query)}"
        try:
            # httpx bounds each phase separately and restarts its read deadline
            # on every chunk, so a host that trickles bytes never trips it. The
            # outer budget is what actually enforces config.total_timeout.
            response = await bounded_await(
                self._search_page(client, url),
                _attempt_budget(self.config, deadline),
                provider=PROVIDER,
                host=self.host,
                operation="search",
            )
        except Exception as exc:
            diagnostic = str(exc)
            request = getattr(exc, "_request", None)
            if request is not None:
                diagnostic += (
                    str(request.url) + repr(request.headers) + repr(request.content)
                )
            echoes_key = bool(self.secret_key) and any(
                token in diagnostic
                for token in (
                    self.secret_key,
                    quote(self.secret_key, safe=""),
                    quote_plus(self.secret_key, safe=""),
                )
            )
            if self._had_member_session or isinstance(exc, SourceError) or echoes_key:
                attributed = self._as_source_error(exc)
                _raise_keyed_error(
                    type(attributed)(
                        PROVIDER,
                        self.host,
                        "Browser verification or member request failed"
                        if isinstance(exc, SourceError)
                        else type(exc).__name__,
                        reason=attributed.reason,
                    )
                )
            raise self._as_source_error(exc) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        seen = set()

        # Each record renders TWO /md5/ anchors: a cover image with empty text,
        # then the title. Dedupe-by-first kept the cover, so every result came
        # back titled "Unknown" (#78). Select only text-bearing anchors — measured
        # over 100 records on two unrelated queries, every md5 group has exactly
        # two anchors and exactly one of them bears text.
        for link in soup.select("a[href^='/md5/']"):
            title = link.get_text(strip=True)
            if not title:
                continue

            md5 = link.get("href", "").split("/")[-1]
            if not md5 or md5 in seen:
                continue
            seen.add(md5)

            results.append(self._build_result(link, md5, title))

        # HTML entities decode during parsing; scan the actual returned metadata
        # as well as raw HTML so an escaped credential cannot become a title.
        if self.secret_key:
            secrets = [self.secret_key]
            if self._had_member_session:
                secrets.extend(c.value for c in client.cookies.jar)
            if any(secret and secret in repr(results) for secret in secrets):
                _raise_keyed_error(
                    ProviderResponseError(
                        PROVIDER,
                        self.host,
                        "Credential echo omitted",
                        reason="protocol_error",
                    )
                )
        return results

    def _build_result(self, link: Tag, md5: str, title: str) -> UnifiedBookResult:
        """Assemble one search result from its title anchor.

        Args:
            link: The text-bearing /md5/ anchor
            md5: MD5 extracted from its href
            title: Its text

        Returns:
            UnifiedBookResult with whatever fields the card exposed
        """
        card = link.parent
        # Values are not uniformly str: also_available_on is a list. The dict is
        # spread into the JSON tool response, where both serialise fine.
        extra: Dict[str, object] = {"url": f"{self.base_url}/md5/{md5}"}

        # Author and publisher carry semantic icon markers, which survive the
        # generated-class churn that would break a class-name selector.
        author = ""
        if card is not None:
            author_icon = card.select_one('a span[class*="mdi--user-edit"]')
            if author_icon is not None and author_icon.parent is not None:
                author = author_icon.parent.get_text(strip=True)

            publisher_icon = card.select_one('a span[class*="mdi--company"]')
            if publisher_icon is not None and publisher_icon.parent is not None:
                publisher = publisher_icon.parent.get_text(strip=True)
                # The company-marked line is usually "<publisher>, <year>", but
                # on records with no publisher it degrades to a bare number —
                # commonly a year, sometimes a sentinel like 0. No publisher is
                # purely numeric, so reject the whole class rather than only the
                # years _YEAR_RE happens to cover: gating on the year parser let
                # values outside 1000-2099 through as publishers.
                if publisher and not publisher.isdigit():
                    extra["publisher"] = publisher

        meta: Dict[str, str] = {}
        strip_host = card.parent if card is not None else None
        if strip_host is not None:
            strip = _find_metadata_strip(strip_host)
            if strip:
                meta = _parse_metadata_strip(strip)

        if "language" in meta:
            extra["language"] = meta["language"]
        if "content_type" in meta:
            extra["content_type"] = meta["content_type"]

        # Which OTHER sources hold this same file, as claimed by Anna's. Verified
        # to mean "retrievable", not merely "ingested from": 3/3 records marked
        # /lgli resolved on LibGen, 0/2 unmarked did (#78).
        #
        # Surfaced only. Choosing a source from it is deliberately NOT done here:
        # how it is reported is #96, and cross-source dedup is #52. Comparison
        # logic inside a single source's adapter is what invariant 4 forbids.
        if "provenance" in meta:
            extra["also_available_on"] = [
                part for part in meta["provenance"].split("/") if part
            ]

        return UnifiedBookResult(
            md5=md5,
            title=title,
            source=SourceType.ANNAS_ARCHIVE,
            author=author,
            year=meta.get("year", ""),
            extension=meta.get("extension", ""),
            size=meta.get("size", ""),
            extra=extra,
        )

    async def get_download_url(
        self,
        md5: str,
        deadline: Optional[WalkDeadline] = None,
    ) -> DownloadResult:
        """Get fast download URL for a book.

        Calls /dyn/api/fast_download.json with MD5 and API key.
        CRITICAL: Uses domain_index=1 (domain_index=0 has SSL errors).

        Args:
            md5: MD5 hash identifying the book

        Returns:
            DownloadResult with URL and quota info

        Raises:
            ProviderConfigurationError: If ANNAS_SECRET_KEY is not configured,
                or if the configured
                base URL's host is not a known Anna's Archive domain (the key is
                never sent to unverified hosts)
            ProviderResponseError: If the API returns an error or omits download_url
        """
        host = (urlsplit(self.base_url).hostname or "").lower()
        if not self.secret_key:
            _raise_keyed_error(
                ProviderConfigurationError(
                    PROVIDER,
                    host,
                    "ANNAS_SECRET_KEY not configured",
                )
            )

        self._guard_key_host()

        url = f"{self.base_url}/dyn/api/fast_download.json"
        params = {
            "md5": md5,
            "key": self.secret_key,
            "domain_index": 1,  # CRITICAL: Use 1, not 0 (SSL errors on 0)
        }

        try:
            await self._preflight(deadline)
            client = await self._get_client()
            response = await bounded_await(
                self._fetch(client, url, params=params),
                _attempt_budget(self.config, deadline),
                provider=PROVIDER,
                host=self.host,
                operation="download resolution",
            )
            response.content.decode("utf-8")
            data = response.json()
        except httpx.HTTPStatusError as exc:
            final_url = exc.response.request.url
            configured_url = httpx.URL(url)
            is_configured_fast_download_endpoint = (
                (
                    (final_url.scheme, final_url.port)
                    == (configured_url.scheme, configured_url.port)
                    or (configured_url.scheme == "http" and final_url.scheme == "https")
                )
                and final_url.host == configured_url.host
                and final_url.raw_path.partition(b"?")[0]
                == configured_url.raw_path.partition(b"?")[0]
                and final_url.userinfo == configured_url.userinfo
                and final_url.params.get_list("key") == [self.secret_key]
                and final_url.params.get_list("md5") == [md5]
            )
            if (
                exc.response.status_code in (401, 403)
                and is_configured_fast_download_endpoint
            ):
                _raise_keyed_error(
                    ProviderConfigurationError(
                        PROVIDER,
                        self.host,
                        f"ANNAS_SECRET_KEY rejected by fast-download endpoint "
                        f"(HTTP {exc.response.status_code})",
                    )
                )
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    f"HTTP {exc.response.status_code}",
                    reason="http_error",
                )
            )
        except UnicodeDecodeError:
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    "Response body is not valid UTF-8",
                    reason="protocol_error",
                )
            )
        except Exception as exc:
            attributed = self._as_source_error(exc)
            # Preserve classification, but never transport prose or attached
            # request/response attributes. Search keeps its diagnostic chain.
            _raise_keyed_error(
                type(attributed)(
                    PROVIDER, self.host, type(exc).__name__, reason=attributed.reason
                )
            )

        if not isinstance(data, dict):
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    f"Expected JSON object, received {type(data).__name__}",
                    reason="protocol_error",
                )
            )

        # Check for API error
        if data.get("error"):
            detail = str(data["error"])
            # Keep benign API diagnostics, but omit an echoed keyed request.
            if (
                self.secret_key in detail
                or quote(self.secret_key, safe="").lower() in detail.lower()
                or quote_plus(self.secret_key, safe="").lower() in detail.lower()
                or "key=" in detail.lower()
            ):
                detail = "details omitted"
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    f"Anna's Archive API error: {detail}",
                    reason="http_error",
                )
            )

        # Extract download URL
        download_url = data.get("download_url")
        if not isinstance(download_url, str) or not _valid_download_url(download_url):
            _raise_keyed_error(
                ProviderResponseError(
                    PROVIDER,
                    self.host,
                    "download_url must be an absolute HTTP(S) URL with a host, valid port, "
                    "no credentials, surrounding whitespace or control characters",
                    reason="protocol_error",
                )
            )

        # Extract quota info if available
        quota_info = None
        if "account_fast_download_info" in data:
            account_info = data["account_fast_download_info"]
            if not isinstance(account_info, dict):
                _raise_keyed_error(
                    ProviderResponseError(
                        PROVIDER,
                        self.host,
                        "account_fast_download_info must be an object",
                        reason="protocol_error",
                    )
                )
            for field in (
                "downloads_left",
                "downloads_per_day",
                "downloads_done_today",
            ):
                if account_info.get(field) is not None and (
                    type(account_info[field]) is not int or account_info[field] < 0
                ):
                    _raise_keyed_error(
                        ProviderResponseError(
                            PROVIDER,
                            self.host,
                            f"{field} must be a non-negative integer",
                            reason="protocol_error",
                        )
                    )
            if account_info.get("downloads_left") is not None:
                quota_info = QuotaInfo(
                    downloads_left=account_info["downloads_left"],
                    downloads_per_day=account_info.get("downloads_per_day"),
                    downloads_done_today=account_info.get("downloads_done_today"),
                )

        return DownloadResult(
            url=download_url,
            source=SourceType.ANNAS_ARCHIVE,
            quota_info=quota_info,
            mirror=self.host,
        )

    async def close(self) -> None:
        """Clean up HTTP client resources."""
        try:
            if self._client:
                await self._client.aclose()
        finally:
            self._client = None
            for logger in self._transport_loggers:
                logger.removeFilter(self._transport_filter)
            self._transport_loggers.clear()
        self._member_state = "none"
        self._member_retry_at = 0.0
