"""Tests for SourceRouter with fallback logic.

Tests routing behavior, fallback on failure/quota exhaustion, and source selection.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from lib.sources.router import SourceRouter
from lib.sources.base import SourceAdapter
from lib.sources.config import SourceConfig
from lib.sources.models import DownloadResult, QuotaInfo, SourceType, UnifiedBookResult
from lib.sources.annas import QuotaExhaustedError
from lib.sources.errors import AllSourcesFailedError, SourceError

pytestmark = pytest.mark.unit


@pytest.fixture
def config_with_annas():
    """Configuration with Anna's Archive API key."""
    return SourceConfig(
        annas_secret_key="test-key",
        fallback_enabled=True,
    )


@pytest.fixture
def config_without_annas():
    """Configuration without Anna's Archive API key."""
    return SourceConfig(
        annas_secret_key="",
        fallback_enabled=True,
    )


@pytest.fixture
def config_no_fallback():
    """Configuration with Anna's key but fallback disabled."""
    return SourceConfig(
        annas_secret_key="test-key",
        fallback_enabled=False,
    )


class TestSourceSelection:
    """Tests for _determine_source logic."""

    def test_auto_uses_annas_when_key_present(self, config_with_annas):
        """Auto mode should prefer Anna's when API key is configured."""
        router = SourceRouter(config_with_annas)
        assert router._determine_source("auto") == "annas"

    def test_auto_uses_libgen_when_no_key(self, config_without_annas):
        """Auto mode should fall back to LibGen when no Anna's key."""
        router = SourceRouter(config_without_annas)
        assert router._determine_source("auto") == "libgen"

    def test_explicit_annas_respected(self, config_with_annas):
        """Explicit 'annas' source should be used as-is."""
        router = SourceRouter(config_with_annas)
        assert router._determine_source("annas") == "annas"

    def test_explicit_libgen_respected(self, config_with_annas):
        """Explicit 'libgen' source should be used even with Anna's key."""
        router = SourceRouter(config_with_annas)
        assert router._determine_source("libgen") == "libgen"


class TestRouterSearch:
    """Tests for search routing and fallback."""

    @pytest.mark.asyncio
    async def test_search_returns_results_with_source(self, config_with_annas):
        """Search results should include source field indicating origin."""
        router = SourceRouter(config_with_annas)
        mock_result = [
            UnifiedBookResult(
                md5="abc123",
                title="Test Book",
                source=SourceType.ANNAS_ARCHIVE,
            )
        ]

        with patch.object(router, "_get_annas") as mock_get_annas:
            mock_adapter = AsyncMock()
            mock_adapter.search.return_value = mock_result
            mock_get_annas.return_value = mock_adapter

            results = await router.search("test")

            assert len(results) == 1
            assert results[0].source == SourceType.ANNAS_ARCHIVE
            assert results[0].md5 == "abc123"

    @pytest.mark.asyncio
    async def test_fallback_on_annas_failure(self, config_with_annas):
        """Should fall back to LibGen when Anna's search fails."""
        router = SourceRouter(config_with_annas)
        libgen_result = [
            UnifiedBookResult(
                md5="def456",
                title="Fallback Book",
                source=SourceType.LIBGEN,
            )
        ]

        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            annas_adapter.search.side_effect = Exception("Network error")
            mock_get_annas.return_value = annas_adapter

            libgen_adapter = AsyncMock()
            libgen_adapter.search.return_value = libgen_result
            mock_get_libgen.return_value = libgen_adapter

            results = await router.search("test")

            assert len(results) == 1
            assert results[0].source == SourceType.LIBGEN

    @pytest.mark.asyncio
    async def test_fallback_on_empty_results(self, config_with_annas):
        """Should fall back to LibGen when Anna's returns empty results."""
        router = SourceRouter(config_with_annas)
        libgen_result = [
            UnifiedBookResult(
                md5="ghi789",
                title="LibGen Book",
                source=SourceType.LIBGEN,
            )
        ]

        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            annas_adapter.search.return_value = []  # Empty results
            mock_get_annas.return_value = annas_adapter

            libgen_adapter = AsyncMock()
            libgen_adapter.search.return_value = libgen_result
            mock_get_libgen.return_value = libgen_adapter

            results = await router.search("test")

            assert len(results) == 1
            assert results[0].source == SourceType.LIBGEN

    @pytest.mark.asyncio
    async def test_no_fallback_when_disabled(self, config_no_fallback):
        """Should raise, not return [], when fallback is disabled and Anna's fails.

        Returning [] made an unreachable provider indistinguishable from a
        query with no matches — the caller cannot tell it should retry, switch
        source, or fix its config.
        """
        router = SourceRouter(config_no_fallback)

        with patch.object(router, "_get_annas") as mock_get_annas:
            annas_adapter = AsyncMock()
            annas_adapter.search.side_effect = Exception("Network error")
            mock_get_annas.return_value = annas_adapter

            with pytest.raises(AllSourcesFailedError) as excinfo:
                await router.search("test")

            assert "annas" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_direct_libgen_search(self, config_without_annas):
        """Should use LibGen directly when no Anna's key."""
        router = SourceRouter(config_without_annas)
        libgen_result = [
            UnifiedBookResult(
                md5="xyz123",
                title="Direct LibGen",
                source=SourceType.LIBGEN,
            )
        ]

        with patch.object(router, "_get_libgen") as mock_get_libgen:
            libgen_adapter = AsyncMock()
            libgen_adapter.search.return_value = libgen_result
            mock_get_libgen.return_value = libgen_adapter

            results = await router.search("test")

            assert len(results) == 1
            assert results[0].source == SourceType.LIBGEN


class TestRouterDownload:
    """Tests for download URL routing and quota fallback."""

    @pytest.mark.asyncio
    async def test_missing_annas_key_is_a_structured_configuration_failure(
        self, config_without_annas
    ):
        """An explicit Anna download without a key is caller config, not outage."""
        router = SourceRouter(config_without_annas)

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await router.get_download_url("abc123", source="annas")

        assert len(excinfo.value.failures) == 1
        failure = excinfo.value.failures[0]
        assert failure.provider == "annas"
        assert failure.reason == "configuration_error"
        assert "ANNAS_SECRET_KEY" in failure.detail

    @pytest.mark.asyncio
    async def test_download_returns_url_with_source(self, config_with_annas):
        """Download result should include source and quota info."""
        router = SourceRouter(config_with_annas)

        with patch.object(router, "_get_annas") as mock_get_annas:
            mock_adapter = AsyncMock()
            mock_adapter.get_download_url.return_value = DownloadResult(
                url="https://example.com/file.pdf",
                source=SourceType.ANNAS_ARCHIVE,
                quota_info=QuotaInfo(
                    downloads_left=24,
                    downloads_per_day=25,
                    downloads_done_today=1,
                ),
            )
            mock_get_annas.return_value = mock_adapter

            result = await router.get_download_url("abc123")

            assert result.source == SourceType.ANNAS_ARCHIVE
            assert result.quota_info.downloads_left == 24

    @pytest.mark.asyncio
    async def test_base_candidate_iteration_yields_the_existing_single_result(self):
        """A single-URL adapter remains compatible with the candidate contract."""

        class SingleAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return []

            async def get_download_url(self, md5, **kwargs):
                return DownloadResult(
                    url=f"https://single.example/{md5}", source=SourceType.LIBGEN
                )

            async def close(self):
                pass

        candidates = SingleAdapter().iter_download_candidates("abc")

        assert (await anext(candidates)).url == "https://single.example/abc"
        with pytest.raises(StopAsyncIteration):
            await anext(candidates)

    @pytest.mark.asyncio
    async def test_router_flattens_provider_candidate_streams_in_order(
        self, config_with_annas
    ):
        """Stopping after the first provider candidate would hide later mirrors."""
        router = SourceRouter(config_with_annas)

        async def annas_candidates(_md5, **kwargs):
            yield DownloadResult(
                url="https://annas.example/book", source=SourceType.ANNAS_ARCHIVE
            )

        async def libgen_candidates(_md5, **kwargs):
            yield DownloadResult(url="https://libgen.li/book", source=SourceType.LIBGEN)
            yield DownloadResult(url="https://libgen.vg/book", source=SourceType.LIBGEN)

        router._annas = AsyncMock()
        router._annas.iter_download_candidates = annas_candidates
        router._libgen = AsyncMock()
        router._libgen.iter_download_candidates = libgen_candidates

        results = [
            candidate
            async for candidate in router.iter_download_candidates("abc", source="auto")
        ]

        assert [result.url for result in results] == [
            "https://annas.example/book",
            "https://libgen.li/book",
            "https://libgen.vg/book",
        ]

    @pytest.mark.asyncio
    async def test_closing_router_stream_closes_suspended_provider_stream(
        self, config_with_annas
    ):
        """Early consumer success must release the adapter's client context."""
        router = SourceRouter(config_with_annas)
        closed = False

        class ResourceAdapter:
            async def iter_download_candidates(self, _md5, **kwargs):
                nonlocal closed
                try:
                    yield DownloadResult(
                        url="https://libgen.li/book", source=SourceType.LIBGEN
                    )
                    await asyncio.sleep(30)
                finally:
                    closed = True

        router._libgen = ResourceAdapter()
        stream = router.iter_download_candidates("abc", source="libgen")

        assert (await anext(stream)).url == "https://libgen.li/book"
        await stream.aclose()

        assert closed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source", "expected_url"),
        [
            ("annas", "https://annas.example/book"),
            ("libgen", "https://libgen.example/book"),
        ],
    )
    async def test_explicit_candidate_iteration_never_crosses_providers(
        self, config_with_annas, source, expected_url
    ):
        """An explicit source selection must never invoke the other adapter."""
        router = SourceRouter(config_with_annas)

        async def annas_candidates(_md5, **kwargs):
            yield DownloadResult(
                url="https://annas.example/book", source=SourceType.ANNAS_ARCHIVE
            )

        async def libgen_candidates(_md5, **kwargs):
            yield DownloadResult(
                url="https://libgen.example/book", source=SourceType.LIBGEN
            )

        router._annas = AsyncMock()
        router._annas.iter_download_candidates = annas_candidates
        router._libgen = AsyncMock()
        router._libgen.iter_download_candidates = libgen_candidates

        results = [
            candidate
            async for candidate in router.iter_download_candidates("abc", source=source)
        ]

        assert [result.url for result in results] == [expected_url]

    @pytest.mark.asyncio
    async def test_fallback_on_quota_exhausted(self, config_with_annas):
        """Should fall back to LibGen when Anna's quota is exhausted."""
        router = SourceRouter(config_with_annas)

        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            annas_adapter.get_download_url.side_effect = QuotaExhaustedError(
                "Quota exhausted"
            )
            mock_get_annas.return_value = annas_adapter

            libgen_adapter = AsyncMock()
            libgen_adapter.get_download_url.return_value = DownloadResult(
                url="https://libgen.example/file.pdf",
                source=SourceType.LIBGEN,
            )
            mock_get_libgen.return_value = libgen_adapter

            result = await router.get_download_url("abc123")

            assert result.source == SourceType.LIBGEN
            assert "libgen" in result.url

    @pytest.mark.asyncio
    async def test_fallback_on_zero_quota_remaining(self, config_with_annas):
        """Should fall back to LibGen when quota returns 0 downloads left."""
        router = SourceRouter(config_with_annas)

        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            annas_adapter.get_download_url.return_value = DownloadResult(
                url="https://example.com/file.pdf",
                source=SourceType.ANNAS_ARCHIVE,
                quota_info=QuotaInfo(
                    downloads_left=0,  # Quota exhausted
                    downloads_per_day=25,
                    downloads_done_today=25,
                ),
            )
            mock_get_annas.return_value = annas_adapter

            libgen_adapter = AsyncMock()
            libgen_adapter.get_download_url.return_value = DownloadResult(
                url="https://libgen.example/file.pdf",
                source=SourceType.LIBGEN,
            )
            mock_get_libgen.return_value = libgen_adapter

            result = await router.get_download_url("abc123")

            assert result.source == SourceType.LIBGEN

    @pytest.mark.asyncio
    async def test_quota_exhausted_no_fallback(self, config_no_fallback):
        """Explicit Anna quota exhaustion retains the canonical provider envelope."""
        router = SourceRouter(config_no_fallback)

        with patch.object(router, "_get_annas") as mock_get_annas:
            annas_adapter = AsyncMock()
            annas_adapter.get_download_url.side_effect = QuotaExhaustedError(
                "Quota exhausted"
            )
            mock_get_annas.return_value = annas_adapter

            with pytest.raises(SourceError) as excinfo:
                await router.get_download_url("abc123")

        assert excinfo.value.provider == "annas"
        assert excinfo.value.reason == "quota_exhausted"

    @pytest.mark.asyncio
    async def test_fallback_on_download_error(self, config_with_annas):
        """Should fall back to LibGen on generic download errors."""
        router = SourceRouter(config_with_annas)

        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            annas_adapter.get_download_url.side_effect = Exception("API error")
            mock_get_annas.return_value = annas_adapter

            libgen_adapter = AsyncMock()
            libgen_adapter.get_download_url.return_value = DownloadResult(
                url="https://libgen.example/file.pdf",
                source=SourceType.LIBGEN,
            )
            mock_get_libgen.return_value = libgen_adapter

            result = await router.get_download_url("abc123")

            assert result.source == SourceType.LIBGEN


class TestRouterLifecycle:
    """Tests for adapter lifecycle management."""

    @pytest.mark.asyncio
    async def test_close_cleans_up_adapters(self, config_with_annas):
        """Close should clean up all adapter resources."""
        router = SourceRouter(config_with_annas)

        # Force creation of adapters
        with (
            patch.object(router, "_get_annas") as mock_get_annas,
            patch.object(router, "_get_libgen") as mock_get_libgen,
        ):
            annas_adapter = AsyncMock()
            libgen_adapter = AsyncMock()
            mock_get_annas.return_value = annas_adapter
            mock_get_libgen.return_value = libgen_adapter

            # Create adapters by calling methods
            router._annas = annas_adapter
            router._libgen = libgen_adapter

            await router.close()

            annas_adapter.close.assert_called_once()
            libgen_adapter.close.assert_called_once()
            assert router._annas is None
            assert router._libgen is None

    def test_lazy_adapter_creation(self, config_with_annas):
        """Adapters should only be created when needed."""
        router = SourceRouter(config_with_annas)

        assert router._annas is None
        assert router._libgen is None

        # Getting annas should create it
        annas = router._get_annas()
        assert annas is not None
        assert router._annas is not None

        # Getting libgen should create it
        libgen = router._get_libgen()
        assert libgen is not None
        assert router._libgen is not None

    def test_annas_adapter_is_built_without_key(self, config_without_annas):
        """Anna's adapter is built with or without a key (#74).

        This previously asserted the adapter was None without a key. That
        encoded the bug: Anna's search is credential-free HTML scraping, so
        gating construction on the key made an explicit source="annas" fall
        through to LibGen silently. The key belongs on get_download_url, which
        enforces it itself.
        """
        router = SourceRouter(config_without_annas)

        annas = router._get_annas()
        assert annas is not None
        assert router._annas is annas  # and is cached

    @pytest.mark.asyncio
    async def test_explicit_annas_is_not_silently_swapped_for_libgen(
        self, config_without_annas
    ):
        """An explicit source="annas" must query Anna's, not LibGen (#74).

        The user-visible bug: search_multi_source(source="annas") returned
        LibGen results with nothing indicating the requested source was never
        contacted.
        """
        router = SourceRouter(config_without_annas)

        annas_hit = False

        async def fake_annas_search(query, **kwargs):
            nonlocal annas_hit
            annas_hit = True
            return [
                UnifiedBookResult(
                    md5="deadbeef", title="From Anna's", source=SourceType.ANNAS_ARCHIVE
                )
            ]

        async def fail_libgen_search(query, **kwargs):
            raise AssertionError("LibGen was queried for an explicit source='annas'")

        router._get_annas().search = fake_annas_search
        router._get_libgen().search = fail_libgen_search

        results = await router.search("hegel", source="annas")

        assert annas_hit
        assert len(results) == 1
        assert results[0].source == SourceType.ANNAS_ARCHIVE

    @pytest.mark.asyncio
    async def test_auto_still_prefers_libgen_without_key(self, config_without_annas):
        """'auto' stays LibGen-first without a key — deliberately unchanged.

        Fixing the explicit-source gate must not quietly reroute 'auto'. LibGen
        returns a resolvable download link; an Anna's result without a key still
        needs a route the caller may not have.
        """
        router = SourceRouter(config_without_annas)

        async def fake_libgen_search(query, **kwargs):
            return [
                UnifiedBookResult(
                    md5="cafe", title="From LibGen", source=SourceType.LIBGEN
                )
            ]

        async def fail_annas_search(query, **kwargs):
            raise AssertionError("Anna's was queried for source='auto' with no key")

        router._get_libgen().search = fake_libgen_search
        router._get_annas().search = fail_annas_search

        results = await router.search("hegel", source="auto")

        assert results[0].source == SourceType.LIBGEN
