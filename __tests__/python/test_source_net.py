"""Tests for the bounded-network helpers in lib/sources/net.py.

These cover the machinery that was missing when three python_bridge.py search
processes were found alive for hours on 2026-08-11: a wall-clock bound on a
blocking third-party call, a thread that cannot keep the interpreter alive,
and a probe that says WHICH failure happened rather than just "it failed".
"""

import asyncio
import socket
import ssl
import threading
import time

import httpx
import pytest

from lib.sources import net
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderTimeoutError, ProviderUnreachableError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    net.reset_probe_cache()
    yield
    net.reset_probe_cache()


class TestBuildTimeout:
    """Every phase of an httpx request must carry a budget."""

    def test_all_phases_bounded(self):
        config = SourceConfig(connect_timeout=7.0, read_timeout=11.0)
        timeout = net.build_timeout(config)

        assert timeout.connect == 7.0
        assert timeout.read == 11.0
        assert timeout.write == 11.0
        assert timeout.pool == 7.0


class TestBoundedResolver:
    """Actual httpx resolution must not enter asyncio's joined executor."""

    @pytest.mark.asyncio
    async def test_httpx_hostname_resolution_uses_a_daemon_bounded_worker(
        self, monkeypatch
    ):
        release = threading.Event()
        worker_daemon = []

        def stalled_getaddrinfo(*_args, **_kwargs):
            worker_daemon.append(threading.current_thread().daemon)
            release.wait(5)
            return []

        monkeypatch.setattr(socket, "getaddrinfo", stalled_getaddrinfo)
        started = time.monotonic()
        try:
            async with net.bounded_resolver(0.05):
                async with httpx.AsyncClient(trust_env=False) as client:
                    with pytest.raises(httpx.ConnectError) as excinfo:
                        await client.get("http://resolver-stall.invalid/")
        finally:
            release.set()

        assert time.monotonic() - started < 1
        assert worker_daemon == [True]
        assert net.classify_httpx_error(excinfo.value)[0] == "dns_timeout"

    def test_no_phase_is_unbounded(self):
        """A None anywhere here is the bug this module exists to prevent."""
        timeout = net.build_timeout(SourceConfig())

        assert None not in (timeout.connect, timeout.read, timeout.write, timeout.pool)


class TestClassifyHttpxError:
    """DNS failure and connect timeout must not collapse into one reason."""

    def test_connect_timeout(self):
        reason, _ = net.classify_httpx_error(httpx.ConnectTimeout("too slow"))
        assert reason == "connect_timeout"

    def test_read_timeout(self):
        reason, _ = net.classify_httpx_error(httpx.ReadTimeout("no body"))
        assert reason == "read_timeout"

    def test_dns_failure_read_from_the_cause_chain(self):
        """httpx raises ConnectError for DNS too; only __cause__ tells them apart."""
        exc = httpx.ConnectError("boom")
        exc.__cause__ = socket.gaierror(-2, "Name or service not known")

        reason, detail = net.classify_httpx_error(exc)

        assert reason == "dns_failure"
        assert "Name or service not known" in detail

    def test_dns_timeout_marker_beats_generic_gaierror_in_cause_chain(self):
        """A bounded resolver deadline is temporary, not a missing domain."""
        exc = httpx.ConnectError("boom")
        exc.__cause__ = socket.gaierror(
            socket.EAI_AGAIN, f"{net.DNS_TIMEOUT_MARKER}:5s"
        )

        reason, detail = net.classify_httpx_error(exc)

        assert reason == "dns_timeout"
        assert "deadline exceeded" in detail

    def test_dns_failure_from_message_when_cause_is_missing(self):
        reason, _ = net.classify_httpx_error(
            httpx.ConnectError("[Errno -2] Name or service not known")
        )
        assert reason == "dns_failure"

    def test_connection_refused(self):
        exc = httpx.ConnectError("nope")
        exc.__cause__ = ConnectionRefusedError()
        reason, _ = net.classify_httpx_error(exc)
        assert reason == "connect_refused"

    def test_tls_error(self):
        exc = httpx.ConnectError("handshake")
        exc.__cause__ = ssl.SSLError("bad cert")
        reason, _ = net.classify_httpx_error(exc)
        assert reason == "tls_error"

    def test_http_status_error_names_the_status(self):
        request = httpx.Request("GET", "https://example.invalid/")
        response = httpx.Response(503, request=request)
        reason, detail = net.classify_httpx_error(
            httpx.HTTPStatusError("boom", request=request, response=response)
        )
        assert reason == "http_error"
        assert "503" in detail

    def test_plain_connect_error_is_not_reported_as_dns(self):
        reason, _ = net.classify_httpx_error(httpx.ConnectError("network unreachable"))
        assert reason == "connect_error"


class TestClassifyRequestsError:
    """The LibGen library flattens everything into RequestException prose."""

    def test_timeout_sentence(self):
        reason, _ = net.classify_requests_error(
            Exception("Request to https://libgen.li timed out")
        )
        assert reason == "read_timeout"

    def test_connect_sentence(self):
        reason, _ = net.classify_requests_error(
            Exception("Failed to connect to https://libgen.is")
        )
        assert reason == "connect_error"

    def test_cause_chain_beats_the_sentence(self):
        exc = Exception("Failed to connect to https://libgen.is")
        exc.__cause__ = socket.gaierror(-2, "Name or service not known")
        reason, _ = net.classify_requests_error(exc)
        assert reason == "dns_failure"


@pytest.mark.real_preflight
class TestProbeHost:
    """The pre-flight probe distinguishes the two observed outage shapes.

    Marked real_preflight so the conftest stub that keeps other unit tests off
    the network does not stub out the very function under test.
    """

    @pytest.mark.asyncio
    async def test_dns_failure_is_named_as_such(self, monkeypatch):
        """The annas-archive.org shape: the name has no address at all."""

        def _fail(*_args, **_kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)

        with pytest.raises(ProviderUnreachableError) as excinfo:
            await net.probe_host("annas", "annas-archive.invalid", timeout=1.0)

        assert excinfo.value.reason == "dns_failure"
        assert excinfo.value.host == "annas-archive.invalid"
        assert excinfo.value.unreachable is True

    @pytest.mark.asyncio
    async def test_timed_out_dns_resolver_cannot_keep_the_bridge_alive(
        self, monkeypatch
    ):
        """The resolver worker must be daemonised because it cannot be cancelled."""
        release = threading.Event()
        seen = {}

        def _hang(*_args, **_kwargs):
            seen["daemon"] = threading.current_thread().daemon
            release.wait(30)
            return []

        monkeypatch.setattr(socket, "getaddrinfo", _hang)

        try:
            with pytest.raises(ProviderUnreachableError) as excinfo:
                await net.probe_host(
                    "annas",
                    "stalled-resolver.invalid",
                    timeout=0.05,
                    use_cache=False,
                )
        finally:
            release.set()

        assert excinfo.value.reason == "dns_timeout"
        assert seen["daemon"] is True

    @pytest.mark.asyncio
    async def test_connect_timeout_when_syn_is_dropped(self, monkeypatch):
        """The libgen.is shape: resolves fine, never completes a handshake."""

        def _resolves(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

        async def _never_connects(*_args, **_kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(socket, "getaddrinfo", _resolves)
        monkeypatch.setattr(net, "_open_resolved_connection", _never_connects)

        with pytest.raises(ProviderUnreachableError) as excinfo:
            await net.probe_host("libgen", "libgen.is", timeout=0.05)

        assert excinfo.value.reason == "connect_timeout"
        assert excinfo.value.reason != "dns_failure", "must not blur into DNS failure"

    @pytest.mark.asyncio
    async def test_probe_uses_one_deadline_for_slow_dns_and_tcp(self, monkeypatch):
        """DNS cannot spend the whole preflight budget and restart TCP's."""
        address = ("192.0.2.1", 443)

        async def slow_dns(*_args, **_kwargs):
            await asyncio.sleep(0.06)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

        async def slow_connect(_loop, sock, _target):
            await asyncio.sleep(0.06)
            return object(), _Writer(sock)

        class _Writer:
            def __init__(self, sock):
                self.sock = sock

            def close(self):
                self.sock.close()

            async def wait_closed(self):
                return None

        monkeypatch.setattr(net, "run_bounded", slow_dns)
        monkeypatch.setattr(net, "_open_resolved_connection", slow_connect)

        with pytest.raises(ProviderUnreachableError) as excinfo:
            await net.probe_host(
                "annas", "slow-probe.example", timeout=0.1, use_cache=False
            )

        assert excinfo.value.reason == "connect_timeout"

    @pytest.mark.asyncio
    async def test_tcp_probe_reuses_the_daemon_resolved_address(self, monkeypatch):
        """The TCP phase must not ask open_connection to resolve the host again."""
        address = ("192.0.2.25", 443)
        connected = []

        def _resolves(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)
            ]

        async def _open_resolved(_loop, sock, target):
            connected.append((sock, target))
            return object(), _Writer(sock)

        class _Writer:
            def __init__(self, sock):
                self.sock = sock

            def close(self):
                self.sock.close()

            async def wait_closed(self):
                return None

        monkeypatch.setattr(socket, "getaddrinfo", _resolves)
        monkeypatch.setattr(net, "_open_resolved_connection", _open_resolved)

        await net.probe_host("libgen", "libgen.example", timeout=0.2, use_cache=False)

        assert len(connected) == 1
        assert connected[0][1] == address

    @pytest.mark.asyncio
    async def test_reachable_host_returns_quietly(self):
        """Probe a real listening socket on localhost."""
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await net.probe_host("test", "127.0.0.1", port=port, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_verdict_is_cached_within_a_process(self, monkeypatch):
        """LibGen walks three mirrors; a repeated host must not re-probe."""
        calls = []

        async def _fail(provider, host, port, timeout):
            calls.append(host)
            return ProviderUnreachableError(provider, host, reason="dns_failure")

        monkeypatch.setattr(net, "_probe_host_uncached", _fail)

        for _ in range(3):
            with pytest.raises(ProviderUnreachableError):
                await net.probe_host("libgen", "libgen.is", timeout=1.0)

        assert calls == ["libgen.is"]


class TestRunBounded:
    """The mechanism that replaces asyncio.to_thread for uncancellable calls."""

    @pytest.mark.asyncio
    async def test_returns_the_value(self):
        result = await net.run_bounded(lambda: 42, 5.0, provider="libgen")
        assert result == 42

    @pytest.mark.asyncio
    async def test_propagates_the_exception(self):
        def _boom():
            raise ValueError("bad mirror")

        with pytest.raises(ValueError, match="bad mirror"):
            await net.run_bounded(_boom, 5.0, provider="libgen")

    @pytest.mark.asyncio
    async def test_returns_at_the_deadline_not_when_the_call_finishes(self):
        started = threading.Event()

        def _hang():
            started.set()
            time.sleep(30)

        began = time.monotonic()
        with pytest.raises(ProviderTimeoutError) as excinfo:
            await net.run_bounded(
                _hang, 0.1, provider="libgen", host="libgen.is", operation="search"
            )
        elapsed = time.monotonic() - began

        assert started.is_set()
        assert elapsed < 5, f"waited {elapsed:.1f}s on a 0.1s budget"
        assert excinfo.value.reason == "search_timeout"
        assert excinfo.value.provider == "libgen"
        assert excinfo.value.host == "libgen.is"

    @pytest.mark.asyncio
    async def test_worker_thread_is_a_daemon(self):
        """Non-daemon workers are joined at interpreter exit.

        That is the property that kept whole bridge processes alive after their
        request was gone, so it is asserted directly rather than inferred.
        """
        seen = {}
        ready = threading.Event()

        def _record():
            seen["daemon"] = threading.current_thread().daemon
            ready.set()
            return "done"

        await net.run_bounded(_record, 5.0, provider="libgen")

        assert ready.wait(2)
        assert seen["daemon"] is True

    @pytest.mark.asyncio
    async def test_a_straggler_reporting_late_does_not_explode(self):
        """The abandoned thread finishes after the awaiter has given up."""
        release = threading.Event()

        def _slow():
            release.wait(5)
            return "late"

        with pytest.raises(ProviderTimeoutError):
            await net.run_bounded(_slow, 0.05, provider="libgen")

        release.set()
        # Give the straggler a turn to deliver into a cancelled future.
        await asyncio.sleep(0.2)


class TestPortDerivation:
    """`port_of` — the probe must target what the request targets."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://annas-archive.gl", 443),
            ("http://localhost:8080", 8080),
            ("http://example.com", 80),
            ("https://example.com:8443/path", 8443),
            ("ftp://example.com", 443),
        ],
    )
    def test_derives_port_from_url(self, url, expected):
        assert net.port_of(url) == expected


class TestProxyDetection:
    """A direct probe must not veto a request the proxy could have carried."""

    @pytest.fixture(autouse=True)
    def _clear_proxy_env(self, monkeypatch):
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_no_proxy_configured(self):
        assert net.proxy_in_use("https://annas-archive.gl") is False

    def test_https_proxy_applies_to_https(self, monkeypatch):
        monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
        assert net.proxy_in_use("https://annas-archive.gl") is True

    def test_http_proxy_does_not_apply_to_https(self, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
        assert net.proxy_in_use("https://annas-archive.gl") is False

    def test_no_proxy_exclusion_is_respected(self, monkeypatch):
        # An excluded host really is reached directly, so it SHOULD be probed.
        monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
        monkeypatch.setenv("no_proxy", "annas-archive.gl")
        assert net.proxy_in_use("https://annas-archive.gl") is False

    @pytest.mark.real_preflight
    @pytest.mark.asyncio
    async def test_probe_is_skipped_entirely_behind_a_proxy(self, monkeypatch):
        """The whole point: no socket is opened, so nothing can be vetoed."""
        monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
        net.reset_probe_cache()

        def _explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("preflight opened a socket behind a proxy")

        monkeypatch.setattr(socket, "getaddrinfo", _explode)
        monkeypatch.setattr(asyncio, "open_connection", _explode)

        await net.probe_host("annas", "annas-archive.gl", port=443)


@pytest.mark.asyncio
class TestBoundedAwait:
    """`bounded_await` — one deadline over a whole async operation."""

    async def test_returns_the_value_when_it_finishes_in_time(self):
        async def quick():
            return "done"

        assert await net.bounded_await(quick(), 5.0, provider="annas") == "done"

    async def test_raises_at_the_deadline(self):
        async def trickle():
            # Stands in for a host that keeps the read deadline alive by
            # sending a byte at a time: never idle, never finished.
            await asyncio.sleep(30)

        start = time.monotonic()
        with pytest.raises(ProviderTimeoutError) as excinfo:
            await net.bounded_await(
                trickle(), 0.2, provider="annas", host="annas-archive.gl"
            )
        assert time.monotonic() - start < 5
        assert excinfo.value.reason == "search_timeout"
        assert excinfo.value.provider == "annas"

    async def test_propagates_the_original_exception(self):
        async def boom():
            raise ValueError("upstream")

        with pytest.raises(ValueError, match="upstream"):
            await net.bounded_await(boom(), 5.0, provider="annas")
