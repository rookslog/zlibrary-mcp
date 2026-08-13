"""Anna's Archive source adapter.

Provides search and fast download functionality for Anna's Archive.
Search uses HTML scraping, downloads use the fast download API.

Key decisions:
- ANNAS-DOMAIN-INDEX-1: Use domain_index=1 for fast download API
  (domain_index=0 has SSL errors)
- ANNAS-SCRAPE-SEARCH: Search via HTML scraping (no search API exists)
"""

import re
from typing import Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import SourceAdapter
from .config import ANNAS_TRUSTED_HOSTS, SourceConfig
from .errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderUnreachableError,
    SourceError,
)
from .models import DownloadResult, QuotaInfo, SourceType, UnifiedBookResult
from .net import (
    bounded_await,
    build_timeout,
    classify_httpx_error,
    port_of,
    probe_host,
)

PROVIDER = "annas"

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


class AnnasArchiveAdapter(SourceAdapter):
    """Anna's Archive adapter implementing SourceAdapter interface.

    Provides:
    - search(query) -> List[UnifiedBookResult] via HTML scraping
    - get_download_url(md5) -> DownloadResult via fast download API

    Configuration:
    - annas_base_url: Base URL for Anna's Archive (default: https://annas-archive.gl)
    - annas_secret_key: API key for fast downloads (required for get_download_url)

    Security: the secret key is only ever attached to hosts in
    config.ANNAS_TRUSTED_HOSTS. Anna's Archive domains lapse and get re-registered
    by squatters (annas-archive.li is a Trellian parking page as of 2026-03), and
    the fast-download API sends the key as a URL query parameter — sending it to
    an unverified host would disclose it to whoever now controls the domain.
    Search (which carries no key) is not restricted.
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

    async def _preflight(self) -> None:
        """Fail fast if the configured Anna's host is not reachable.

        Anna's domains lapse (annas-archive.org and .se are NXDOMAIN as of
        2026-08-11), so "this domain no longer exists" is a routine outcome and
        deserves a one-probe answer naming the host, not a full request budget
        spent on a name that cannot resolve.
        """
        if not self.config.preflight_enabled or not self.host:
            return
        await probe_host(
            PROVIDER,
            self.host,
            port=self.port,
            timeout=self.config.preflight_timeout,
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
        response = (
            await client.get(url, params=params) if params else await client.get(url)
        )
        response.raise_for_status()
        return response

    async def search(self, query: str, **kwargs) -> List[UnifiedBookResult]:
        """Search Anna's Archive for books.

        Scrapes HTML from /search?q={query} and extracts MD5 hashes
        using selector: a[href^='/md5/']

        Args:
            query: Search query string
            **kwargs: Additional search options (unused)

        Returns:
            List of UnifiedBookResult with source=ANNAS_ARCHIVE

        Raises:
            ProviderUnreachableError: If the host does not resolve or connect
            ProviderResponseError: If it answers with an HTTP or protocol error
        """
        await self._preflight()

        client = await self._get_client()
        url = f"{self.base_url}/search?q={quote(query)}"
        try:
            # httpx bounds each phase separately and restarts its read deadline
            # on every chunk, so a host that trickles bytes never trips it. The
            # outer budget is what actually enforces config.total_timeout.
            response = await bounded_await(
                self._fetch(client, url),
                self.config.total_timeout,
                provider=PROVIDER,
                host=self.host,
                operation="search",
            )
        except Exception as exc:
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

    async def get_download_url(self, md5: str) -> DownloadResult:
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
            raise ProviderConfigurationError(
                PROVIDER,
                host,
                "ANNAS_SECRET_KEY not configured",
            )

        if host not in ANNAS_TRUSTED_HOSTS:
            raise ProviderConfigurationError(
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

        await self._preflight()

        client = await self._get_client()
        url = f"{self.base_url}/dyn/api/fast_download.json"
        params = {
            "md5": md5,
            "key": self.secret_key,
            "domain_index": 1,  # CRITICAL: Use 1, not 0 (SSL errors on 0)
        }

        try:
            response = await bounded_await(
                self._fetch(client, url, params=params),
                self.config.total_timeout,
                provider=PROVIDER,
                host=self.host,
                operation="download resolution",
            )
            data = response.json()
        except httpx.HTTPStatusError as exc:
            final_url = urlsplit(str(exc.response.url))
            final_host = (final_url.hostname or "").lower()
            is_configured_fast_download_endpoint = (
                final_host == self.host
                and final_host in ANNAS_TRUSTED_HOSTS
                and final_url.path == "/dyn/api/fast_download.json"
            )
            if (
                exc.response.status_code in (401, 403)
                and is_configured_fast_download_endpoint
            ):
                raise ProviderConfigurationError(
                    PROVIDER,
                    self.host,
                    f"ANNAS_SECRET_KEY rejected by fast-download endpoint "
                    f"(HTTP {exc.response.status_code})",
                ) from exc
            raise self._as_source_error(exc) from exc
        except Exception as exc:
            raise self._as_source_error(exc) from exc

        # Check for API error
        if data.get("error"):
            raise ProviderResponseError(
                PROVIDER,
                self.host,
                f"Anna's Archive API error: {data['error']}",
                reason="http_error",
            )

        # Extract download URL
        download_url = data.get("download_url")
        if not download_url:
            raise ProviderResponseError(
                PROVIDER,
                self.host,
                "No download_url in response",
                reason="protocol_error",
            )

        # Extract quota info if available
        quota_info = None
        account_info = data.get("account_fast_download_info")
        if account_info:
            quota_info = QuotaInfo(
                downloads_left=account_info.get("downloads_left", 0),
                downloads_per_day=account_info.get("downloads_per_day", 0),
                downloads_done_today=account_info.get("downloads_done_today", 0),
            )

        return DownloadResult(
            url=download_url,
            source=SourceType.ANNAS_ARCHIVE,
            quota_info=quota_info,
        )

    async def close(self) -> None:
        """Clean up HTTP client resources."""
        if self._client:
            await self._client.aclose()
            self._client = None
