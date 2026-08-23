"""TDD tests for Anna's Archive adapter.

Tests cover:
1. Search functionality via HTML scraping
2. Fast download API with domain_index=1
3. Quota tracking from API responses
4. Error handling for invalid inputs
"""

import asyncio
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from unittest.mock import AsyncMock, MagicMock, patch

from lib.sources.models import QuotaInfo, SourceType
from lib.sources.errors import ProviderConfigurationError, ProviderResponseError

pytestmark = pytest.mark.unit

# Test data: mocked HTML response for search
MOCK_SEARCH_HTML = """
<!DOCTYPE html>
<html>
<head><title>Search - Anna's Archive</title></head>
<body>
<div class="search-results">
  <a href="/md5/abc123def456">Python Programming Guide</a>
  <a href="/md5/abc123def456">Python Programming Guide</a>  <!-- duplicate -->
  <a href="/md5/789xyz000111">Learning Python</a>
  <a href="/md5/deadbeef1234">Advanced Python Techniques</a>
</div>
</body>
</html>
"""

# Test data: mocked API response for fast download
MOCK_FAST_DOWNLOAD_RESPONSE = {
    "download_url": "http://partner.example.com/download/abc123def456.pdf",
    "account_fast_download_info": {
        "downloads_left": 23,
        "downloads_per_day": 25,
        "downloads_done_today": 2,
    },
}

# Test data: API error response
MOCK_ERROR_RESPONSE = {
    "error": "Invalid MD5 hash",
}


class TestAnnasArchiveSearch:
    """Test cases for Anna's Archive search functionality."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """Search should return UnifiedBookResult list with source=ANNAS_ARCHIVE."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig
        from lib.sources.models import SourceType

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.text = MOCK_SEARCH_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            results = await adapter.search("python programming")

        # Verify results
        assert len(results) == 3  # 3 unique MD5s (one duplicate removed)
        assert all(r.source == SourceType.ANNAS_ARCHIVE for r in results)
        assert results[0].md5 == "abc123def456"
        assert results[0].title == "Python Programming Guide"

        await adapter.close()

    @pytest.mark.asyncio
    async def test_search_deduplicates_md5(self):
        """Search should deduplicate results by MD5 hash."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.text = MOCK_SEARCH_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            results = await adapter.search("python")

        # HTML has duplicate abc123def456, should be deduplicated
        md5_list = [r.md5 for r in results]
        assert len(md5_list) == len(set(md5_list)), "Results should be deduplicated"

        await adapter.close()

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_results(self):
        """Search with empty query should still work (returns recent books)."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.text = MOCK_SEARCH_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            results = await adapter.search("")

        # Should return whatever the page has
        assert isinstance(results, list)

        await adapter.close()

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        """Search with no matching results should return empty list."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        empty_html = """
        <!DOCTYPE html>
        <html><body><div>No results found</div></body></html>
        """

        mock_response = MagicMock()
        mock_response.text = empty_html
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            results = await adapter.search("nonexistent_book_xyz123")

        assert results == []

        await adapter.close()


class TestAnnasArchiveFastDownload:
    """Test cases for Anna's Archive fast download API."""

    @pytest.mark.asyncio
    async def test_get_download_url_returns_result(self):
        """get_download_url should return DownloadResult with working URL."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig
        from lib.sources.models import SourceType

        config = SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.json.return_value = MOCK_FAST_DOWNLOAD_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            result = await adapter.get_download_url("abc123def456")

        assert result.url == "http://partner.example.com/download/abc123def456.pdf"
        assert result.source == SourceType.ANNAS_ARCHIVE

        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_download_url_uses_domain_index_1(self):
        """CRITICAL: get_download_url must use domain_index=1 (not 0)."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.json.return_value = MOCK_FAST_DOWNLOAD_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            await adapter.get_download_url("abc123def456")

            # Verify domain_index=1 was used in the request
            call_args = mock_client.get.call_args
            params = call_args.kwargs.get("params", {})
            assert params.get("domain_index") == 1, "Must use domain_index=1"

        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_download_url_extracts_quota_info(self):
        """get_download_url should extract quota info from API response."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.json.return_value = MOCK_FAST_DOWNLOAD_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            result = await adapter.get_download_url("abc123def456")

        assert result.quota_info is not None
        assert result.quota_info.downloads_left == 23
        assert result.quota_info.downloads_per_day == 25
        assert result.quota_info.downloads_done_today == 2

        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_download_url_raises_on_api_error(self):
        """A JSON API error retains Anna's provider, host, and response reason."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.json.return_value = MOCK_ERROR_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            with pytest.raises(ProviderResponseError) as exc_info:
                await adapter.get_download_url("invalid_md5")

            assert exc_info.value.provider == "annas"
            assert exc_info.value.host == "annas-archive.gl"
            assert exc_info.value.reason == "http_error"
            assert "Invalid MD5 hash" in exc_info.value.detail

        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_download_url_raises_without_secret_key(self):
        """get_download_url should raise ValueError if no secret key configured."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="",  # No key
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        with pytest.raises(ValueError) as exc_info:
            await adapter.get_download_url("abc123def456")

        assert "ANNAS_SECRET_KEY" in str(exc_info.value)

        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_download_url_raises_on_missing_url(self):
        """A malformed success response retains typed protocol attribution."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        # Response without download_url
        mock_response = MagicMock()
        mock_response.json.return_value = {"some_other_field": "value"}
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            with pytest.raises(ProviderResponseError) as exc_info:
                await adapter.get_download_url("abc123def456")

            assert exc_info.value.provider == "annas"
            assert exc_info.value.host == "annas-archive.gl"
            assert exc_info.value.reason == "protocol_error"
            assert "download_url" in exc_info.value.detail.lower()

        await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], None], ids=["array", "null"])
    async def test_get_download_url_rejects_non_object_json(self, payload):
        """A 2xx JSON body must be an object before fields are inspected."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-secret-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            with pytest.raises(ProviderResponseError) as exc_info:
                await adapter.get_download_url("abc123def456")

        assert exc_info.value.provider == "annas"
        assert exc_info.value.host == "annas-archive.gl"
        assert exc_info.value.reason == "protocol_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "download_url",
        ["", "   ", 42, ["https://partner.example/book.pdf"]],
        ids=["empty", "whitespace", "number", "array"],
    )
    async def test_get_download_url_requires_non_empty_string(self, download_url):
        """Truthy non-string values cannot cross the DownloadResult boundary."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-secret-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {"download_url": download_url}
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            with pytest.raises(ProviderResponseError) as exc_info:
                await adapter.get_download_url("abc123def456")

        assert exc_info.value.provider == "annas"
        assert exc_info.value.host == "annas-archive.gl"
        assert exc_info.value.reason == "protocol_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "account_info",
        [None, [], "invalid"],
        ids=["null", "array", "string"],
    )
    async def test_get_download_url_rejects_non_object_account_info(self, account_info):
        """Present quota metadata must be an object before fields are read."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-secret-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "download_url": "https://partner.example/book.pdf",
            "account_fast_download_info": account_info,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            with pytest.raises(ProviderResponseError) as exc_info:
                await adapter.get_download_url("abc123def456")

        assert exc_info.value.provider == "annas"
        assert exc_info.value.host == "annas-archive.gl"
        assert exc_info.value.reason == "protocol_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "account_info,expected_quota",
        [
            ({}, None),
            ({"downloads_done_today": 2}, None),
            ({"downloads_per_day": 25, "downloads_done_today": 2}, None),
            (
                {
                    "downloads_left": 5,
                    "downloads_per_day": 25,
                    "downloads_done_today": 20,
                },
                QuotaInfo(
                    downloads_left=5, downloads_per_day=25, downloads_done_today=20
                ),
            ),
            (
                {
                    "downloads_left": 0,
                    "downloads_per_day": 25,
                    "downloads_done_today": 25,
                },
                QuotaInfo(
                    downloads_left=0, downloads_per_day=25, downloads_done_today=25
                ),
            ),
        ],
        ids=[
            "empty_dict",
            "partial_done_today",
            "partial_per_day_and_done",
            "full_quota_positive",
            "full_quota_zero",
        ],
    )
    async def test_get_download_url_handles_empty_or_partial_quota_info(
        self, account_info, expected_quota
    ):
        """Empty or partial quota info with valid download_url should not default to 0."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig
        from lib.sources.models import SourceType

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-secret-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "download_url": "https://partner.example/book.pdf",
            "account_fast_download_info": account_info,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            result = await adapter.get_download_url("abc123def456")

        assert result.url == "https://partner.example/book.pdf"
        assert result.source == SourceType.ANNAS_ARCHIVE
        assert result.quota_info == expected_quota


class TestAnnasArchiveAdapterInterface:
    """Test that AnnasArchiveAdapter properly implements SourceAdapter interface."""

    def test_implements_source_adapter(self):
        """AnnasArchiveAdapter should implement SourceAdapter ABC."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.base import SourceAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        assert isinstance(adapter, SourceAdapter)

    @pytest.mark.asyncio
    async def test_close_cleans_up_client(self):
        """close() should clean up the HTTP client."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        # Force client creation
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            # Manually set the client to simulate usage
            adapter._client = mock_client

            await adapter.close()

            # Verify client was closed
            mock_client.aclose.assert_called_once()
            assert adapter._client is None


class TestQuotaExhaustedError:
    """Test cases for QuotaExhaustedError exception."""

    def test_quota_exhausted_error_exists(self):
        """QuotaExhaustedError should be importable from annas module."""
        from lib.sources.annas import QuotaExhaustedError

        error = QuotaExhaustedError("Quota exhausted")
        assert isinstance(error, Exception)
        assert str(error) == "Quota exhausted"


class TestSecretKeyHostAllowlist:
    """Regression tests for the secret-key host allowlist.

    Anna's Archive domains lapse and get re-registered by squatters
    (annas-archive.li became a Trellian parking page in 2026-03), and the
    fast-download API sends ANNAS_SECRET_KEY as a URL query parameter. The
    adapter must never attach the key to a host outside ANNAS_TRUSTED_HOSTS.
    """

    def test_default_base_url_host_is_allowlisted(self):
        """The shipped default must point at a host the key may be sent to."""
        from urllib.parse import urlsplit

        from lib.sources.config import ANNAS_TRUSTED_HOSTS, SourceConfig

        default_host = urlsplit(SourceConfig().annas_base_url).hostname
        assert default_host in ANNAS_TRUSTED_HOSTS

    def test_parked_former_default_is_not_allowlisted(self):
        """annas-archive.li is parked (Trellian/Above.com) — never trust it."""
        from lib.sources.config import ANNAS_TRUSTED_HOSTS

        assert "annas-archive.li" not in ANNAS_TRUSTED_HOSTS

    def test_impostor_host_is_not_allowlisted(self):
        """annas-archive.is impersonates the project — never trust it.

        Removed 2026-08-10. The discriminator is the endpoint this allowlist
        exists to authorize: /dyn/api/fast_download.json returns 401 on the
        genuine .gl (present, key rejected) and 404 on .is (absent). It also
        serves /books/{id} rather than /md5/ URLs, runs a Google Analytics
        property, and hosts a secret-key "recovery" form. Do not re-add it
        without reproducing the 401 on that endpoint.
        """
        from lib.sources.config import ANNAS_TRUSTED_HOSTS

        assert "annas-archive.is" not in ANNAS_TRUSTED_HOSTS

    @pytest.mark.asyncio
    async def test_key_not_sent_to_unknown_host(self):
        """get_download_url must refuse before any request leaves the process."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="super-secret-key",
            annas_base_url="https://annas-archive.li",  # parked squatter domain
        )
        adapter = AnnasArchiveAdapter(config)

        mock_client = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_client):
            with pytest.raises(ValueError, match="Refusing to send ANNAS_SECRET_KEY"):
                await adapter.get_download_url("abc123def456")

        # The key must never have been put on the wire.
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_key_sent_to_allowlisted_host(self):
        """Allowlisted hosts keep working (control case for the guard)."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.gl",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.json.return_value = MOCK_FAST_DOWNLOAD_RESPONSE
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            result = await adapter.get_download_url("abc123def456")

        assert result.url == MOCK_FAST_DOWNLOAD_RESPONSE["download_url"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_key_is_a_permanent_configuration_error(self, status):
        """Only auth failures from the trusted keyed endpoint are permanent."""
        import httpx

        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="rejected-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        request = httpx.Request(
            "GET", "https://annas-archive.gl/dyn/api/fast_download.json"
        )
        mock_client = AsyncMock()
        mock_client.get.return_value = httpx.Response(status, request=request)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            with pytest.raises(ProviderConfigurationError) as excinfo:
                await adapter.get_download_url("abc123def456")

        assert excinfo.value.provider == "annas"
        assert excinfo.value.host == "annas-archive.gl"
        assert excinfo.value.reason == "configuration_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "location"),
        [
            (401, "https://edge.example/challenge"),
            (403, "https://annas-archive.gl/login"),
        ],
    )
    async def test_redirected_off_endpoint_auth_status_is_not_key_rejection(
        self, status, location
    ):
        """Only the configured keyed endpoint can reject ANNAS_SECRET_KEY."""
        import httpx

        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-key",
                annas_base_url="https://annas-archive.gl",
            )
        )

        def handler(request):
            if request.url.path == "/dyn/api/fast_download.json":
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(status)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            with patch.object(adapter, "_get_client", return_value=client):
                with pytest.raises(ProviderResponseError) as excinfo:
                    await adapter.get_download_url("abc123def456")

        assert excinfo.value.provider == "annas"
        assert excinfo.value.reason == "http_error"

    @pytest.mark.asyncio
    async def test_non_auth_fast_download_status_keeps_http_classification(self):
        """A 503 is provider health evidence, not a rejected credential."""
        import httpx

        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig
        from lib.sources.errors import ProviderResponseError

        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key="test-key",
                annas_base_url="https://annas-archive.gl",
            )
        )
        request = httpx.Request(
            "GET", "https://annas-archive.gl/dyn/api/fast_download.json"
        )
        mock_client = AsyncMock()
        mock_client.get.return_value = httpx.Response(503, request=request)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            with pytest.raises(ProviderResponseError) as excinfo:
                await adapter.get_download_url("abc123def456")

        assert excinfo.value.reason == "http_error"

    @pytest.mark.asyncio
    async def test_search_still_allowed_on_unknown_host(self):
        """Plain search carries no key, so it is not restricted by the allowlist."""
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        config = SourceConfig(
            annas_secret_key="test-key",
            annas_base_url="https://annas-archive.li",
        )
        adapter = AnnasArchiveAdapter(config)

        mock_response = MagicMock()
        mock_response.text = MOCK_SEARCH_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            results = await adapter.search("python")

        assert len(results) == 3


class TestAnnasMetadataExtraction:
    """Extraction of full metadata from real Anna's search HTML (#78).

    These run against `test_files/annas/search_results.html`, trimmed from a
    live capture rather than hand-written, because the bug being fixed was
    caused by real markup that a hand-written mock did not reproduce: each
    record renders two /md5/ anchors, and deduping by first kept the cover
    image (empty text) over the title.
    """

    FIXTURE = (
        Path(__file__).resolve().parents[2]
        / "test_files"
        / "annas"
        / "search_results.html"
    )

    @pytest.fixture
    def results(self):
        """Drive the real AnnasArchiveAdapter.search() against the fixture.

        Deliberately NOT a reimplementation of search()'s filtering loop. An
        earlier version of this fixture called _build_result directly, which
        meant reverting the dedupe fix left these tests green — the headline
        regression was not pinned at all. The whole point is that search() is
        the thing under test.
        """
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        adapter = AnnasArchiveAdapter(
            SourceConfig(annas_base_url="https://annas-archive.gl")
        )

        mock_response = MagicMock()
        mock_response.text = self.FIXTURE.read_text()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client
            # asyncio.run creates AND closes its loop. The earlier
            # new_event_loop() here leaked one per fixture use, which trips
            # suites that promote ResourceWarning to an error.
            return asyncio.run(adapter.search("hegel"))

    def test_fixture_reproduces_the_two_anchor_markup(self):
        """Guard the guard.

        These tests are only meaningful if the fixture contains the cover +
        title anchor PAIR that caused the bug. A fixture holding just the title
        anchor passes against the broken implementation too, so assert the
        precondition rather than trusting it.
        """
        soup = BeautifulSoup(self.FIXTURE.read_text(), "html.parser")
        groups = {}
        for anchor in soup.select("a[href^='/md5/']"):
            groups.setdefault(anchor.get("href", "").split("/")[-1], []).append(anchor)

        assert len(groups) == 4
        for md5, anchors in groups.items():
            assert len(anchors) == 2, f"{md5} must render cover + title"
            texts = sorted(len(a.get_text(strip=True)) for a in anchors)
            assert texts[0] == 0, f"{md5} must have an empty-text cover anchor"
            assert texts[1] > 0, f"{md5} must have a text-bearing title anchor"

    def test_titles_are_extracted_not_unknown(self, results):
        """The headline bug: every keyless result came back titled 'Unknown'."""
        assert len(results) == 4
        assert all(r.title and r.title != "Unknown" for r in results)
        assert results[0].title == "Phenomenology of spirit"

    def test_full_metadata_is_extracted(self, results):
        """Option E: author, year, extension and size, not titles alone."""
        first = results[0]
        assert first.author == "Georg Wilhelm Friedrich Hegel"
        assert first.year == "1977"
        assert first.extension == "azw3"
        assert first.size == "0.6MB"

    def test_missing_year_does_not_corrupt_extension(self, results):
        """The regression this parser exists to prevent.

        Year is absent from ~9% of records. Parsing the metadata strip by
        position would shift the next segment into the year slot and the
        extension into the size slot. Record 2 has no year; every other field
        must still land correctly.
        """
        no_year = results[1]
        assert no_year.year == ""
        assert no_year.extension == "pdf"
        assert no_year.size == "6.7MB"
        assert no_year.title == "Phenomenology of Spirit"

    def test_year_is_never_mistaken_for_an_extension(self):
        """A bare [A-Z0-9]{2,5} extension pattern also matches a 4-digit year."""
        from lib.sources.annas import _parse_metadata_strip

        parsed = _parse_metadata_strip(
            "English [en] · PDF · 5.6MB · 2008 · 📕 Book (fiction)"
        )
        assert parsed["year"] == "2008"
        assert parsed["extension"] == "pdf"
        assert parsed["size"] == "5.6MB"
        assert parsed["language"] == "en"

    def test_segments_are_order_independent(self):
        """Order is not guaranteed, so parsing must not depend on it."""
        from lib.sources.annas import _parse_metadata_strip

        forward = _parse_metadata_strip("English [en] · EPUB · 1.2MB · 1999")
        shuffled = _parse_metadata_strip("1999 · 1.2MB · EPUB · English [en]")
        assert forward == shuffled

    def test_upstream_provenance_is_surfaced(self, results):
        """Anna's names which other sources hold the same file (#78).

        Verified to mean 'retrievable', not merely 'ingested from': 3/3 records
        marked /lgli resolved on LibGen, 0/2 unmarked did.
        """
        assert results[0].extra["also_available_on"] == ["lgli", "zlib"]
        assert "lgli" in results[2].extra["also_available_on"]

    def test_provenance_is_surfaced_but_not_acted_on(self, results):
        """Adapters expose provenance; they must not rank or dedupe on it.

        Reporting is #96 and cross-source dedup is #52. Source-comparison logic
        inside one source's adapter is what invariant 4 forbids.
        """
        for r in results:
            assert r.source == SourceType.ANNAS_ARCHIVE
            assert r.download_url == ""

    def test_absent_fields_are_empty_strings(self, results):
        """Matches UnifiedBookResult defaults and LibGen's behaviour."""
        for r in results:
            for field in (r.author, r.year, r.extension, r.size, r.download_url):
                assert isinstance(field, str)

    def test_pre_1500_years_are_preserved(self):
        """Incunabula and early-modern imprints carry dates before 1500.

        A 15xx-and-later floor silently dropped a year that was present
        upstream. This is a scholarly-text tool; those dates are real.
        """
        from lib.sources.annas import _parse_metadata_strip

        parsed = _parse_metadata_strip("Latin [la] · PDF · 12.0MB · 1492 · 📘 Book")
        assert parsed["year"] == "1492"
        assert parsed["extension"] == "pdf"

    def test_strip_without_size_is_still_parsed(self):
        """Every segment is optional — including the one used to find the strip.

        Recognising the strip by its size segment discarded the entire strip for
        records that omit size, losing language, extension, year and content
        type that were all present.
        """
        from bs4 import BeautifulSoup

        from lib.sources.annas import _find_metadata_strip, _parse_metadata_strip

        card = BeautifulSoup(
            "<div><div>English [en] · PDF · 2008 · 📕 Book (fiction)</div></div>",
            "html.parser",
        )
        strip = _find_metadata_strip(card)
        assert strip is not None, "strip must be found without a size segment"

        parsed = _parse_metadata_strip(strip)
        assert parsed["language"] == "en"
        assert parsed["extension"] == "pdf"
        assert parsed["year"] == "2008"
        assert "size" not in parsed

    def test_bare_year_is_not_exported_as_publisher(self, results):
        """The company-marked line degrades to a bare year on some records.

        A year is not a publisher, and exporting one puts junk into a field
        callers will render. Fixture record 7f9bce5f… is such a record.
        """
        for r in results:
            publisher = r.extra.get("publisher")
            if publisher is not None:
                assert not publisher.isdigit(), f"{r.md5}: bare year as publisher"

        joos = next(r for r in results if r.md5.startswith("7f9bce5f"))
        assert "publisher" not in joos.extra
        assert joos.author == "Joos."  # author itself is still extracted

    @pytest.mark.parametrize("value", ["1996", "0", "999", "20250"])
    def test_all_numeric_publishers_are_rejected(self, value):
        """No publisher is purely numeric, so reject the whole class.

        Gating on the metadata-year parser (1000-2099) let values outside that
        range through — a sentinel like "0" or a three-digit date would still be
        exported, contradicting the invariant the sibling test asserts.
        """
        from bs4 import BeautifulSoup

        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        html = (
            "<div><div>"
            '<a href="/md5/abc">A Title</a>'
            f'<a href="/search?q=x"><span class="icon-[mdi--company]"></span>{value}</a>'
            "</div><div>English [en] · PDF · 1.0MB</div></div>"
        )
        soup = BeautifulSoup(html, "html.parser")
        link = soup.select_one("a[href^='/md5/']")

        adapter = AnnasArchiveAdapter(SourceConfig())
        result = adapter._build_result(link, "abc", "A Title")

        assert "publisher" not in result.extra, f"{value!r} exported as publisher"

    def test_real_publisher_still_survives(self):
        """The guard must not throw out actual publishers."""
        from bs4 import BeautifulSoup

        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        html = (
            "<div><div>"
            '<a href="/md5/abc">A Title</a>'
            '<a href="/search?q=x"><span class="icon-[mdi--company]"></span>'
            "Oxford University Press, 1977</a>"
            "</div><div>English [en] · PDF · 1.0MB</div></div>"
        )
        link = BeautifulSoup(html, "html.parser").select_one("a[href^='/md5/']")
        result = AnnasArchiveAdapter(SourceConfig())._build_result(
            link, "abc", "A Title"
        )

        assert result.extra["publisher"] == "Oxford University Press, 1977"


class TestKeyedDownloadTracebacksCannotCarryTheKey:
    """The fast-download API takes ANNAS_SECRET_KEY as a URL query parameter.

    So `httpx` exceptions raised on that request carry the key inside
    `.request.url`, and chaining one as `__cause__` puts it into every
    formatted traceback — logs, crash reports, an error surfaced to an MCP
    client. The adapter's own message already says what went wrong.

    Recovered from the unlanded `fix/106-envelope-followup` stack (#137); the
    fix was written, verified, and never merged.
    """

    SECRET = "s3cr3t-key-that-must-not-appear"

    # A TRUSTED host, deliberately. `annas-archive.org` is absent from
    # ANNAS_TRUSTED_HOSTS, so the adapter refuses before `_fetch` is ever
    # called — and these tests then passed on that early
    # ProviderConfigurationError without reaching the raise sites they exist to
    # guard. Verified by reverting the three `from None` changes: all three
    # tests stayed green (Codex on #151). A security regression test that
    # passes against the unfixed code is worse than none.
    HOST = "annas-archive.gl"

    def _adapter(self):
        from lib.sources.annas import AnnasArchiveAdapter
        from lib.sources.config import SourceConfig

        return AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key=self.SECRET,
                annas_base_url="https://annas-archive.gl",
                preflight_enabled=False,
            )
        )

    @staticmethod
    def _formatted(exc: BaseException) -> str:
        import traceback

        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    @pytest.mark.asyncio
    async def test_an_http_error_does_not_chain_the_keyed_request(self, mocker):
        import httpx

        adapter = self._adapter()
        url = f"https://annas-archive.gl/dyn/api/fast_download.json?key={self.SECRET}"
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)

        mocker.patch.object(
            adapter,
            "_fetch",
            side_effect=httpx.HTTPStatusError(
                "403", request=request, response=response
            ),
        )
        mocker.patch.object(adapter, "_preflight", return_value=None)

        with pytest.raises(Exception) as excinfo:
            await adapter.get_download_url("a" * 32)

        assert excinfo.value.__cause__ is None, (
            "chaining the httpx error puts the key-bearing request URL into "
            "the traceback"
        )
        assert self.SECRET not in self._formatted(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_unexpected_error_does_not_chain_it_either(self, mocker):
        """The catch-all path is the one most likely to be forgotten."""
        import httpx

        adapter = self._adapter()
        url = f"https://annas-archive.gl/dyn/api/fast_download.json?key={self.SECRET}"
        request = httpx.Request("GET", url)

        mocker.patch.object(
            adapter,
            "_fetch",
            side_effect=httpx.ConnectError("boom", request=request),
        )
        mocker.patch.object(adapter, "_preflight", return_value=None)

        with pytest.raises(Exception) as excinfo:
            await adapter.get_download_url("a" * 32)

        assert excinfo.value.__cause__ is None
        assert self.SECRET not in self._formatted(excinfo.value)

    @pytest.mark.asyncio
    async def test_search_still_chains_its_cause(self, mocker):
        """Search sends no key, so it keeps the diagnostic chain.

        Suppressing causes everywhere would trade a real leak for a real loss
        of debuggability. The distinction is whether the request carried the
        secret, not whether the code path is an error path.
        """
        import httpx

        adapter = self._adapter()
        mocker.patch.object(adapter, "_preflight", return_value=None)
        mocker.patch.object(
            adapter,
            "_fetch",
            side_effect=httpx.ConnectError(
                "boom",
                request=httpx.Request(
                    "GET", "https://annas-archive.gl/search?q=anything"
                ),
            ),
        )

        with pytest.raises(Exception) as excinfo:
            await adapter.search("anything")

        assert excinfo.value.__cause__ is not None, (
            "the search request carries no secret, so its cause is pure "
            "diagnostic value and must not be suppressed along with the "
            "download path's"
        )
