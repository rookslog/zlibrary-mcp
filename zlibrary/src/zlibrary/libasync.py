import asyncio
import httpx
import aiofiles
import os
from pathlib import Path

from typing import List, Union, Optional, Dict
from urllib.parse import quote

from .logger import logger
from .exception import (
    EmptyQueryError,
    ProxyNotMatchError,
    NoProfileError,
    NoDomainError,
    LoginFailed,
    DownloadError,
)
from .util import GET_request, POST_request, GET_request_cookies
from .eapi import (
    EAPIClient,
    normalize_eapi_search_response,
    resolve_eapi_domain,
    select_advertised_domain,
)
from .profile import ZlibProfile
from .const import Extension, Language, OrderOptions
import json


ZLIB_DOMAIN = "https://z-library.sk/"
LOGIN_DOMAIN = "https://z-library.sk/rpc.php"

ZLIB_TOR_DOMAIN = (
    "http://bookszlibb74ugqojhzhg2a63w5i2atv5bqarulgczawnbmsb6s6qead.onion"
)
LOGIN_TOR_DOMAIN = (
    "http://loginzlib2vrak5zzpcocc3ouizykn6k5qecgj2tzlnab5wcbqhembyd.onion/rpc.php"
)


class AsyncZlib:
    semaphore = True
    onion = False

    __semaphore = asyncio.Semaphore(64)

    cookies = None
    proxy_list = None

    _mirror = ""
    login_domain = None
    domain = None
    profile = None
    _eapi: Optional[EAPIClient] = None
    _personal_domain: Optional[str] = None

    @property
    def mirror(self):
        return self._mirror

    @mirror.setter
    def mirror(self, value):
        if not value.startswith("http"):
            value = "https://" + value
        self._mirror = value

    def __init__(
        self,
        onion: bool = False,
        proxy_list: Optional[list] = None,
        disable_semaphore: bool = False,
    ):
        if proxy_list:
            if type(proxy_list) is list:
                self.proxy_list = proxy_list
                logger.debug("Set proxy_list: %s", str(proxy_list))
            else:
                raise ProxyNotMatchError

        if onion:
            self.onion = True
            self.login_domain = LOGIN_TOR_DOMAIN
            self.domain = ZLIB_TOR_DOMAIN
            self.mirror = self.domain

            if not proxy_list:
                print(
                    "Tor proxy must be set to route through onion domains.\n"
                    "Set up a tor service and use: onion=True, proxy_list=['socks5://127.0.0.1:9050']"
                )
                exit(1)
        else:
            self.login_domain = LOGIN_DOMAIN
            self.domain = ZLIB_DOMAIN

        if disable_semaphore:
            self.semaphore = False

    async def _r(self, url: str):
        if self.semaphore:
            async with self.__semaphore:
                response = await GET_request(
                    url, proxy_list=self.proxy_list, cookies=self.cookies
                )
                if hasattr(response, "text"):
                    logger.debug(f"Response text for {url}: {response.text[:1000]}")
                else:
                    logger.debug(
                        f"Response for {url} is not an HTTPX object, it is a string: {str(response)[:1000]}"
                    )
                return response
        else:
            response = await GET_request(
                url, proxy_list=self.proxy_list, cookies=self.cookies
            )
            if hasattr(response, "text"):
                logger.debug(f"Response text for {url}: {response.text[:1000]}")
            else:
                logger.debug(
                    f"Response for {url} is not an HTTPX object, it is a string: {str(response)[:1000]}"
                )
            return response

    async def login(self, email: str, password: str):
        data = {
            "isModal": True,
            "email": email,
            "password": password,
            "site_mode": "books",
            "action": "login",
            "isSingleLogin": 1,
            "redirectUrl": "",
            "gg_json_mode": 1,
        }

        resp, jar = await POST_request(
            self.login_domain, data, proxy_list=self.proxy_list
        )
        resp = json.loads(resp)
        resp = resp["response"]
        logger.debug(f"Login response: {resp}")
        if resp.get("validationError"):
            raise LoginFailed(json.dumps(resp, indent=4))
        self._jar = jar

        self.cookies = {}
        for cookie in self._jar:
            self.cookies[cookie.key] = cookie.value
        logger.debug("Set cookies: %s", self.cookies)

        if self.onion and self.domain:
            url = self.domain + "/?remix_userkey=%s&remix_userid=%s" % (
                self.cookies["remix_userkey"],
                self.cookies["remix_userid"],
            )
            resp, jar = await GET_request_cookies(
                url, proxy_list=self.proxy_list, cookies=self.cookies
            )

            self._jar = jar
            for cookie in self._jar:
                self.cookies[cookie.key] = cookie.value
            logger.debug("Set cookies: %s", self.cookies)

            self.mirror = self.domain
            logger.info("Set working mirror: %s" % self.mirror)
        else:
            self.mirror = ZLIB_DOMAIN.strip("/")

            if not self.mirror:
                raise NoDomainError

        # Initialize EAPI client with auth cookies. Respects
        # ZLIBRARY_EAPI_DOMAIN, otherwise probes the fallback list
        # (ISSUE-API-002: the old hardcoded z-library.sk is DiamWall-walled).
        eapi_domain = await resolve_eapi_domain()
        self._eapi = EAPIClient(
            domain=eapi_domain,
            remix_userid=self.cookies.get("remix_userid"),
            remix_userkey=self.cookies.get("remix_userkey"),
        )

        # Discover personal domain for downloads — probe advertised domains
        # before adopting one, since /eapi/info/domains advertises walled
        # domains first (ISSUE-API-002).
        try:
            domains_resp = await self._eapi.get_domains()
            domains = domains_resp.get("domains", [])
            usable = await select_advertised_domain(domains, eapi_domain)
            if usable:
                self._personal_domain = usable
                logger.info(f"EAPI personal domain: {self._personal_domain}")
            elif domains:
                logger.warning(
                    "No advertised EAPI domain passed the health probe; "
                    f"keeping {eapi_domain} for downloads."
                )
        except Exception as e:
            logger.warning(f"Failed to discover EAPI domains: {e}. Using default.")

        self.profile = ZlibProfile(
            self._r, self.cookies, self.mirror, ZLIB_DOMAIN, eapi_client=self._eapi
        )
        return self.profile

    async def logout(self):
        self._jar = None
        self.cookies = None
        if self._eapi:
            await self._eapi.close()
            self._eapi = None

    async def search(
        self,
        q: str = "",
        exact: bool = False,
        from_year: Optional[int] = None,
        to_year: Optional[int] = None,
        lang: List[Union[Language, str]] = [],
        extensions: List[Union[Extension, str]] = [],
        content_types: Optional[List[str]] = None,
        order: Optional[Union[OrderOptions, str]] = None,
        count: int = 10,
    ):
        if not self.profile:
            raise NoProfileError
        if not q and not (
            order and (order == OrderOptions.NEWEST or order == "date_created")
        ):
            raise EmptyQueryError(
                "Search query cannot be empty unless ordering by newest."
            )

        if not self._eapi:
            raise NoProfileError("EAPI client not initialized. Call login() first.")

        # Build EAPI search params
        languages = None
        if lang:
            languages = []
            for la in lang:
                if isinstance(la, str):
                    languages.append(la)
                elif isinstance(la, Language):
                    languages.append(la.value)

        ext_list = None
        if extensions:
            ext_list = []
            for ext in extensions:
                if isinstance(ext, str):
                    ext_list.append(ext.upper())
                elif isinstance(ext, Extension):
                    ext_list.append(ext.value.upper())

        order_str = None
        if order:
            if isinstance(order, OrderOptions):
                order_str = order.value
            elif isinstance(order, str):
                allowed_orders = [opt.value for opt in OrderOptions]
                if order in allowed_orders:
                    order_str = order
                else:
                    logger.warning(f"Invalid order value '{order}'. Ignoring.")

        logger.info(
            f"EAPI search: q={q}, exact={exact}, count={count}, order={order_str}"
        )

        try:
            eapi_resp = await self._eapi.search(
                message=q,
                limit=count,
                page=1,
                year_from=from_year,
                year_to=to_year,
                languages=languages,
                extensions=ext_list,
                exact=exact,
                order=order_str,
            )
        except Exception as e:
            logger.error(f"EAPI search failed: {e}", exc_info=True)
            raise

        books = normalize_eapi_search_response(eapi_resp)

        # Build a lightweight paginator-like wrapper for backward compat
        paginator = _EAPISearchResult(
            books,
            eapi_resp,
            self._eapi,
            q,
            count=count,
            exact=exact,
            from_year=from_year,
            to_year=to_year,
            languages=languages,
            extensions=ext_list,
            order=order_str,
        )
        payload = f"{self.mirror}/eapi/book/search?message={quote(q)}"

        logger.info(f"EAPI search returned {len(books)} results")
        return paginator, payload

    async def full_text_search(
        self,
        q: str = "",
        exact: bool = False,
        phrase: bool = False,
        words: bool = False,
        from_year: Optional[int] = None,
        to_year: Optional[int] = None,
        lang: List[Union[Language, str]] = [],
        extensions: List[Union[Extension, str]] = [],
        content_types: Optional[List[str]] = None,
        count: int = 10,
    ):
        """Full text search via EAPI.

        NOTE: EAPI does not distinguish full-text vs title search.
        This routes through the same EAPI search endpoint. The phrase/words
        parameters are accepted for API compatibility but have no effect
        on the EAPI backend.
        """
        if not self.profile:
            raise NoProfileError
        if not q:
            raise EmptyQueryError
        if not phrase and not words:
            raise Exception(
                "You should either specify 'words=True' to match words, or 'phrase=True' to match phrase."
            )

        if not self._eapi:
            raise NoProfileError("EAPI client not initialized. Call login() first.")

        # Build language list
        languages = None
        if lang:
            languages = []
            for la in lang:
                if isinstance(la, str):
                    languages.append(la)
                elif isinstance(la, Language):
                    languages.append(la.value)

        ext_list = None
        if extensions:
            ext_list = []
            for ext in extensions:
                if isinstance(ext, str):
                    ext_list.append(ext.upper())
                elif isinstance(ext, Extension):
                    ext_list.append(ext.value.upper())

        logger.info(f"EAPI full_text_search (routed to search): q={q}, exact={exact}")

        try:
            eapi_resp = await self._eapi.search(
                message=q,
                limit=count,
                page=1,
                year_from=from_year,
                year_to=to_year,
                languages=languages,
                extensions=ext_list,
                exact=exact,
            )
        except Exception as e:
            logger.error(f"EAPI full_text_search failed: {e}", exc_info=True)
            raise

        books = normalize_eapi_search_response(eapi_resp)
        paginator = _EAPISearchResult(
            books,
            eapi_resp,
            self._eapi,
            q,
            count=count,
            exact=exact,
            from_year=from_year,
            to_year=to_year,
            languages=languages,
            extensions=ext_list,
        )
        payload = f"{self.mirror}/eapi/book/search?message={quote(q)}"

        logger.info(f"EAPI full_text_search returned {len(books)} results")
        return paginator, payload

    async def download_book(self, book_details: Dict, output_dir_str: str) -> str:
        """Downloads a book using EAPI to get the download link."""
        if not self.profile:
            raise NoProfileError()

        if not self._eapi:
            raise NoProfileError("EAPI client not initialized. Call login() first.")

        book_id = book_details.get("id", "Unknown")
        book_hash = book_details.get("hash") or book_details.get("book_hash", "")

        if not book_hash:
            logger.warning(
                f"No hash found in book_details for book ID {book_id}. "
                f"Attempting download without hash may fail."
            )

        logger.info(f"EAPI download_book: id={book_id}, hash={book_hash}")

        try:
            # Get download link from EAPI
            dl_resp = await self._eapi.get_download_link(int(book_id), book_hash)
            download_url = dl_resp.get("file", {}).get("downloadLink") or dl_resp.get(
                "downloadLink", ""
            )

            if not download_url:
                # Try alternative response structures
                if isinstance(dl_resp, dict):
                    # Some EAPI responses put URL at top level
                    download_url = dl_resp.get("url", "") or dl_resp.get("link", "")

            if not download_url:
                raise DownloadError(
                    f"EAPI returned no download link for book ID {book_id}. Response: {dl_resp}"
                )

            # Make URL absolute if needed
            if download_url.startswith("/"):
                base = (
                    f"https://{self._personal_domain}"
                    if self._personal_domain
                    else self.mirror
                )
                download_url = f"{base.rstrip('/')}{download_url}"

            logger.info(f"EAPI download URL: {download_url}")

        except httpx.HTTPStatusError as e:
            logger.error(f"EAPI download link request failed: {e}", exc_info=True)
            raise DownloadError(
                f"Failed to get download link for book ID {book_id} (HTTP {e.response.status_code})"
            ) from e
        except DownloadError:
            raise
        except Exception as e:
            logger.error(
                f"Error getting EAPI download link for book ID {book_id}: {e}",
                exc_info=True,
            )
            raise DownloadError(
                f"Failed to get download link for book ID {book_id}"
            ) from e

        # Construct output path
        extension = book_details.get("extension", "epub")
        filename = f"{book_id}.{extension}"
        output_directory = Path(output_dir_str)
        actual_output_path = output_directory / filename

        logger.info(f"Downloading from {download_url} to {actual_output_path}")

        # Ensure output directory exists
        try:
            os.makedirs(output_directory, exist_ok=True)
        except OSError as e:
            raise DownloadError(
                f"Failed to create output directory {output_directory}: {e}"
            ) from e

        # Stream download
        try:
            cookies = self._eapi._cookies if self._eapi else (self.cookies or {})
            async with httpx.AsyncClient(
                cookies=cookies,
                # Never replay session cookies to a redirect destination.
                follow_redirects=False,
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as client:
                async with client.stream("GET", download_url) as response:
                    response.raise_for_status()
                    total_size = int(response.headers.get("content-length", 0))
                    logger.info(f"Starting download ({total_size} bytes)...")

                    async with aiofiles.open(actual_output_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)

            logger.info(
                f"Successfully downloaded book ID {book_id} to {actual_output_path}"
            )
            return str(actual_output_path)

        except httpx.HTTPStatusError as e:
            if os.path.exists(actual_output_path):
                os.remove(actual_output_path)
            raise DownloadError(
                f"Download failed for book ID {book_id} (HTTP {e.response.status_code})"
            ) from e
        except httpx.RequestError as e:
            if os.path.exists(actual_output_path):
                os.remove(actual_output_path)
            raise DownloadError(
                f"Download failed for book ID {book_id} (Network Error)"
            ) from e
        except Exception as e:
            try:
                if os.path.exists(actual_output_path):
                    os.remove(actual_output_path)
            except OSError:
                pass
            raise DownloadError(
                f"An unexpected error occurred during download for book ID {book_id}"
            ) from e


class _EAPISearchResult:
    """Lightweight paginator-like wrapper around EAPI search results.

    Provides backward compatibility with SearchPaginator interface
    (result, total, next/prev page navigation).
    """

    def __init__(
        self,
        books,
        eapi_response,
        eapi_client,
        query,
        count=10,
        exact=False,
        from_year=None,
        to_year=None,
        languages=None,
        extensions=None,
        order=None,
    ):
        self.result = books
        self.storage = {1: books}
        self.page = 1
        self.total = eapi_response.get("totalPages", 1)
        self.count = count
        self._eapi = eapi_client
        self._query = query
        self._exact = exact
        self._from_year = from_year
        self._to_year = to_year
        self._languages = languages
        self._extensions = extensions
        self._order = order
        self.__pos = 0

    def __repr__(self):
        return f"<EAPISearchResult query='{self._query}', count={self.count}, results={len(self.result)}, pages={self.total}>"

    async def next(self):
        if self.__pos >= len(self.storage.get(self.page, [])):
            await self.next_page()
            if not self.storage.get(self.page):
                self.result = []
                return self.result

        self.result = self.storage.get(self.page, [])[
            self.__pos : self.__pos + self.count
        ]
        self.__pos += self.count
        return self.result

    async def prev(self):
        self.__pos -= self.count
        if self.__pos < 0:
            await self.prev_page()
            if not self.storage.get(self.page):
                self.result = []
                return self.result

        start = max(0, self.__pos)
        self.result = self.storage.get(self.page, [])[start : start + self.count]
        return self.result

    async def next_page(self):
        if self.page < self.total:
            self.page += 1
            self.__pos = 0
        else:
            return

        if not self.storage.get(self.page):
            try:
                eapi_resp = await self._eapi.search(
                    message=self._query,
                    limit=self.count,
                    page=self.page,
                    year_from=self._from_year,
                    year_to=self._to_year,
                    languages=self._languages,
                    extensions=self._extensions,
                    exact=self._exact,
                    order=self._order,
                )
                self.storage[self.page] = normalize_eapi_search_response(eapi_resp)
            except Exception as e:
                logger.error(f"EAPI next_page failed: {e}")
                self.storage[self.page] = []

    async def prev_page(self):
        if self.page > 1:
            self.page -= 1
            self.__pos = 0
        else:
            self.__pos = 0
            return

        if not self.storage.get(self.page):
            try:
                eapi_resp = await self._eapi.search(
                    message=self._query,
                    limit=self.count,
                    page=self.page,
                    year_from=self._from_year,
                    year_to=self._to_year,
                    languages=self._languages,
                    extensions=self._extensions,
                    exact=self._exact,
                    order=self._order,
                )
                self.storage[self.page] = normalize_eapi_search_response(eapi_resp)
            except Exception as e:
                logger.error(f"EAPI prev_page failed: {e}")
                self.storage[self.page] = []

        self.__pos = len(self.storage.get(self.page, []))
