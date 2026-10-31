#!/usr/bin/env python3
import sys
import os
import json
import hashlib
import re
import tempfile
from contextlib import suppress

# Read the private pipe before importing code that could spawn grandchildren.
os.environ.pop("ZLIBRARY_EAPI_SESSION", None)  # Retired env carrier is never trusted.
_session_input = None
_lazy_session = False
_session_published = None
if __name__ == "__main__":
    _private_mode = os.environ.pop("ZLIBRARY_EAPI_CHANNEL", "")
    if _private_mode and _private_mode not in {"1", "lazy-v2"}:
        raise SystemExit("Invalid private session input") from None
    if _private_mode in {"1", "lazy-v2"}:
        try:
            os.set_inheritable(3, False)
            if _private_mode == "lazy-v2":
                os.set_inheritable(4, False)
                _lazy_session = True
            else:
                _session_input = json.loads(sys.stdin.readline(8193))
                if not isinstance(_session_input, dict):
                    raise ValueError()
        except (ValueError, OSError):
            raise SystemExit("Invalid private session input") from None
import httpx
from typing import Optional
from email.message import Message
from urllib.parse import unquote, urlsplit

# Add project root to sys.path to allow importing 'lib'
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import asyncio
import signal

from pathlib import Path
from filename_utils import (
    create_unified_filename,
    is_degraded_unified_filename,
    is_preservable_server_filename,
    normalize_document_extension,
    sanitize_preserved_filename,
    truncate_filename_stem,
)
import logging
from lib.auth_diagnostics import (
    DiagnosticLogFilter,
    failure_diagnostics,
    failure_type,
    encode_failure,
    certificate_failure,
)


# Import enhanced metadata extraction
from lib import enhanced_metadata

# Import multi-source router
from lib.sources.router import SourceRouter
from lib.sources.config import SourceConfig, get_preflight_timeout, get_source_config
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
    SourceError,
)
from lib.sources.net import (
    bounded_await,
    bounded_resolver,
    build_timeout,
    classify_httpx_error,
)

# Add zlibrary source directory to path for EAPI imports
zlibrary_src_path = os.path.join(os.path.dirname(__file__), "..", "zlibrary", "src")
if zlibrary_src_path not in sys.path:
    sys.path.insert(0, zlibrary_src_path)
from zlibrary.eapi import (
    EAPIClient,
    EAPIAuthenticationError,
    NonJSONResponseError,
    sanitized_auth_token,
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
_source_router_for_download = False


def _install_cooperative_signal_handlers():
    """Turn POSIX termination into task cancellation so async cleanup runs."""
    state = {"signal": None}
    originals = {}
    if os.name == "nt":
        return state, originals

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        return state, originals

    def cancel(signum):
        if state["signal"] is None:
            state["signal"] = signum
            task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        originals[signum] = signal.getsignal(signum)
        loop.add_signal_handler(signum, cancel, signum)
    return state, originals


def _restore_signal_handlers(originals):
    if not originals:
        return
    loop = asyncio.get_running_loop()
    for signum, original in originals.items():
        loop.remove_signal_handler(signum)
        signal.signal(signum, original)


# Debug mode configuration (ISSUE-009)
# Enable with: ZLIBRARY_DEBUG=1 or DEBUG=1
DEBUG_MODE = os.environ.get("ZLIBRARY_DEBUG", os.environ.get("DEBUG", "")).lower() in (
    "1",
    "true",
    "yes",
)


def _configure_debug_logging():
    """Configure logging based on debug mode setting."""
    targets = (
        logger,
        logging.getLogger("zlibrary.sources"),
        logging.getLogger("lib.enhanced_metadata"),
    )
    for target in targets:
        if not any(isinstance(f, DiagnosticLogFilter) for f in target.filters):
            target.addFilter(DiagnosticLogFilter())
    # Wire-level response headers may contain bearer cookies even in debug mode.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
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

    # Logger filters are not inherited by descendants. Filter every installed
    # bridge/root handler so future child loggers cannot bypass projection.
    for target in (*targets, logging.getLogger()):
        for handler in target.handlers:
            if not any(isinstance(f, DiagnosticLogFilter) for f in handler.filters):
                handler.addFilter(DiagnosticLogFilter())


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


async def _validate_eapi_profile(client):
    profile = await client.get_profile()
    if isinstance(profile, dict) and profile.get("success") == 0:
        token = sanitized_auth_token(profile.get("error"))
        if any(
            secret and secret in token
            for secret in (
                os.environ.get("ZLIBRARY_EMAIL"),
                os.environ.get("ZLIBRARY_PASSWORD"),
                client.remix_userkey,
            )
        ):
            token = "<redacted>"
        raise EAPIAuthenticationError(
            f"EAPI authentication rejected: {token}", status_code=200, token=token
        )
    if (
        not isinstance(profile, dict)
        or profile.get("success") != 1
        or not isinstance(profile.get("user"), dict)
        or str(profile["user"].get("id", "")) != client.remix_userid
    ):
        raise RuntimeError("Unexpected EAPI profile response; session retained")


async def initialize_eapi_client() -> EAPIClient:
    """Use the parent generation, or log in only for a parent acquisition/per-call."""
    global _eapi_client, _session_input
    email = os.environ.get("ZLIBRARY_EMAIL")
    password = os.environ.get("ZLIBRARY_PASSWORD")
    if not email or not password:
        error = ValueError(
            "ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD environment variables required"
        )
        error._eapi_local_refusal = True
        raise error
    if _lazy_session and _session_input is None:
        from lib.eapi_session_protocol import request_session, SessionProtocolError

        try:
            budget = int(os.environ.get("ZLIBRARY_EAPI_HANDOFF_TIMEOUT_MS", "240000"))
            if not 0 < budget <= 2147483647:
                raise ValueError()
        except ValueError:
            raise SessionProtocolError("Invalid private session deadline") from None
        _session_input = await request_session(timeout=budget / 1000)
    record = _session_input
    if record is not None:
        if record.get("identity") != email or record.get("action") not in (
            "login",
            "validate",
            "operation",
        ):
            raise ValueError("Invalid private session identity or action")
        if record["action"] != "login":
            if not all(
                isinstance(record.get(k), str) and record[k]
                for k in ("domain", "remix_userid", "remix_userkey")
            ):
                raise ValueError("Invalid private session record")
            domain = (
                os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip() or record["domain"]
            )
            _eapi_client = EAPIClient(
                domain,
                remix_userid=record["remix_userid"],
                remix_userkey=record["remix_userkey"],
            )
        else:
            _eapi_client = await _login_eapi_client(email, password)
        _attach_eapi_session(_eapi_client)
        if record["action"] in ("login", "validate"):
            await _validate_eapi_profile(_eapi_client)
    else:
        _eapi_client = await _login_eapi_client(email, password)
    return _eapi_client


def _publish_eapi_session():
    """Only fd 3 carries bearer material; stdout/stderr remain public."""
    global _session_published
    if (
        _session_input is None
        or _eapi_client is None
        or _session_published is _session_input
    ):
        return
    client = _eapi_client
    payload = {
        "generation": _session_input["generation"],
        "identity": _session_input["identity"],
        "domain": client.domain,
        "remix_userid": client.remix_userid,
        "remix_userkey": client.remix_userkey,
    }
    if client.needs_profile_validation:
        payload["validate_next"] = True
    encoded = (json.dumps(payload) + "\n").encode()
    if len(encoded) > 8192:
        raise ValueError("Invalid private session output")
    _session_published = _session_input
    os.write(3, encoded)


def _attach_eapi_session(client):
    async def recover():
        from zlibrary.eapi import DiamWallError

        # Defence in depth: resolver also honours the pin, never switch it here.
        if os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip():
            return False
        target = await resolve_eapi_domain()
        if target == client.domain:
            return False
        replacement = EAPIClient(
            target, remix_userid=client.remix_userid, remix_userkey=client.remix_userkey
        )
        try:
            try:
                await _validate_eapi_profile(replacement)
            except (
                httpx.RequestError,
                httpx.HTTPStatusError,
                DiamWallError,
                NonJSONResponseError,
            ):
                return False
            await client.close()
            client.domain = replacement.domain
            client._recovered_domain = True
            return True
        finally:
            await replacement.close()

    client.on_domain_failed = recover


def _safe_diagnostic(value):
    """Scrub diagnostic values, never schema keys, fixed codes or tool results."""
    secrets = [
        os.environ.get("ZLIBRARY_EMAIL", ""),
        os.environ.get("ZLIBRARY_PASSWORD", ""),
    ]
    if _session_input:
        secrets.append(_session_input.get("remix_userkey", ""))
    if _eapi_client:
        secrets.append(_eapi_client.remix_userkey or "")

    def visit(item):
        if isinstance(item, str):
            for secret in filter(None, secrets):
                item = item.replace(secret, "<redacted>")
                escaped = json.dumps(secret)[1:-1]
                if escaped != secret:
                    item = item.replace(escaped, "<redacted>")
            return item
        if isinstance(item, dict):
            return {
                key: val
                if key in ("type", "reason", "code", "provider")
                else visit(val)
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [visit(val) for val in item]
        return item

    return visit(value)


async def _login_eapi_client(email: str, password: str) -> EAPIClient:
    """Perform the existing login/domain discovery without logging auth data."""
    # ISSUE-API-002: the old single default (z-library.sk) is fronted by the
    # DiamWall anti-bot wall. resolve_eapi_domain() honours an explicit
    # ZLIBRARY_EAPI_DOMAIN override without probing; otherwise it probes the
    # fallback list (one-row POST /eapi/book/search — never login, which is
    # rate-limited) and logs in on the first healthy candidate.
    domain_pinned = bool(os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip())
    initial_domain = await resolve_eapi_domain()

    client = EAPIClient(initial_domain)
    try:
        login_result = await client.login(email, password)
        if (
            not isinstance(login_result, dict)
            or login_result.get("success") != 1
            or not client.remix_userid
            or not client.remix_userkey
        ):
            raise EAPIAuthenticationError(
                "EAPI login failed",
                status_code=200,
                token=sanitized_auth_token(
                    login_result.get("error")
                    if isinstance(login_result, dict)
                    else None
                ),
            )
    except BaseException:
        await client.close()
        raise

    logger.info("EAPI client authenticated")

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
        except Exception:
            logger.warning("Domain discovery failed, using initial domain")

    return client


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
                "error": "Unexpected EAPI response",
                "error_code": "malformed_response",
                "diagnostics": failure_diagnostics(RuntimeError()),
            }
    except EAPIAuthenticationError as e:
        if _session_input is not None:
            raise
        return {
            "status": "unhealthy",
            "transport": "eapi",
            "error": "EAPI authentication rejected",
            "error_code": "unknown_error",
            "diagnostics": failure_diagnostics(e),
        }
    except Exception as e:
        diagnostics = failure_diagnostics(e)
        return {
            "status": "unhealthy",
            "transport": "eapi",
            "error": "DiamWall anti-bot wall detected; set ZLIBRARY_EAPI_DOMAIN to a working domain"
            if diagnostics["failure_kind"] == "wall"
            else "EAPI health check failed",
            "error_code": _classify_health_error(e),
            "diagnostics": diagnostics,
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
    page=1,
    author=None,
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
    _validate_page(count, page)
    if author:
        from lib.author_tools import format_author_query

        author_query = format_author_query(author, exact=exact and not query)
        query = " ".join(part for part in [query, author_query] if part)
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
        page=page,
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
        except EAPIAuthenticationError:
            raise  # Only the parent can refresh; do not try another search strategy.
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
        except EAPIAuthenticationError:
            raise  # Only the parent can refresh; do not try another search strategy.
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


def _validate_page(count, page):
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")


async def get_download_history(count=10, page=1):
    """Get user's download history via EAPI."""
    _validate_page(count, page)
    eapi = await get_eapi_client()
    response = await eapi.get_downloaded(limit=count, page=page)
    books = response.get("books", [])
    return [
        {
            **normalize_eapi_book(b),
            **({"downloaded_at": b["downloaded_at"]} if b.get("downloaded_at") else {}),
        }
        for b in books
    ]


async def get_download_limits():
    """Get user's download limits via EAPI profile.

    /eapi/user/profile reports `downloads_limit` (the daily cap) and
    `downloads_today` (how many are spent). It has never carried
    `downloads_today_limit` or `downloads_today_left`, the names this function
    used to read, so both values fell through to the "unknown" default and the
    tool could not answer the one question callers ask it — whether there is
    quota left to spend. Verified against a live profile response 2026-08-11.

    `downloads_remaining` is derived, not reported; it is clamped at zero
    because the server counts a download the moment it is issued and can report
    `downloads_today` above the cap.

    Returns:
        dict with daily_limit (reported value, or "unknown" when absent),
        daily_remaining (int only when cap and spend are non-negative,
        non-bool ints; otherwise "unknown"), plus downloads_today and is_premium.
    """
    eapi = await get_eapi_client()
    profile = await eapi.get_profile()
    user = profile.get("user", profile)

    limit = user.get("downloads_limit")
    used = user.get("downloads_today")

    remaining = "unknown"
    if type(limit) is int and type(used) is int and limit >= 0 and used >= 0:
        remaining = max(0, limit - used)

    if limit is None:
        logger.warning(
            "EAPI profile has no 'downloads_limit' field; response keys: %s",
            sorted(user.keys()),
        )

    return {
        "daily_limit": limit if limit is not None else "unknown",
        "daily_remaining": remaining,
        "downloads_today": used if used is not None else "unknown",
        "is_premium": bool(user.get("isPremium", 0)),
    }


# --- Core Bridge Functions ---


# --- download_book function needs to be async ---
SOURCE_ALIASES = {"libgen", "annas", "annas_archive"}
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_DOCUMENT_EXTENSIONS = {"pdf", "epub", "txt"}


class _DownloadedPath(str):
    """String-compatible path carrying response-extension evidence strength."""

    def __new__(cls, value: str, extension_evidence: str = ""):
        instance = super().__new__(cls, value)
        instance.extension_evidence = extension_evidence
        return instance


def _response_extension(
    headers, response_url: str, signature: bytes
) -> tuple[str, str]:
    """Infer a safe document extension from strongest to weakest evidence."""
    signature_extension, signature_evidence = _signature_extension(signature)
    if signature_extension:
        return signature_extension, signature_evidence

    disposition = headers.get("content-disposition", "")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename() or ""
        suffix = Path(filename.replace("\\", "/")).suffix.lower().lstrip(".")
        if suffix in _DOCUMENT_EXTENSIONS:
            return suffix, "content_disposition"

    url_name = Path(unquote(urlsplit(response_url).path)).name
    suffix = Path(url_name).suffix.lower().lstrip(".")
    if suffix in _DOCUMENT_EXTENSIONS:
        return suffix, "response_url"

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    by_content_type = {
        "application/pdf": "pdf",
        "application/epub+zip": "epub",
        "text/plain": "txt",
    }
    if content_type in by_content_type:
        return by_content_type[content_type], "content_type"

    return "", ""


def _signature_probe(signature: bytes) -> bytes:
    """Strip a UTF-8 BOM and leading whitespace for content classification."""
    return signature.removeprefix(b"\xef\xbb\xbf").lstrip()


def _looks_like_html(signature: bytes) -> bool:
    """Recognize common leading HTML shapes independent of response headers."""
    probe = _signature_probe(signature).lower()
    return bool(
        re.match(
            rb"^(?:<!doctype\s+html|<html(?:\s|>)|<head(?:\s|>)|<body(?:\s|>)|<script(?:\s|>)|<!--)",
            probe,
        )
    )


def _signature_extension(signature: bytes) -> tuple[str, str]:
    """Classify supported document bytes without trusting names or MIME.

    Returns the extension and how strong the classification is. A magic-byte
    match is ``signature`` evidence and outranks a declared extension; a merely
    printable prefix is ``printable_text`` — every text-based container (RTF,
    FB2, HTML-ish TXT) looks the same at that level, so it must never override
    the extension the search result declared.
    """
    probe = _signature_probe(signature)
    if probe.startswith(b"%PDF"):
        return "pdf", "signature"
    if probe.startswith(b"PK\x03\x04") and b"application/epub+zip" in signature:
        return "epub", "signature"
    if probe.startswith(b"{\\rtf"):
        return "rtf", "signature"
    if not probe or b"\x00" in probe:
        return "", ""
    if b"<FictionBook" in probe:
        return "fb2", "signature"
    try:
        text = probe.decode("utf-8")
    except UnicodeDecodeError:
        return "", ""
    if all(character.isprintable() or character in "\r\n\t" for character in text):
        return "txt", "printable_text"
    return "", ""


def _md5_digest():
    """Build an MD5 hasher usable on FIPS-enforcing builds.

    These digests only ever compare against a provider's catalog identifier, so
    they are not a security control. Without the flag `hashlib.md5()` raises
    `ValueError` under FIPS policy — an exception no candidate-walk handler
    classifies, which would abort the whole walk instead of trying a mirror.
    """
    try:
        return hashlib.md5(usedforsecurity=False)
    except TypeError:  # pragma: no cover - builds without the keyword
        return hashlib.md5()


def _publish_no_replace(source: Path, destination: Path) -> None:
    """Link and consume an owned file without replacement, including private staging."""
    os.link(source, destination)
    source.unlink()


def _file_md5(path: Path, *, check_deadline: bool = False) -> str:
    """Digest an artifact, optionally bounding a pre-commit reuse comparison."""
    if check_deadline:
        from lib.acquisition import _check_deadline

        _check_deadline("reuse comparison")
    digest = _md5_digest()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            if check_deadline:
                _check_deadline("reuse comparison")
            digest.update(chunk)
    if check_deadline:
        _check_deadline("reuse comparison")
    return digest.hexdigest()


def _artifacts_match(source: Path, destination: Path, expected_md5: str) -> bool:
    """Decide whether an existing destination already holds this acquisition."""
    if source.stat().st_size != destination.stat().st_size:
        return False
    if expected_md5:
        return _file_md5(destination, check_deadline=True) == expected_md5
    return _file_md5(source, check_deadline=True) == _file_md5(
        destination, check_deadline=True
    )


def _publish_or_reuse(source: Path, destination: Path, expected_md5: str = "") -> bool:
    """Publish a staged artifact, reusing an identical existing destination.

    Acquisition is idempotent: re-downloading the same result into the same
    directory resolves to the same deterministic name, and finding our own
    prior artifact there is success, not a collision. A destination that does
    not match is still never replaced. Stage cleanup is attempted on every
    path; cleanup after a successful final link is best effort so a cleanup
    failure cannot replace a committed success.
    """
    from lib.acquisition import _check_deadline, _mark_published

    if source.resolve() == destination.resolve():
        return False
    _check_deadline("publication")
    try:
        os.link(source, destination)
        _mark_published()
        with suppress(OSError):  # The final link, not cleanup, is the commit.
            source.unlink()
        return False
    except FileExistsError:
        pass
    try:
        if _artifacts_match(source, destination, expected_md5):
            _mark_published()
            return True
        raise FileExistsError(
            f"Refusing to replace {destination}: the existing file differs from the "
            f"freshly downloaded artifact staged at {source}"
        )
    finally:
        source.unlink(missing_ok=True)


async def _download_url_to_file(
    url: str,
    output_dir: str,
    md5: str,
    provider: str,
    *,
    enforce_timeout: bool = True,
    host_observer=None,
    progress_observer=None,
    config: Optional[SourceConfig] = None,
) -> str:
    """Stream a resolved source URL to disk and return the raw path.

    The source-agnostic half of acquisition: everything downstream of this
    (unified filename and publication contract) already works on
    any source, so this is the only piece that had to exist for LibGen and
    Anna's results to become downloadable.

    Guards against the two ways these CDNs fail without erroring: an HTML
    error/interstitial page served with HTTP 200, and a truncated body.
    """
    import httpx

    from lib.sources.config import DEFAULT_BROWSER_USER_AGENT
    from lib.sources.libgen import get_user_agent

    expected_md5 = md5.strip().lower()
    if not _MD5_RE.fullmatch(expected_md5):
        raise ValueError("Source downloads require a normalized 32-hex MD5")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    descriptor, attempt_name = tempfile.mkstemp(
        prefix=".source-", suffix=".part", dir=output_path
    )
    os.close(descriptor)
    attempt_path = Path(attempt_name)

    # LIBGEN_USER_AGENT is scoped to LibGen. This function is source-agnostic,
    # so sending an operator's LibGen-specific string to Anna's host would both
    # change an unrelated transfer's behaviour and hand a custom identifying
    # string to a provider that never asked for it (Codex on #146). Other
    # sources get the neutral browser default, which is what they sent before
    # the override existed.
    user_agent = (
        get_user_agent(config)
        if str(provider).lower() == "libgen"
        else DEFAULT_BROWSER_USER_AGENT
    )

    original_host = (urlsplit(url).hostname or "").lower()
    active_host = original_host
    # The caller's config wins when it has one. A router built with an
    # injected or cached config would otherwise resolve its UA and timeouts
    # from one object and move the file with another (Codex on #146).
    config = config or get_source_config()

    written = 0
    bytes_received = 0
    resumes_tried = 0
    body_complete = False

    def progress_detail(detail: str) -> str:
        if bytes_received:
            return (
                f"partial transfer: bytes_received={bytes_received} "
                f"resumes_tried={resumes_tried}; {detail}"
            )
        return detail

    def observe_progress() -> None:
        if progress_observer is not None:
            progress_observer(bytes_received, resumes_tried, body_complete)

    async def stream_to_disk() -> tuple[int, str]:
        nonlocal active_host, written, bytes_received, resumes_tried, body_complete
        try:
            # Keep the admitted source-specific UA and one client throughout
            # recovery. The caller owns the single deadline around this loop.
            async with httpx.AsyncClient(
                timeout=build_timeout(config),
                follow_redirects=True,
                headers={"User-Agent": user_agent},
            ) as client:
                while True:
                    offset = written
                    request_options = (
                        {"headers": {"Range": f"bytes={offset}-"}} if offset else {}
                    )
                    try:
                        async with client.stream(
                            "GET", url, **request_options
                        ) as response:
                            response_url = getattr(response, "url", None)
                            if response_url is not None:
                                active_host = (
                                    urlsplit(str(response_url)).hostname or active_host
                                ).lower()
                            if host_observer is not None:
                                host_observer(active_host)
                            status = getattr(response, "status_code", 200)
                            if offset and status == 416:
                                match = re.fullmatch(
                                    r"bytes \*/(\d+)",
                                    response.headers.get("content-range", ""),
                                )
                                if match is not None and int(match[1]) == offset:
                                    body_complete = True
                                    observe_progress()
                                    # A final-frame reset can leave a complete
                                    # stage. Check it within the same deadline
                                    # before discarding or publishing anything.
                                    digest = _md5_digest()
                                    with open(attempt_path, "rb") as handle:
                                        while chunk := handle.read(65536):
                                            digest.update(chunk)
                                            await asyncio.sleep(0)
                                    if digest.hexdigest() == expected_md5:
                                        # Close the response, then use the normal
                                        # validation and no-replace commit below.
                                        break
                                # Otherwise the next admitted request starts
                                # this URL at zero, within the remaining budget.
                                attempt_path.write_bytes(b"")
                                written = 0
                                body_complete = False
                                if resumes_tried < config.download_resume_attempts:
                                    resumes_tried += 1
                                    observe_progress()
                                    await asyncio.sleep(0)
                                    continue
                            response.raise_for_status()
                            content_type = response.headers.get("content-type", "")
                            if "text/html" in content_type:
                                raise ProviderResponseError(
                                    provider,
                                    active_host,
                                    f"HTML response for {md5}; download key may have expired",
                                    reason="protocol_error",
                                )

                            expected_length = None
                            expected_total = None
                            if status == 206:
                                match = re.fullmatch(
                                    r"bytes (\d+)-(\d+)/(\d+|\*)",
                                    response.headers.get("content-range", ""),
                                )
                                if (
                                    not offset
                                    or match is None
                                    or int(match[1]) != offset
                                    or int(match[2]) < offset
                                    or (
                                        match[3] != "*"
                                        and int(match[3]) <= int(match[2])
                                    )
                                    or "content-encoding" in response.headers
                                ):
                                    raise ProviderResponseError(
                                        provider,
                                        active_host,
                                        "refused append: invalid Content-Range or Content-Encoding",
                                        reason="protocol_error",
                                    )
                                expected_length = int(match[2]) - offset + 1
                                if match[3] != "*":
                                    expected_total = int(match[3])
                            elif status == 200:
                                # A server ignoring Range sent a full file.
                                # Truncation occurs before its first body byte.
                                written = 0
                            else:
                                raise ProviderResponseError(
                                    provider,
                                    active_host,
                                    f"unexpected transfer status {status}",
                                    reason="protocol_error",
                                )

                            encoded = "content-encoding" in response.headers
                            length = response.headers.get("content-length", "")
                            if not encoded and length.isdecimal():
                                declared_length = int(length)
                                if (
                                    expected_length is not None
                                    and declared_length != expected_length
                                ):
                                    raise ProviderResponseError(
                                        provider,
                                        active_host,
                                        "Content-Length disagrees with Content-Range",
                                        reason="protocol_error",
                                    )
                                expected_length = declared_length
                            received = 0
                            try:
                                with open(
                                    attempt_path, "ab" if written else "wb"
                                ) as handle:
                                    async for chunk in response.aiter_bytes(65536):
                                        handle.write(chunk)
                                        written += len(chunk)
                                        received += len(chunk)
                                        bytes_received += len(chunk)
                                        observe_progress()
                                if (
                                    expected_length is not None
                                    and received < expected_length
                                ):
                                    raise httpx.RemoteProtocolError(
                                        f"body ended after {received} of {expected_length} bytes"
                                    )
                                if (
                                    expected_length is not None
                                    and received > expected_length
                                ):
                                    raise ProviderResponseError(
                                        provider,
                                        active_host,
                                        "body exceeded declared range/length",
                                        reason="protocol_error",
                                    )
                            except (
                                httpx.ReadError,
                                httpx.ReadTimeout,
                                httpx.RemoteProtocolError,
                                httpx.DecodingError,
                            ):
                                if encoded:
                                    # aiter_bytes decoded the representation;
                                    # those offsets cannot address encoded bytes.
                                    written = 0
                                    attempt_path.write_bytes(b"")
                                raise

                        # A valid 206 may cover only part of the remainder.
                        # Close it before requesting the next staged offset;
                        # the normal recovery gate charges each extra request.
                        if status == 206 and expected_total is None:
                            # A fully delivered wildcard slice may already
                            # complete the file. Only its expected digest can
                            # establish that without another server response.
                            # Hashing is completed-body validation for both
                            # the local deadline and the caller's progress view.
                            body_complete = True
                            observe_progress()
                            digest = _md5_digest()
                            with open(attempt_path, "rb") as handle:
                                while chunk := handle.read(65536):
                                    digest.update(chunk)
                                    await asyncio.sleep(0)
                            if digest.hexdigest() != expected_md5:
                                body_complete = False
                                observe_progress()
                                raise httpx.RemoteProtocolError(
                                    f"range ended at {written} with unknown total"
                                )
                        if expected_total is not None and written < expected_total:
                            raise httpx.RemoteProtocolError(
                                f"range ended at {written} of {expected_total} bytes"
                            )
                        body_complete = True
                        observe_progress()
                    except (
                        httpx.ReadError,
                        httpx.ReadTimeout,
                        httpx.RemoteProtocolError,
                        httpx.DecodingError,
                    ) as exc:
                        if (
                            not bytes_received
                            and not isinstance(exc, httpx.DecodingError)
                        ) or resumes_tried >= config.download_resume_attempts:
                            raise
                        resumes_tried += 1
                        observe_progress()
                        # This cancellation point uses the caller's existing
                        # timer; there is no new budget per retry or mirror.
                        await asyncio.sleep(0)
                        continue
                    break

                # Re-read the whole owned file, including the prefix.
                # Yield between blocks so the same timer can cancel
                # validation as well as the response stream.
                digest = _md5_digest()
                signature = bytearray()
                with open(attempt_path, "rb") as handle:
                    while chunk := handle.read(65536):
                        digest.update(chunk)
                        if len(signature) < 4096:
                            signature.extend(chunk[: 4096 - len(signature)])
                        await asyncio.sleep(0)
                if _looks_like_html(bytes(signature)):
                    raise ProviderResponseError(
                        provider,
                        active_host,
                        f"HTML body for {md5}; download key may have expired",
                        reason="protocol_error",
                    )
                actual_md5 = digest.hexdigest()
                if actual_md5 != expected_md5:
                    raise ProviderResponseError(
                        provider,
                        active_host,
                        f"expected={expected_md5} actual={actual_md5}",
                        reason="integrity_mismatch",
                    )
                extension, extension_evidence = _response_extension(
                    response.headers,
                    str(response_url or url),
                    bytes(signature),
                )
                completed_path = attempt_path.with_suffix(
                    f".{extension}" if extension else ""
                )
                _publish_no_replace(attempt_path, completed_path)
                return written, _DownloadedPath(str(completed_path), extension_evidence)
        except BaseException:
            attempt_path.unlink(missing_ok=True)
            raise

    try:
        if enforce_timeout:
            written, completed_path = await bounded_await(
                stream_to_disk(),
                config.download_timeout,
                provider=provider,
                host=original_host,
                operation="download",
            )
        else:
            written, completed_path = await stream_to_disk()
    except SourceError as exc:
        if isinstance(exc, ProviderTimeoutError) and exc.reason == "search_timeout":
            partial = bytes_received and not body_complete
            error_type = ProviderResponseError if partial else ProviderTimeoutError
            raise error_type(
                provider,
                active_host,
                progress_detail(exc.detail) if partial else exc.detail,
                reason="partial_transfer" if partial else "read_timeout",
            ) from exc
        if bytes_received:
            raise type(exc)(
                exc.provider, exc.host, progress_detail(exc.detail), reason=exc.reason
            ) from exc
        raise
    except httpx.HTTPError as exc:
        reason, detail = classify_httpx_error(exc)
        if bytes_received and isinstance(
            exc,
            (
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.DecodingError,
            ),
        ):
            reason = "partial_transfer"
        try:
            request = exc.request
        except RuntimeError:
            # Manually raised or transport-created exceptions may expose the
            # property while leaving it unset. The active response/original
            # host remains the correct fallback in that case.
            request = None
        request_url = getattr(request, "url", None)
        failure_host = active_host
        if request_url is not None:
            failure_host = (urlsplit(str(request_url)).hostname or failure_host).lower()
        error_type = (
            ProviderUnreachableError
            if reason
            in {
                "dns_failure",
                "dns_timeout",
                "connect_timeout",
                "connect_refused",
                "connect_error",
                "tls_error",
            }
            else ProviderTimeoutError
            if reason == "read_timeout"
            else ProviderResponseError
        )
        raise error_type(
            provider,
            failure_host,
            detail if reason == "http_error" else progress_detail(detail),
            reason=reason,
        ) from exc

    if written == 0:
        Path(completed_path).unlink(missing_ok=True)
        raise ProviderResponseError(
            provider, active_host, f"empty body for {md5}", reason="protocol_error"
        )

    logger.info(f"Downloaded {written} bytes from source to {completed_path}")
    return completed_path


def _provenance_hostname(value) -> Optional[str]:
    """Keep mirror provenance to a hostname, even for URL-shaped configuration."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        host = urlsplit(
            value if "://" in value or value.startswith("//") else f"//{value}"
        ).hostname
    except ValueError:
        return None
    return host.lower() if host else None


async def _fetch_from_source(book_details: dict, output_dir: str) -> str:
    """Resolve and fetch a non-Z-Library book. Returns the raw file path."""
    md5 = (book_details.get("md5") or "").strip().lower()
    source = (book_details.get("source") or "auto").lower()
    if not md5:
        raise ValueError(
            "Missing 'md5' in bookDetails. Source downloads are addressed by "
            "md5 — pass a result from search_multi_source."
        )

    router = await get_source_router(for_download=True)
    selection = (
        "libgen" if source == "libgen" else "annas" if "annas" in source else "auto"
    )
    if not _MD5_RE.fullmatch(md5):
        raise ValueError("Source downloads require a normalized 32-hex MD5")

    config = getattr(router, "config", None) or get_source_config(for_download=True)
    active_host = ""
    failures = []
    seen_failure_ids = set()
    transfer_progress = (0, 0, False)

    def observe_progress(received: int, resumes: int, complete: bool) -> None:
        nonlocal transfer_progress
        transfer_progress = (received, resumes, complete)

    def record(failure):
        identity = id(failure)
        if identity not in seen_failure_ids:
            seen_failure_ids.add(identity)
            failures.append(failure)

    async def acquire() -> str:
        nonlocal active_host, transfer_progress

        candidate_stream = router.iter_download_candidates(md5, source=selection)
        try:
            async for result in candidate_stream:
                provider = getattr(result.source, "value", result.source) or selection
                if provider == "annas_archive":
                    provider = "annas"
                active_host = (urlsplit(result.url).hostname or "").lower()
                try:
                    quota = getattr(result, "quota_info", None)
                    from datetime import datetime, timezone

                    observed_at = (
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    )
                    transfer_progress = (0, 0, False)
                    downloaded = await _download_url_to_file(
                        result.url,
                        output_dir,
                        md5,
                        str(provider),
                        enforce_timeout=False,
                        host_observer=lambda host: _set_active_host(host),
                        progress_observer=observe_progress,
                        config=config,
                    )
                    if not isinstance(downloaded, _DownloadedPath):
                        downloaded = _DownloadedPath(str(downloaded))
                    downloaded.provenance = {
                        "route": "fast_download"
                        if provider == "annas"
                        else "mirror_download",
                        "host": active_host or None,
                        "mirror": _provenance_hostname(getattr(result, "mirror", "")),
                    }
                    if (
                        quota is not None
                        and type(quota.downloads_left) is int
                        and quota.downloads_left >= 0
                    ):
                        downloaded.quota_estimate = {
                            "source": str(provider),
                            "remaining": quota.downloads_left,
                            "as_of": observed_at,
                            "basis": "observed",
                        }
                    return downloaded
                except AllSourcesFailedError as exc:
                    transfer_progress = (0, 0, False)
                    for failure in exc.failures:
                        record(failure)
                except SourceError as exc:
                    transfer_progress = (0, 0, False)
                    record(exc)
        except AllSourcesFailedError as exc:
            for failure in exc.failures:
                record(failure)
        except SourceError as exc:
            record(exc)
        finally:
            close = getattr(candidate_stream, "aclose", None)
            if close is not None:
                await close()

        raise AllSourcesFailedError("download", failures)

    def _set_active_host(host: str) -> None:
        nonlocal active_host
        active_host = host

    try:
        from lib.acquisition import _remaining_transfer_budget

        return await bounded_await(
            acquire(),
            _remaining_transfer_budget(config.download_timeout),
            provider=selection,
            host=active_host,
            operation="download",
        )
    except ProviderTimeoutError as exc:
        if exc.reason == "search_timeout":
            received, resumes, complete = transfer_progress
            partial = received and not complete
            detail = exc.detail
            if partial:
                detail = (
                    f"partial transfer: bytes_received={received} "
                    f"resumes_tried={resumes}; {detail}"
                )
            error_type = ProviderResponseError if partial else ProviderTimeoutError
            terminal_timeout = error_type(
                selection,
                active_host or exc.host,
                detail,
                reason="partial_transfer" if partial else "read_timeout",
            )
            if failures:
                record(terminal_timeout)
                raise AllSourcesFailedError("download", failures) from exc
            raise terminal_timeout from exc
        raise


_RETIRED_PROCESS_FLAG = "process_for_" + "r" + "ag"


def _admit_download_args(arguments: dict) -> None:
    """Reject removed options before authentication, quota use or transfer."""
    if (
        _RETIRED_PROCESS_FLAG in arguments
        and arguments[_RETIRED_PROCESS_FLAG] is not False
    ) or "processed_output_format" in arguments:
        raise ValueError(
            "Text processing was removed in 2.0. Use a separate 1.4.x installation for text processing."
        )
    unknown = arguments.keys() - {"book_details", "output_dir", _RETIRED_PROCESS_FLAG}
    if unknown:
        raise ValueError(f"Unknown download argument(s): {', '.join(sorted(unknown))}")


async def download_book(
    book_details: dict,
    output_dir: str = None,
    *,
    filename=None,
    publication_hook=None,
    **legacy_arguments,
):
    """Download the original book and return its local file_path."""
    _admit_download_args(legacy_arguments)
    from lib.acquisition import output_directory, _prepare_directory

    directory = output_directory(output_dir)
    _prepare_directory(directory)
    output_dir = str(directory)

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
    eapi_staging = None
    original_download_path_str = None
    eapi_provenance = {"route": "eapi", "host": None, "mirror": None}

    def observe_eapi_host(host: str) -> None:
        eapi_provenance["host"] = host or None

    try:
        # Step 1: Fetch the file. Z-Library goes through the EAPI client;
        # every other source resolves a URL through the router and streams it.
        if from_source:
            original_download_path_str = await _fetch_from_source(
                book_details, output_dir
            )
        else:
            # Raw server names can equal a previously published identity name.
            # Keep EAPI's replacing write in a private per-call namespace until
            # the final no-replace publication checks have run.
            eapi_staging = tempfile.TemporaryDirectory(prefix=".eapi-", dir=output_dir)
            receipt_options = {}
            if publication_hook is not None:
                receipt_options["host_observer"] = observe_eapi_host
            original_download_path_str = await eapi.download_file(
                book_id=int(book_id),
                book_hash=book_hash,
                output_dir=eapi_staging.name,
                **receipt_options,
            )
            if publication_hook is not None:
                # Link resolution can recover onto a different EAPI domain.
                eapi_provenance["mirror"] = _provenance_hostname(
                    getattr(eapi, "domain", None)
                )

        if (
            not original_download_path_str
            or not Path(original_download_path_str).exists()
        ):
            raise FileNotFoundError(
                f"Book download failed or file not found at: {original_download_path_str}"
            )

        # Step 2: Create the unified filename.
        declared_extension = normalize_document_extension(book_details.get("extension"))
        strong_response_extension = getattr(
            original_download_path_str, "extension_evidence", ""
        ) in {"signature", "content_disposition", "response_url"}
        if original_download_path_str and (
            not declared_extension or strong_response_extension
        ):
            _, ext_from_path = os.path.splitext(
                str(original_download_path_str).rstrip(" .")
            )
            book_details["extension"] = ext_from_path.lstrip(".")

        # Use unified filename generation with disambiguation fields
        unified_filename = create_unified_filename(
            book_details,
            year=book_details.get("year", ""),
            language=book_details.get("language", ""),
        )
        final_filename = unified_filename
        # Preserve the server's wording for sparse metadata while keeping the
        # normalized extension and deterministic identity suffix used by acquisition.
        if is_degraded_unified_filename(unified_filename):
            server_name = Path(original_download_path_str).name
            if is_preservable_server_filename(server_name):
                preserved = sanitize_preserved_filename(server_name)
                if preserved:
                    logger.info(
                        "Sparse book metadata degraded the unified filename; "
                        "keeping server-provided name %r",
                        preserved,
                    )
                    stem = Path(preserved).stem
                    identity = Path(unified_filename).stem.rsplit("_", 1)[-1]
                    identity_suffix = f"_{identity}"
                    ending = identity_suffix + Path(unified_filename).suffix
                    stem = truncate_filename_stem(stem, ending)
                    final_filename = stem + ending
        if filename is not None:
            from lib.acquisition import destination_name

            final_filename = destination_name(book_details, filename)
        final_file_path = Path(output_dir) / final_filename
        final_file_path_str = str(final_file_path)

        # Step 3: Rename the downloaded file to the enhanced filename, or adopt
        # an identical artifact a previous acquisition of this book already
        # published under the same deterministic name.
        catalog_md5 = str(book_details.get("md5") or "").strip().lower()
        if publication_hook is not None:
            publication_hook(Path(original_download_path_str), final_file_path)
        reused = _publish_or_reuse(
            Path(original_download_path_str),
            final_file_path,
            catalog_md5 if _MD5_RE.fullmatch(catalog_md5) else "",
        )
        if reused:
            logger.info(
                f"Reused existing matching artifact at {final_file_path_str}; "
                f"discarded redundant download {original_download_path_str}"
            )
        else:
            final_file_path.chmod(0o600)
            logger.info(
                f"Published downloaded file from {original_download_path_str} to {final_file_path_str}"
            )
        downloaded_file_path_str = final_file_path_str

        result = {"file_path": downloaded_file_path_str}
        if publication_hook is not None:
            result["publication_reused"] = reused
            result["provenance"] = getattr(
                original_download_path_str,
                "provenance",
                eapi_provenance,
            )
            quota_estimate = getattr(original_download_path_str, "quota_estimate", None)
            if quota_estimate is not None:
                result["quota_estimate"] = quota_estimate
        return result

    except Exception as e:
        identifier = book_details.get("id") or book_details.get("md5") or "unknown"
        logger.exception(f"Error in download_book for {identifier}")
        raise e
    finally:
        if original_download_path_str:
            owned_stage = Path(original_download_path_str)
            if (
                owned_stage.parent.resolve() == Path(output_dir).resolve()
                and str(owned_stage) != final_file_path_str
                and owned_stage.name.startswith(".source-")
            ):
                with suppress(OSError):
                    owned_stage.unlink(missing_ok=True)
        if eapi_staging is not None:
            with suppress(OSError):
                eapi_staging.cleanup()


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

    except EAPIAuthenticationError:
        raise  # Preserve the typed verdict across the metadata wrapper.
    except Exception as e:
        logger.exception(f"Error fetching complete metadata for book {book_id}")
        raise RuntimeError(
            f"Failed to fetch complete metadata for book {book_id}: {e}"
        ) from e


# --- Main Execution Block ---
import argparse  # Moved import here

# ===== PHASE 3: TERM AND AUTHOR TOOLS =====


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


async def get_recent_books(count: int = 10) -> dict:
    """Get recently added books via EAPI."""
    eapi = await get_eapi_client()
    response = await eapi.get_recently()
    books = response.get("books", [])
    return {
        "books": [normalize_eapi_book(b) for b in books[:count]],
    }


# ===== PHASE 12: MULTI-SOURCE SEARCH (Anna's Archive + LibGen) =====


async def get_source_router(*, for_download: bool = False) -> SourceRouter:
    """Get or create the source router.

    Creates a SourceRouter instance using configuration from environment
    variables. The router is cached for its ordinary or long owning operation;
    changing owner closes the old router before admitting a new provider walk.

    Returns:
        Configured SourceRouter instance
    """
    global _source_router, _source_router_for_download
    if _source_router is not None and _source_router_for_download != for_download:
        await _source_router.close()
        _source_router = None
    if _source_router is None:
        config = get_source_config(for_download=for_download)
        _source_router = SourceRouter(config)
        _source_router_for_download = for_download
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
    _validate_page(count, 1)
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

    Multi-source search never does. Neither does
    a download whose bookDetails came from search_multi_source (source in
    SOURCE_ALIASES): download_book only acquires the EAPI client on its
    Z-Library branch, and logging in up front took LibGen — the
    credential-free fallback — down with every Z-Library auth outage (#129).
    """
    if function_name in {"search_multi_source", "acquire_book"}:
        return False
    if function_name == "download_book":
        source = str((args_dict.get("book_details") or {}).get("source") or "").lower()
        return source not in SOURCE_ALIASES
    return True


async def main():
    global _session_published
    _session_published = None
    parser = argparse.ArgumentParser(description="Z-Library Python Bridge")
    parser.add_argument("function_name", help="Name of the function to call")
    parser.add_argument("args_json", help="JSON string of arguments for the function")
    cli_args = parser.parse_args()

    function_name = cli_args.function_name
    termination, original_signal_handlers = _install_cooperative_signal_handlers()
    try:
        args_dict = json.loads(cli_args.args_json)

    except json.JSONDecodeError:
        print(
            json.dumps({"error": "Invalid JSON arguments provided."}), file=sys.stderr
        )
        sys.exit(1)

    needs_eapi = False
    try:
        if function_name == "download_book":
            _admit_download_args(args_dict)
            # Legacy CLI calls authenticate before dispatch. Admit their output
            # first, just as acquire_book does before its deferred login.
            from lib.acquisition import output_directory, _prepare_directory

            directory = output_directory(args_dict.get("output_dir"))
            _prepare_directory(directory)
            args_dict["output_dir"] = str(directory)
        elif function_name == "process_document":
            raise ValueError("Text processing was removed in 2.0.")
        # Decide before dispatch whether this call needs Z-Library
        # authentication at all — see _requires_eapi_client (#106, #129).
        if function_name in {"search", "get_download_history", "search_multi_source"}:
            _validate_page(args_dict.get("count", 10), args_dict.get("page", 1))
        needs_eapi = _requires_eapi_client(function_name, args_dict)

        # Install before authentication and dispatch: httpx/AnyIO resolves
        # again even after source preflight, and its default executor is joined
        # during asyncio.run() shutdown.
        resolver_timeout = get_preflight_timeout()
        async with bounded_resolver(resolver_timeout):
            if needs_eapi or (
                _session_input is not None
                and _session_input.get("action") in ("login", "validate")
            ):
                await initialize_eapi_client()

            # Standardize 'language' key to 'languages' for search functions.
            if function_name in ["search", "full_text_search"]:
                if "language" in args_dict and args_dict["language"]:
                    args_dict["languages"] = args_dict.pop("language")
                elif not args_dict.get("languages"):
                    args_dict["languages"] = []
                if not args_dict.get("content_types"):
                    args_dict["content_types"] = []
            if _session_input and _session_input["action"] in ("login", "validate"):
                result = {"authenticated": True}
            else:
                result = await _dispatch_bridge_function(function_name, args_dict)

        # Print only confirmation and path to stdout to avoid large content
        mcp_style_response = {"content": [{"type": "text", "text": json.dumps(result)}]}
        if _eapi_client:
            _publish_eapi_session()
        print(json.dumps(mcp_style_response))

    except asyncio.CancelledError:
        signum = termination["signal"] or signal.SIGTERM
        logger.info(f"python_bridge.main: received signal {signum}; cancelling")
        raise SystemExit(128 + int(signum))
    except Exception as e:
        # Recovery was profile-validated even when the subsequent operation failed.
        if _eapi_client and (
            getattr(_eapi_client, "_recovered_domain", False)
            or _eapi_client.needs_profile_validation
        ):
            _publish_eapi_session()
        # Print error as JSON to stderr
        error_info = {"error": str(e), "type": type(e).__name__}
        if isinstance(e, httpx.HTTPStatusError):
            error_info["status_code"] = e.response.status_code
        # Provider failures carry which source failed and why (DNS vs connect
        # timeout vs HTTP error). Pass that through as structured data so the
        # MCP caller can act on it instead of pattern-matching prose.
        from lib.acquisition import AcquisitionError

        if isinstance(e, (SourceError, AllSourcesFailedError, AcquisitionError)):
            error_info["details"] = e.to_dict()
            operation = (
                "download"
                if function_name == "download_book"
                else "search"
                if function_name == "search_multi_source"
                else function_name
            )
            error_info["details"].setdefault("operation", operation)
        error_info = _safe_diagnostic(error_info)
        diagnostics = None
        if not (
            getattr(e, "_eapi_local_refusal", False) is True
            or (
                isinstance(e, AcquisitionError)
                and e.issued is False
                and e.reason
                in (
                    "invalid_argument",
                    "output_refused",
                    "configuration_error",
                    "breaker_open",
                    "walk_budget_exhausted",
                )
            )
            or (
                function_name == "acquire_book"
                and isinstance(e, AcquisitionError)
                and e.reason == "walk_budget_exhausted"
            )
        ) and (
            needs_eapi
            or _session_input is not None
            or _eapi_client is not None
            or getattr(e, "_eapi_initialization_failure", False) is True
        ):
            diagnostics = failure_diagnostics(
                e,
                refreshed=bool(
                    _session_input and _session_input.get("authRetry") is True
                ),
            )
            error_info["error"] = f"EAPI request failed ({diagnostics['failure_kind']})"
            error_info["diagnostics"] = diagnostics
            error_info["type"] = failure_type(e)
            # Acquisition wrappers carry useful issuance/reason evidence, not prose.
            if "details" in error_info:
                detail = error_info["details"]
                error_info["details"] = {
                    k: detail[k]
                    for k in ("reason", "request_issued", "destination_occupied")
                    if k in detail
                    and (
                        type(detail[k]) is bool
                        or detail[k]
                        in (
                            "auth_rejected",
                            "outcome_unknown",
                            "http_error",
                            "read_timeout",
                            "partial_transfer",
                            "walk_budget_exhausted",
                            "connect_timeout",
                            "configuration_error",
                            "output_refused",
                            "invalid_argument",
                            "breaker_open",
                            "integrity_mismatch",
                            "internal",
                        )
                    )
                }
            logger.warning("", extra={"auth_diagnostics": diagnostics})
        if (
            diagnostics is not None
            and _session_input is not None
            and _session_input["action"] == "login"
        ):
            failure = {"status_code": diagnostics.get("http_status")}
            status = failure["status_code"]
            cause = (
                "Z-Library rejected the login"
                if diagnostics["failure_kind"] == "auth_rejected"
                else "Z-Library login failed"
            )
            if status:
                cause += f" (HTTP {status})"
            # Do not treat a short, regex-clean provider string as an allowlist.
            failure["token"] = (
                "TLS certificate verification failed"
                if certificate_failure(e)
                else "Authentication rejected"
                if diagnostics["failure_kind"] == "auth_rejected"
                else "Upstream request failed"
            )
            error_info = {
                "error": cause,
                "type": failure_type(e),
                "login_failed": failure,
                "diagnostics": diagnostics,
            }
            print(encode_failure(error_info, mcp=True))
        elif isinstance(e, EAPIAuthenticationError) and _session_input is not None:
            # The parent alone refreshes, with generation CAS and a login cooldown.
            error_info["auth_rejected"] = {"generation": _session_input["generation"]}
            print(encode_failure(error_info, mcp=True))
        else:
            print(
                encode_failure(error_info) if diagnostics else json.dumps(error_info),
                file=sys.stderr,
            )
            sys.exit(1)
    finally:
        try:
            for resource in (_eapi_client, _source_router):
                if resource is None:
                    continue
                try:
                    await resource.close()
                except Exception as cleanup_error:
                    if not (
                        needs_eapi
                        or _session_input is not None
                        or _eapi_client is not None
                    ):
                        raise
                    # Cleanup must not replace the already emitted result/failure
                    # with a traceback containing an upstream exception's payload.
                    logger.warning(
                        "",
                        extra={"auth_diagnostics": failure_diagnostics(cleanup_error)},
                    )
        finally:
            _restore_signal_handlers(original_signal_handlers)


async def _dispatch_bridge_function(function_name: str, args_dict: dict):
    """Dispatch one parsed bridge operation after lifecycle setup."""
    if function_name == "acquire_book":
        from lib.acquisition import acquire, AcquisitionError

        async def authenticated_transfer(book, directory, **kwargs):
            source = str(book.get("source") or "zlibrary").lower()
            if source not in SOURCE_ALIASES:
                if not os.environ.get("ZLIBRARY_EMAIL") or not os.environ.get(
                    "ZLIBRARY_PASSWORD"
                ):
                    raise AcquisitionError(
                        "configuration_error",
                        "Configure ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD",
                    )
                try:
                    await initialize_eapi_client()
                except Exception as error:
                    # Deferred per-call login/discovery can fail before either
                    # global exists. Mark the failure, not local receipt refusals.
                    error._eapi_initialization_failure = True
                    raise
            elif source in {"annas", "annas_archive"} and not os.environ.get(
                "ANNAS_SECRET_KEY"
            ):
                raise AcquisitionError(
                    "configuration_error", "Configure ANNAS_SECRET_KEY"
                )
            return await download_book(book, directory, **kwargs)

        return await acquire(**args_dict, transfer=authenticated_transfer)
    if function_name == "search":
        return await search(**args_dict)
    elif function_name == "full_text_search":
        return await full_text_search(**args_dict)
    elif function_name == "get_download_history":
        return await get_download_history(**args_dict)
    elif function_name == "get_download_limits":
        return await get_download_limits(**args_dict)
    elif function_name == "download_book":
        return await download_book(**args_dict)
    elif function_name == "get_book_metadata_complete":
        return await get_book_metadata_complete(**args_dict)
    elif function_name == "search_by_term_bridge":
        return await search_by_term_bridge(**args_dict)
    elif function_name == "search_by_author_bridge":
        return await search_by_author_bridge(**args_dict)
    elif function_name == "search_advanced":
        return await search_advanced(**args_dict)
    elif function_name == "get_recent_books":
        return await get_recent_books(**args_dict)
    elif function_name == "eapi_health_check":
        return await eapi_health_check()
    elif function_name == "search_multi_source":
        return await search_multi_source(**args_dict)
    raise ValueError(f"Unknown function: {function_name}")


if __name__ == "__main__":
    asyncio.run(main())
