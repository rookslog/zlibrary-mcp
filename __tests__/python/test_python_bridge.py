# Tests for lib/python_bridge.py (EAPI-based)

import json
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
from lib.sources.errors import ProviderTimeoutError

# Import functions from the module under test
from python_bridge import (
    main,
    process_document,
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
    fetch_booklist_bridge,
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


@pytest.fixture
def mock_rag_process_document(mocker):
    """Mock the shared RAG orchestrator entry point used by the bridge."""
    return mocker.patch("lib.rag_processing.process_document", AsyncMock())


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
        node_src = (repo_root / "src" / "lib" / "zlibrary-api.ts").read_text()
        bridge_src = (repo_root / "lib" / "python_bridge.py").read_text()

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


# --- Tests for Bridge Functions (term, author, booklist) ---


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

    @pytest.mark.asyncio
    async def test_fetch_booklist_bridge(self, patch_eapi_client, mocker):
        """Should pass eapi_client to booklist_tools."""
        mock_booklist = AsyncMock(
            return_value={
                "booklist_id": "123",
                "books": [],
                "degraded": True,
                "topic": "test",
                "metadata": {},
                "page": 1,
                "booklist_hash": "abc",
            }
        )
        mocker.patch("lib.booklist_tools.fetch_booklist", mock_booklist)
        result = await fetch_booklist_bridge(
            booklist_id="123", booklist_hash="abc", topic="test"
        )
        assert result["degraded"] is True
        call_kwargs = mock_booklist.call_args[1]
        assert call_kwargs["eapi_client"] is patch_eapi_client


# --- Tests for Download Book (now uses EAPIClient.download_file) ---


class TestDownloadBook:
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
        ready.write_text('ready')
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
            assert partial.exists()

            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)
            stdout, _stderr = process.communicate()

            assert not partial.exists()
            assert stdout == ""
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_annas_transfer_uses_canonical_provider_name(self, tmp_path, mocker):
        """Transfer failures use `annas`, while success models retain their enum."""
        router = SimpleNamespace(
            get_download_url=AsyncMock(
                return_value=SimpleNamespace(
                    url="https://cdn.example/book",
                    source=SimpleNamespace(value="annas_archive"),
                )
            )
        )
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

        result = await python_bridge._download_url_to_file(
            "https://mirror.example/get", str(tmp_path), "slow-valid", "libgen"
        )

        assert Path(result).read_bytes() == b"%PDF slow but valid"

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

        with pytest.raises(ProviderTimeoutError) as excinfo:
            await python_bridge._download_url_to_file(
                "https://mirror.example/get",
                str(tmp_path),
                "over-budget",
                "libgen",
            )

        assert excinfo.value.reason == "read_timeout"
        assert excinfo.value.host == "cdn.example"
        assert not (tmp_path / "over-budget.download").exists()

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

        router = SimpleNamespace(
            get_download_url=AsyncMock(
                return_value=SimpleNamespace(
                    url="https://cdn.example/book", source="libgen"
                )
            ),
            close=AsyncMock(),
        )
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
        assert envelope["details"]["provider"] == "libgen"
        assert envelope["details"]["host"] == "cdn.example"
        assert envelope["details"]["reason"] == expected_reason
        assert envelope["details"]["operation"] == "download"
        assert not (tmp_path / "0123456789abcdef0123456789abcdef.download").exists()

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

        router = SimpleNamespace(
            get_download_url=AsyncMock(
                return_value=SimpleNamespace(
                    url="https://mirror.example/get", source="libgen"
                )
            ),
            close=AsyncMock(),
        )
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
        assert envelope["details"]["host"] == "cdn.example"
        assert envelope["details"]["reason"] == "read_timeout"
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
        mocker.patch(
            "python_bridge.create_unified_filename", return_value=expected_filename
        )
        mocker.patch("os.rename")
        mocker.patch("pathlib.Path.exists", return_value=True)
        mocker.patch("python_bridge.process_document", AsyncMock())

        result = await download_book(
            book_details=book_details_mock,
            output_dir=output_dir_mock,
            process_for_rag=False,
        )

        assert result["file_path"] == expected_final_path
        assert result.get("processed_file_path") is None

    @pytest.mark.asyncio
    async def test_download_book_with_rag(
        self, mock_eapi_download, tmp_path, mocker, patch_eapi_client
    ):
        """Tests download_book with RAG processing."""
        book_details_mock = {
            "id": "988",
            "extension": "txt",
            "name": "RAG Test Book",
            "author": "RAG Author",
            "url": "https://example.com/book/988/test",
            "hash": "raghash123",
        }
        output_dir_mock = str(tmp_path / "downloads_rag")
        original_path = str(Path(output_dir_mock) / "988.txt")
        expected_filename = "AuthorRAG_RAG_Test_Book_988.txt"
        expected_final_path = str(Path(output_dir_mock) / expected_filename)
        processed_path = expected_final_path + ".processed.md"

        mock_eapi_download.download_file.return_value = original_path
        mocker.patch(
            "python_bridge.create_unified_filename", return_value=expected_filename
        )
        mocker.patch("os.rename")
        mocker.patch("pathlib.Path.exists", return_value=True)
        mocker.patch(
            "python_bridge.process_document",
            AsyncMock(
                return_value={
                    "processed_file_path": processed_path,
                    "metadata_file_path": processed_path + ".metadata.json",
                    "content_types_produced": ["body", "metadata"],
                    "output_files": {
                        "body": processed_path,
                        "metadata": processed_path + ".metadata.json",
                    },
                }
            ),
        )

        result = await download_book(
            book_details=book_details_mock,
            output_dir=output_dir_mock,
            process_for_rag=True,
            processed_output_format="markdown",
        )

        assert result["file_path"] == expected_final_path
        assert result["processed_file_path"] == processed_path
        assert result["metadata_file_path"] == processed_path + ".metadata.json"
        assert result["output_files"]["body"] == processed_path

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


# --- Tests for process_document (unchanged — local file processing) ---


@pytest.mark.asyncio
async def test_process_document_epub_success(
    tmp_path, mocker, mock_rag_process_document
):
    epub_path = tmp_path / "test.epub"
    epub_path.touch()
    expected_result = {
        "processed_file_path": "/tmp/test.epub.processed.txt",
        "metadata_file_path": "/tmp/test.epub.metadata.json",
        "stats": {"word_count": 5, "char_count": 34, "format": "txt"},
        "content_types_produced": ["body"],
        "output_files": {
            "body": "/tmp/test.epub.processed.txt",
            "metadata": "/tmp/test.epub.metadata.json",
        },
    }
    mock_rag_process_document.return_value = expected_result

    result = await process_document(
        str(epub_path), book_id=None, author=None, title=None
    )

    mock_rag_process_document.assert_called_once_with(
        file_path_str=str(epub_path),
        output_format="txt",
        book_details=None,
    )
    assert result == expected_result


@pytest.mark.asyncio
async def test_process_document_epub_read_error(
    tmp_path, mocker, mock_rag_process_document
):
    epub_path = tmp_path / "test.epub"
    epub_path.touch()
    mock_rag_process_document.side_effect = Exception("EPUB read failed")

    with pytest.raises(
        RuntimeError, match=r"Error processing document .*test\.epub: EPUB read failed"
    ):
        await process_document(str(epub_path), book_id=None, author=None, title=None)
    mock_rag_process_document.assert_called_once()


@pytest.mark.asyncio
async def test_process_document_txt_utf8(tmp_path, mocker, mock_rag_process_document):
    txt_path = tmp_path / "test_utf8.txt"
    content = "This is a UTF-8 file.\nWith multiple lines.\nAnd special chars."
    txt_path.write_text(content, encoding="utf-8")
    expected_result = {
        "processed_file_path": "/tmp/test_utf8.txt.processed.txt",
        "metadata_file_path": "/tmp/test_utf8.txt.metadata.json",
        "stats": {"word_count": 12, "char_count": len(content), "format": "txt"},
        "content_types_produced": ["body"],
        "output_files": {
            "body": "/tmp/test_utf8.txt.processed.txt",
            "metadata": "/tmp/test_utf8.txt.metadata.json",
        },
    }
    mock_rag_process_document.return_value = expected_result

    result = await process_document(
        str(txt_path), book_id=None, author=None, title=None
    )

    mock_rag_process_document.assert_called_once_with(
        file_path_str=str(txt_path),
        output_format="txt",
        book_details=None,
    )
    assert result == expected_result


@pytest.mark.asyncio
async def test_process_document_pdf_success(
    tmp_path, mocker, mock_rag_process_document
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.touch()
    expected_result = {
        "processed_file_path": "/tmp/sample.pdf.processed.markdown",
        "metadata_file_path": "/tmp/sample.pdf.metadata.json",
        "footnotes_file_path": "/tmp/sample.pdf.processed_footnotes.markdown",
        "stats": {"word_count": 4, "char_count": 24, "format": "markdown"},
        "content_types_produced": ["body", "footnotes"],
        "output_files": {
            "body": "/tmp/sample.pdf.processed.markdown",
            "metadata": "/tmp/sample.pdf.metadata.json",
            "footnotes": "/tmp/sample.pdf.processed_footnotes.markdown",
        },
    }
    mock_rag_process_document.return_value = expected_result

    result = await process_document(
        str(pdf_path),
        output_format="markdown",
        book_id="123",
        author="Test Author",
        title="Test Title",
    )

    mock_rag_process_document.assert_called_once_with(
        file_path_str=str(pdf_path),
        output_format="markdown",
        book_details={"id": "123", "author": "Test Author", "title": "Test Title"},
    )
    assert result == expected_result


@pytest.mark.asyncio
async def test_process_document_pdf_encrypted(
    tmp_path, mocker, mock_rag_process_document
):
    pdf_path = tmp_path / "encrypted.pdf"
    pdf_path.touch()
    mock_rag_process_document.side_effect = ValueError("PDF is encrypted")

    with pytest.raises(
        RuntimeError,
        match=r"Error processing document .*encrypted\.pdf: PDF is encrypted",
    ):
        await process_document(str(pdf_path), book_id=None, author=None, title=None)
    mock_rag_process_document.assert_called_once()
