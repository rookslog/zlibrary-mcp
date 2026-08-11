"""Adapter and router behaviour when a provider is unreachable.

Regression suite for the 2026-08-11 hang: `search_multi_source` had no network
deadline, so an unroutable LibGen mirror blocked the bridge indefinitely and
left the Python process orphaned. These tests assert the three properties that
prevent a repeat — every path is bounded, every failure names its provider and
reason, and an explicitly requested source is never silently swapped.
"""

import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lib.sources import annas, net
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderUnreachableError,
)
from lib.sources.libgen import LibgenAdapter
from lib.sources.models import SourceType, UnifiedBookResult
from lib.sources.router import SourceRouter

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    net.reset_probe_cache()
    yield
    net.reset_probe_cache()


@pytest.fixture
def fast_config():
    """Short budgets so a test that regresses fails quickly instead of hanging."""
    return SourceConfig(
        connect_timeout=0.2,
        read_timeout=0.2,
        total_timeout=0.3,
        preflight_timeout=0.2,
    )


class TestConfigTimeouts:
    """Timeouts must be configurable and can never resolve to 'no timeout'."""

    def test_defaults_are_all_positive(self):
        config = get_source_config()
        assert config.connect_timeout > 0
        assert config.read_timeout > 0
        assert config.total_timeout > 0
        assert config.preflight_timeout > 0

    def test_env_overrides_are_honoured(self, monkeypatch):
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", "3.5")
        monkeypatch.setenv("BOOK_SOURCE_READ_TIMEOUT", "12")
        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "20")
        monkeypatch.setenv("BOOK_SOURCE_PREFLIGHT", "false")

        config = get_source_config()

        assert config.connect_timeout == 3.5
        assert config.read_timeout == 12.0
        assert config.total_timeout == 20.0
        assert config.preflight_enabled is False

    @pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
    def test_nonsense_falls_back_to_the_default_not_to_unbounded(
        self, monkeypatch, bad
    ):
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", bad)
        config = get_source_config()
        assert config.connect_timeout > 0


class TestLibgenSearchIsBounded:
    """LibGen search is the path that hung; it gets the closest scrutiny."""

    @pytest.mark.asyncio
    async def test_unreachable_mirror_fails_fast_without_calling_the_library(
        self, fast_config
    ):
        """The blocking third-party call must never be entered for a dead host.

        libgen_api_enhanced issues requests.get with no timeout, so once that
        call starts it can only be abandoned. The probe is what keeps us out.
        """
        adapter = LibgenAdapter(fast_config)

        async def _unreachable(provider, host, **_kwargs):
            raise ProviderUnreachableError(provider, host, reason="connect_timeout")

        with (
            patch("lib.sources.libgen.probe_host", side_effect=_unreachable),
            patch("lib.sources.libgen.LibgenSearch") as mock_search_class,
        ):
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.search("hegel")

            mock_search_class.assert_not_called()

        assert all(f.provider == "libgen" for f in excinfo.value.failures)
        assert {f.reason for f in excinfo.value.failures} == {"connect_timeout"}

    @pytest.mark.asyncio
    async def test_all_mirrors_are_tried_before_giving_up(self, fast_config):
        adapter = LibgenAdapter(fast_config)
        probed = []

        async def _unreachable(provider, host, **_kwargs):
            probed.append(host)
            raise ProviderUnreachableError(provider, host, reason="dns_failure")

        with patch("lib.sources.libgen.probe_host", side_effect=_unreachable):
            with pytest.raises(AllSourcesFailedError):
                await adapter.search("hegel")

        assert probed == ["libgen.li", "libgen.vg", "libgen.la"]

    @pytest.mark.asyncio
    async def test_failover_to_a_working_mirror(self, fast_config):
        """One dead mirror must not mean no results."""
        adapter = LibgenAdapter(fast_config)

        async def _first_mirror_dead(provider, host, **_kwargs):
            if host == "libgen.li":
                raise ProviderUnreachableError(provider, host, reason="connect_timeout")

        book = MagicMock(md5="abc", title="Phenomenology", author="Hegel")

        with (
            patch("lib.sources.libgen.probe_host", side_effect=_first_mirror_dead),
            patch("lib.sources.libgen.LibgenSearch") as mock_search_class,
        ):
            mock_search_class.return_value.search_title.return_value = [book]
            results = await adapter.search("hegel")

        assert len(results) == 1
        assert results[0].source == SourceType.LIBGEN
        assert mock_search_class.call_args.kwargs["mirror"] == "vg"

    @pytest.mark.asyncio
    async def test_a_blocking_library_call_cannot_outlast_the_budget(self, fast_config):
        """The core regression: an un-timed requests.get must not hang the bridge."""
        adapter = LibgenAdapter(fast_config)

        def _hang(*_args, **_kwargs):
            time.sleep(30)

        with (
            patch("lib.sources.libgen.probe_host", new=AsyncMock(return_value=None)),
            patch("lib.sources.libgen.LibgenSearch") as mock_search_class,
        ):
            mock_search_class.return_value.search_title.side_effect = _hang

            began = time.monotonic()
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.search("hegel")
            elapsed = time.monotonic() - began

        # Three mirrors x 0.3s budget, generously bounded.
        assert elapsed < 10, f"took {elapsed:.1f}s; the budget is not being enforced"
        assert {f.reason for f in excinfo.value.failures} == {"search_timeout"}

    @pytest.mark.asyncio
    async def test_empty_results_are_not_an_error(self, fast_config):
        adapter = LibgenAdapter(fast_config)

        with (
            patch("lib.sources.libgen.probe_host", new=AsyncMock(return_value=None)),
            patch("lib.sources.libgen.LibgenSearch") as mock_search_class,
        ):
            mock_search_class.return_value.search_title.return_value = []
            assert await adapter.search("nothing matches this") == []


class TestAnnasSearchIsBounded:
    """Anna's had a timeout already; what it lacked was attribution."""

    @pytest.mark.asyncio
    async def test_client_uses_the_configured_budgets(self):
        config = SourceConfig(connect_timeout=4.0, read_timeout=9.0)
        adapter = AnnasArchiveAdapter(config)

        client = await adapter._get_client()
        try:
            assert client.timeout.connect == 4.0
            assert client.timeout.read == 9.0
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_dns_failure_names_the_provider_and_host(self, fast_config):
        adapter = AnnasArchiveAdapter(fast_config)

        async def _unreachable(provider, host, **_kwargs):
            raise ProviderUnreachableError(
                provider, host, "Name or service not known", reason="dns_failure"
            )

        with patch("lib.sources.annas.probe_host", side_effect=_unreachable):
            with pytest.raises(ProviderUnreachableError) as excinfo:
                await adapter.search("hegel")

        assert excinfo.value.provider == "annas"
        assert excinfo.value.host == "annas-archive.gl"
        assert excinfo.value.reason == "dns_failure"
        assert "annas-archive.gl" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_transport_failure_is_classified_not_swallowed(self, fast_config):
        adapter = AnnasArchiveAdapter(fast_config)
        exc = httpx.ConnectError("boom")
        exc.__cause__ = socket.gaierror(-2, "Name or service not known")

        with patch("lib.sources.annas.probe_host", new=AsyncMock(return_value=None)):
            with patch.object(adapter, "_get_client") as mock_get_client:
                mock_client = AsyncMock()
                mock_client.get.side_effect = exc
                mock_get_client.return_value = mock_client

                with pytest.raises(ProviderUnreachableError) as excinfo:
                    await adapter.search("hegel")

        assert excinfo.value.reason == "dns_failure"

    @pytest.mark.asyncio
    async def test_http_error_is_a_response_failure_not_a_reachability_one(
        self, fast_config
    ):
        adapter = AnnasArchiveAdapter(fast_config)
        request = httpx.Request("GET", "https://annas-archive.gl/search")
        response = httpx.Response(503, request=request)

        with patch("lib.sources.annas.probe_host", new=AsyncMock(return_value=None)):
            with patch.object(adapter, "_get_client") as mock_get_client:
                mock_response = MagicMock()
                mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_response
                mock_get_client.return_value = mock_client

                with pytest.raises(ProviderResponseError) as excinfo:
                    await adapter.search("hegel")

        assert excinfo.value.reason == "http_error"
        assert excinfo.value.unreachable is False


class TestRouterSourceSelection:
    """`auto` means fall back; an explicit source means answer for that source."""

    @pytest.mark.asyncio
    async def test_explicit_annas_failure_does_not_silently_return_libgen(self):
        """A request tagged source=annas must not end up executing on LibGen.

        This is how an `annas` call came to hang inside LibGen's un-timed
        search on 2026-08-11 — the failure was attributed to the wrong source
        and the caller waited on a provider it had not asked for.
        """
        router = SourceRouter(SourceConfig(fallback_enabled=True))

        with (
            patch.object(router, "_get_annas") as mock_annas,
            patch.object(router, "_get_libgen") as mock_libgen,
        ):
            annas = AsyncMock()
            annas.search.side_effect = ProviderUnreachableError(
                "annas", "annas-archive.gl", reason="dns_failure"
            )
            mock_annas.return_value = annas
            mock_libgen.return_value = AsyncMock()

            with pytest.raises(AllSourcesFailedError) as excinfo:
                await router.search("hegel", source="annas")

            mock_libgen.return_value.search.assert_not_called()

        assert excinfo.value.failures[0].provider == "annas"
        assert excinfo.value.failures[0].reason == "dns_failure"

    @pytest.mark.asyncio
    async def test_explicit_libgen_failure_does_not_fall_back_to_annas(self):
        router = SourceRouter(SourceConfig(fallback_enabled=True))

        with (
            patch.object(router, "_get_annas") as mock_annas,
            patch.object(router, "_get_libgen") as mock_libgen,
        ):
            libgen = AsyncMock()
            libgen.search.side_effect = ProviderUnreachableError(
                "libgen", "libgen.li", reason="connect_timeout"
            )
            mock_libgen.return_value = libgen
            mock_annas.return_value = AsyncMock()

            with pytest.raises(AllSourcesFailedError):
                await router.search("hegel", source="libgen")

            mock_annas.return_value.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_the_other_provider(self):
        """source=auto is where fallback belongs, and it works both ways."""
        router = SourceRouter(SourceConfig(fallback_enabled=True))

        with (
            patch.object(router, "_get_annas") as mock_annas,
            patch.object(router, "_get_libgen") as mock_libgen,
        ):
            libgen = AsyncMock()
            libgen.search.side_effect = ProviderUnreachableError(
                "libgen", "libgen.li", reason="connect_timeout"
            )
            mock_libgen.return_value = libgen

            annas = AsyncMock()
            annas.search.return_value = [
                UnifiedBookResult(md5="x", title="T", source=SourceType.ANNAS_ARCHIVE)
            ]
            mock_annas.return_value = annas

            results = await router.search("hegel", source="auto")

        assert len(results) == 1
        assert results[0].source == SourceType.ANNAS_ARCHIVE

    @pytest.mark.asyncio
    async def test_every_provider_failure_is_reported_with_its_own_reason(self):
        """Two providers down for two different reasons must not be flattened."""
        router = SourceRouter(SourceConfig(annas_secret_key="k", fallback_enabled=True))

        with (
            patch.object(router, "_get_annas") as mock_annas,
            patch.object(router, "_get_libgen") as mock_libgen,
        ):
            annas = AsyncMock()
            annas.search.side_effect = ProviderUnreachableError(
                "annas", "annas-archive.gl", reason="dns_failure"
            )
            mock_annas.return_value = annas

            libgen = AsyncMock()
            libgen.search.side_effect = ProviderUnreachableError(
                "libgen", "libgen.li", reason="connect_timeout"
            )
            mock_libgen.return_value = libgen

            with pytest.raises(AllSourcesFailedError) as excinfo:
                await router.search("hegel", source="auto")

        payload = excinfo.value.to_dict()
        by_provider = {f["provider"]: f["reason"] for f in payload["failures"]}
        assert by_provider == {"annas": "dns_failure", "libgen": "connect_timeout"}

    @pytest.mark.asyncio
    async def test_a_reachable_provider_with_no_matches_returns_empty(self):
        """No match is not an outage; it must not raise."""
        router = SourceRouter(SourceConfig(fallback_enabled=True))

        with (
            patch.object(router, "_get_annas") as mock_annas,
            patch.object(router, "_get_libgen") as mock_libgen,
        ):
            for mock in (mock_annas, mock_libgen):
                adapter = AsyncMock()
                adapter.search.return_value = []
                mock.return_value = adapter

            assert await router.search("zzzz no such book", source="auto") == []


class TestPreflightTargetsTheRealEndpoint:
    """The probe must address the port and scheme the request will use.

    The autouse `_no_preflight_probes` stub in conftest replaces `probe_host`
    wholesale, which is why the wrong-port bug survived the first round of
    tests. These record the arguments instead of suppressing the call.
    """

    @staticmethod
    def _recording_probe(calls):
        async def _probe(
            provider, host, port=443, timeout=5.0, use_cache=True, scheme="https"
        ):
            calls.append(
                {"host": host, "port": port, "scheme": scheme, "provider": provider}
            )

        return _probe

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url, expected_host, expected_port, expected_scheme",
        [
            ("https://annas-archive.gl", "annas-archive.gl", 443, "https"),
            ("http://localhost:8080", "localhost", 8080, "http"),
            ("https://mirror.example:8443", "mirror.example", 8443, "https"),
            ("http://example.com", "example.com", 80, "http"),
        ],
    )
    async def test_annas_probes_the_configured_port(
        self, monkeypatch, base_url, expected_host, expected_port, expected_scheme
    ):
        calls = []
        monkeypatch.setattr(annas, "probe_host", self._recording_probe(calls))

        config = SourceConfig(annas_base_url=base_url, preflight_enabled=True)
        adapter = annas.AnnasArchiveAdapter(config)
        await adapter._preflight()

        assert calls == [
            {
                "host": expected_host,
                "port": expected_port,
                "scheme": expected_scheme,
                "provider": "annas",
            }
        ]


@pytest.mark.asyncio
class TestPartialFailureIsNotAnEmptyResult:
    """An unreachable provider must never be reported as 'no matches'."""

    async def test_empty_plus_failure_raises_rather_than_returning_empty(self):
        config = SourceConfig(fallback_enabled=True)
        router = SourceRouter(config)

        healthy = AsyncMock()
        healthy.search = AsyncMock(return_value=[])
        broken = AsyncMock()
        broken.search = AsyncMock(
            side_effect=ProviderUnreachableError(
                "annas", "annas-archive.gl", reason="dns_failure"
            )
        )
        router._libgen = healthy
        router._annas = broken

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await router.search("obscure title", source="auto")

        # The caller must be able to see WHICH provider went unsearched.
        reasons = {f.reason for f in excinfo.value.failures}
        assert reasons == {"dns_failure"}
        assert any(f.provider == "annas" for f in excinfo.value.failures)

    async def test_all_providers_answering_empty_is_still_an_empty_result(self):
        config = SourceConfig(fallback_enabled=True)
        router = SourceRouter(config)

        for name in ("_libgen", "_annas"):
            adapter = AsyncMock()
            adapter.search = AsyncMock(return_value=[])
            setattr(router, name, adapter)

        assert await router.search("nothing matches", source="auto") == []
