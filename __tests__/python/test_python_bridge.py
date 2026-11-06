# Tests for lib/python_bridge.py (EAPI-based)

import json
import dataclasses
import hashlib
import pytest
import os
import signal
import subprocess
import sys
import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from pathlib import Path

# Add lib directory to sys.path explicitly
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
)

import python_bridge  # Import the module itself
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
)
from lib.sources.models import DownloadResult, SourceType

# Import functions from the module under test
from python_bridge import (
    main,
    download_book,
    normalize_book_details,
    eapi_health_check,
    search,
    full_text_search,
    get_download_history,
    get_download_limits,
    get_book_metadata_complete,
    search_by_term_bridge,
    search_by_author_bridge,
)

pytestmark = pytest.mark.unit


# --- EAPI Mock Fixtures ---

MOCK_EAPI_SEARCH_RESPONSE = {
    "success": 1,
    "books": [
        {
            "id": 12345,
            "title": "The Great Test",
            "author": "Py Test",
            "year": "2025",
            "language": "english",
            "extension": "epub",
            "filesize": "1048576",
            "hash": "abc123hash",
            "rating": "4.5",
            "qualityScore": "4.0",
            "cover": "",
            "href": "",
            "isbn": "",
            "publisher": "",
            "pages": "200",
        }
    ],
}

MOCK_EAPI_BOOK_INFO = {
    "book": {
        "id": 12345,
        "title": "The Great Test",
        "author": "Py Test",
        "description": "A test book description.",
        "isbn": "978-0-123-45678-9",
        "rating": "4.5",
        "ratingCount": 100,
        "series": "Test Series",
        "categories": ["Testing", "Software"],
        "qualityScore": "4.0",
        "hash": "abc123hash",
    }
}


@pytest.fixture
def mock_eapi_client():
    """Create a mock EAPIClient for testing."""
    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=MOCK_EAPI_SEARCH_RESPONSE)
    mock_client.get_book_info = AsyncMock(return_value=MOCK_EAPI_BOOK_INFO)
    mock_client.get_downloaded = AsyncMock(
        return_value={"books": MOCK_EAPI_SEARCH_RESPONSE["books"]}
    )
    # Field names copied from a live /eapi/user/profile response (2026-08-11).
    # The previous fixture invented `downloads_today_limit` /
    # `downloads_today_left` to match the code, so the test passed while the
    # real tool returned {"daily_limit": "unknown", "daily_remaining":
    # "unknown"} against the actual API. A fixture must mirror the service, not
    # the caller's assumption about it.
    mock_client.get_profile = AsyncMock(
        return_value={
            "success": 1,
            "user": {
                "id": 1,
                "downloads_today": 3,
                "downloads_limit": 10,
                "isPremium": 0,
            },
        }
    )
    mock_client.get_recently = AsyncMock(return_value=MOCK_EAPI_SEARCH_RESPONSE)
    mock_client.close = AsyncMock()
    return mock_client


@pytest.fixture
def patch_eapi_client(mock_eapi_client, mocker):
    """Patch the module-level _eapi_client."""
    mocker.patch.object(python_bridge, "_eapi_client", mock_eapi_client)
    return mock_eapi_client


@pytest.fixture
def mock_eapi_download(mocker, mock_eapi_client):
    """Mocks EAPIClient.download_file for download operations."""
    mock_eapi_client.download_file = AsyncMock(
        return_value="/mock/downloaded/book.epub"
    )
    return mock_eapi_client


# --- Tests for normalize_book_details (EAPI format) ---


class TestNormalizeBookDetails:
    def test_eapi_hash_field(self):
        """EAPI books have 'hash' directly available."""
        book = {"id": "123", "hash": "abc123", "url": "http://example.com/book/123"}
        normalized = normalize_book_details(book)
        assert normalized["book_hash"] == "abc123"

    def test_eapi_book_hash_already_present(self):
        """If book_hash already exists, don't overwrite."""
        book = {"id": "123", "hash": "abc", "book_hash": "existing"}
        normalized = normalize_book_details(book)
        assert normalized["book_hash"] == "existing"

    def test_legacy_href_extraction(self):
        """Legacy format: extract hash from href path."""
        book = {"id": "123", "href": "/book/123/abc456/title"}
        normalized = normalize_book_details(book)
        assert normalized["book_hash"] == "abc456"

    def test_url_construction_from_href(self):
        """Construct URL from href if url missing."""
        book = {"id": "123", "href": "/book/123/abc/title"}
        normalized = normalize_book_details(book)
        assert "url" in normalized
        assert normalized["url"].endswith("/book/123/abc/title")


# --- Tests for EAPI Health Check ---


class TestEAPIHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, patch_eapi_client):
        """Should return healthy when EAPI responds correctly."""
        result = await eapi_health_check()
        assert result["status"] == "healthy"
        assert result["transport"] == "eapi"

    @pytest.mark.asyncio
    async def test_unhealthy_bad_response(self, patch_eapi_client):
        """Should return unhealthy when EAPI response is malformed."""
        patch_eapi_client.search = AsyncMock(return_value={"success": 0})
        result = await eapi_health_check()
        assert result["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_unhealthy_exception(self, mocker):
        """Should return unhealthy when EAPI client not initialized."""
        mocker.patch.object(python_bridge, "_eapi_client", None)
        result = await eapi_health_check()
        assert result["status"] == "unhealthy"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_health_check_detects_cloudflare(self, patch_eapi_client):
        """Should detect Cloudflare challenge and return cloudflare_blocked error code."""
        patch_eapi_client.search = AsyncMock(
            side_effect=Exception("Checking your browser before accessing")
        )
        result = await eapi_health_check()
        assert result["status"] == "unhealthy"
        assert result["error_code"] == "cloudflare_blocked"

    @pytest.mark.asyncio
    async def test_health_check_detects_network_error(self, patch_eapi_client):
        """Should detect network errors and return network_error code."""
        patch_eapi_client.search = AsyncMock(
            side_effect=ConnectionError("Connection refused")
        )
        result = await eapi_health_check()
        assert result["status"] == "unhealthy"
        assert result["error_code"] == "network_error"

    @pytest.mark.asyncio
    async def test_health_check_detects_malformed_response(self, patch_eapi_client):
        """Should return malformed_response for non-standard EAPI responses."""
        patch_eapi_client.search = AsyncMock(return_value={"success": 0})
        result = await eapi_health_check()
        assert result["status"] == "unhealthy"
        assert result["error_code"] == "malformed_response"


# --- Tests for Search (EAPI-based) ---


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_basic(self, patch_eapi_client):
        """Should search via EAPI and normalize results."""
        result = await search(query="python")
        assert "books" in result
        assert "retrieved_from_url" in result
        assert len(result["books"]) == 1
        assert result["books"][0]["id"] == "12345"
        assert result["books"][0]["book_hash"] == "abc123hash"

    @pytest.mark.asyncio
    async def test_search_with_filters(self, patch_eapi_client):
        """Should pass filters to EAPI search."""
        await search(
            query="test",
            exact=True,
            from_year=2020,
            to_year=2025,
            languages=["English"],
            extensions=["pdf"],
            count=5,
        )
        call_kwargs = patch_eapi_client.search.call_args[1]
        assert call_kwargs["exact"] is True
        assert call_kwargs["year_from"] == 2020
        assert call_kwargs["year_to"] == 2025
        assert call_kwargs["limit"] == 5

    @pytest.mark.asyncio
    async def test_full_text_search_routes_to_search(self, patch_eapi_client):
        """Full text search routes through regular EAPI search."""
        result = await full_text_search(query="test phrase")
        assert "books" in result
        patch_eapi_client.search.assert_called_once()


class TestSearchAdvanced:
    """search_advanced separates strict (e=1) matches from fuzzy matches.

    Regression tests for GH-16: the Python implementation was deleted in the
    EAPI migration (bd63d33) while the Node tool stayed registered, so every
    live call raised "Unknown function: search_advanced".
    """

    EXACT_RESPONSE = {
        "success": 1,
        "books": [
            {"id": 1, "title": "Exact One", "author": "A", "hash": "h1"},
            {"id": 2, "title": "Exact Two", "author": "B", "hash": "h2"},
        ],
    }
    FUZZY_RESPONSE = {
        "success": 1,
        "books": [
            {"id": 1, "title": "Exact One", "author": "A", "hash": "h1"},
            {"id": 3, "title": "Fuzzy Three", "author": "C", "hash": "h3"},
            {"id": 4, "title": "Fuzzy Four", "author": "D", "hash": "h4"},
        ],
    }

    @pytest.mark.asyncio
    async def test_separates_exact_and_fuzzy(self, patch_eapi_client):
        """Two EAPI calls; fuzzy list excludes ids already in the exact set."""
        patch_eapi_client.search = AsyncMock(
            side_effect=[self.EXACT_RESPONSE, self.FUZZY_RESPONSE]
        )
        result = await python_bridge.search_advanced(query="test")
        assert [b["id"] for b in result["exact_matches"]] == ["1", "2"]
        assert [b["id"] for b in result["fuzzy_matches"]] == ["3", "4"]
        assert result["has_fuzzy_matches"] is True
        assert result["total_results"] == 4
        assert result["query"] == "test"
        first, second = patch_eapi_client.search.call_args_list
        assert first[1]["exact"] is True
        assert second[1]["exact"] is False

    @pytest.mark.asyncio
    async def test_exact_only_skips_fuzzy_search(self, patch_eapi_client):
        """exact=True issues a single strict search and no fuzzy list."""
        patch_eapi_client.search = AsyncMock(return_value=self.EXACT_RESPONSE)
        result = await python_bridge.search_advanced(query="test", exact=True)
        assert len(result["exact_matches"]) == 2
        assert result["fuzzy_matches"] == []
        assert result["has_fuzzy_matches"] is False
        patch_eapi_client.search.assert_called_once()
        assert patch_eapi_client.search.call_args[1]["exact"] is True

    @pytest.mark.asyncio
    async def test_passes_filters_to_both_searches(self, patch_eapi_client):
        """Year filters and count reach the EAPI client on both calls."""
        patch_eapi_client.search = AsyncMock(
            side_effect=[self.EXACT_RESPONSE, self.FUZZY_RESPONSE]
        )
        await python_bridge.search_advanced(
            query="test", from_year=2020, to_year=2025, count=5
        )
        for call in patch_eapi_client.search.call_args_list:
            assert call[1]["year_from"] == 2020
            assert call[1]["year_to"] == 2025
            assert call[1]["limit"] == 5


class TestNodePythonDispatchContract:
    """Every function name Node sends must have a dispatch branch (GH-16)."""

    def test_all_node_function_names_are_dispatched(self):
        import re

        repo_root = Path(__file__).resolve().parents[2]
        node_src = (repo_root / "src" / "lib" / "zlibrary-api.ts").read_text(
            encoding="utf-8"
        )
        bridge_src = (repo_root / "lib" / "python_bridge.py").read_text(
            encoding="utf-8"
        )

        node_names = set(re.findall(r"callPythonFunction\(\s*'([^']+)'", node_src))
        assert node_names, "no callPythonFunction() names found — regex drifted?"
        dispatched = set(
            re.findall(r"function_name\s*==\s*[\"']([^\"']+)[\"']", bridge_src)
        )
        missing = node_names - dispatched
        assert not missing, (
            f"Node calls Python functions with no dispatch branch in "
            f"python_bridge.main: {sorted(missing)}"
        )


# --- Tests for Download History / Limits (EAPI-based) ---


class TestProfileEndpoints:
    @pytest.mark.asyncio
    async def test_get_download_history(self, patch_eapi_client):
        result = await get_download_history(count=5)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "12345"

    @pytest.mark.asyncio
    async def test_get_download_limits(self, patch_eapi_client):
        """Real numbers, from the field names the EAPI actually sends."""
        result = await get_download_limits()
        assert result["daily_limit"] == 10
        assert result["daily_remaining"] == 7
        assert result["downloads_today"] == 3
        assert result["is_premium"] is False

    @pytest.mark.asyncio
    async def test_get_download_limits_clamps_at_zero(self, patch_eapi_client):
        """The server counts a download when issued and can exceed the cap."""
        patch_eapi_client.get_profile = AsyncMock(
            return_value={"user": {"downloads_today": 12, "downloads_limit": 10}}
        )
        result = await get_download_limits()
        assert result["daily_remaining"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spend", [None, "unknown", "3", False, -1])
    async def test_get_download_limits_never_invents_remaining_from_unknown_spend(
        self, patch_eapi_client, spend
    ):
        patch_eapi_client.get_profile = AsyncMock(
            return_value={"user": {"downloads_limit": 10, "downloads_today": spend}}
        )
        result = await get_download_limits()
        assert result["daily_limit"] == 10
        assert result["daily_remaining"] == "unknown"

    @pytest.mark.asyncio
    async def test_get_download_limits_cap_only_is_not_remaining(
        self, patch_eapi_client
    ):
        patch_eapi_client.get_profile = AsyncMock(
            return_value={"user": {"downloads_limit": 10}}
        )
        result = await get_download_limits()
        assert result["daily_remaining"] == "unknown"

    @pytest.mark.asyncio
    async def test_get_download_limits_reports_unknown_if_the_shape_changes(
        self, patch_eapi_client
    ):
        """A renamed field must degrade to 'unknown', not to a wrong number."""
        patch_eapi_client.get_profile = AsyncMock(
            return_value={"user": {"something_else": 5}}
        )
        result = await get_download_limits()
        assert result["daily_limit"] == "unknown"
        assert result["daily_remaining"] == "unknown"


# --- Tests for get_book_metadata_complete (EAPI-based) ---


class TestGetBookMetadataComplete:
    @pytest.mark.asyncio
    async def test_metadata_via_eapi(self, patch_eapi_client):
        """Should fetch metadata via EAPI get_book_info."""
        result = await get_book_metadata_complete(
            book_id="12345", book_hash="abc123hash"
        )
        assert result["id"] == "12345"
        assert result["book_hash"] == "abc123hash"
        assert result["description"] == "A test book description."
        assert result["series"] == "Test Series"

    @pytest.mark.asyncio
    async def test_metadata_requires_hash(self, patch_eapi_client):
        """Should raise if book_hash not provided."""
        with pytest.raises(ValueError, match="book_hash is required"):
            await get_book_metadata_complete(book_id="12345")


# --- Tests for Bridge Functions (term, author) ---


class TestBridgeFunctions:
    @pytest.mark.asyncio
    async def test_search_by_term_bridge(self, patch_eapi_client, mocker):
        """Should pass eapi_client to term_tools."""
        mock_term_search = AsyncMock(
            return_value={"term": "dialectic", "books": [], "total_results": 0}
        )
        mocker.patch("lib.term_tools.search_by_term", mock_term_search)
        result = await search_by_term_bridge(term="dialectic")
        assert result["term"] == "dialectic"
        # Verify eapi_client was passed
        call_kwargs = mock_term_search.call_args[1]
        assert call_kwargs["eapi_client"] is patch_eapi_client

    @pytest.mark.asyncio
    async def test_search_by_author_bridge(self, patch_eapi_client, mocker):
        """Should pass eapi_client to author_tools."""
        mock_author_search = AsyncMock(
            return_value={"author": "Hegel", "books": [], "total_results": 0}
        )
        mocker.patch("lib.author_tools.search_by_author", mock_author_search)
        result = await search_by_author_bridge(author="Hegel")
        assert result["author"] == "Hegel"
        call_kwargs = mock_author_search.call_args[1]
        assert call_kwargs["eapi_client"] is patch_eapi_client


# --- Tests for Download Book (now uses EAPIClient.download_file) ---


class TestDownloadBook:
    @pytest.mark.asyncio
    async def test_acquisition_rejects_li_digest_then_accepts_vg_once(
        self, tmp_path, mocker
    ):
        """Probe success cannot make a corrupt li body final or restart li."""
        good_body = b"%PDF-1.7 matching body"
        expected_md5 = hashlib.md5(good_body).hexdigest()
        yielded = []

        async def candidates(_md5, source):
            for host in ("li", "vg"):
                yielded.append(host)
                yield DownloadResult(
                    url=f"https://libgen.{host}/book", source=SourceType.LIBGEN
                )

        class Response:
            headers = {"content-type": "application/pdf"}

            def __init__(self, url):
                self.url = httpx.URL(url)

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"wrong body" if self.url.host == "libgen.li" else good_body

        class Stream:
            def __init__(self, url):
                self.response = Response(url)

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, url):
                return Stream(url)

        preexisting = tmp_path / f"{expected_md5}.download"
        preexisting.write_bytes(b"keep")
        unrelated = tmp_path / "unrelated.bin"
        unrelated.write_bytes(b"also keep")
        router = SimpleNamespace(iter_download_candidates=candidates)
        mocker.patch(
            "python_bridge.get_source_router", new=AsyncMock(return_value=router)
        )
        mocker.patch("httpx.AsyncClient", Client)

        result = await python_bridge._fetch_from_source(
            {"md5": expected_md5.upper(), "source": "libgen"}, str(tmp_path)
        )

        assert Path(result).read_bytes() == good_body
        assert Path(result).suffix == ".pdf"
        assert yielded == ["li", "vg"]
        assert preexisting.read_bytes() == b"keep"
        assert unrelated.read_bytes() == b"also keep"
        assert not [path for path in tmp_path.iterdir() if path.suffix == ".part"]

    @pytest.mark.asyncio
    async def test_successful_acquisition_closes_suspended_candidate_stream(
        self, tmp_path, mocker
    ):
        """Returning the first successful transfer must close router resources."""
        body = b"%PDF-1.7 successful close"
        digest = hashlib.md5(body).hexdigest()
        closed = False

        async def candidates(_md5, source):
            nonlocal closed
            try:
                yield DownloadResult(
                    url="https://libgen.li/book", source=SourceType.LIBGEN
                )
                await asyncio.sleep(30)
            finally:
                closed = True

        router = SimpleNamespace(iter_download_candidates=candidates)
        mocker.patch(
            "python_bridge.get_source_router", new=AsyncMock(return_value=router)
        )
        raw_path = tmp_path / "owned.pdf"
        raw_path.write_bytes(body)
        mocker.patch(
            "python_bridge._download_url_to_file",
            new=AsyncMock(return_value=str(raw_path)),
        )

        result = await python_bridge._fetch_from_source(
            {"md5": digest, "source": "libgen"}, str(tmp_path)
        )

        assert result == str(raw_path)
        assert closed is True

    @pytest.mark.asyncio
    async def test_integrity_mismatch_names_expected_actual_and_final_cdn_host(
        self, tmp_path, mocker
    ):
        """Changing digest comparison or redirect attribution breaks the envelope."""
        body = b"not the requested book"
        expected = "0" * 32

        class Response:
            url = httpx.URL("https://cdn-vg.booksdl.test/book")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(Exception) as excinfo:
            await python_bridge._download_url_to_file(
                "https://libgen.vg/get", str(tmp_path), expected, "libgen"
            )

        failure = excinfo.value
        assert failure.reason == "integrity_mismatch"
        assert failure.host == "cdn-vg.booksdl.test"
        assert f"expected={expected}" in failure.detail
        assert f"actual={hashlib.md5(body).hexdigest()}" in failure.detail
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("headers", "final_url", "body", "expected_suffix"),
        [
            (
                {"content-disposition": 'attachment; filename="safe-book.epub"'},
                "https://cdn.example/get",
                b"PK\x03\x04mimetypeapplication/epub+zip",
                ".epub",
            ),
            (
                {"content-type": "application/octet-stream"},
                "https://cdn.example/redirected-book.pdf?token=1",
                b"%PDF-1.7 url evidence",
                ".pdf",
            ),
            (
                {"content-type": "application/pdf"},
                "https://cdn.example/get",
                b"not a signature but declared pdf",
                ".txt",
            ),
            (
                {"content-type": "text/plain; charset=utf-8"},
                "https://cdn.example/get",
                b"plain text evidence",
                ".txt",
            ),
            (
                {"content-type": "application/octet-stream"},
                "https://cdn.example/get.download",
                b"%PDF-1.7 signature evidence",
                ".pdf",
            ),
            (
                {"content-type": "application/octet-stream"},
                "https://cdn.example/get.part",
                b"\x00\x01unknown binary",
                "",
            ),
        ],
    )
    async def test_transfer_recovers_safe_extension_from_response_evidence(
        self, headers, final_url, body, expected_suffix, tmp_path, mocker
    ):
        """Dropping response evidence would hand RAG a .part/.download path."""
        digest = hashlib.md5(body).hexdigest()

        class Response:
            url = httpx.URL(final_url)

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        Response.headers = headers

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )

        assert Path(result).suffix == expected_suffix
        assert Path(result).read_bytes() == body

    @pytest.mark.asyncio
    async def test_mislabeled_html_signature_is_a_protocol_failure(
        self, tmp_path, mocker
    ):
        """PDF headers and names cannot disguise an HTML interstitial."""
        body = b"\xef\xbb\xbf  \r\n\t<HtMl><body>challenge</body></html>"
        digest = hashlib.md5(body).hexdigest()

        class Response:
            url = httpx.URL("https://cdn.example/book.pdf")
            headers = {
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="book.pdf"',
            }

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(ProviderResponseError) as excinfo:
            await python_bridge._download_url_to_file(
                "https://mirror.example/book", str(tmp_path), digest, "libgen"
            )

        assert excinfo.value.reason == "protocol_error"
        assert excinfo.value.host == "cdn.example"
        assert not list(tmp_path.glob(".source-*"))

    @pytest.mark.asyncio
    async def test_all_candidate_transfer_failures_are_ordered_and_unique(
        self, tmp_path, mocker
    ):
        """A failed candidate walk must retain one envelope per definitive attempt."""

        async def candidates(_md5, source):
            for host in ("li", "vg", "la"):
                yield DownloadResult(
                    url=f"https://cdn-{host}.example/book", source=SourceType.LIBGEN
                )

        class Stream:
            def __init__(self, url):
                self.url = url

            async def __aenter__(self):
                request = httpx.Request("GET", self.url)
                raise httpx.ConnectError("offline", request=request)

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, url):
                return Stream(url)

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await python_bridge._fetch_from_source(
                {"md5": "0" * 32, "source": "libgen"}, str(tmp_path)
            )

        assert [failure.host for failure in excinfo.value.failures] == [
            "cdn-li.example",
            "cdn-vg.example",
            "cdn-la.example",
        ]
        assert len({str(failure) for failure in excinfo.value.failures}) == 3

    @pytest.mark.asyncio
    async def test_equal_failure_envelopes_preserve_distinct_candidate_attempts(
        self, tmp_path, mocker
    ):
        """Two mirrors reaching one failed CDN are two definitive attempts."""

        async def candidates(_md5, source):
            for mirror in ("li", "vg"):
                yield DownloadResult(
                    url=f"https://libgen.{mirror}/book", source=SourceType.LIBGEN
                )

        transfer = mocker.patch(
            "python_bridge._download_url_to_file",
            new=AsyncMock(
                side_effect=[
                    ProviderUnreachableError(
                        "libgen",
                        "shared-cdn.example",
                        "offline",
                        reason="connect_error",
                    ),
                    ProviderUnreachableError(
                        "libgen",
                        "shared-cdn.example",
                        "offline",
                        reason="connect_error",
                    ),
                ]
            ),
        )
        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await python_bridge._fetch_from_source(
                {"md5": "0" * 32, "source": "libgen"}, str(tmp_path)
            )

        assert transfer.await_count == 2
        assert [failure.host for failure in excinfo.value.failures] == [
            "shared-cdn.example",
            "shared-cdn.example",
        ]

    @pytest.mark.asyncio
    async def test_transfer_and_resolution_failures_share_ordered_aggregate(
        self, tmp_path, mocker
    ):
        """Resolution failures after a transfer attempt cannot disappear."""

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://cdn-li.example/book", source=SourceType.LIBGEN
            )
            raise AllSourcesFailedError(
                "download resolution",
                [
                    ProviderResponseError(
                        "libgen", "libgen.vg", "no GET link", reason="protocol_error"
                    ),
                    ProviderUnreachableError(
                        "libgen", "libgen.la", "offline", reason="connect_error"
                    ),
                ],
            )

        class Stream:
            async def __aenter__(self):
                request = httpx.Request("GET", "https://cdn-li.example/book")
                raise httpx.ConnectError("offline", request=request)

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await python_bridge._fetch_from_source(
                {"md5": "0" * 32, "source": "libgen"}, str(tmp_path)
            )

        assert [failure.host for failure in excinfo.value.failures] == [
            "cdn-li.example",
            "libgen.vg",
            "libgen.la",
        ]

    @pytest.mark.asyncio
    async def test_auto_advances_provider_after_libgen_transfer_failure(
        self, tmp_path, mocker
    ):
        """Only auto may continue from exhausted LibGen candidates to Anna's."""
        good_body = b"%PDF-1.7 Anna fallback"
        digest = hashlib.md5(good_body).hexdigest()
        attempted = []

        async def candidates(_md5, source):
            assert source == "auto"
            yield DownloadResult(
                url="https://libgen.example/book", source=SourceType.LIBGEN
            )
            yield DownloadResult(
                url="https://annas.example/book", source=SourceType.ANNAS_ARCHIVE
            )

        class Response:
            headers = {"content-type": "application/pdf"}

            def __init__(self, url):
                self.url = httpx.URL(url)
                attempted.append(self.url.host)

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"wrong" if self.url.host == "libgen.example" else good_body

        class Stream:
            def __init__(self, url):
                self.url = url

            async def __aenter__(self):
                return Response(self.url)

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, url):
                return Stream(url)

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        result = await python_bridge._fetch_from_source(
            {"md5": digest, "source": "auto"}, str(tmp_path)
        )

        assert Path(result).read_bytes() == good_body
        assert attempted == ["libgen.example", "annas.example"]

    @pytest.mark.asyncio
    async def test_one_outer_budget_stops_before_later_candidates(
        self, tmp_path, mocker
    ):
        """A fresh full timeout per mirror would start vg after li consumed the budget."""
        started = []

        async def candidates(_md5, source):
            for host in ("li", "vg"):
                started.append(host)
                yield DownloadResult(
                    url=f"https://{host}.example/book", source=SourceType.LIBGEN
                )

        class Response:
            url = httpx.URL("https://li.example/book")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"partial"
                await asyncio.sleep(1)

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        config = python_bridge.get_source_config()
        config.download_timeout = 0.03
        mocker.patch("python_bridge.get_source_config", return_value=config)
        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(ProviderResponseError):
            await python_bridge._fetch_from_source(
                {"md5": "0" * 32, "source": "libgen"}, str(tmp_path)
            )

        assert started == ["li"]
        assert not [path for path in tmp_path.iterdir() if path.suffix == ".part"]

    @pytest.mark.asyncio
    async def test_outer_timeout_retains_prior_attempt_and_active_host(
        self, tmp_path, mocker
    ):
        """A late timeout augments, rather than replaces, earlier evidence."""
        closed = False

        async def candidates(_md5, source):
            nonlocal closed
            try:
                yield DownloadResult(
                    url="https://first-cdn.example/book", source=SourceType.LIBGEN
                )
                yield DownloadResult(
                    url="https://mirror-vg.example/book", source=SourceType.LIBGEN
                )
            finally:
                closed = True

        class HangingResponse:
            url = httpx.URL("https://final-cdn.example/book.pdf")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"partial"
                await asyncio.sleep(30)

        class Stream:
            def __init__(self, url):
                self.url = url

            async def __aenter__(self):
                if "first-cdn" in self.url:
                    request = httpx.Request("GET", self.url)
                    raise httpx.ConnectError("offline", request=request)
                return HangingResponse()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, url):
                return Stream(url)

        config = python_bridge.get_source_config()
        config.download_timeout = 0.03
        mocker.patch("python_bridge.get_source_config", return_value=config)
        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await python_bridge._fetch_from_source(
                {"md5": "0" * 32, "source": "libgen"}, str(tmp_path)
            )

        assert [failure.reason for failure in excinfo.value.failures] == [
            "connect_error",
            "partial_transfer",
        ]
        assert [failure.host for failure in excinfo.value.failures] == [
            "first-cdn.example",
            "final-cdn.example",
        ]
        assert closed is True
        assert not list(tmp_path.glob(".source-*"))

    @pytest.mark.asyncio
    async def test_cancelled_transfer_removes_only_its_unique_attempt(
        self, tmp_path, mocker
    ):
        """Cancellation cannot delete fixed-name or unrelated artifacts."""
        started = asyncio.Event()
        expected = "0" * 32
        preexisting = tmp_path / f"{expected}.download"
        preexisting.write_bytes(b"preexisting")
        unrelated = tmp_path / "other.part"
        unrelated.write_bytes(b"other attempt")

        class Response:
            url = httpx.URL("https://cdn.example/book")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"partial"
                started.set()
                await asyncio.sleep(30)

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)
        task = asyncio.create_task(
            python_bridge._download_url_to_file(
                "https://mirror.example/book", str(tmp_path), expected, "libgen"
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert preexisting.read_bytes() == b"preexisting"
        assert unrelated.read_bytes() == b"other attempt"
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
            [preexisting.name, unrelated.name]
        )

    @pytest.mark.asyncio
    async def test_raw_extension_publish_never_replaces_preexisting_sibling(
        self, tmp_path, mocker
    ):
        """Changing unique staging suffixes cannot overwrite another attempt."""
        body = b"%PDF-1.7 owned body"
        digest = hashlib.md5(body).hexdigest()
        attempt = tmp_path / ".source-fixed.part"
        sibling = tmp_path / ".source-fixed.pdf"
        sibling.write_bytes(b"another attempt")

        def fixed_mkstemp(**_kwargs):
            descriptor = os.open(attempt, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            return descriptor, str(attempt)

        class Response:
            url = httpx.URL("https://cdn.example/book.pdf")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("python_bridge.tempfile.mkstemp", side_effect=fixed_mkstemp)
        mocker.patch("httpx.AsyncClient", Client)

        with pytest.raises(FileExistsError):
            await python_bridge._download_url_to_file(
                "https://mirror.example/book", str(tmp_path), digest, "libgen"
            )

        assert sibling.read_bytes() == b"another attempt"
        assert not attempt.exists()

    @pytest.mark.asyncio
    async def test_final_filename_collision_preserves_existing_and_owned_artifacts(
        self, tmp_path, mocker
    ):
        """A deterministic unified name must never silently replace another book."""
        digest = "a" * 32
        owned = tmp_path / ".source-owned.pdf"
        owned.write_bytes(b"newly acquired")
        final_path = tmp_path / f"UnknownAuthor_Collision_{digest}.pdf"
        final_path.write_bytes(b"existing artifact")
        mocker.patch(
            "python_bridge._fetch_from_source",
            new=AsyncMock(return_value=str(owned)),
        )

        with pytest.raises(FileExistsError) as excinfo:
            await download_book(
                book_details={
                    "md5": digest,
                    "source": "libgen",
                    "title": "Collision",
                    "extension": "pdf",
                },
                output_dir=str(tmp_path),
            )

        assert final_path.read_bytes() == b"existing artifact"
        # The mismatching destination survives; the staged artifact never
        # outlives the call that created it (PR #131).
        assert not owned.exists()
        assert str(final_path) in str(excinfo.value)
        assert str(owned) in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_repeat_acquisition_reuses_matching_destination(
        self, tmp_path, mocker
    ):
        """Downloading the same result twice is idempotent, not a collision."""
        body = b"%PDF-1.7 the very same book"
        digest = hashlib.md5(body).hexdigest()
        owned = tmp_path / ".source-owned.pdf"
        owned.write_bytes(body)
        final_path = tmp_path / f"UnknownAuthor_Repeat_{digest}.pdf"
        final_path.write_bytes(body)
        mocker.patch(
            "python_bridge._fetch_from_source",
            new=AsyncMock(return_value=str(owned)),
        )

        result = await download_book(
            book_details={
                "md5": digest,
                "source": "libgen",
                "title": "Repeat",
                "extension": "pdf",
            },
            output_dir=str(tmp_path),
        )

        assert result["file_path"] == str(final_path)
        assert final_path.read_bytes() == body
        assert not owned.exists()
        assert sorted(path.name for path in tmp_path.iterdir()) == [final_path.name]

    @pytest.mark.asyncio
    async def test_repeat_acquisition_rejects_same_size_different_content(
        self, tmp_path, mocker
    ):
        """Idempotent reuse compares the catalog MD5, not just the byte count."""
        body = b"%PDF-1.7 freshly acquired"
        digest = hashlib.md5(body).hexdigest()
        owned = tmp_path / ".source-owned.pdf"
        owned.write_bytes(body)
        final_path = tmp_path / f"UnknownAuthor_Impostor_{digest}.pdf"
        final_path.write_bytes(b"%PDF-1.8 freshly acquired")
        assert final_path.stat().st_size == owned.stat().st_size
        mocker.patch(
            "python_bridge._fetch_from_source",
            new=AsyncMock(return_value=str(owned)),
        )

        with pytest.raises(FileExistsError):
            await download_book(
                book_details={
                    "md5": digest,
                    "source": "libgen",
                    "title": "Impostor",
                    "extension": "pdf",
                },
                output_dir=str(tmp_path),
            )

        assert final_path.read_bytes() == b"%PDF-1.8 freshly acquired"
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_repeat_acquisition_without_catalog_md5_rejects_same_size_different_content(
        self, tmp_path, mocker, mock_eapi_download, patch_eapi_client
    ):
        """When no catalog MD5 is available, same-size different-content is rejected."""
        staged = tmp_path / "downloads" / "staged.pdf"
        final_path = tmp_path / "downloads" / "Author_Title_123.pdf"
        tmp_path.joinpath("downloads").mkdir(parents=True)
        staged.write_bytes(b"freshly downloaded content")
        final_path.write_bytes(b"stale corrupted content!!!")
        assert staged.stat().st_size == final_path.stat().st_size

        mock_eapi_download.download_file.return_value = str(staged)
        mocker.patch(
            "python_bridge.create_unified_filename", return_value="Author_Title_123.pdf"
        )

        with pytest.raises(FileExistsError) as excinfo:
            await download_book(
                book_details={
                    "id": "123",
                    "title": "Title",
                    "author": "Author",
                    "extension": "pdf",
                    "hash": "hash123",
                },
                output_dir=str(tmp_path / "downloads"),
            )

        assert str(final_path) in str(excinfo.value)
        assert str(staged) in str(excinfo.value)
        assert final_path.read_bytes() == b"stale corrupted content!!!"
        assert not staged.exists()

    @pytest.mark.asyncio
    async def test_repeat_acquisition_without_catalog_md5_reuses_matching_content(
        self, tmp_path, mocker, mock_eapi_download, patch_eapi_client
    ):
        """When no catalog MD5 is available, identical content is reused."""
        staged = tmp_path / "downloads" / "staged.pdf"
        final_path = tmp_path / "downloads" / "Author_Title_123.pdf"
        tmp_path.joinpath("downloads").mkdir(parents=True)
        content = b"%PDF-1.7 identical content"
        staged.write_bytes(content)
        final_path.write_bytes(content)

        mock_eapi_download.download_file.return_value = str(staged)
        mocker.patch(
            "python_bridge.create_unified_filename", return_value="Author_Title_123.pdf"
        )

        result = await download_book(
            book_details={
                "id": "123",
                "title": "Title",
                "author": "Author",
                "extension": "pdf",
                "hash": "hash123",
            },
            output_dir=str(tmp_path / "downloads"),
        )

        assert result["file_path"] == str(final_path)
        assert final_path.read_bytes() == content
        assert not staged.exists()

    @pytest.mark.asyncio
    async def test_download_book_when_download_path_equals_unified_filename(
        self, tmp_path, mocker, mock_eapi_download, patch_eapi_client
    ):
        """When EAPI saves directly to the unified filename, artifact is not unlinked."""
        output_dir = tmp_path / "downloads"
        output_dir.mkdir(parents=True)
        filename = "Author_Title_123.pdf"
        file_path = output_dir / filename
        content = b"%PDF-1.7 eapi direct body"
        file_path.write_bytes(content)

        mock_eapi_download.download_file.return_value = str(file_path)
        mocker.patch("python_bridge.create_unified_filename", return_value=filename)

        result = await download_book(
            book_details={
                "id": "123",
                "title": "Title",
                "author": "Author",
                "extension": "pdf",
                "hash": "hash123",
            },
            output_dir=str(output_dir),
        )

        assert result["file_path"] == str(file_path)
        assert file_path.exists()
        assert file_path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_transfer_digest_survives_fips_enforcing_builds(
        self, tmp_path, mocker
    ):
        """A FIPS ValueError from md5() would abort the whole candidate walk."""
        body = b"%PDF-1.7 fips body"
        digest = hashlib.md5(body).hexdigest()
        real_md5 = hashlib.md5

        def fips_md5(*args, **kwargs):
            if kwargs.get("usedforsecurity", True):
                raise ValueError("[digital envelope routines] unsupported")
            kwargs.pop("usedforsecurity", None)
            return real_md5(*args, **kwargs)

        mocker.patch("python_bridge.hashlib.md5", side_effect=fips_md5)

        class Response:
            url = httpx.URL("https://cdn.example/get")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )

        assert Path(result).read_bytes() == body

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
    def test_sigterm_cancels_dispatch_and_removes_partial_download(self, tmp_path):
        """SIGTERM must unwind cleanup in the production EAPI download method."""
        partial = tmp_path / "bridge-partial.epub"
        ready = tmp_path / "ready"
        args_json = json.dumps(
            {
                "book_details": {
                    "id": "123",
                    "hash": "abc123",
                    "title": "Signal test",
                    "author": "Test Author",
                    "extension": "epub",
                },
                "output_dir": str(tmp_path),
            }
        )
        code = f"""
import asyncio
import pathlib
import sys
from types import SimpleNamespace
sys.path.insert(0, {str(Path(python_bridge.__file__).parent)!r})
import python_bridge
import zlibrary.eapi as eapi
partial = pathlib.Path({str(partial)!r})
ready = pathlib.Path({str(ready)!r})

class SignalResponse:
    headers = {{'content-disposition': 'attachment; filename="bridge-partial.epub"'}}
    url = 'https://download.example/bridge-partial.epub'

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        yield b'partial'
        ready.write_text('ready', encoding='utf-8')
        await asyncio.sleep(600)

class SignalStream:
    async def __aenter__(self):
        return SignalResponse()

    async def __aexit__(self, *_args):
        return False

class SignalClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return SignalStream()

class SignalEAPIClient(eapi.EAPIClient):
    async def get_download_link(self, book_id, book_hash):
        return {{'file': {{'downloadLink': 'https://download.example/book'}}}}

eapi.httpx.AsyncClient = SignalClient
python_bridge._eapi_client = SignalEAPIClient('test.example')

async def use_test_client():
    return python_bridge._eapi_client

python_bridge.initialize_eapi_client = use_test_client
python_bridge.get_source_config = lambda: SimpleNamespace(preflight_timeout=1)
sys.argv = ['python_bridge.py', 'download_book', {args_json!r}]
asyncio.run(python_bridge.main())
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not ready.exists():
                time.sleep(0.02)
            assert ready.exists()
            assert not partial.exists()
            staging_files = [path for path in tmp_path.rglob("*.part")]
            assert len(staging_files) == 1

            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)
            stdout, _stderr = process.communicate()

            assert not partial.exists()
            assert not [path for path in tmp_path.rglob("*.part")]
            assert not list(tmp_path.glob(".eapi-*"))
            assert stdout == ""
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_annas_transfer_uses_canonical_provider_name(self, tmp_path, mocker):
        """Transfer failures use `annas`, while success models retain their enum."""

        async def candidates(_md5, source):
            yield SimpleNamespace(
                url="https://cdn.example/book",
                source=SimpleNamespace(value="annas_archive"),
            )

        router = SimpleNamespace(iter_download_candidates=candidates)
        transfer = mocker.patch(
            "python_bridge._download_url_to_file",
            new=AsyncMock(return_value="book.pdf"),
        )
        mocker.patch(
            "python_bridge.get_source_router", new=AsyncMock(return_value=router)
        )

        await python_bridge._fetch_from_source(
            {"md5": "0123456789abcdef0123456789abcdef", "source": "annas"},
            str(tmp_path),
        )

        assert transfer.await_args.args[3] == "annas"

    @pytest.mark.asyncio
    async def test_valid_slow_transfer_can_exceed_provider_resolution_budget(
        self, tmp_path, mocker
    ):
        """A large transfer must not inherit the short resolution deadline."""

        class SlowResponse:
            url = httpx.URL("https://cdn.example/file")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                await asyncio.sleep(0.06)
                yield b"%PDF slow but valid"

        class SlowStream:
            async def __aenter__(self):
                return SlowResponse()

            async def __aexit__(self, *_args):
                return False

        class SlowClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return SlowStream()

        config = python_bridge.get_source_config()
        config.total_timeout = 0.02
        config.download_timeout = 0.15
        mocker.patch("python_bridge.get_source_config", return_value=config)
        mocker.patch("httpx.AsyncClient", SlowClient)

        body = b"%PDF slow but valid"
        digest = hashlib.md5(body).hexdigest()

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )

        assert Path(result).read_bytes() == body

    @pytest.mark.asyncio
    async def test_transfer_exceeding_download_budget_is_typed_and_cleans_partial(
        self, tmp_path, mocker
    ):
        """The larger transfer budget remains finite and cleanup-safe."""

        class OverBudgetResponse:
            url = httpx.URL("https://cdn.example/file")
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"partial"
                await asyncio.sleep(0.15)
                yield b"late"

        class OverBudgetStream:
            async def __aenter__(self):
                return OverBudgetResponse()

            async def __aexit__(self, *_args):
                return False

        class OverBudgetClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return OverBudgetStream()

        config = python_bridge.get_source_config()
        config.total_timeout = 1.0
        config.download_timeout = 0.03
        mocker.patch("python_bridge.get_source_config", return_value=config)
        mocker.patch("httpx.AsyncClient", OverBudgetClient)

        with pytest.raises(ProviderResponseError) as excinfo:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get",
                str(tmp_path),
                "0" * 32,
                "libgen",
            )

        assert excinfo.value.reason == "partial_transfer"
        assert excinfo.value.host == "cdn.example"
        assert not [path for path in tmp_path.iterdir() if path.suffix == ".part"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure", "expected_reason"),
        [
            (httpx.ConnectTimeout("connect stalled"), "connect_timeout"),
            (httpx.ReadTimeout("body stalled"), "read_timeout"),
            (
                httpx.HTTPStatusError(
                    "bad status",
                    request=httpx.Request("GET", "https://cdn.example/book"),
                    response=httpx.Response(503),
                ),
                "http_error",
            ),
            ("trickle", "read_timeout"),
        ],
    )
    async def test_source_transfer_failures_reach_main_as_typed_envelopes(
        self, failure, expected_reason, tmp_path, mocker, monkeypatch, capsys
    ):
        """The actual source transfer boundary must classify every failure."""

        class FakeResponse:
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                if isinstance(failure, httpx.HTTPStatusError):
                    raise failure

            async def aiter_bytes(self, _chunk_size):
                if failure == "trickle":
                    await asyncio.sleep(1)
                    yield b"late"
                    return
                if isinstance(failure, httpx.ReadTimeout):
                    raise failure
                yield b"%PDF"

        class FakeStream:
            async def __aenter__(self):
                if isinstance(failure, httpx.ConnectTimeout):
                    raise failure
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeStream()

        async def candidates(_md5, source):
            yield SimpleNamespace(url="https://cdn.example/book", source="libgen")

        router = SimpleNamespace(iter_download_candidates=candidates, close=AsyncMock())
        config = python_bridge.get_source_config()
        config.total_timeout = 0.03
        config.download_timeout = 0.03
        config.preflight_timeout = 0.03
        mocker.patch(
            "python_bridge.get_source_router", new=AsyncMock(return_value=router)
        )
        mocker.patch("python_bridge.get_source_config", return_value=config)
        mocker.patch("httpx.AsyncClient", FakeClient)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "python_bridge.py",
                "download_book",
                json.dumps(
                    {
                        "book_details": {
                            "md5": "0123456789abcdef0123456789abcdef",
                            "source": "libgen",
                        },
                        "output_dir": str(tmp_path),
                    }
                ),
            ],
        )

        with pytest.raises(SystemExit):
            await main()

        envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        failure_envelope = (
            envelope["details"]
            if failure == "trickle"
            else envelope["details"]["failures"][0]
        )
        assert failure_envelope["provider"] == "libgen"
        assert failure_envelope["host"] == "cdn.example"
        assert failure_envelope["reason"] == expected_reason
        assert envelope["details"]["operation"] == "download"
        assert not (tmp_path / "0123456789abcdef0123456789abcdef.download").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("headers", "declared_extension", "body", "expected_extension"),
        [
            (
                {"content-disposition": 'attachment; filename="actual.epub"'},
                "epub",
                b"%PDF-1.7 signature wins",
                ".pdf",
            ),
            (
                {"content-type": "application/pdf"},
                "epub",
                b"\x00\x01unknown binary",
                ".epub",
            ),
            (
                # Printable bytes are the weakest evidence there is: every
                # text-based container looks identical at that level, so a
                # declared extension outranks them (PR #131).
                {"content-type": "application/epub+zip"},
                "epub",
                b"plain text signature",
                ".epub",
            ),
            (
                {"content-type": "application/epub+zip"},
                "txt",
                b"{\\rtf1\\ansi printable but not plain text}",
                ".rtf",
            ),
            (
                {"content-type": "application/epub+zip"},
                "txt",
                b'<?xml version="1.0"?><FictionBook xmlns="http://x"><body/></FictionBook>',
                ".fb2",
            ),
            (
                {"content-type": "application/pdf"},
                "../escape.exe",
                b"\x00\x01unknown binary",
                ".pdf",
            ),
            (
                {"content-type": "application/octet-stream"},
                "../escape.exe",
                b"plain text with no declared extension",
                ".txt",
            ),
        ],
    )
    async def test_extension_evidence_precedence_reaches_final_boundary(
        self,
        headers,
        declared_extension,
        body,
        expected_extension,
        tmp_path,
        mocker,
    ):
        """Signatures outrank names/metadata; metadata outranks MIME alone."""
        digest = hashlib.md5(body).hexdigest()

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://mirror.example/get", source=SourceType.LIBGEN
            )

        class Response:
            url = httpx.URL("https://cdn.example/get")

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        Response.headers = headers

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        mocker.patch("httpx.AsyncClient", Client)

        result = await download_book(
            book_details={
                "md5": digest,
                "source": "libgen",
                "title": "Evidence",
                "extension": declared_extension,
            },
            output_dir=str(tmp_path),
        )

        final_path = Path(result["file_path"])
        assert final_path.exists()
        assert final_path.suffix == expected_extension

    @pytest.mark.asyncio
    async def test_redirected_partial_read_timeout_attributes_the_cdn_host(
        self, tmp_path, mocker, monkeypatch, capsys
    ):
        """After a redirect, the failing request host owns the failure."""
        cdn_request = httpx.Request("GET", "https://cdn.example/file")

        class FakeResponse:
            url = cdn_request.url
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield b"partial"
                raise httpx.ReadTimeout("CDN body stalled", request=cdn_request)

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeStream()

        async def candidates(_md5, source):
            yield SimpleNamespace(url="https://mirror.example/get", source="libgen")

        router = SimpleNamespace(iter_download_candidates=candidates, close=AsyncMock())
        mocker.patch(
            "python_bridge.get_source_router", new=AsyncMock(return_value=router)
        )
        mocker.patch("httpx.AsyncClient", FakeClient)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "python_bridge.py",
                "download_book",
                json.dumps(
                    {
                        "book_details": {
                            "md5": "0123456789abcdef0123456789abcdef",
                            "source": "libgen",
                        },
                        "output_dir": str(tmp_path),
                    }
                ),
            ],
        )

        with pytest.raises(SystemExit):
            await main()

        envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        failure_envelope = envelope["details"]["failures"][0]
        assert failure_envelope["host"] == "cdn.example"
        assert failure_envelope["reason"] == "partial_transfer"
        assert "bytes_received=21" in failure_envelope["detail"]
        assert "resumes_tried=2" in failure_envelope["detail"]
        assert envelope["details"]["operation"] == "download"
        assert not (tmp_path / "0123456789abcdef0123456789abcdef.download").exists()

    @pytest.mark.asyncio
    async def test_main_routes_libgen_before_eapi_initialization(
        self, tmp_path, mocker, monkeypatch, capsys
    ):
        """The CLI dispatch must inspect source before deciding authentication."""
        args = {
            "book_details": {
                "md5": "0123456789abcdef0123456789abcdef",
                "source": "libgen",
                "title": "LibGen Book",
            },
            "output_dir": str(tmp_path),
        }
        monkeypatch.setattr(
            sys, "argv", ["python_bridge.py", "download_book", json.dumps(args)]
        )
        initialize = mocker.patch(
            "python_bridge.initialize_eapi_client",
            new=AsyncMock(side_effect=AssertionError("EAPI initialization attempted")),
        )
        mocker.patch(
            "python_bridge.download_book",
            new=AsyncMock(return_value={"file_path": str(tmp_path / "book.pdf")}),
        )

        await main()

        initialize.assert_not_awaited()
        response = json.loads(capsys.readouterr().out)
        assert json.loads(response["content"][0]["text"])["file_path"].endswith(
            "book.pdf"
        )

    @pytest.mark.asyncio
    async def test_libgen_download_does_not_initialize_zlibrary(self, tmp_path, mocker):
        """A LibGen search result must route without unrelated EAPI credentials."""
        raw_path = tmp_path / "raw.pdf"
        raw_path.write_bytes(b"%PDF test")
        initialize = mocker.patch(
            "python_bridge.initialize_eapi_client",
            new=AsyncMock(side_effect=AssertionError("EAPI initialization attempted")),
        )
        fetch = mocker.patch(
            "python_bridge._fetch_from_source",
            new=AsyncMock(return_value=str(raw_path)),
        )
        mocker.patch(
            "python_bridge.create_unified_filename", return_value="libgen-book.pdf"
        )

        result = await download_book(
            book_details={
                "md5": "0123456789abcdef0123456789abcdef",
                "source": "libgen",
                "title": "LibGen Book",
                "extension": "pdf",
            },
            output_dir=str(tmp_path),
        )

        initialize.assert_not_awaited()
        fetch.assert_awaited_once()
        assert result["file_path"].endswith("libgen-book.pdf")

    @pytest.mark.asyncio
    async def test_empty_declared_extension_uses_recovered_download_path(
        self, tmp_path, mocker
    ):
        """Whitespace metadata cannot erase a response-supported EPUB suffix."""
        raw_path = tmp_path / ".source-attempt.epub"
        raw_path.write_bytes(b"PK\x03\x04mimetypeapplication/epub+zip")
        mocker.patch(
            "python_bridge._fetch_from_source",
            new=AsyncMock(return_value=str(raw_path)),
        )

        result = await download_book(
            book_details={
                "md5": "a" * 32,
                "source": "annas_archive",
                "author": "Test Author",
                "title": "Recovered Extension",
                "extension": "   ",
            },
            output_dir=str(tmp_path),
        )

        final_path = Path(result["file_path"])
        assert final_path.exists()
        assert final_path.suffix == ".epub"
        assert final_path.name.endswith(f"_{'a' * 32}.epub")

    @pytest.mark.asyncio
    async def test_download_book_success(
        self, mock_eapi_download, tmp_path, mocker, patch_eapi_client
    ):
        """Tests download_book for successful execution."""
        book_details_mock = {
            "id": "987",
            "extension": "pdf",
            "name": "Bridge Test Book",
            "author": "Test Author",
            "url": "https://example.com/book/987/test",
            "hash": "testhash123",
        }
        output_dir_mock = str(tmp_path / "downloads")

        original_path = str(Path(output_dir_mock) / "987.pdf")
        expected_filename = "AuthorTest_Bridge_Test_Book_987.pdf"
        expected_final_path = str(Path(output_dir_mock) / expected_filename)

        mock_eapi_download.download_file.return_value = original_path
        Path(output_dir_mock).mkdir(parents=True)
        Path(original_path).write_bytes(b"%PDF EAPI fixture")
        mocker.patch(
            "python_bridge.create_unified_filename", return_value=expected_filename
        )

        result = await download_book(
            book_details=book_details_mock,
            output_dir=output_dir_mock,
            process_for_rag=False,
        )

        assert result["file_path"] == expected_final_path
        assert set(result) == {"file_path"}

    @pytest.mark.asyncio
    async def test_download_book_error(
        self, mock_eapi_download, tmp_path, mocker, patch_eapi_client
    ):
        """Tests download_book handles download errors."""
        book_details_mock = {
            "id": "989",
            "extension": "epub",
            "name": "Error Test",
            "url": "https://example.com/book/989/test",
            "hash": "errhash",
        }
        mock_eapi_download.download_file.side_effect = RuntimeError("Download failed")

        with pytest.raises(RuntimeError, match="Download failed"):
            await download_book(
                book_details=book_details_mock,
                output_dir=str(tmp_path / "downloads_err"),
                process_for_rag=False,
            )


# --- Acquisition authentication routing ---


class TestRequiresEapiClient:
    """EAPI login must happen only where the EAPI is actually used (#129):
    forcing it on every call took LibGen — the credential-free fallback —
    down with every Z-Library auth outage.
    """

    def test_zlibrary_functions_require_eapi(self):
        from lib import python_bridge

        for fn in ("search", "full_text_search", "get_download_limits"):
            assert python_bridge._requires_eapi_client(fn, {}) is True

    def test_local_and_multi_source_functions_do_not(self):
        from lib import python_bridge

        assert python_bridge._requires_eapi_client("search_multi_source", {}) is False
        assert python_bridge._requires_eapi_client("search_multi_source", {}) is False

    def test_download_of_zlibrary_result_requires_eapi(self):
        from lib import python_bridge

        args = {"book_details": {"id": "123", "hash": "abc"}}
        assert python_bridge._requires_eapi_client("download_book", args) is True

    def test_download_of_multi_source_result_does_not(self):
        from lib import python_bridge

        for source in ("libgen", "annas", "annas_archive", "LIBGEN"):
            args = {"book_details": {"md5": "f" * 32, "source": source}}
            assert (
                python_bridge._requires_eapi_client("download_book", args) is False
            ), source

    def test_download_with_missing_details_defaults_to_requiring_eapi(self):
        from lib import python_bridge

        assert python_bridge._requires_eapi_client("download_book", {}) is True


class TestLibgenDownloadNeedsNoZlibraryLogin:
    """A source-routed download must succeed while the EAPI client is
    unavailable — the exact co-failure observed 2026-08-17 (#129).
    """

    @pytest.mark.asyncio
    async def test_download_book_libgen_source_never_touches_eapi(
        self, tmp_path, monkeypatch
    ):
        from lib import python_bridge

        raw = tmp_path / "ffff.download"
        raw.write_bytes(b"%PDF-1.4 fake body")

        async def fake_fetch(book_details, output_dir):
            return str(raw)

        async def dead_eapi(*args, **kwargs):
            raise RuntimeError("EAPI client must not be touched on this path")

        monkeypatch.setattr(python_bridge, "_fetch_from_source", fake_fetch)
        monkeypatch.setattr(python_bridge, "get_eapi_client", dead_eapi)
        monkeypatch.setattr(python_bridge, "initialize_eapi_client", dead_eapi)

        result = await python_bridge.download_book(
            book_details={
                "md5": "f" * 32,
                "source": "libgen",
                "title": "Fake Book",
                "author": "Nobody",
                "extension": "pdf",
            },
            output_dir=str(tmp_path),
        )

        assert result["file_path"]
        from pathlib import Path as _P

        assert _P(result["file_path"]).exists()


class TestDownloadHonoursTheLibgenUserAgentOverride:
    """The file transfer must send the configured UA, not the compiled default.

    Codex on #146: search and key resolution honoured `LIBGEN_USER_AGENT`
    while this function imported the module-level constant, so an operator who
    set the override to escape a widened blocklist would see search recover and
    downloads keep failing — with the config appearing to have addressed it.
    LibGen serves blocked UAs an HTML stub at HTTP 200, which the guard here
    then misreads as an expired key, so the symptom points away from the cause.
    """

    @pytest.mark.asyncio
    async def test_configured_user_agent_reaches_the_transfer(
        self, tmp_path, mocker, monkeypatch
    ):
        import httpx

        from lib import python_bridge

        body = b"%PDF-1.6" + b"\x00" * 2040
        digest = hashlib.md5(body).hexdigest()
        monkeypatch.setenv("LIBGEN_USER_AGENT", "operator-chosen/2.0")

        seen = {}

        class Response:
            url = httpx.URL("https://mirror.example/get")
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **kwargs):
                seen["headers"] = kwargs.get("headers") or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )

        assert seen["headers"].get("User-Agent") == "operator-chosen/2.0", (
            "the transfer must use the operator's override; sending the "
            "compiled-in default here is the failure #146 caught"
        )

    @pytest.mark.asyncio
    async def test_caller_supplied_config_wins_over_the_ambient_one(
        self, tmp_path, mocker, monkeypatch
    ):
        """A router built with an injected config must not be second-guessed.

        Codex round 2 on #146: the caller resolves a router (which carries its
        own `config`, possibly injected or cached) and this function then
        called `get_source_config()` again. Two config objects, one of which
        moves the file — the same split the UA fix was meant to close.
        """
        import httpx

        from lib import python_bridge
        from lib.sources.config import get_source_config

        body = b"%PDF-1.6" + b"\x00" * 2040
        digest = hashlib.md5(body).hexdigest()
        monkeypatch.setenv("LIBGEN_USER_AGENT", "ambient-should-lose/1.0")

        injected = dataclasses.replace(
            get_source_config(), libgen_user_agent="injected-should-win/3.0"
        )
        seen = {}

        class Response:
            url = httpx.URL("https://mirror.example/get")
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **kwargs):
                seen["headers"] = kwargs.get("headers") or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        await python_bridge._download_url_to_file(
            "https://mirror.example/get",
            str(tmp_path),
            digest,
            "libgen",
            config=injected,
        )

        assert seen["headers"].get("User-Agent") == "injected-should-win/3.0"

    @pytest.mark.asyncio
    async def test_the_libgen_override_does_not_leak_to_other_providers(
        self, tmp_path, mocker, monkeypatch
    ):
        """`LIBGEN_USER_AGENT` is scoped to LibGen, and this function is not.

        Codex round 3 on #146: an operator sets this variable to satisfy one
        provider's blocklist. Sending that string to Anna's host changes an
        unrelated transfer's behaviour and hands a custom identifying value to
        a provider that never asked for it — and `auto` routing means the
        operator does not choose which host receives it.
        """
        import httpx

        from lib import python_bridge
        from lib.sources.config import DEFAULT_BROWSER_USER_AGENT

        body = b"%PDF-1.6" + b"\x00" * 2040
        digest = hashlib.md5(body).hexdigest()
        monkeypatch.setenv("LIBGEN_USER_AGENT", "libgen-only/4.0")

        seen = {}

        class Response:
            url = httpx.URL("https://annas.example/get")
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self, _chunk_size):
                yield body

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **kwargs):
                seen["headers"] = kwargs.get("headers") or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args):
                return Stream()

        mocker.patch("httpx.AsyncClient", Client)

        await python_bridge._download_url_to_file(
            "https://annas.example/get", str(tmp_path), digest, "annas"
        )

        sent = seen["headers"].get("User-Agent")
        assert sent == DEFAULT_BROWSER_USER_AGENT
        assert sent != "libgen-only/4.0", (
            "a LibGen-scoped override reaching Anna's is both a behaviour "
            "change on an unrelated transfer and an identifier leak"
        )


RESUME_BODY = b"%PDF-1.7 test book " * 1000
RESUME_DIGEST = hashlib.md5(RESUME_BODY).hexdigest()
RESUME_CUT = 8000


class ScriptedResponse:
    """One scripted HTTP response, doubling as its own stream context.

    `fail_after` reproduces the observed failure: N bytes arrive and the
    connection then dies. No live request is made anywhere in these tests.
    """

    def __init__(
        self,
        *,
        body=b"",
        status_code=200,
        headers=None,
        url="https://cdn3.booksdl.test/file",
        fail_after=None,
        error=None,
    ):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.url = httpx.URL(url)
        self.fail_after = fail_after
        self.error = error or httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", self.url),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self, _chunk_size=65536):
        if self.fail_after is None:
            yield self.body
            return
        yield self.body[: self.fail_after]
        raise self.error


class ScriptedTransport:
    """httpx.AsyncClient stand-in that records what each request asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    @property
    def ranges(self):
        return [headers.get("Range") for _url, headers in self.requests]

    def install(self, mocker):
        transport = self

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, url, **kwargs):
                transport.requests.append((url, dict(kwargs.get("headers") or {})))
                assert transport.responses, "the transfer made an unscripted request"
                return transport.responses.pop(0)

        mocker.patch("httpx.AsyncClient", Client)
        return self


def content_range(start, total):
    return f"bytes {start}-{total - 1}/{total}"


class TestPartialTransferRecovery:
    """A transfer that dies mid-body resumes instead of restarting (#135)."""

    @pytest.fixture(autouse=True)
    def _advance_retry_wait_without_wall_time(self, monkeypatch):
        """These cases test recovery/validation; separate loopback tests pin pacing."""
        sleep = asyncio.sleep

        async def step(delay):
            await sleep(0 if delay == 0.25 else delay)

        monkeypatch.setattr(asyncio, "sleep", step)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["416", "206", "200", "budget"])
    async def test_wildcard_range_uses_digest_or_continues(
        self, terminal, tmp_path, mocker
    ):
        """A matching stage completes; a shorter stage still needs recovery."""
        body = RESUME_BODY * 10
        expected = body[:66536] if terminal == "budget" else body
        requests = []

        class DroppedBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body[:65536]
                raise httpx.RemoteProtocolError("offline dropped body")

        def handler(request):
            requested_range = request.headers.get("Range")
            requests.append(requested_range)
            if requested_range is None:
                if len(requests) == 1:
                    return httpx.Response(200, stream=DroppedBody())
                assert terminal == "416"
                return httpx.Response(200, content=body)
            if requested_range == "bytes=65536-":
                return httpx.Response(
                    206,
                    headers={"Content-Range": "bytes 65536-66535/*"},
                    content=body[65536:66536],
                )
            assert terminal != "budget", "recovery exceeded its request budget"
            assert requested_range == "bytes=66536-"
            if terminal == "200":
                return httpx.Response(200, content=body)
            if terminal == "206":
                return httpx.Response(
                    206,
                    headers={
                        "Content-Range": f"bytes 66536-{len(body) - 1}/{len(body)}"
                    },
                    content=body[66536:],
                )
            return httpx.Response(416, headers={"Content-Range": "bytes */66536"})

        client_type = httpx.AsyncClient
        mocker.patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                **kwargs, transport=httpx.MockTransport(handler), trust_env=False
            ),
        )
        config = python_bridge.get_source_config()
        config.download_resume_attempts = {"budget": 1, "416": 3}.get(terminal, 2)
        operation = python_bridge._download_url_to_file(
            "https://mirror.example/book",
            str(tmp_path),
            hashlib.md5(expected).hexdigest(),
            "libgen",
            config=config,
        )
        result = await operation
        assert Path(result).read_bytes() == expected
        assert list(tmp_path.iterdir()) == [Path(result)]
        assert requests == [None, "bytes=65536-"] + (
            [] if terminal == "budget" else ["bytes=66536-"]
        ) + ([None] if terminal == "416" else [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("next_headers", [{"content-range": "bytes */*"}, {}])
    async def test_wildcard_completion_needs_no_unspecified_416(
        self, next_headers, tmp_path, mocker
    ):
        """A complete wildcard slice publishes without asking the server again."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT),
                ScriptedResponse(
                    body=RESUME_BODY[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-range": f"bytes {RESUME_CUT}-{len(RESUME_BODY) - 1}/*"
                    },
                ),
                ScriptedResponse(status_code=416, headers=next_headers),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert Path(result).read_bytes() == RESUME_BODY
        assert list(tmp_path.iterdir()) == [Path(result)]
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume_attempts", [1, 2])
    async def test_wildcard_digest_mismatch_never_publishes(
        self, resume_attempts, tmp_path, mocker
    ):
        """A wrong stage keeps recovering, even if it has the full byte count."""
        corrupt = RESUME_BODY[:-1] + b"!"
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=corrupt, fail_after=RESUME_CUT),
                ScriptedResponse(
                    body=corrupt[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-range": f"bytes {RESUME_CUT}-{len(corrupt) - 1}/*"
                    },
                ),
                ScriptedResponse(body=corrupt),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_resume_attempts = resume_attempts

        with pytest.raises(ProviderResponseError) as caught:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get",
                str(tmp_path),
                RESUME_DIGEST,
                "libgen",
                config=config,
            )

        assert caught.value.reason == (
            "partial_transfer" if resume_attempts == 1 else "integrity_mismatch"
        )
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"] + (
            [] if resume_attempts == 1 else [f"bytes={len(corrupt)}-"]
        )
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("digest", ["", "not-an-md5"])
    async def test_download_rejects_invalid_md5_before_transfer(
        self, digest, tmp_path, mocker
    ):
        """Required-MD5 validation rejects empty or invalid input before transfer."""
        ScriptedTransport([]).install(mocker)
        output = tmp_path / "download"
        with pytest.raises(ValueError, match="normalized 32-hex MD5"):
            await python_bridge._download_url_to_file(
                "https://mirror.example/get", str(output), digest, "libgen"
            )
        assert not output.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outer", [False, True], ids=["direct", "provider-chain"])
    @pytest.mark.parametrize("terminal", ["200", "206", "416", "wildcard"])
    async def test_completed_body_validation_deadline_stays_timeout(
        self, terminal, outer, tmp_path, mocker
    ):
        """Timing out during local hashing is not a partial network transfer."""
        responses = [ScriptedResponse(body=RESUME_BODY)]
        if terminal != "200":
            responses = [
                ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT),
                ScriptedResponse(
                    body=RESUME_BODY[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-range": (
                            f"bytes {RESUME_CUT}-{len(RESUME_BODY) - 1}/*"
                            if terminal == "wildcard"
                            else content_range(RESUME_CUT, len(RESUME_BODY))
                        )
                    },
                    fail_after=len(RESUME_BODY) - RESUME_CUT
                    if terminal == "416"
                    else None,
                ),
            ]
            if terminal == "416":
                responses.append(
                    ScriptedResponse(
                        status_code=416,
                        headers={"content-range": f"bytes */{len(RESUME_BODY)}"},
                    )
                )
        transport = ScriptedTransport(responses).install(mocker)
        config = python_bridge.get_source_config()
        config.download_timeout = 0.03
        hashing = False
        original_digest = python_bridge._md5_digest
        original_sleep = asyncio.sleep

        def start_hashing():
            nonlocal hashing
            hashing = True
            return original_digest()

        async def pause_validation(delay):
            await original_sleep(30 if hashing else delay)

        mocker.patch("python_bridge._md5_digest", side_effect=start_hashing)
        mocker.patch("python_bridge.asyncio.sleep", side_effect=pause_validation)

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://mirror.example/book", source=SourceType.LIBGEN
            )
            pytest.fail("the shared deadline must not admit another mirror")

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates, config=config
                )
            ),
        )
        with pytest.raises(ProviderTimeoutError) as caught:
            if outer:
                await python_bridge._fetch_from_source(
                    {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
                )
            else:
                await python_bridge._download_url_to_file(
                    "https://mirror.example/book",
                    str(tmp_path),
                    RESUME_DIGEST,
                    "libgen",
                    config=config,
                )
        assert caught.value.reason == "read_timeout"
        assert hashing
        expected_requests = {"200": 1, "206": 2, "416": 3, "wildcard": 2}
        assert len(transport.requests) == expected_requests[terminal]
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outer", [False, True], ids=["direct", "provider-chain"])
    async def test_wildcard_mismatch_recovery_deadline_is_partial_transfer(
        self, outer, tmp_path, mocker
    ):
        """A mismatching stage is incomplete again while awaiting more bytes."""
        corrupt = RESUME_BODY[:-1] + b"!"

        class PausingResponse(ScriptedResponse):
            async def __aenter__(self):
                await asyncio.sleep(30)
                return self

        transport = ScriptedTransport(
            [
                ScriptedResponse(body=corrupt, fail_after=RESUME_CUT),
                ScriptedResponse(
                    body=corrupt[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-range": f"bytes {RESUME_CUT}-{len(corrupt) - 1}/*"
                    },
                ),
                PausingResponse(),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_timeout = 0.03
        config.download_resume_attempts = 2

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://mirror.example/book", source=SourceType.LIBGEN
            )
            pytest.fail("the shared deadline must not admit another mirror")

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates, config=config
                )
            ),
        )
        with pytest.raises(ProviderResponseError) as caught:
            if outer:
                await python_bridge._fetch_from_source(
                    {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
                )
            else:
                await python_bridge._download_url_to_file(
                    "https://mirror.example/book",
                    str(tmp_path),
                    RESUME_DIGEST,
                    "libgen",
                    config=config,
                )
        assert caught.value.reason == "partial_transfer"
        assert transport.ranges == [
            None,
            f"bytes={RESUME_CUT}-",
            f"bytes={len(corrupt)}-",
        ]
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decoded_bytes", [False, True], ids=["empty", "partial"])
    @pytest.mark.parametrize("recover", [False, True], ids=["exhausted", "recovered"])
    async def test_encoded_decoding_error_restarts_without_decoded_offsets(
        self, decoded_bytes, recover, tmp_path, mocker
    ):
        """Corrupt gzip bodies restart at zero within the same attempt budget."""
        import gzip

        body = RESUME_BODY * 10
        compressed = gzip.compress(body)
        requests = []

        class CorruptGzip(httpx.AsyncByteStream):
            async def __aiter__(self):
                if decoded_bytes:
                    yield compressed[:-8]
                    yield b"\x00" * 8  # Invalid CRC after decoded bytes arrived.
                else:
                    yield b"this is not a gzip header"

        def handler(request):
            requests.append(request.headers.get("Range"))
            if recover and len(requests) == 2:
                return httpx.Response(200, content=body)
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=CorruptGzip()
            )

        client_type = httpx.AsyncClient
        mocker.patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                **kwargs, transport=httpx.MockTransport(handler), trust_env=False
            ),
        )
        config = python_bridge.get_source_config()
        config.download_resume_attempts = 1
        operation = python_bridge._download_url_to_file(
            "https://mirror.example/book",
            str(tmp_path),
            hashlib.md5(body).hexdigest(),
            "libgen",
            config=config,
        )
        if recover:
            result = await operation
            assert Path(result).read_bytes() == body
            assert list(tmp_path.iterdir()) == [Path(result)]
        else:
            with pytest.raises(ProviderResponseError) as caught:
                await operation
            assert caught.value.reason == (
                "partial_transfer" if decoded_bytes else "protocol_error"
            )
            assert not list(tmp_path.iterdir())
        assert requests == [None, None]

    @pytest.mark.asyncio
    async def test_resume_http_error_keeps_exact_status_detail(self, tmp_path, mocker):
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=5000),
                ScriptedResponse(status_code=503),
                ScriptedResponse(status_code=503),
            ]
        ).install(mocker)
        with pytest.raises(ProviderResponseError) as caught:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
            )
        assert caught.value.reason == "http_error"
        assert caught.value.detail == "HTTP 503"
        assert transport.ranges == [None, "bytes=5000-", "bytes=5000-"]
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attempts", [1, 2])
    async def test_short_206_continues_until_complete_or_budget_ends(
        self, attempts, tmp_path, mocker
    ):
        """A valid 5000-5999/20000 slice is incomplete, not corrupt."""
        body = b"%PDF-1.7\n" + b"x" * 19991
        digest = hashlib.md5(body).hexdigest()
        responses = [
            ScriptedResponse(body=body, fail_after=5000),
            ScriptedResponse(
                body=body[5000:6000],
                status_code=206,
                headers={"content-range": "bytes 5000-5999/20000"},
            ),
        ]
        if attempts == 2:
            responses.append(
                ScriptedResponse(
                    body=body[6000:],
                    status_code=206,
                    headers={"content-range": "bytes 6000-19999/20000"},
                )
            )
        transport = ScriptedTransport(responses).install(mocker)
        config = python_bridge.get_source_config()
        config.download_resume_attempts = attempts
        operation = python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen", config=config
        )
        if attempts == 2:
            result = await operation
            assert Path(result).read_bytes() == body
            assert transport.ranges == [None, "bytes=5000-", "bytes=6000-"]
        else:
            with pytest.raises(ProviderResponseError) as caught:
                await operation
            assert caught.value.reason == "partial_transfer"
            assert "bytes_received=6000 resumes_tried=1" in caught.value.detail
            assert transport.ranges == [None, "bytes=5000-"]
            assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outer", [False, True], ids=["direct", "provider-chain"])
    async def test_short_206_deadline_reports_partial_transfer(
        self, outer, tmp_path, mocker
    ):
        class PausedResponse(ScriptedResponse):
            async def __aenter__(self):
                await asyncio.sleep(30)

        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=5000),
                ScriptedResponse(
                    body=RESUME_BODY[5000:6000],
                    status_code=206,
                    headers={"content-range": f"bytes 5000-5999/{len(RESUME_BODY)}"},
                ),
                PausedResponse(),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_timeout = 0.03

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://mirror.example/get", source=SourceType.LIBGEN
            )
            pytest.fail("deadline exhaustion must not start another mirror")

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates,
                    config=config,
                )
            ),
        )
        with pytest.raises(ProviderResponseError) as caught:
            if outer:
                await python_bridge._fetch_from_source(
                    {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
                )
            else:
                await python_bridge._download_url_to_file(
                    "https://mirror.example/get",
                    str(tmp_path),
                    RESUME_DIGEST,
                    "libgen",
                    config=config,
                )
        assert caught.value.reason == "partial_transfer"
        assert "bytes_received=6000 resumes_tried=2" in caught.value.detail
        assert transport.ranges == [None, "bytes=5000-", "bytes=6000-"]
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    async def test_stream_close_failure_never_leaves_a_published_file(
        self, tmp_path, mocker
    ):
        class CloseFailure(ScriptedResponse):
            async def __aexit__(self, *_args):
                raise httpx.ReadError("offline stream close failure")

        ScriptedTransport([CloseFailure(body=RESUME_BODY)]).install(mocker)
        config = python_bridge.get_source_config()
        config.download_resume_attempts = 0
        with pytest.raises(ProviderResponseError) as caught:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get",
                str(tmp_path),
                RESUME_DIGEST,
                "libgen",
                config=config,
            )
        assert caught.value.reason == "partial_transfer"
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume_status", [503, 206])
    async def test_real_httpx_redirected_resume_keeps_status_and_declared_length(
        self, resume_status, tmp_path, mocker
    ):
        """Real httpx follows each redirect and sends Range to the CDN."""
        body = RESUME_BODY * 10
        digest = hashlib.md5(body).hexdigest()
        requests = []

        class DroppedBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body[:65536]
                raise httpx.RemoteProtocolError("offline dropped body")

        def handler(request):
            requests.append((request.url.host, request.headers.get("Range")))
            if request.url.host == "mirror.example":
                return httpx.Response(
                    302, headers={"Location": "https://cdn.example/book"}
                )
            if request.headers.get("Range") is None:
                return httpx.Response(
                    200,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Length": str(len(body)),
                    },
                    stream=DroppedBody(),
                )
            if resume_status == 503:
                return httpx.Response(503, headers={"Content-Length": "0"})
            start = 65536 if request.headers["Range"] == "bytes=65536-" else 66536
            end = 66536 if start == 65536 else len(body)
            return httpx.Response(
                206,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(end - start),
                    "Content-Range": f"bytes {start}-{end - 1}/{len(body)}",
                },
                content=body[start:end],
            )

        client_type = httpx.AsyncClient
        mocker.patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                **kwargs, transport=httpx.MockTransport(handler), trust_env=False
            ),
        )
        operation = python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )
        expected_requests = [
            ("mirror.example", None),
            ("cdn.example", None),
            ("mirror.example", "bytes=65536-"),
            ("cdn.example", "bytes=65536-"),
        ]
        if resume_status == 503:
            expected_requests.extend(
                [
                    ("mirror.example", "bytes=65536-"),
                    ("cdn.example", "bytes=65536-"),
                ]
            )
            with pytest.raises(ProviderResponseError) as caught:
                await operation
            assert caught.value.detail == "HTTP 503"
            assert caught.value.host == "cdn.example"
            assert not list(tmp_path.iterdir())
        else:
            result = await operation
            assert Path(result).read_bytes() == body
            expected_requests.extend(
                [
                    ("mirror.example", "bytes=66536-"),
                    ("cdn.example", "bytes=66536-"),
                ]
            )
        assert requests == expected_requests

    @pytest.mark.asyncio
    async def test_dropped_transfer_resumes_from_the_staged_offset(
        self, tmp_path, mocker
    ):
        """The retry asks for the remainder and the file is assembled whole."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                    fail_after=RESUME_CUT,
                ),
                ScriptedResponse(
                    body=RESUME_BODY[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-type": "application/pdf",
                        "content-range": content_range(RESUME_CUT, len(RESUME_BODY)),
                    },
                ),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert Path(result).read_bytes() == RESUME_BODY
        assert Path(result).stat().st_size == len(RESUME_BODY)
        assert not list(tmp_path.glob(".source-*.part"))

    @pytest.mark.asyncio
    async def test_truncated_body_against_declared_length_resumes(
        self, tmp_path, mocker
    ):
        """A body that ends early WITHOUT erroring is still a partial transfer."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY[:RESUME_CUT],
                    headers={
                        "content-type": "application/pdf",
                        "content-length": str(len(RESUME_BODY)),
                    },
                ),
                ScriptedResponse(
                    body=RESUME_BODY[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-type": "application/pdf",
                        "content-range": content_range(RESUME_CUT, len(RESUME_BODY)),
                    },
                ),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert Path(result).read_bytes() == RESUME_BODY

    @pytest.mark.asyncio
    async def test_server_that_ignores_range_restarts_rather_than_appends(
        self, tmp_path, mocker
    ):
        """A 200 answer to a Range request is the whole file, not a remainder."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                    fail_after=RESUME_CUT,
                ),
                ScriptedResponse(
                    body=RESUME_BODY,
                    status_code=200,
                    headers={"content-type": "application/pdf"},
                ),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert Path(result).read_bytes() == RESUME_BODY
        assert Path(result).stat().st_size == len(RESUME_BODY)

    @pytest.mark.asyncio
    async def test_resume_that_returns_a_different_file_is_rejected(
        self, tmp_path, mocker
    ):
        """Concatenating someone else's bytes must fail, never report success."""
        other = b"%PDF-1.7 an entirely different book" * 400
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                    fail_after=RESUME_CUT,
                ),
                ScriptedResponse(
                    body=other[RESUME_CUT:],
                    status_code=206,
                    headers={
                        "content-type": "application/pdf",
                        "content-range": content_range(RESUME_CUT, len(other)),
                    },
                ),
            ]
        ).install(mocker)

        with pytest.raises(ProviderResponseError) as excinfo:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
            )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert excinfo.value.reason == "integrity_mismatch"
        # The spliced file is destroyed rather than parked: a later attempt
        # resuming from it would inherit the corruption.
        assert not list(tmp_path.glob(".source-*"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("headers", "why"),
        [
            ({}, "no Content-Range at all"),
            ({"content-range": "bytes 0-99/100"}, "a range starting somewhere else"),
            (
                {
                    "content-range": content_range(RESUME_CUT, len(RESUME_BODY)),
                    "content-encoding": "gzip",
                },
                "an encoded body whose offsets are not the staged ones",
            ),
        ],
    )
    async def test_resume_rejects_a_206_that_does_not_continue_the_partial(
        self, headers, why, tmp_path, mocker
    ):
        """A 206 the staged bytes cannot be appended to is a failure, not a guess."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                    fail_after=RESUME_CUT,
                ),
                ScriptedResponse(
                    body=RESUME_BODY[RESUME_CUT:],
                    status_code=206,
                    headers={"content-type": "application/pdf", **headers},
                ),
            ]
        ).install(mocker)

        with pytest.raises(ProviderResponseError) as excinfo:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
            )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"], why
        assert excinfo.value.reason == "protocol_error", why
        assert not list(tmp_path.glob(".source-*")), why

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interruption", ["reset", "wrong-length"])
    async def test_unsatisfiable_range_at_true_eof_publishes_complete_stage(
        self, interruption, tmp_path, mocker
    ):
        """A final-frame reset must not discard a digest-matching complete file."""
        headers = {"content-type": "application/pdf"}
        if interruption == "wrong-length":
            headers["content-length"] = str(len(RESUME_BODY) + 1)
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers=headers,
                    fail_after=len(RESUME_BODY) if interruption == "reset" else None,
                ),
                ScriptedResponse(
                    status_code=416,
                    headers={"content-range": f"bytes */{len(RESUME_BODY)}"},
                ),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_resume_attempts = 1  # No spare request after the 416.

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get",
            str(tmp_path),
            RESUME_DIGEST,
            "libgen",
            config=config,
        )

        assert transport.ranges == [None, f"bytes={len(RESUME_BODY)}-"]
        assert Path(result).read_bytes() == RESUME_BODY
        assert Path(result).suffix == ".pdf"
        assert list(tmp_path.iterdir()) == [Path(result)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_range_header",
        [None, f"bytes */{len(RESUME_BODY) + 1}", "bytes */bogus", "bytes */*"],
        ids=["missing", "different-total", "malformed", "unknown-total"],
    )
    async def test_unsatisfiable_range_without_matching_total_restarts_complete_stage(
        self, content_range_header, tmp_path, mocker
    ):
        """Even a valid digest cannot rescue a missing or nonmatching EOF total."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=len(RESUME_BODY)),
                ScriptedResponse(
                    status_code=416,
                    headers={"content-range": content_range_header}
                    if content_range_header is not None
                    else {},
                ),
                ScriptedResponse(body=RESUME_BODY),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={len(RESUME_BODY)}-", None]
        assert Path(result).read_bytes() == RESUME_BODY
        assert list(tmp_path.iterdir()) == [Path(result)]

    @pytest.mark.asyncio
    async def test_unsatisfiable_range_at_true_eof_with_wrong_digest_restarts(
        self, tmp_path, mocker
    ):
        """An equal byte count must not publish corrupt staged content."""
        corrupt = RESUME_BODY[:-1] + bytes([RESUME_BODY[-1] ^ 1])
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=corrupt, fail_after=len(corrupt)),
                ScriptedResponse(
                    status_code=416,
                    headers={"content-range": f"bytes */{len(corrupt)}"},
                ),
                ScriptedResponse(body=RESUME_BODY),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={len(corrupt)}-", None]
        assert Path(result).read_bytes() == RESUME_BODY
        assert list(tmp_path.iterdir()) == [Path(result)]

    @pytest.mark.asyncio
    async def test_unsatisfiable_range_discards_the_partial_and_starts_over(
        self, tmp_path, mocker
    ):
        """A partial the server will not resume must not become a permanent trap."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                    fail_after=RESUME_CUT,
                ),
                ScriptedResponse(status_code=416, headers={}),
                ScriptedResponse(
                    body=RESUME_BODY,
                    headers={"content-type": "application/pdf"},
                ),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), RESUME_DIGEST, "libgen"
        )

        assert transport.ranges == [None, f"bytes={RESUME_CUT}-", None]
        assert Path(result).read_bytes() == RESUME_BODY

    @pytest.mark.asyncio
    async def test_large_transfer_resumes_over_one_mib(self, tmp_path, mocker):
        """Recover a multi-block transfer under the shipped retry budget."""
        body = b"%PDF-1.7 " + bytes(range(256)) * (3 << 12)
        digest = hashlib.md5(body).hexdigest()
        cut = 3 << 19  # 1.5 MiB prefix of a >3 MiB file

        transport = ScriptedTransport(
            [
                ScriptedResponse(
                    body=body,
                    headers={"content-type": "application/pdf"},
                    fail_after=cut,
                ),
                ScriptedResponse(
                    body=body[cut:],
                    status_code=206,
                    headers={
                        "content-type": "application/pdf",
                        "content-range": content_range(cut, len(body)),
                    },
                ),
            ]
        ).install(mocker)

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), digest, "libgen"
        )

        assert transport.ranges == [None, f"bytes={cut}-"]
        assert Path(result).stat().st_size == len(body)
        assert hashlib.md5(Path(result).read_bytes()).hexdigest() == digest

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outer", [False, True], ids=["direct", "provider-chain"])
    async def test_deadline_expires_inside_resume_without_another_mirror(
        self, outer, tmp_path, mocker
    ):
        """A fresh timer per resume would let this stream outlive the call."""

        class SlowSuffix(ScriptedResponse):
            async def aiter_bytes(self, _chunk_size=65536):
                await asyncio.sleep(0.015)
                yield b"suffix"
                await asyncio.sleep(1)

        first = ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT)
        transport = ScriptedTransport(
            [
                first,
                SlowSuffix(
                    status_code=206,
                    headers={
                        "content-range": content_range(RESUME_CUT, len(RESUME_BODY))
                    },
                ),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_timeout = 0.05
        mocker.patch("python_bridge.get_source_config", return_value=config)
        started = []

        async def candidates(_md5, source):
            for host in ("first.example", "second.example"):
                started.append(host)
                yield DownloadResult(
                    url=f"https://{host}/book", source=SourceType.LIBGEN
                )

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates, config=config
                )
            ),
        )
        with pytest.raises(ProviderResponseError) as caught:
            if outer:
                await python_bridge._fetch_from_source(
                    {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
                )
            else:
                await python_bridge._download_url_to_file(
                    "https://first.example/book", str(tmp_path), RESUME_DIGEST, "libgen"
                )
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert caught.value.reason == "partial_transfer"
        assert "partial transfer" in caught.value.detail
        assert f"bytes_received={RESUME_CUT + 6}" in caught.value.detail
        assert "resumes_tried=1" in caught.value.detail
        assert not list(tmp_path.iterdir())
        if outer:
            assert started == ["first.example"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "configured, retries",
        [
            (None, 2),
            ("0", 0),
            ("1", 1),
            ("bad", 2),
            ("-1", 2),
            ("", 2),
            (" 3 ", 3),
            ("1000000", 10),
        ],
    )
    async def test_retry_budget_and_partial_diagnosis_reach_mirror_detail(
        self, configured, retries, tmp_path, mocker, monkeypatch
    ):
        """Removing the config bound would issue unscripted requests indefinitely."""
        if configured is None:
            monkeypatch.delenv("BOOK_SOURCE_DOWNLOAD_RESUME_ATTEMPTS", raising=False)
        else:
            monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_RESUME_ATTEMPTS", configured)
        responses = [ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT)]
        offset = RESUME_CUT
        for _ in range(retries):
            responses.append(
                ScriptedResponse(
                    body=RESUME_BODY[offset:],
                    fail_after=0,
                    status_code=206,
                    headers={"content-range": content_range(offset, len(RESUME_BODY))},
                )
            )
        transport = ScriptedTransport(responses).install(mocker)

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://mirror.example/book", source=SourceType.LIBGEN
            )

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        with pytest.raises(AllSourcesFailedError) as caught:
            await python_bridge._fetch_from_source(
                {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
            )
        failure = caught.value.to_dict()["failures"][0]
        assert len(transport.requests) == retries + 1
        assert failure["reason"] == "partial_transfer"
        assert "partial transfer" in failure["detail"]
        assert f"bytes_received={offset}" in failure["detail"]
        assert f"resumes_tried={retries}" in failure["detail"]
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    async def test_prefix_is_deleted_before_next_mirror_and_next_call(
        self, tmp_path, mocker
    ):
        """Same-call recovery must not leave a resumable prefix for another owner."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT),
                ScriptedResponse(
                    status_code=206,
                    headers={
                        "content-range": content_range(RESUME_CUT, len(RESUME_BODY))
                    },
                    body=RESUME_BODY[RESUME_CUT:],
                    fail_after=0,
                ),
                ScriptedResponse(body=RESUME_BODY),
                ScriptedResponse(body=RESUME_BODY),
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_resume_attempts = 1

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://first.example/book", source=SourceType.LIBGEN
            )
            assert not list(tmp_path.glob(".source-*"))
            yield DownloadResult(
                url="https://second.example/book", source=SourceType.LIBGEN
            )

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates, config=config
                )
            ),
        )
        first = await python_bridge._fetch_from_source(
            {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
        )
        second = await python_bridge._download_url_to_file(
            "https://first.example/book",
            str(tmp_path),
            RESUME_DIGEST,
            "libgen",
            config=config,
        )
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-", None, None]
        assert transport.requests[0][0] == transport.requests[1][0]
        assert Path(first).read_bytes() == Path(second).read_bytes() == RESUME_BODY
        assert not list(tmp_path.glob("*.part"))

    @pytest.mark.asyncio
    async def test_cancellation_mid_resume_deletes_only_owned_stage(
        self, tmp_path, mocker
    ):
        """Swallowing cancellation in the retry loop would fetch another suffix."""
        suffix_started = asyncio.Event()

        class PausedSuffix(ScriptedResponse):
            async def aiter_bytes(self, _chunk_size=65536):
                yield b"suffix"
                suffix_started.set()
                await asyncio.sleep(30)

        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT),
                PausedSuffix(
                    status_code=206,
                    headers={
                        "content-range": content_range(RESUME_CUT, len(RESUME_BODY))
                    },
                ),
            ]
        ).install(mocker)
        unrelated = tmp_path / "unrelated.part"
        unrelated.write_bytes(b"keep")
        task = asyncio.create_task(
            python_bridge._download_url_to_file(
                "https://mirror.example/book", str(tmp_path), RESUME_DIGEST, "libgen"
            )
        )
        try:
            await asyncio.wait_for(suffix_started.wait(), 0.2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert list(tmp_path.iterdir()) == [unrelated]
        assert unrelated.read_bytes() == b"keep"

    @pytest.mark.asyncio
    async def test_next_resolution_timeout_does_not_inherit_previous_progress(
        self, tmp_path, mocker
    ):
        """The terminal resolution failure must not claim bytes from another attempt."""
        transport = ScriptedTransport(
            [
                ScriptedResponse(body=RESUME_BODY, fail_after=RESUME_CUT),
                ScriptedResponse(
                    status_code=206
                ),  # invalid continuation; rejects before append
            ]
        ).install(mocker)
        config = python_bridge.get_source_config()
        config.download_timeout = 0.04

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://first.example/book", source=SourceType.LIBGEN
            )
            await asyncio.sleep(1)

        mocker.patch(
            "python_bridge.get_source_router",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    iter_download_candidates=candidates, config=config
                )
            ),
        )
        with pytest.raises(AllSourcesFailedError) as caught:
            await python_bridge._fetch_from_source(
                {"md5": RESUME_DIGEST, "source": "libgen"}, str(tmp_path)
            )
        assert transport.ranges == [None, f"bytes={RESUME_CUT}-"]
        assert "partial transfer" in caught.value.failures[0].detail
        assert "partial transfer" not in caught.value.failures[-1].detail
        assert caught.value.failures[-1].reason == "read_timeout"
        assert not list(tmp_path.iterdir())
