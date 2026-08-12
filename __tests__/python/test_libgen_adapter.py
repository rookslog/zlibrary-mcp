"""TDD tests for LibGen adapter.

Tests verify LibgenAdapter implements SourceAdapter interface correctly,
returning UnifiedBookResult with source=LIBGEN and wrapping sync
LibgenSearch calls in asyncio.to_thread().
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from lib.sources.config import SourceConfig
from lib.sources.models import SourceType

pytestmark = pytest.mark.unit


class TestLibgenAdapterSearch:
    """Tests for LibgenAdapter.search() method."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.fixture
    def mock_book(self):
        """Create a mock book result from LibgenSearch."""
        book = MagicMock()
        book.md5 = "abc123def456"
        book.title = "Python Programming"
        book.author = "John Doe"
        book.year = "2023"
        book.extension = "pdf"
        book.size = "5 MB"
        book.tor_download_link = "https://example.com/download/abc123def456"
        book.id = "12345"
        book.language = "English"
        book.pages = "500"
        return book

    @pytest.mark.asyncio
    async def test_search_returns_results(self, config, mock_book):
        """Search should return list of UnifiedBookResult with source=LIBGEN."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            results = await adapter.search("python")

            assert len(results) == 1
            assert results[0].md5 == "abc123def456"
            assert results[0].title == "Python Programming"
            assert results[0].author == "John Doe"
            assert results[0].source == SourceType.LIBGEN
            # Search deliberately carries no download URL: the only one it could
            # offer is a .onion needing Tor, and a clearnet key expires in under
            # 2.5h. Callers resolve on demand via get_download_url.
            assert results[0].download_url == ""

    @pytest.mark.asyncio
    async def test_search_empty_returns_empty_list(self, config):
        """Search with no results should return empty list."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = []
            mock_search_class.return_value = mock_instance

            results = await adapter.search("nonexistent_book_xyz123")

            assert results == []

    @pytest.mark.asyncio
    async def test_search_runs_under_a_wall_clock_budget(self, config, mock_book):
        """Search must go through run_bounded, not asyncio.to_thread.

        libgen_api_enhanced calls requests.get with no timeout, and
        asyncio.to_thread's workers are joined at interpreter exit — together
        that kept whole bridge processes alive for hours after their MCP call
        was gone. The blocking call has to run somewhere it can be abandoned.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            with patch("lib.sources.libgen.run_bounded") as mock_run_bounded:

                async def call_func(func, timeout, **kwargs):
                    call_func.timeout = timeout
                    return func()

                mock_run_bounded.side_effect = call_func

                await adapter.search("python")

                mock_run_bounded.assert_called_once()
                assert call_func.timeout == config.total_timeout

    @pytest.mark.asyncio
    async def test_search_does_not_use_asyncio_to_thread(self, config, mock_book):
        """asyncio.to_thread must not reappear in the search path.

        Regression guard for the orphaned-process bug: to_thread's pool threads
        are non-daemon, so an abandoned request keeps the interpreter alive.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            with patch("asyncio.to_thread") as mock_to_thread:
                await adapter.search("python")

            mock_to_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_handles_missing_attributes(self, config):
        """Search should handle books with missing attributes gracefully."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        # Book with minimal attributes
        minimal_book = MagicMock(spec=[])  # Empty spec means no attributes
        minimal_book.configure_mock(**{})

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = [minimal_book]
            mock_search_class.return_value = mock_instance

            results = await adapter.search("python")

            # Should not raise, should use empty defaults
            assert len(results) == 1
            assert results[0].md5 == ""
            assert results[0].title == ""
            assert results[0].source == SourceType.LIBGEN


MD5 = "abc123def456"
ADS_PAGE_WITH_KEY = (
    "<html><body><table><tr><td>"
    f'<a href="/get.php?md5={MD5}&key=TESTKEY123">GET</a>'
    "</td></tr></table></body></html>"
)
ADS_PAGE_NO_KEY = "<html><body><p>No download links here</p></body></html>"
PDF_BYTES = b"%PDF-1.6" + b"\x00" * 2040


class TestLibgenAdapterDownload:
    """Tests for LibgenAdapter.get_download_url().

    The adapter resolves downloads over HTTP against ads.php rather than
    through LibgenSearch, so these mock httpx transport — mocking LibgenSearch
    would leave the requests hitting the live service.
    """

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.fixture
    def adapter(self, config):
        """Adapter with rate limiting neutralised so tests do not sleep."""
        from lib.sources.libgen import LibgenAdapter

        instance = LibgenAdapter(config)
        instance.MIN_REQUEST_INTERVAL = 0
        return instance

    @staticmethod
    def _patched_client(handler):
        """Patch httpx.AsyncClient so the adapter's own client uses `handler`."""
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        return patch("lib.sources.libgen.httpx.AsyncClient", factory)

    @pytest.mark.asyncio
    async def test_resolves_key_from_ads_page(self, adapter):
        """A GET anchor's key becomes the get.php download URL."""

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url == f"https://libgen.li/get.php?md5={MD5}&key=TESTKEY123"
        assert result.source == SourceType.LIBGEN
        assert result.quota_info is None  # LibGen has no quota

    @pytest.mark.asyncio
    async def test_falls_over_when_a_mirror_errors(self, adapter):
        """A mirror that fails at the network level is skipped, not fatal."""
        seen = []

        def handler(request):
            host = request.url.host
            seen.append(host)
            if host == "libgen.li":
                raise httpx.ConnectError("TLS failure", request=request)
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")
        assert "libgen.li" in seen

    @pytest.mark.asyncio
    async def test_falls_over_when_mirror_resolves_but_cdn_is_dead(self, adapter):
        """Resolving a key is not evidence the CDN behind it can serve.

        Regression guard for the 2026-08-10 measurement: libgen.li handed out
        a valid key while its CDN node failed TLS.
        """

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if request.url.host == "libgen.li":
                return httpx.Response(
                    200,
                    text="<html>error</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")

    @pytest.mark.asyncio
    async def test_expired_key_bounce_is_treated_as_failure(self, adapter):
        """An expired key 307s back to ads.php rather than erroring."""

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if request.url.host == "libgen.li":
                return httpx.Response(
                    307, headers={"location": f"https://libgen.li/ads.php?md5={MD5}"}
                )
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")

    @pytest.mark.asyncio
    async def test_raises_when_no_mirror_yields_a_key(self, adapter):
        """DOM drift on every mirror surfaces as an error naming the attempts."""

        def handler(request):
            return httpx.Response(200, text=ADS_PAGE_NO_KEY)

        with self._patched_client(handler):
            with pytest.raises(ValueError, match="No LibGen mirror"):
                await adapter.get_download_url(MD5)

    @pytest.mark.asyncio
    async def test_tries_configured_mirror_first_without_duplicates(self, adapter):
        """Mirror order starts at the configured one and repeats none."""
        adapter.mirror = "vg"

        assert adapter._mirror_candidates() == ["vg", "li", "la"]

    @pytest.mark.asyncio
    async def test_resolution_walk_obeys_each_mirrors_total_deadline(self, adapter):
        """A trickling CDN must not defer failure to the 30-minute bridge budget."""
        adapter.config.total_timeout = 0.02

        async def trickle(*_args, **_kwargs):
            await asyncio.sleep(30)

        with (
            patch.object(adapter, "_preflight", new=AsyncMock(return_value=None)),
            patch.object(adapter, "_rate_limit", new=AsyncMock(return_value=None)),
            patch.object(adapter, "_resolve_key", new=AsyncMock(return_value="KEY")),
            patch.object(adapter, "_serves_bytes", side_effect=trickle),
        ):
            with pytest.raises(ValueError, match="No LibGen mirror"):
                await asyncio.wait_for(adapter.get_download_url(MD5), timeout=0.2)


class TestLibgenAdapterRateLimiting:
    """Tests for LibgenAdapter rate limiting."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.mark.asyncio
    async def test_rate_limiting_enforced(self, config):
        """Second request should wait for MIN_REQUEST_INTERVAL."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_title.return_value = []
            mock_search_class.return_value = mock_instance

            with patch("lib.sources.libgen.asyncio.sleep") as mock_sleep:
                # First request
                await adapter.search("python")

                # Force _last_request to be recent
                import time

                adapter._last_request = time.time()

                # Second request should trigger sleep
                await adapter.search("java")

                # Sleep should have been called
                mock_sleep.assert_called()


class TestLibgenAdapterInterface:
    """Tests verifying LibgenAdapter implements SourceAdapter interface."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    def test_implements_source_adapter(self, config):
        """LibgenAdapter should implement SourceAdapter ABC."""
        from lib.sources.base import SourceAdapter
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        assert isinstance(adapter, SourceAdapter)

    @pytest.mark.asyncio
    async def test_close_is_callable(self, config):
        """LibgenAdapter.close() should be callable without error."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        await adapter.close()  # Should not raise
