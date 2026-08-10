"""TDD tests for Anna's Archive adapter.

Tests cover:
1. Search functionality via HTML scraping
2. Fast download API with domain_index=1
3. Quota tracking from API responses
4. Error handling for invalid inputs
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
        """get_download_url should raise exception on API error."""
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

            with pytest.raises(Exception) as exc_info:
                await adapter.get_download_url("invalid_md5")

            assert "Invalid MD5 hash" in str(exc_info.value)

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
        """get_download_url should raise exception if no download_url in response."""
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

            with pytest.raises(Exception) as exc_info:
                await adapter.get_download_url("abc123def456")

            assert "download_url" in str(exc_info.value).lower()

        await adapter.close()


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
