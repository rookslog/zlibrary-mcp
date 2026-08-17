"""
EAPI client for Z-Library JSON API endpoints.

Bypasses Cloudflare by using POST/GET to /eapi/* endpoints
with cookie-based authentication.
"""

import ntpath
import posixpath
import re
import httpx
import aiofiles
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union
from urllib.parse import unquote

from .logger import logger


EAPI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
}

# Candidate EAPI domains tried in order when ZLIBRARY_EAPI_DOMAIN is not set.
# z-library.sk and 1lib.sk are fronted by the DiamWall anti-bot wall as of
# 2026-07-24 (ISSUE-API-002) but stay listed in case the wall is lifted;
# z-library.ec serves normal EAPI JSON and goes first.
DEFAULT_EAPI_DOMAINS = ["z-library.ec", "z-library.sk", "1lib.sk"]

# Status codes observed from DiamWall-fronted domains: a 307 self-redirect
# setting a `__diamwall` cookie, then 513/517 "Access Denied" on retry.
# 403 covers generic bot-blocking.
WALLED_STATUS_CODES = (307, 403, 513, 517)


class DiamWallError(RuntimeError):
    """An /eapi request was intercepted by the DiamWall anti-bot wall.

    Raised when a domain returns the DiamWall block page (or its status
    codes) where EAPI JSON was expected. The message names the wall and the
    remedy so it survives str(e) propagation through the health check.
    """


def _diamwall_error(domain: str, detail: str) -> DiamWallError:
    return DiamWallError(
        f"DiamWall anti-bot wall detected on {domain} ({detail}): the domain "
        f"blocks programmatic /eapi access. Set ZLIBRARY_EAPI_DOMAIN to a "
        f"working domain (e.g. export ZLIBRARY_EAPI_DOMAIN=z-library.ec) "
        f"or unset it to let the built-in fallback list pick one."
    )


def decode_eapi_json(resp: httpx.Response, domain: str) -> dict:
    """Parse an /eapi response as JSON, classifying anti-bot walls explicitly.

    A healthy EAPI endpoint returns HTTP 200 with JSON. DiamWall-fronted
    domains return a 307 self-redirect, 403/513/517 "Access Denied", or an
    HTML block page — this raises DiamWallError for those so callers see
    "anti-bot wall" instead of a bare JSONDecodeError or status error.
    """
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            pass  # fall through to wall classification
    body = resp.text
    if "diamwall" in body.lower():
        raise _diamwall_error(domain, f"HTTP {resp.status_code} DiamWall block page")
    if resp.status_code in WALLED_STATUS_CODES:
        raise _diamwall_error(domain, f"HTTP {resp.status_code}")
    resp.raise_for_status()
    raise RuntimeError(
        f"Expected JSON from {domain}, got non-JSON HTTP {resp.status_code} "
        f"response (possible block page or upstream contract change)"
    )


async def probe_eapi_domain(
    domain: str,
    *,
    timeout: float = 8.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> bool:
    """Cheaply check whether a domain serves real EAPI JSON.

    Issues an unauthenticated GET /eapi/info/domains — never /eapi/user/login,
    which is rate-limited (~10/hour/IP) and would lock out real credentials.
    Healthy means HTTP 200 with a parseable JSON object containing a domains
    payload. Redirects are NOT followed: DiamWall's 307 self-redirect is
    itself the walled signal.

    The `transport` parameter exists for tests (httpx.MockTransport); it is
    None in production.
    """
    url = f"https://{domain.strip().rstrip('/')}/eapi/info/domains"
    try:
        async with httpx.AsyncClient(
            headers=EAPI_HEADERS,
            timeout=httpx.Timeout(timeout, connect=timeout),
            follow_redirects=False,
            transport=transport,
        ) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - any network failure means unusable
        logger.debug(f"EAPI probe failed for {domain}: {type(exc).__name__}: {exc}")
        return False
    if resp.status_code != 200:
        logger.debug(f"EAPI probe: {domain} returned HTTP {resp.status_code}")
        return False
    if "diamwall" in resp.text.lower():
        logger.debug(f"EAPI probe: {domain} served a DiamWall block page")
        return False
    try:
        payload = resp.json()
    except ValueError:
        logger.debug(f"EAPI probe: {domain} returned non-JSON")
        return False
    return isinstance(payload, dict) and "domains" in payload


async def resolve_eapi_domain(
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> str:
    """Resolve the EAPI domain to use for login.

    If ZLIBRARY_EAPI_DOMAIN is set, that domain is returned as-is with no
    probing — an explicit override means no silent switching, ever.
    Otherwise each candidate in DEFAULT_EAPI_DOMAINS is probed in order and
    the first healthy one wins. If every candidate fails the probe, the
    first candidate is returned so login can surface the real error.
    """
    override = os.environ.get("ZLIBRARY_EAPI_DOMAIN")
    if override and override.strip():
        domain = override.strip()
        logger.info(f"EAPI domain pinned via ZLIBRARY_EAPI_DOMAIN: {domain}")
        return domain
    for candidate in DEFAULT_EAPI_DOMAINS:
        if await probe_eapi_domain(candidate, transport=transport):
            logger.info(f"EAPI domain selected by probe: {candidate}")
            return candidate
        logger.warning(
            f"EAPI candidate domain {candidate} failed health probe "
            f"(anti-bot wall or unreachable), trying next"
        )
    logger.warning(
        f"All EAPI candidate domains failed the health probe; "
        f"falling back to {DEFAULT_EAPI_DOMAINS[0]}"
    )
    return DEFAULT_EAPI_DOMAINS[0]


async def select_advertised_domain(
    advertised: Sequence[Union[str, dict]],
    current: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[str]:
    """Pick the first usable domain from a /eapi/info/domains listing.

    /eapi/info/domains still advertises walled domains first (ISSUE-API-002),
    so blindly adopting the primary entry would switch a working client onto
    a dead domain. Each advertised entry is probed before being accepted;
    walled/dead ones are skipped. Returns `current` if it appears in the list
    before any healthy alternative (no switch needed), the first advertised
    domain that passes the probe otherwise, or None when nothing advertised
    is usable — the caller keeps whatever domain it already has.
    """
    for entry in advertised or []:
        domain = entry if isinstance(entry, str) else (entry or {}).get("domain", "")
        if not domain:
            continue
        if domain == current:
            return current
        if await probe_eapi_domain(domain, transport=transport):
            return domain
        logger.warning(
            f"Skipping advertised EAPI domain {domain}: failed health probe "
            f"(anti-bot wall or unreachable)"
        )
    return None


# RFC 6266 / RFC 5987 permit three spellings of the filename parameter, and
# Z-Library uses all of them depending on the title's character set. Tried in
# priority order: the extended form wins when present, because a server sending
# `filename*` also sends an ASCII-mangled `filename` fallback that loses
# characters.
_CD_EXTENDED = re.compile(r"filename\*\s*=\s*UTF-8''([^;\s]+)", re.IGNORECASE)
_CD_QUOTED = re.compile(r'filename\s*=\s*"([^"]*)"', re.IGNORECASE)
_CD_BARE = re.compile(r'filename\s*=\s*([^;"\s]+)', re.IGNORECASE)
_DOWNLOAD_STAGING_PREFIX = ".zlibrary-eapi-"


def filename_from_content_disposition(header: str) -> Optional[str]:
    """Extract a filename from a Content-Disposition header.

    Returns None when the header carries no usable filename, leaving the caller
    to fall back to the URL path.

    The previous implementation split on the literal ``"filename="``, which
    mis-parsed the RFC 6266 extended form: ``filename*=UTF-8''%E2%80%A6`` split
    into a fragment beginning with ``*`` and yielded percent-encoded bytes as the
    name. It also could not tell ``filename`` from ``filename*``.
    """
    if not header:
        return None

    match = _CD_EXTENDED.search(header)
    if match:
        return unquote(match.group(1)).strip() or None

    for pattern in (_CD_QUOTED, _CD_BARE):
        match = pattern.search(header)
        if match:
            # A bare value may still be single-quoted by lenient servers.
            return match.group(1).strip().strip("'") or None

    return None


def sanitize_download_filename(filename: str) -> str:
    """Reduce a filename to a bare basename safe to join onto a directory.

    The value can originate from a server-controlled Content-Disposition header,
    so directory components must be stripped before it reaches the filesystem.
    Both separators are handled regardless of host platform: ``os.path.basename``
    on POSIX does not treat a backslash as a separator, so a Windows-style
    ``..\\..\\x`` payload would survive on a Linux server and then escape once the
    path reached a Windows client.
    """
    if not filename:
        return ""
    # Take the last segment under either separator convention.
    candidate = ntpath.basename(posixpath.basename(filename)).strip()
    # "." and ".." are not usable names; treat them as absent.
    if candidate in {"", ".", ".."}:
        return ""
    return candidate


class EAPIClient:
    """Z-Library EAPI client using httpx with cookie-based auth."""

    def __init__(
        self,
        domain: str,
        remix_userid: Optional[str] = None,
        remix_userkey: Optional[str] = None,
    ):
        self.domain = domain.rstrip("/")
        self.remix_userid = remix_userid
        self.remix_userkey = remix_userkey
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def _cookies(self) -> Dict[str, str]:
        cookies = {"siteLanguageV2": "en"}
        if self.remix_userid:
            cookies["remix_userid"] = str(self.remix_userid)
        if self.remix_userkey:
            cookies["remix_userkey"] = str(self.remix_userkey)
        return cookies

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=EAPI_HEADERS,
                cookies=self._cookies,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> dict:
        client = await self._get_client()
        url = f"{self.base_url}{path}"
        resp = await client.post(url, data=data or {})
        return decode_eapi_json(resp, self.domain)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        client = await self._get_client()
        url = f"{self.base_url}{path}"
        resp = await client.get(url, params=params)
        return decode_eapi_json(resp, self.domain)

    # --- Auth ---

    async def login(self, email: str, password: str) -> dict:
        """POST /eapi/user/login — returns {success, user: {id, remix_userkey}}."""
        result = await self._post(
            "/eapi/user/login",
            {
                "email": email,
                "password": password,
            },
        )
        if result.get("success") == 1 and "user" in result:
            user = result["user"]
            self.remix_userid = str(user.get("id", ""))
            self.remix_userkey = str(user.get("remix_userkey", ""))
            # Recreate client with new cookies
            await self.close()
        return result

    # --- Book endpoints ---

    async def search(
        self,
        message: str,
        *,
        limit: int = 10,
        page: int = 1,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        languages: Optional[List[str]] = None,
        extensions: Optional[List[str]] = None,
        exact: bool = False,
        order: Optional[str] = None,
    ) -> dict:
        """POST /eapi/book/search with form-encoded body."""
        data: Dict[str, Any] = {
            "message": message,
            "limit": str(limit),
            "page": str(page),
        }
        if year_from is not None:
            data["yearFrom"] = str(year_from)
        if year_to is not None:
            data["yearTo"] = str(year_to)
        if languages:
            data["languages[]"] = languages
        if extensions:
            data["extensions[]"] = extensions
        if exact:
            data["e"] = "1"
        if order:
            data["order"] = order
        return await self._post("/eapi/book/search", data)

    async def get_book_info(self, book_id: int, book_hash: str) -> dict:
        """GET /eapi/book/{id}/{hash}."""
        return await self._get(f"/eapi/book/{book_id}/{book_hash}")

    async def get_download_link(self, book_id: int, book_hash: str) -> dict:
        """GET /eapi/book/{id}/{hash}/file."""
        return await self._get(f"/eapi/book/{book_id}/{book_hash}/file")

    async def get_recently(self) -> dict:
        """GET /eapi/book/recently."""
        return await self._get("/eapi/book/recently")

    async def get_most_popular(self) -> dict:
        """GET /eapi/book/most-popular."""
        return await self._get("/eapi/book/most-popular")

    async def get_downloaded(
        self,
        order: Optional[str] = None,
        page: int = 1,
        limit: int = 10,
    ) -> dict:
        """GET /eapi/user/book/downloaded."""
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if order:
            params["order"] = order
        return await self._get("/eapi/user/book/downloaded", params)

    async def get_profile(self) -> dict:
        """GET /eapi/user/profile."""
        return await self._get("/eapi/user/profile")

    async def get_similar(self, book_id: int, book_hash: str) -> dict:
        """GET /eapi/book/{id}/{hash}/similar."""
        return await self._get(f"/eapi/book/{book_id}/{book_hash}/similar")

    async def get_domains(self) -> dict:
        """GET /eapi/info/domains."""
        return await self._get("/eapi/info/domains")

    async def download_file(
        self,
        book_id: int,
        book_hash: str,
        output_dir: str,
        filename: Optional[str] = None,
    ) -> str:
        """Download a book file using EAPI download link.

        Args:
            book_id: Z-Library book ID
            book_hash: Book hash for URL construction
            output_dir: Directory to save the file
            filename: Optional filename; derived from response headers or URL if omitted

        Returns:
            Absolute path to the downloaded file

        Raises:
            httpx.HTTPStatusError: On HTTP errors
            RuntimeError: If no download link or empty response
        """
        # Get download link from EAPI
        dl_resp = await self.get_download_link(book_id, book_hash)
        download_url = (
            dl_resp.get("file", {}).get("downloadLink")
            or dl_resp.get("downloadLink", "")
            or dl_resp.get("url", "")
            or dl_resp.get("link", "")
        )

        if not download_url:
            raise RuntimeError(
                f"EAPI returned no download link for book {book_id}. Response: {dl_resp}"
            )

        # Make URL absolute if needed
        if download_url.startswith("/"):
            download_url = f"{self.base_url}{download_url}"

        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)

        output_path: Optional[Path] = None
        staging_path: Optional[Path] = None
        try:
            # Stream download
            async with httpx.AsyncClient(
                cookies=self._cookies,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as dl_client:
                async with dl_client.stream("GET", download_url) as response:
                    response.raise_for_status()

                    # Determine filename
                    if not filename:
                        cd = response.headers.get("content-disposition", "")
                        filename = filename_from_content_disposition(cd)
                        if not filename:
                            # Derive from URL path
                            url_path = str(response.url).split("?")[0]
                            filename = url_path.split("/")[-1] or f"{book_id}.bin"

                    # The filename may come from a server-controlled header, so reduce
                    # it to a bare basename before joining. Without this,
                    # `filename="../../x"` escapes output_dir entirely, since
                    # Path("/downloads") / "../../x" resolves outside the directory.
                    filename = sanitize_download_filename(filename) or f"{book_id}.bin"

                    output_path = Path(output_dir) / filename
                    staging_fd, staging_name = tempfile.mkstemp(
                        dir=output_dir,
                        prefix=_DOWNLOAD_STAGING_PREFIX,
                        suffix=".part",
                    )
                    staging_path = Path(staging_name)
                    os.close(staging_fd)

                    async with aiofiles.open(staging_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)

            if staging_path.stat().st_size == 0:
                raise RuntimeError(f"Download produced empty file for book {book_id}")

            os.replace(staging_path, output_path)
            staging_path = None
        finally:
            if staging_path is not None:
                try:
                    staging_path.unlink(missing_ok=True)
                except OSError as exc:
                    # Cleanup must not replace the transfer's original exception or
                    # cancellation with an unlink failure.
                    logger.warning(
                        f"Failed to remove incomplete EAPI download {staging_path}: {exc}"
                    )

        return str(output_path.resolve())


def normalize_eapi_book(eapi_book: dict) -> dict:
    """Map EAPI book fields to existing MCP tool output format."""
    author = eapi_book.get("author", "")
    return {
        "id": str(eapi_book.get("id", "")),
        "name": eapi_book.get("title", ""),
        "title": eapi_book.get("title", ""),
        "author": author,
        "authors": [author] if author else [],
        "year": eapi_book.get("year", ""),
        "language": eapi_book.get("language", ""),
        "extension": eapi_book.get("extension", ""),
        "size": eapi_book.get("filesize", ""),
        "rating": eapi_book.get("rating", ""),
        "quality": eapi_book.get("qualityScore", ""),
        "cover": eapi_book.get("cover", ""),
        "url": eapi_book.get("href", ""),
        "isbn": eapi_book.get("isbn", ""),
        "publisher": eapi_book.get("publisher", ""),
        "hash": eapi_book.get("hash", ""),
        "book_hash": eapi_book.get("hash", ""),
        "pages": eapi_book.get("pages", ""),
    }


def normalize_eapi_search_response(eapi_response: dict) -> List[dict]:
    """Extract books array from EAPI search response and normalize each."""
    books = eapi_response.get("books", [])
    return [normalize_eapi_book(b) for b in books]
