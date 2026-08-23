"""Adapter and router behaviour when a provider is unreachable.

Regression suite for the 2026-08-11 hang: `search_multi_source` had no network
deadline, so an unroutable LibGen mirror blocked the bridge indefinitely and
left the Python process orphaned. These tests assert the three properties that
prevent a repeat — every path is bounded, every failure names its provider and
reason, and an explicitly requested source is never silently swapped.
"""

import asyncio
import math
import os
import socket
import subprocess
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
    ProviderTimeoutError,
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
        assert config.download_timeout > config.total_timeout
        assert math.isfinite(config.download_timeout)
        assert config.preflight_timeout > 0

    def test_env_overrides_are_honoured(self, monkeypatch):
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", "3.5")
        monkeypatch.setenv("BOOK_SOURCE_READ_TIMEOUT", "12")
        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "20")
        monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_TIMEOUT", "900")
        monkeypatch.setenv("BOOK_SOURCE_PREFLIGHT", "false")

        config = get_source_config()

        assert config.connect_timeout == 3.5
        assert config.read_timeout == 12.0
        assert config.total_timeout == 20.0
        assert config.download_timeout == 900.0
        assert config.preflight_enabled is False

    @pytest.mark.parametrize(
        "bad", ["0", "-5", "abc", "", "inf", "Infinity", "1e999", "nan"]
    )
    def test_invalid_download_budget_falls_back_to_a_finite_default(
        self, monkeypatch, bad
    ):
        monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_TIMEOUT", bad)

        config = get_source_config()

        assert config.download_timeout == 1500.0
        assert math.isfinite(config.download_timeout)

    @pytest.mark.parametrize(
        "bad", ["0", "-5", "abc", "", "inf", "Infinity", "1e999", "nan"]
    )
    def test_nonsense_falls_back_to_the_default_not_to_unbounded(
        self, monkeypatch, bad
    ):
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", bad)
        config = get_source_config()
        assert config.connect_timeout == 10.0

    def test_env_walk_budget_fits_the_env_resolved_node_bridge_timeout(
        self, monkeypatch
    ):
        """A Python override cannot outlive the Node process that owns it.

        Production mutation caught: accept ``BOOK_SOURCE_WALK_BUDGET`` without
        comparing it to the process's resolved ``PYTHON_BRIDGE_TIMEOUT`` and
        bridge margin.  A 300-second walk would then be SIGTERM'd by a
        240-second Node bridge before it could return attributed failures.
        """
        from lib.sources.config import worst_case_search_seconds

        monkeypatch.setenv("PYTHON_BRIDGE_TIMEOUT", "240000")
        monkeypatch.setenv("BOOK_SOURCE_WALK_BUDGET", "300")

        config = get_source_config()

        assert worst_case_search_seconds(config) < 240.0

    def test_impossibly_short_node_budget_is_rejected_before_the_bridge_starts(
        self, monkeypatch
    ):
        """No positive walk can fit when Node leaves no overhead allowance."""
        monkeypatch.setenv("PYTHON_BRIDGE_TIMEOUT", "30000")
        monkeypatch.setenv("BOOK_SOURCE_WALK_BUDGET", "1")

        with pytest.raises(ValueError, match="PYTHON_BRIDGE_TIMEOUT"):
            get_source_config()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("2147483647", id="maximum-timer-delay"),
            pytest.param("2147483648", id="above-maximum-timer-delay"),
            pytest.param("9" * 5000, id="python-int-digit-limit"),
            pytest.param("\ufeff300000", id="bom-prefix"),
            pytest.param("300000\ufeff", id="bom-suffix"),
        ],
    )
    def test_node_timeout_parser_matches_node_at_boundaries(self, monkeypatch, raw):
        """Python must exactly match Node for boundary and trim inputs."""
        from lib.sources.config import _node_bridge_timeout_seconds

        monkeypatch.setenv("PYTHON_BRIDGE_TIMEOUT", raw)

        node = subprocess.run(
            [
                "node",
                "-e",
                "const raw = process.env.PYTHON_BRIDGE_TIMEOUT?.trim(); "
                "const value = raw && /^\\d+$/.test(raw) ? Number(raw) : NaN; "
                "process.stdout.write(String(Number.isSafeInteger(value) && "
                "value > 0 && value <= 2147483647 ? value / 1000 : 240));",
            ],
            check=True,
            capture_output=True,
            env=os.environ,
            text=True,
        )

        assert _node_bridge_timeout_seconds() == float(node.stdout)


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

        book = MagicMock(md5="a" * 32, title="Phenomenology", author="Hegel")

        with (
            patch("lib.sources.libgen.probe_host", side_effect=_first_mirror_dead),
            patch("lib.sources.libgen.LibgenSearch") as mock_search_class,
        ):
            mock_search_class.return_value.search_default.return_value = [book]
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
            mock_search_class.return_value.search_default.side_effect = _hang

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
            mock_search_class.return_value.search_default.return_value = []
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

    @pytest.mark.asyncio
    async def test_total_deadline_reason_survives_adapter_classification(
        self, fast_config
    ):
        """The outer wall-clock timeout is already a fully attributed error."""
        adapter = AnnasArchiveAdapter(fast_config)

        async def trickle(*_args, **_kwargs):
            await asyncio.sleep(30)

        client = AsyncMock()
        client.get.side_effect = trickle

        with patch("lib.sources.annas.probe_host", new=AsyncMock(return_value=None)):
            with patch.object(
                adapter, "_get_client", new=AsyncMock(return_value=client)
            ):
                with pytest.raises(ProviderTimeoutError) as excinfo:
                    await adapter.search("hegel")

        assert excinfo.value.provider == "annas"
        assert excinfo.value.host == "annas-archive.gl"
        assert excinfo.value.reason == "search_timeout"


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
    async def test_annas_preflight_is_clamped_to_the_shared_deadline(self, monkeypatch):
        """A late Anna's probe must not start with a fresh full timeout.

        Production mutation caught: call ``_preflight()`` without the shared
        deadline, or pass ``config.preflight_timeout`` through unchanged.
        Either leaves a late probe free to overrun the walk before the actual
        request receives its correctly clamped budget.
        """
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(5.0, clock=lambda: now[0])
        now[0] = 4.5
        probe_timeouts = []

        async def record_probe(*_args, timeout, **_kwargs):
            probe_timeouts.append(timeout)

        monkeypatch.setattr(annas, "probe_host", record_probe)
        adapter = AnnasArchiveAdapter(
            SourceConfig(preflight_enabled=True, preflight_timeout=5.0)
        )

        async def empty_search(*_args, **_kwargs):
            return httpx.Response(200, text="<html></html>")

        monkeypatch.setattr(adapter, "_fetch", empty_search)
        try:
            assert await adapter.search("anything", deadline=deadline) == []
        finally:
            await adapter.close()

        assert probe_timeouts == [pytest.approx(0.5)]

    @pytest.mark.asyncio
    async def test_annas_does_not_start_preflight_after_the_shared_deadline(
        self, monkeypatch
    ):
        """An expired walk names budget exhaustion instead of probing a host."""
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(5.0, clock=lambda: now[0])
        now[0] = 5.0
        probe_calls = []

        async def record_probe(*_args, **_kwargs):
            probe_calls.append(True)

        monkeypatch.setattr(annas, "probe_host", record_probe)
        adapter = AnnasArchiveAdapter(SourceConfig(preflight_enabled=True))

        async def empty_search(*_args, **_kwargs):
            return httpx.Response(200, text="<html></html>")

        monkeypatch.setattr(adapter, "_fetch", empty_search)
        try:
            with pytest.raises(
                ProviderTimeoutError, match="walk ran out of time"
            ) as excinfo:
                await adapter.search("anything", deadline=deadline)
        finally:
            await adapter.close()

        assert excinfo.value.reason == "walk_budget_exhausted"
        assert probe_calls == []


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


class TestTheDocumentedTimeoutMarginIsEnforced:
    """The composition in `config.py` must be checked, not asserted (#152).

    That comment claimed `4 x 55s = 220s` against a 240-second
    `PYTHON_BRIDGE_TIMEOUT`. It was wrong for months: `_mirror_candidates()`
    prepends the configured mirror, so any `LIBGEN_MIRROR` outside the fallback
    set produced four LibGen attempts and a 275-second worst case. Node then
    killed the subprocess and the operator saw a generic bridge timeout instead
    of the attributed per-mirror failures the error taxonomy exists to produce.

    The first fix capped the mirror list, which made the sum constant by
    deleting `la` from the download walk — where the budget is generous and
    byte-driven `li -> vg -> la` failover is a stated repo contract (Codex on
    #153). The walk is now bounded by a shared `WalkDeadline` instead, so the
    worst case is one configurable number and NOT a function of how many
    mirrors or providers exist. Several of these tests assert that
    independence directly, because it is the property that was missing.

    A comment cannot fail. These tests can.
    """

    @staticmethod
    def _node_bridge_timeout_seconds() -> float:
        """Read the real Node budget out of the TypeScript source.

        A literal copied here would be independently maintained, so lowering
        `DEFAULT_BRIDGE_TIMEOUT_MS` would leave this test green while the walk
        it approves gets killed — the guard reproducing the exact cross-language
        drift it exists to prevent (Codex on #153). Parsed rather than copied,
        and the parse failing is itself a failure: an unreadable budget means
        the guard is not guarding.
        """
        import re
        from pathlib import Path

        source = (
            Path(__file__).parent.parent.parent / "src" / "lib" / "python-runner.ts"
        ).read_text()
        match = re.search(
            r"DEFAULT_BRIDGE_TIMEOUT_MS\s*=\s*positiveIntEnv\("
            r"\s*'PYTHON_BRIDGE_TIMEOUT'\s*,\s*(\d+)\s*\)",
            source,
        )
        assert match, (
            "could not read DEFAULT_BRIDGE_TIMEOUT_MS from python-runner.ts — "
            "the timeout guard cannot verify anything without it"
        )
        return int(match.group(1)) / 1000.0

    def test_the_documented_margin_still_holds(self):
        from lib.sources.config import SourceConfig, worst_case_search_seconds

        worst = worst_case_search_seconds(SourceConfig())

        assert worst < self._node_bridge_timeout_seconds(), (
            f"an auto search can take {worst}s against a "
            f"{self._node_bridge_timeout_seconds()}s bridge budget — Node will "
            f"kill a legitimate walk. Raise PYTHON_BRIDGE_TIMEOUT and this "
            f"constant together, or lower a provider budget."
        )

    def test_every_configured_fallback_survives_a_custom_mirror(self):
        """Capping the list is the fix this one rejects (Codex on #153).

        With `LIBGEN_MIRROR=rs` a three-attempt cap truncated `[rs, li, vg, la]`
        to `[rs, li, vg]` — and `_mirror_candidates()` is shared by search AND
        download, so `la` stopped being reachable on the long-budget download
        walk even when it was the only mirror serving bytes.
        """
        from lib.sources.config import SourceConfig
        from lib.sources.libgen import FALLBACK_MIRRORS, LibgenAdapter

        for mirror in ("rs", "is", "unknown-mirror"):
            candidates = LibgenAdapter(
                SourceConfig(libgen_mirror=mirror)
            )._mirror_candidates()

            for fallback in FALLBACK_MIRRORS:
                assert fallback in candidates, (
                    f"{fallback!r} is unreachable with LIBGEN_MIRROR={mirror!r}; "
                    f"the walk is bounded by the clock, not by dropping mirrors"
                )

    def test_the_configured_mirror_is_tried_first(self):
        """A walk that runs out of clock must have spent it on the right mirror."""
        from lib.sources.config import SourceConfig
        from lib.sources.libgen import LibgenAdapter

        for mirror in ("rs", "is", "unknown-mirror"):
            candidates = LibgenAdapter(
                SourceConfig(libgen_mirror=mirror)
            )._mirror_candidates()

            assert candidates[0] == mirror, (
                f"{mirror!r} must be attempted first: the deadline can end a "
                f"walk early, so the operator's own choice has to be the one "
                f"that already had its turn"
            )

    def test_a_raised_provider_budget_fails_here_rather_than_in_production(self):
        """The guard has to actually bind, or it is another dead comment."""
        from lib.sources.config import SourceConfig, worst_case_search_seconds

        generous = SourceConfig(walk_budget=300.0)

        assert (
            worst_case_search_seconds(generous) > self._node_bridge_timeout_seconds()
        ), "the computation must be sensitive to the budgets it composes"

    def test_the_node_budget_is_read_from_the_typescript_source(self):
        """The guard must not carry its own copy of the number it checks.

        A literal `240.0` here is independently maintained, so lowering
        `DEFAULT_BRIDGE_TIMEOUT_MS` would leave this green while the walk it
        approves gets killed — the guard reproducing the exact cross-language
        drift it exists to prevent (Codex on #153).
        """
        assert self._node_bridge_timeout_seconds() == 240.0, (
            "reading python-runner.ts should currently yield 240s; if that "
            "default changed, this test and the margin move together, which "
            "is the whole point of reading it"
        )

    def test_the_worst_case_does_not_move_with_the_mirror_count(self):
        """The property the old arithmetic lacked, asserted directly.

        A custom `LIBGEN_MIRROR` genuinely adds a fourth mirror — that part was
        never in dispute, and this test confirms it still does. What must NOT
        move is the walk's worst case, because the mirrors share one clock
        rather than each bringing their own budget.
        """
        from lib.sources.config import SourceConfig, worst_case_search_seconds
        from lib.sources.libgen import LibgenAdapter

        default = SourceConfig()
        custom = SourceConfig(libgen_mirror="rs")

        assert len(LibgenAdapter(custom)._mirror_candidates()) == (
            len(LibgenAdapter(default)._mirror_candidates()) + 1
        ), "the premise of #152 no longer holds; this test is testing nothing"

        assert worst_case_search_seconds(custom) == worst_case_search_seconds(
            default
        ), (
            "an extra mirror moved the worst case — the walk is being costed "
            "per attempt again instead of against one shared deadline"
        )

    def test_the_worst_case_does_not_move_with_the_provider_count(self):
        """Z-Library joining the `auto` walk under #40 must not break this."""
        from lib.sources.config import SourceConfig, worst_case_search_seconds
        from lib.sources.router import SourceRouter

        one = SourceConfig(fallback_enabled=False)
        many = SourceConfig(fallback_enabled=True)

        router = SourceRouter.__new__(SourceRouter)
        router.config = many
        assert len(router._search_candidates("auto")) > 1, (
            "fallback is not actually adding a provider; nothing is being tested"
        )

        assert worst_case_search_seconds(one) == worst_case_search_seconds(many)

    @staticmethod
    def _node_long_timeout_seconds() -> float:
        """The other Node budget, read rather than copied, for the same reason."""
        import re
        from pathlib import Path

        source = (
            Path(__file__).parent.parent.parent / "src" / "lib" / "python-runner.ts"
        ).read_text()
        match = re.search(
            r"LONG_BRIDGE_TIMEOUT_MS\s*=\s*positiveIntEnv\("
            r"\s*'PYTHON_BRIDGE_LONG_TIMEOUT'\s*,\s*(\d+)\s*\)",
            source,
        )
        assert match, (
            "could not read LONG_BRIDGE_TIMEOUT_MS from python-runner.ts — "
            "the download guard cannot verify anything without it"
        )
        return int(match.group(1)) / 1000.0

    def test_the_download_allocation_fits_the_long_budget(self):
        """Resolution + transfer + OCR + finalization, computed not asserted.

        This allocation lived only in a comment beside DEFAULT_DOWNLOAD_TIMEOUT,
        which is exactly the shape of the sentence that made #152: correct when
        written, never recomputed. The download walk shares the search walk's
        budget, so raising `BOOK_SOURCE_WALK_BUDGET` has to fit BOTH ceilings.
        """
        from lib.sources.config import SourceConfig, worst_case_download_seconds

        worst = worst_case_download_seconds(SourceConfig())

        assert worst <= self._node_long_timeout_seconds(), (
            f"an acquisition can take {worst}s against a "
            f"{self._node_long_timeout_seconds()}s long-bridge budget"
        )


class TestTheWalkDeadline:
    """The primitive the whole budget now rests on."""

    def test_suspension_freezes_remaining_time_until_the_consumer_returns(self):
        """A yielded candidate's transfer is outside active resolution time.

        Production mutation caught: omit the public suspension primitive, or
        merely record a timestamp without freezing/rebasing the deadline.
        """
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(5.0, clock=lambda: now[0])

        with deadline.suspended():
            now[0] = 8.0
            assert deadline.remaining() == 5.0

        assert deadline.remaining() == 5.0
        now[0] = 10.0
        assert deadline.remaining() == 3.0

    def test_suspension_rebases_after_an_exception_without_negative_elapsed_time(self):
        """Cleanup cannot spend time or shorten the deadline on clock regression."""
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(5.0, clock=lambda: now[0])

        with pytest.raises(RuntimeError, match="consumer failed"):
            with deadline.suspended():
                now[0] = -2.0
                raise RuntimeError("consumer failed")

        now[0] = 0.0
        assert deadline.remaining() == 5.0

    @pytest.mark.asyncio
    async def test_suspension_rebases_when_the_consumer_task_is_cancelled(self):
        """Cancelling the consumer unwinds the synchronous context manager."""
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(5.0, clock=lambda: now[0])

        async def cancelled_consumer():
            with deadline.suspended():
                now[0] = 8.0
                await asyncio.sleep(30)

        task = asyncio.create_task(cancelled_consumer())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert deadline.remaining() == 5.0

    @pytest.mark.asyncio
    async def test_libgen_search_uses_a_short_budget_when_preflight_is_disabled(
        self,
    ):
        """A no-preflight search still has time for useful resolution work.

        Production mutation caught: reserve two preflight phases unconditionally
        before a LibGen search.  With preflight disabled, that rejects a
        two-second walk before the actual bounded search is even started.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(
            SourceConfig(
                preflight_enabled=False,
                total_timeout=2.0,
                walk_budget=2.0,
            )
        )
        expected = [MagicMock()]
        with (
            patch("lib.sources.libgen.LibgenSearch") as search_class,
            patch.object(adapter, "_rate_limit", new=AsyncMock(return_value=None)),
            patch.object(adapter, "_to_unified", return_value=expected),
        ):
            search_class.return_value.search_default.return_value = [object()]
            assert await adapter.search("short budget") == expected

    @pytest.mark.asyncio
    async def test_libgen_admits_one_aggregate_preflight_and_a_resolution_slice(self):
        """A six-second walk can afford a five-second aggregate preflight."""
        from lib.sources.net import WalkDeadline

        adapter = LibgenAdapter(
            SourceConfig(
                preflight_enabled=True,
                preflight_timeout=5.0,
                total_timeout=6.0,
                walk_budget=6.0,
            )
        )
        preflight_mirrors = []
        expected = [MagicMock()]
        now = [0.0]

        async def record_preflight(mirror):
            preflight_mirrors.append(mirror)

        with (
            patch.object(adapter, "_preflight", new=record_preflight),
            patch.object(adapter, "_to_unified", return_value=expected),
            patch("lib.sources.libgen.LibgenSearch") as search_class,
        ):
            search_class.return_value.search_default.return_value = [object()]
            assert (
                await adapter.search(
                    "aggregate preflight",
                    deadline=WalkDeadline(6.0, clock=lambda: now[0]),
                )
                == expected
            )

        assert preflight_mirrors == ["li"]

    @pytest.mark.asyncio
    async def test_libgen_candidate_resolution_uses_a_short_budget_without_preflight(
        self,
    ):
        """The download candidate walk shares the no-preflight contract.

        Production mutation caught: retain the unconditional two-preflight
        reservation in ``iter_download_candidates`` after fixing only search.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(
            SourceConfig(
                preflight_enabled=False,
                total_timeout=2.0,
                walk_budget=2.0,
            )
        )
        with (
            patch.object(adapter, "_rate_limit", new=AsyncMock(return_value=None)),
            patch.object(
                adapter, "_resolve_key", new=AsyncMock(return_value=("KEY", ""))
            ),
            patch.object(
                adapter, "_serves_bytes", new=AsyncMock(return_value=(True, ""))
            ),
        ):
            candidates = adapter.iter_download_candidates("a" * 32)
            first = await anext(candidates)
            await candidates.aclose()

        assert first.url.startswith("https://libgen.li/get.php?")

    @pytest.mark.asyncio
    async def test_late_mirror_does_not_sleep_past_its_resolution_budget(
        self, monkeypatch
    ):
        """Pacing is refused before it spends the last useful resolution slice."""
        from lib.sources.libgen import LibgenAdapter
        from lib.sources.net import WalkDeadline

        now = [0.0]
        wall_clock = [100.0]
        adapter = LibgenAdapter(
            SourceConfig(
                preflight_enabled=False,
                total_timeout=2.0,
                walk_budget=2.0,
            )
        )
        adapter._last_request = 99.5
        sleep_calls = []

        async def advance_clock(delay):
            sleep_calls.append(delay)
            now[0] += delay
            wall_clock[0] += delay

        monkeypatch.setattr("lib.sources.libgen.time.time", lambda: wall_clock[0])
        monkeypatch.setattr("lib.sources.libgen.asyncio.sleep", advance_clock)

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await adapter.search(
                "late mirror", deadline=WalkDeadline(2.0, clock=lambda: now[0])
            )

        assert sleep_calls == []
        assert [failure.reason for failure in excinfo.value.failures] == [
            "walk_budget_exhausted"
        ]

    def test_an_attempt_is_clamped_to_what_the_walk_has_left(self):
        from lib.sources.net import WalkDeadline

        now = [0.0]
        deadline = WalkDeadline(165.0, clock=lambda: now[0])

        assert deadline.reserve(45.0, minimum=11.0) == 45.0

        now[0] = 140.0  # 25s left: less than a full attempt
        assert deadline.reserve(45.0, minimum=11.0) == 25.0, (
            "a late attempt started with a full 45s budget is how a walk "
            "overruns the bridge timeout it was supposed to fit inside"
        )

        now[0] = 160.0  # 5s left: enough to time out, not enough to inform
        assert deadline.reserve(45.0, minimum=11.0) is None
        assert not deadline.expired(), (
            "refusing a useless slice is not the same as having no time left; "
            "conflating them would report the wrong reason to the caller"
        )

        now[0] = 170.0
        assert deadline.expired()
        assert deadline.remaining() == 0.0, "remaining() must never go negative"

    @pytest.mark.asyncio
    async def test_a_spent_walk_names_the_mirrors_it_never_attempted(self):
        """The reason code has to distinguish this from a slow host.

        Attributing an exhausted walk to the next mirror would report a healthy
        server as slow, and send the operator to fix the wrong thing: this one
        is fixed by raising BOOK_SOURCE_WALK_BUDGET.
        """
        from lib.sources.config import SourceConfig
        from lib.sources.errors import AllSourcesFailedError
        from lib.sources.libgen import LibgenAdapter
        from lib.sources.net import WalkDeadline

        adapter = LibgenAdapter(SourceConfig(libgen_mirror="rs"))

        with pytest.raises(AllSourcesFailedError) as excinfo:
            await adapter.search("anything", deadline=WalkDeadline(0.0))

        failures = excinfo.value.failures
        assert [f.reason for f in failures] == ["walk_budget_exhausted"]
        for mirror in ("rs", "li", "vg", "la"):
            assert mirror in failures[0].detail, (
                f"{mirror!r} was skipped without being named; a silently "
                f"absent mirror is indistinguishable from one that answered"
            )

    @pytest.mark.asyncio
    async def test_the_router_gives_every_provider_the_same_clock(self):
        """One walk, one deadline — not one per provider."""
        from unittest.mock import AsyncMock

        from lib.sources.config import SourceConfig
        from lib.sources.router import SourceRouter

        seen = []

        async def record(query, deadline=None, **kwargs):
            seen.append(deadline)
            return []

        router = SourceRouter(SourceConfig(fallback_enabled=True))
        for name in ("_annas", "_libgen"):
            adapter = AsyncMock()
            adapter.search = record
            setattr(router, name, adapter)

        await router.search("nothing matches", source="auto")

        assert len(seen) == 2, "both providers should have been attempted"
        assert seen[0] is not None and seen[0] is seen[1], (
            "each provider got its own deadline, so the walk costs the sum of "
            "them again — which is the bug (#152)"
        )
