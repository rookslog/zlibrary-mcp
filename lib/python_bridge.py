#!/usr/bin/env python3
import sys
import os
import json
import traceback

# Add project root to sys.path to allow importing 'lib'
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import asyncio

from pathlib import Path
from filename_utils import create_unified_filename
import logging

# Import the new RAG processing functions
from lib import rag_processing

# Import enhanced metadata extraction
from lib import enhanced_metadata

# Import multi-source router
from lib.sources.router import SourceRouter
from lib.sources.config import get_source_config

# Add zlibrary source directory to path for EAPI imports
zlibrary_src_path = os.path.join(os.path.dirname(__file__), "..", "zlibrary", "src")
if zlibrary_src_path not in sys.path:
    sys.path.insert(0, zlibrary_src_path)
from zlibrary.eapi import (
    EAPIClient,
    normalize_eapi_book,
    normalize_eapi_search_response,
    resolve_eapi_domain,
    select_advertised_domain,
)

logger = logging.getLogger("zlibrary")  # Get the 'zlibrary' logger instance

# Module-level EAPI client (created after login)
_eapi_client: EAPIClient = None

# Module-level source router (for multi-source search)
_source_router: SourceRouter = None

# Debug mode configuration (ISSUE-009)
# Enable with: ZLIBRARY_DEBUG=1 or DEBUG=1
DEBUG_MODE = os.environ.get("ZLIBRARY_DEBUG", os.environ.get("DEBUG", "")).lower() in (
    "1",
    "true",
    "yes",
)


def _configure_debug_logging():
    """Configure logging based on debug mode setting."""
    if DEBUG_MODE:
        # Set up detailed debug logging
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled via environment variable")
    else:
        # Default: INFO level with simpler format
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        logger.setLevel(logging.INFO)


# Configure logging on module load
_configure_debug_logging()


def is_debug_mode() -> bool:
    """Check if debug mode is enabled.

    Returns:
        True if ZLIBRARY_DEBUG=1 or DEBUG=1 environment variable is set
    """
    return DEBUG_MODE


# Custom Internal Exceptions
class InternalBookNotFoundError(Exception):
    """Custom exception for when a book ID lookup results in a 404."""

    pass


class InternalParsingError(Exception):
    """Custom exception for errors during HTML parsing of book details."""

    pass


class InternalFetchError(Exception):
    """Error during HTTP request (network, non-200 status, timeout)."""

    pass


# Helper function to parse string lists into ZLibrary enums
def _parse_enums(items, enum_class):
    logger.debug(
        f"_parse_enums: received items={items} for enum_class={enum_class.__name__}"
    )
    parsed_items = []
    if items:
        for item_str in items:
            if not item_str or not isinstance(item_str, str):
                logger.warning(
                    f"_parse_enums: skipping invalid item '{item_str}' in items list {items}"
                )
                continue
            try:
                enum_member = getattr(enum_class, item_str.upper())
                parsed_items.append(enum_member)
                logger.debug(
                    f"_parse_enums: successfully parsed '{item_str}' to {enum_member}"
                )
            except AttributeError:
                # If not found in enum, pass the string directly
                parsed_items.append(item_str)
                logger.debug(
                    f"_parse_enums: attribute error for '{item_str.upper()}' in {enum_class.__name__}, appending original string '{item_str}'"
                )
            except Exception as e:
                logger.error(
                    f"_parse_enums: error processing item '{item_str}' for enum {enum_class.__name__}: {e}"
                )
    logger.debug(f"_parse_enums: returning parsed_items={parsed_items}")
    return parsed_items


def normalize_book_details(book: dict, mirror: str = None) -> dict:
    """
    Normalize book details to ensure all required fields present.

    Handles both EAPI response format and legacy format:
    - EAPI provides 'hash' / 'book_hash' directly
    - Legacy format had 'href' with hash embedded in URL path
    - Ensures 'url', 'book_hash' fields for downstream operations

    Args:
        book: Book dictionary from search results
        mirror: Z-Library mirror URL (optional)

    Returns:
        Normalized book dictionary with guaranteed 'url' and 'book_hash' fields
    """
    normalized = book.copy()

    # EAPI provides hash directly — use it
    if "book_hash" not in normalized:
        if "hash" in normalized and normalized["hash"]:
            normalized["book_hash"] = normalized["hash"]
        elif "href" in normalized:
            hash_value = _extract_book_hash_from_href(normalized["href"])
            if hash_value:
                normalized["book_hash"] = hash_value

    # Add 'url' from 'href' if missing
    if "url" not in normalized and "href" in normalized:
        href = normalized["href"]
        if href.startswith("http"):
            normalized["url"] = href
        else:
            mirror_url = mirror or os.getenv("ZLIBRARY_MIRROR", "https://z-library.sk")
            normalized["url"] = f"{mirror_url.rstrip('/')}/{href.lstrip('/')}"

    return normalized


def _extract_book_hash_from_href(href: str) -> str:
    """Extract book hash from href path like '/book/ID/HASH/title'."""
    if not href:
        return None
    parts = href.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "book":
        return parts[2]
    return None


async def get_eapi_client() -> EAPIClient:
    """
    Get the shared EAPI client, creating it if needed.

    The EAPI client is created during initialize_eapi_client() which is
    called from main() after login succeeds.

    Returns:
        Authenticated EAPIClient instance

    Raises:
        RuntimeError: If EAPI client not initialized
    """
    global _eapi_client
    if _eapi_client is None:
        raise RuntimeError(
            "EAPI client not initialized. Call initialize_eapi_client() first."
        )
    return _eapi_client


async def initialize_eapi_client() -> EAPIClient:
    """
    Initialize the shared EAPI client using environment credentials.

    Creates an EAPIClient, logs in, discovers domains, and stores
    the client for reuse by all tool functions.

    Returns:
        Authenticated EAPIClient instance
    """
    global _eapi_client

    email = os.environ.get("ZLIBRARY_EMAIL")
    password = os.environ.get("ZLIBRARY_PASSWORD")

    if not email or not password:
        raise ValueError(
            "ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD environment variables required"
        )

    # ISSUE-API-002: the old single default (z-library.sk) is fronted by the
    # DiamWall anti-bot wall. resolve_eapi_domain() honours an explicit
    # ZLIBRARY_EAPI_DOMAIN override without probing; otherwise it probes the
    # fallback list (GET /eapi/info/domains — never login, which is
    # rate-limited) and logs in on the first healthy candidate.
    domain_pinned = bool(os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip())
    initial_domain = await resolve_eapi_domain()

    client = EAPIClient(initial_domain)
    login_result = await client.login(email, password)

    if login_result.get("success") != 1:
        raise RuntimeError(f"EAPI login failed: {login_result}")

    logger.info(f"EAPI client authenticated (userid={client.remix_userid})")

    # Discover optimal domain — skipped entirely when ZLIBRARY_EAPI_DOMAIN is
    # set (an explicit override means no silent switching, ever). Advertised
    # domains are probed before adoption: /eapi/info/domains still lists
    # walled domains first, and switching blindly would kill a working client.
    if not domain_pinned:
        try:
            domains_result = await client.get_domains()
            domains = domains_result.get("domains", [])
            target = await select_advertised_domain(domains, initial_domain)
            if target and target != initial_domain:
                logger.info(f"Switching EAPI domain: {initial_domain} -> {target}")
                # Create new client on discovered domain with existing credentials
                new_client = EAPIClient(
                    target,
                    remix_userid=client.remix_userid,
                    remix_userkey=client.remix_userkey,
                )
                await client.close()
                client = new_client
            elif not target and domains:
                logger.warning(
                    "No advertised EAPI domain passed the health probe; "
                    f"keeping working domain {initial_domain}"
                )
        except Exception as e:
            logger.warning(f"Domain discovery failed, using initial domain: {e}")

    _eapi_client = client
    return _eapi_client


def _classify_health_error(error: Exception) -> str:
    """Classify a health check error into a specific error code.

    Checks the exception message for anti-bot wall indicators first
    (DiamWall, then Cloudflare), then falls back to exception type checking
    for network errors. Uses only built-in Python exception types (no httpx
    imports) — DiamWallError raised by the EAPI client is matched by its
    message, which always names the wall.
    """
    msg = str(error).lower()
    if "diamwall" in msg:
        return "diamwall_blocked"
    cloudflare_patterns = ["checking your browser", "cloudflare", "cf-", "challenge"]
    for pattern in cloudflare_patterns:
        if pattern in msg:
            return "cloudflare_blocked"
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return "network_error"
    return "unknown_error"


async def eapi_health_check() -> dict:
    """
    Check EAPI connectivity and functionality.

    Performs a minimal search to verify the EAPI client can
    communicate with Z-Library successfully.

    Returns:
        dict with 'status' ('healthy' or 'unhealthy'), 'transport',
        and optionally 'error' and 'error_code'.

    Error codes:
        - diamwall_blocked: DiamWall anti-bot wall served HTML/Access Denied
          where EAPI JSON was expected (fix: export ZLIBRARY_EAPI_DOMAIN to a
          working domain, or unset it to use the built-in fallback list)
        - cloudflare_blocked: Cloudflare challenge detected in response
        - network_error: Connection or timeout failure
        - malformed_response: Non-JSON or unexpected response format
        - unknown_error: Unclassified failure
    """
    try:
        client = await get_eapi_client()
        response = await client.search("test", limit=1)

        if response.get("success") == 1 and isinstance(response.get("books"), list):
            return {
                "status": "healthy",
                "transport": "eapi",
                "books_returned": len(response.get("books", [])),
            }
        else:
            return {
                "status": "unhealthy",
                "transport": "eapi",
                "error": f"Unexpected response: success={response.get('success')}",
                "error_code": "malformed_response",
            }
    except Exception as e:
        return {
            "status": "unhealthy",
            "transport": "eapi",
            "error": str(e),
            "error_code": _classify_health_error(e),
        }


async def search(
    query,
    exact=False,
    from_year=None,
    to_year=None,
    languages=None,
    extensions=None,
    content_types=None,
    count=10,
    client=None,
):
    """
    Search for books using EAPI.

    Args:
        query: Search query string
        exact: Use exact matching
        from_year: Filter by start year
        to_year: Filter by end year
        languages: List of language codes
        extensions: List of file extensions
        content_types: List of content types (ignored by EAPI)
        count: Number of results
        client: Unused (kept for backward compatibility)

    Returns:
        dict with 'retrieved_from_url' and 'books'
    """
    eapi = await get_eapi_client()

    # Convert language/extension enums to strings for EAPI
    lang_list = None
    if languages:
        lang_list = [str(l) if not isinstance(l, str) else l for l in languages]
    ext_list = None
    if extensions:
        ext_list = [str(e) if not isinstance(e, str) else e for e in extensions]

    logger.info(
        f"python_bridge.search: EAPI search query='{query}', exact={exact}, count={count}"
    )

    response = await eapi.search(
        message=query,
        limit=count,
        exact=exact,
        year_from=from_year,
        year_to=to_year,
        languages=lang_list,
        extensions=ext_list,
    )

    books = normalize_eapi_search_response(response)

    return {"retrieved_from_url": f"EAPI search: {query}", "books": books}


async def full_text_search(
    query,
    exact=False,
    phrase=True,
    words=False,
    languages=None,
    extensions=None,
    content_types=None,
    count=10,
):
    """
    Search for text within book contents via EAPI.

    Note: EAPI does not have a separate full-text search endpoint.
    Uses multi-strategy fallback: exact phrase first, then quoted query,
    then standard search. Results are tagged with search_type to indicate
    this is a content-aware fallback, not true full-text search.
    """
    eapi = await get_eapi_client()
    search_type = None
    books = []

    # Convert language/extension lists for EAPI
    lang_list = (
        [str(lang) if not isinstance(lang, str) else lang for lang in languages]
        if languages
        else None
    )
    ext_list = (
        [str(ext) if not isinstance(ext, str) else ext for ext in extensions]
        if extensions
        else None
    )

    # Strategy 1: Try exact phrase search
    if phrase and not exact:
        logger.info("full_text_search: trying exact phrase match for '%s'", query)
        try:
            response = await eapi.search(
                message=query,
                limit=count,
                exact=True,
                languages=lang_list,
                extensions=ext_list,
            )
            result_books = normalize_eapi_search_response(response)
            if result_books:
                books = result_books
                search_type = "exact_phrase"
                logger.info(
                    "full_text_search: exact phrase returned %d results", len(books)
                )
        except Exception as e:
            logger.debug("full_text_search: exact phrase failed: %s", e)

    # Strategy 2: Try quoted query search
    if not books:
        quoted_query = f'"{query}"'
        logger.info("full_text_search: trying quoted query '%s'", quoted_query)
        try:
            response = await eapi.search(
                message=quoted_query,
                limit=count,
                languages=lang_list,
                extensions=ext_list,
            )
            result_books = normalize_eapi_search_response(response)
            if result_books:
                books = result_books
                search_type = "quoted_query"
                logger.info(
                    "full_text_search: quoted query returned %d results", len(books)
                )
        except Exception as e:
            logger.debug("full_text_search: quoted query failed: %s", e)

    # Strategy 3: Fall back to standard search
    if not books:
        logger.info("full_text_search: falling back to standard search for '%s'", query)
        result = await search(
            query=query,
            exact=exact,
            from_year=None,
            to_year=None,
            languages=languages,
            extensions=extensions,
            content_types=content_types,
            count=count,
        )
        books = result.get("books", [])
        search_type = "standard_fallback"
        logger.info("full_text_search: standard search returned %d results", len(books))

    # Tag results with search type
    for book in books:
        book["search_type"] = "content_fallback"

    return {
        "retrieved_from_url": f"EAPI full-text search: {query}",
        "books": books,
        "search_type": search_type,
        "note": (
            "True full-text content search is not available via EAPI. "
            f"Results obtained using '{search_type}' strategy as a content-aware fallback."
        ),
    }


async def search_advanced(
    query,
    exact=False,
    from_year=None,
    to_year=None,
    languages=None,
    extensions=None,
    content_types=None,
    count=10,
    client=None,
):
    """
    Advanced search separating exact matches from fuzzy/approximate matches.

    EAPI has no equivalent of the old website's fuzzyMatchesLine divider, so
    this issues two searches: one in exact mode (e=1 — every term must match
    verbatim, no typo tolerance) and one in default mode (relevance matching
    with typo tolerance). Default-mode results not present in the exact set
    are reported as fuzzy matches. With exact=True the fuzzy search is
    skipped entirely and only strict matches are returned.

    Args:
        query: Search query string
        exact: Only return strict (e=1) matches; skip the fuzzy search
        from_year: Filter by start year
        to_year: Filter by end year
        languages: List of language codes
        extensions: List of file extensions
        content_types: List of content types (ignored by EAPI)
        count: Max results per category
        client: Unused (kept for backward compatibility)

    Returns:
        dict with keys: query, exact_matches, fuzzy_matches,
        has_fuzzy_matches, total_results, retrieved_from_url
    """
    eapi = await get_eapi_client()

    lang_list = None
    if languages:
        lang_list = [str(l) if not isinstance(l, str) else l for l in languages]
    ext_list = None
    if extensions:
        ext_list = [str(e) if not isinstance(e, str) else e for e in extensions]

    logger.info(
        f"python_bridge.search_advanced: EAPI search query='{query}', "
        f"exact={exact}, count={count}"
    )

    exact_response = await eapi.search(
        message=query,
        limit=count,
        exact=True,
        year_from=from_year,
        year_to=to_year,
        languages=lang_list,
        extensions=ext_list,
    )
    exact_matches = normalize_eapi_search_response(exact_response)

    fuzzy_matches = []
    if not exact:
        fuzzy_response = await eapi.search(
            message=query,
            limit=count,
            exact=False,
            year_from=from_year,
            year_to=to_year,
            languages=lang_list,
            extensions=ext_list,
        )
        exact_ids = {b["id"] for b in exact_matches if b.get("id")}
        fuzzy_matches = [
            b
            for b in normalize_eapi_search_response(fuzzy_response)
            if b.get("id") not in exact_ids
        ]

    return {
        "query": query,
        "exact_matches": exact_matches,
        "fuzzy_matches": fuzzy_matches,
        "has_fuzzy_matches": bool(fuzzy_matches),
        "total_results": len(exact_matches) + len(fuzzy_matches),
        "retrieved_from_url": f"EAPI search: {query}",
    }


async def get_download_history(count=10):
    """Get user's download history via EAPI."""
    eapi = await get_eapi_client()
    response = await eapi.get_downloaded(limit=count)
    books = response.get("books", [])
    return [normalize_eapi_book(b) for b in books]


async def get_download_limits():
    """Get user's download limits via EAPI profile."""
    eapi = await get_eapi_client()
    profile = await eapi.get_profile()
    user = profile.get("user", profile)
    return {
        "daily_limit": user.get("downloads_today_limit", "unknown"),
        "daily_remaining": user.get("downloads_today_left", "unknown"),
    }


# --- Core Bridge Functions ---


async def process_document(
    file_path_str: str,
    output_format: str = "txt",
    book_id: str = None,  # Add metadata params
    author: str = None,
    title: str = None,
) -> dict:
    """
    Process a local document and return the path-first structured-output bundle.
    """
    file_path = Path(file_path_str)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path_str}")

    try:
        logger.info(f"Starting processing for: {file_path} with format {output_format}")
        book_details_dict = {
            key: value
            for key, value in {"id": book_id, "author": author, "title": title}.items()
            if value is not None
        }
        return await rag_processing.process_document(
            file_path_str=str(file_path),
            output_format=output_format,
            book_details=book_details_dict or None,
        )

    except Exception as e:
        logger.exception(
            f"Error processing document {file_path_str}"
        )  # Log full traceback
        # Re-raise to be caught by the main handler
        raise RuntimeError(f"Error processing document {file_path_str}: {e}") from e


# --- download_book function needs to be async ---
SOURCE_ALIASES = {"libgen", "annas", "annas_archive"}


async def _download_url_to_file(url: str, output_dir: str, md5: str) -> str:
    """Stream a resolved source URL to disk and return the raw path.

    The source-agnostic half of acquisition: everything downstream of this
    (unified filename, RAG processing, the bundle contract) already works on
    any source, so this is the only piece that had to exist for LibGen and
    Anna's results to become downloadable.

    Guards against the two ways these CDNs fail without erroring: an HTML
    error/interstitial page served with HTTP 200, and a truncated body.
    """
    import httpx

    from lib.sources.libgen import USER_AGENT

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    raw_path = Path(output_dir) / f"{md5}.download"

    # The identifying UA is load-bearing: libgen's hosts serve an HTML stub
    # to blocklisted tool UAs including python-httpx's default (#124), which
    # this function's HTML guard then misreads as an expired key — the
    # adapter verifies the URL with the right UA and the download dies here
    # with the wrong one.
    async with httpx.AsyncClient(
        timeout=180,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                raise ValueError(
                    f"Source served HTML rather than a file for {md5} — the "
                    f"download key has most likely expired. Re-resolve and retry."
                )

            written = 0
            with open(raw_path, "wb") as handle:
                async for chunk in response.aiter_bytes(65536):
                    handle.write(chunk)
                    written += len(chunk)

    if written == 0:
        raw_path.unlink(missing_ok=True)
        raise ValueError(f"Source returned an empty body for {md5}")

    logger.info(f"Downloaded {written} bytes from source to {raw_path}")
    return str(raw_path)


async def _fetch_from_source(book_details: dict, output_dir: str) -> str:
    """Resolve and fetch a non-Z-Library book. Returns the raw file path."""
    md5 = (book_details.get("md5") or "").strip()
    source = (book_details.get("source") or "auto").lower()
    if not md5:
        raise ValueError(
            "Missing 'md5' in bookDetails. Source downloads are addressed by "
            "md5 — pass a result from search_multi_source."
        )

    router = await get_source_router()
    selection = (
        "libgen" if source == "libgen" else "annas" if "annas" in source else "auto"
    )
    result = await router.get_download_url(md5, source=selection)
    logger.info(f"Resolved {source} download for {md5} via {result.source}")

    return await _download_url_to_file(result.url, output_dir, md5)


async def download_book(
    book_details: dict,
    output_dir: str,
    process_for_rag: bool = False,
    processed_output_format: str = "txt",
):
    """
    Downloads a book, optionally processes it, and returns file paths.

    Uses EAPIClient.download_file for the actual download.

    Args:
        book_details: Book dictionary from search (must have 'id' and 'hash'/'book_hash')
        output_dir: Directory to save downloaded file
        process_for_rag: If True, also extract text for RAG
        processed_output_format: Format for RAG output ('txt' or 'markdown')

    Returns:
        dict with 'file_path' and optional 'processed_file_path'
    """
    # Route by source. A result from search_multi_source carries md5 + source
    # and has no Z-Library id/hash, so it takes the source path — which also
    # means a LibGen download needs no Z-Library credentials at all.
    from_source = (book_details.get("source") or "").lower() in SOURCE_ALIASES

    if not from_source:
        eapi = await get_eapi_client()
        # Normalize book details to ensure 'book_hash' field
        book_details = normalize_book_details(book_details)

    book_id = book_details.get("id")
    book_hash = book_details.get("hash") or book_details.get("book_hash", "")

    if not book_id and not from_source:
        logger.error(
            f"Critical: 'id' not found in book_details: {list(book_details.keys())}"
        )
        raise ValueError(
            "Missing 'id' key in bookDetails object. Cannot download without book ID."
        )

    if not book_hash and not from_source:
        logger.warning(
            f"No hash found in book_details for book ID {book_id}. Download may fail."
        )

    downloaded_file_path_str = None
    final_file_path_str = None  # Path with enhanced filename
    processed_file_path_str = None  # Path for RAG processed file
    process_result = None

    try:
        # Step 1: Fetch the file. Z-Library goes through the EAPI client;
        # every other source resolves a URL through the router and streams it.
        if from_source:
            original_download_path_str = await _fetch_from_source(
                book_details, output_dir
            )
        else:
            original_download_path_str = await eapi.download_file(
                book_id=int(book_id),
                book_hash=book_hash,
                output_dir=output_dir,
            )

        if (
            not original_download_path_str
            or not Path(original_download_path_str).exists()
        ):
            raise FileNotFoundError(
                f"Book download failed or file not found at: {original_download_path_str}"
            )

        # Step 2: Create the unified filename.
        if "extension" not in book_details and original_download_path_str:
            _, ext_from_path = os.path.splitext(original_download_path_str)
            book_details["extension"] = ext_from_path.lstrip(".")

        # Use unified filename generation with disambiguation fields
        unified_filename = create_unified_filename(
            book_details,
            year=book_details.get("year", ""),
            language=book_details.get("language", ""),
        )
        final_file_path = Path(output_dir) / unified_filename
        final_file_path_str = str(final_file_path)

        # Step 3: Rename the downloaded file to the enhanced filename.
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        os.rename(original_download_path_str, final_file_path_str)
        logger.info(
            f"Renamed downloaded file from {original_download_path_str} to {final_file_path_str}"
        )
        downloaded_file_path_str = final_file_path_str

        # Step 4: Optionally process for RAG.
        if process_for_rag and downloaded_file_path_str:
            logger.info(
                f"Processing downloaded file for RAG: {downloaded_file_path_str}"
            )
            process_result = await process_document(
                file_path_str=downloaded_file_path_str,
                output_format=processed_output_format,
                book_id=book_details.get("id"),
                author=book_details.get("author"),
                title=book_details.get("name") or book_details.get("title"),
            )
            processed_file_path_str = process_result.get("processed_file_path")

        result = {
            "file_path": downloaded_file_path_str,
            "processed_file_path": processed_file_path_str,
        }
        if process_for_rag and process_result:
            result.update(process_result)
            result["file_path"] = downloaded_file_path_str
        return result

    except Exception as e:
        identifier = book_details.get("id") or book_details.get("md5") or "unknown"
        logger.exception(f"Error in download_book for {identifier}")
        raise e


async def get_book_metadata_complete(book_id: str, book_hash: str = None) -> dict:
    """
    Fetch complete metadata for a book by ID via EAPI.

    Uses the EAPI get_book_info endpoint instead of HTML scraping.

    Args:
        book_id: Z-Library book ID (e.g., "1252896")
        book_hash: Book hash (required)

    Returns:
        Dictionary with complete metadata including enhanced fields
    """
    if not book_hash:
        raise ValueError("book_hash is required for get_book_metadata_complete")

    eapi = await get_eapi_client()

    try:
        metadata = await enhanced_metadata.get_enhanced_metadata(
            book_id=int(book_id),
            book_hash=book_hash,
            eapi_client=eapi,
        )

        # Add book ID and hash to metadata
        metadata["id"] = book_id
        metadata["book_hash"] = book_hash

        logger.info(
            f"Extracted EAPI metadata for book {book_id}: "
            f"description length: {len(metadata.get('description', '') or '')}"
        )

        return metadata

    except Exception as e:
        logger.exception(f"Error fetching complete metadata for book {book_id}")
        raise RuntimeError(
            f"Failed to fetch complete metadata for book {book_id}: {e}"
        ) from e


# --- Main Execution Block ---
import argparse  # Moved import here

# ===== PHASE 3: TERM, AUTHOR, AND BOOKLIST TOOLS =====


async def search_by_term_bridge(
    term: str,
    year_from: int = None,
    year_to: int = None,
    languages: list = None,
    extensions: list = None,
    limit: int = 25,
) -> dict:
    """
    Search for books by conceptual term via EAPI.
    """
    from lib import term_tools

    eapi = await get_eapi_client()

    # Convert lists to comma-separated strings if needed
    langs_str = ",".join(languages) if languages else None
    exts_str = ",".join(extensions) if extensions else None

    logger.info(
        f"python_bridge.search_by_term: term='{term}', year_from={year_from}, year_to={year_to}"
    )

    result = await term_tools.search_by_term(
        term=term,
        email="",  # Not needed when eapi_client provided
        password="",
        year_from=year_from,
        year_to=year_to,
        languages=langs_str,
        extensions=exts_str,
        limit=limit,
        eapi_client=eapi,
    )

    return result


async def search_by_author_bridge(
    author: str,
    exact: bool = False,
    year_from: int = None,
    year_to: int = None,
    languages: list = None,
    extensions: list = None,
    limit: int = 25,
) -> dict:
    """
    Search for books by author via EAPI.
    """
    from lib import author_tools

    eapi = await get_eapi_client()

    langs_str = ",".join(languages) if languages else None
    exts_str = ",".join(extensions) if extensions else None

    logger.info(f"python_bridge.search_by_author: author='{author}', exact={exact}")

    result = await author_tools.search_by_author(
        author=author,
        email="",
        password="",
        exact=exact,
        year_from=year_from,
        year_to=year_to,
        languages=langs_str,
        extensions=exts_str,
        limit=limit,
        eapi_client=eapi,
    )

    return result


async def fetch_booklist_bridge(
    booklist_id: str, booklist_hash: str, topic: str, page: int = 1
) -> dict:
    """
    Fetch a Z-Library booklist via EAPI (degraded: topic search fallback).
    """
    from lib import booklist_tools

    eapi = await get_eapi_client()

    logger.info(
        f"python_bridge.fetch_booklist: id={booklist_id}, hash={booklist_hash}, topic='{topic}'"
    )

    result = await booklist_tools.fetch_booklist(
        booklist_id=booklist_id,
        booklist_hash=booklist_hash,
        topic=topic,
        email="",
        password="",
        page=page,
        eapi_client=eapi,
    )

    return result


async def get_recent_books(count: int = 10) -> dict:
    """Get recently added books via EAPI."""
    eapi = await get_eapi_client()
    response = await eapi.get_recently()
    books = response.get("books", [])
    return {
        "books": [normalize_eapi_book(b) for b in books[:count]],
    }


# ===== PHASE 12: MULTI-SOURCE SEARCH (Anna's Archive + LibGen) =====


async def get_source_router() -> SourceRouter:
    """Get or create the source router.

    Creates a SourceRouter instance using configuration from environment
    variables. The router is cached at module level for reuse.

    Returns:
        Configured SourceRouter instance
    """
    global _source_router
    if _source_router is None:
        config = get_source_config()
        _source_router = SourceRouter(config)
    return _source_router


async def search_multi_source(
    query: str,
    source: str = "auto",
    count: int = 10,
    **kwargs,
) -> dict:
    """Search for books across multiple sources (Anna's Archive, LibGen).

    This function provides an alternative to the Z-Library EAPI search,
    routing queries to Anna's Archive (primary) with LibGen fallback.

    Args:
        query: Search query string
        source: Source selection ('auto', 'annas', 'libgen')
            - 'auto': Use Anna's if ANNAS_SECRET_KEY is set, else LibGen
            - 'annas': Force Anna's Archive (requires ANNAS_SECRET_KEY)
            - 'libgen': Force LibGen
        count: Maximum number of results to return
        **kwargs: Additional arguments passed to adapters

    Returns:
        dict with:
            - books: List of book dicts with md5, title, author, etc.
            - sources_used: List of source names that provided results

    Environment:
        ANNAS_SECRET_KEY: API key for Anna's Archive fast downloads
        LIBGEN_MIRROR: LibGen mirror suffix (default: 'li')
        BOOK_SOURCE_FALLBACK_ENABLED: Enable fallback (default: 'true')
    """
    router = await get_source_router()
    results = await router.search(query, source=source, **kwargs)

    # Convert UnifiedBookResult to dict format for JSON serialization
    books = [
        {
            "md5": r.md5,
            "title": r.title,
            "author": r.author,
            "year": r.year,
            "extension": r.extension,
            "size": r.size,
            "source": r.source.value,
            "download_url": r.download_url,
            **r.extra,
        }
        for r in results[:count]
    ]

    return {
        "books": books,
        "sources_used": list(set(b["source"] for b in books)),
    }


def _requires_eapi_client(function_name: str, args_dict: dict) -> bool:
    """Whether this invocation needs an authenticated Z-Library EAPI client.

    Local document processing and multi-source search never do. Neither does
    a download whose bookDetails came from search_multi_source (source in
    SOURCE_ALIASES): download_book only acquires the EAPI client on its
    Z-Library branch, and logging in up front took LibGen — the
    credential-free fallback — down with every Z-Library auth outage (#129).
    """
    if function_name in ("process_document", "search_multi_source"):
        return False
    if function_name == "download_book":
        source = str((args_dict.get("book_details") or {}).get("source") or "").lower()
        return source not in SOURCE_ALIASES
    return True


async def main():
    parser = argparse.ArgumentParser(description="Z-Library Python Bridge")
    parser.add_argument("function_name", help="Name of the function to call")
    parser.add_argument("args_json", help="JSON string of arguments for the function")
    cli_args = parser.parse_args()

    function_name = cli_args.function_name
    try:
        logger.info(f"python_bridge.main: Received raw args_json: {cli_args.args_json}")
        args_dict_immediately_after_parse = json.loads(cli_args.args_json)
        logger.info(
            f"python_bridge.main: args_dict_immediately_after_parse: {args_dict_immediately_after_parse}"
        )

        # Use a new variable for subsequent operations
        args_dict = args_dict_immediately_after_parse.copy()
        logger.info(
            f"python_bridge.main: Initial args_dict (now a copy) for processing: {args_dict}"
        )

    except json.JSONDecodeError:
        print(
            json.dumps({"error": "Invalid JSON arguments provided."}), file=sys.stderr
        )
        sys.exit(1)

    try:
        # Initialize the EAPI client only where it is actually needed —
        # see _requires_eapi_client for the routing (#129).
        if _requires_eapi_client(function_name, args_dict):
            await initialize_eapi_client()

        # Standardize 'language' key to 'languages' if present for search functions
        if function_name in ["search", "full_text_search"]:
            if "language" in args_dict and args_dict["language"]:
                args_dict["languages"] = args_dict.pop("language")
            elif "languages" in args_dict and args_dict["languages"]:
                pass
            else:
                args_dict["languages"] = []

            if "content_types" not in args_dict or not args_dict["content_types"]:
                args_dict["content_types"] = []

        if function_name == "search":
            logger.info(
                f"python_bridge.main: About to call search with args_dict: {args_dict}"
            )
            result = await search(**args_dict)
        elif function_name == "full_text_search":
            logger.info(
                f"python_bridge.main: About to call full_text_search with args_dict: {args_dict}"
            )
            result = await full_text_search(**args_dict)
        elif function_name == "get_download_history":
            result = await get_download_history(**args_dict)
        elif function_name == "get_download_limits":
            result = await get_download_limits(**args_dict)
        elif function_name == "download_book":
            result = await download_book(**args_dict)
        elif function_name == "process_document":
            if "file_path" in args_dict:
                args_dict["file_path_str"] = args_dict.pop("file_path")
            result = await process_document(**args_dict)
        elif function_name == "get_book_metadata_complete":
            result = await get_book_metadata_complete(**args_dict)
        elif function_name == "search_by_term_bridge":
            result = await search_by_term_bridge(**args_dict)
        elif function_name == "search_by_author_bridge":
            result = await search_by_author_bridge(**args_dict)
        elif function_name == "search_advanced":
            result = await search_advanced(**args_dict)
        elif function_name == "fetch_booklist_bridge":
            result = await fetch_booklist_bridge(**args_dict)
        elif function_name == "get_recent_books":
            result = await get_recent_books(**args_dict)
        elif function_name == "eapi_health_check":
            result = await eapi_health_check()
        elif function_name == "search_multi_source":
            result = await search_multi_source(**args_dict)
        else:
            raise ValueError(f"Unknown function: {function_name}")

        # Print only confirmation and path to stdout to avoid large content
        mcp_style_response = {"content": [{"type": "text", "text": json.dumps(result)}]}
        print(json.dumps(mcp_style_response))

    except Exception as e:
        # Print error as JSON to stderr
        error_info = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc(),
        }
        print(json.dumps(error_info), file=sys.stderr)
        sys.exit(1)
    finally:
        # Clean up EAPI client
        if _eapi_client:
            await _eapi_client.close()
        # Clean up source router
        if _source_router:
            await _source_router.close()


if __name__ == "__main__":
    asyncio.run(main())
