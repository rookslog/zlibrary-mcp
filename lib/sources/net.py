"""Bounded-network helpers for the multi-source path.

Everything outbound in `lib/sources/` goes through here so that no call can
block without a deadline. Three distinct hazards are covered, and they need
three different mechanisms:

1. **httpx calls we own** — bounded by an `httpx.Timeout` built from config.
   `build_timeout` exists so connect/read budgets are configurable rather than
   hard-coded, and so every client in the package gets the same ones.

2. **Blocking third-party calls we do not own** — `libgen_api_enhanced` calls
   `requests.get(...)` with **no `timeout=` argument** (search_request.py), and
   even catches a `requests.exceptions.Timeout` that therefore can never fire.
   An unroutable host (SYN dropped, as libgen.is does from some networks) makes
   that call block forever. `asyncio.to_thread` cannot rescue it: its worker
   threads are non-daemon and are joined at interpreter shutdown, so the whole
   Python process survives the MCP call that spawned it — the orphaned
   `python_bridge.py search...` processes observed 2026-08-11 with 9h of
   elapsed time. `run_bounded` runs such calls on a **daemon** thread instead:
   the await is bounded, and an abandoned thread cannot keep the process alive.

3. **Hosts that are simply gone** — `probe_host` is a cheap pre-flight that
   separates "DNS has no address for this name" from "the name resolves but
   the host drops our packets". Those need different fixes (update the mirror
   list vs. try another mirror), so they get different reason codes rather
   than one generic failure. It probes the port and scheme the real request
   will use, and skips itself entirely when a proxy is configured — a probe
   that vetoes a request the real client could have completed is worse than
   no probe at all.

4. **Operations that outlive their phases** — an `httpx.Timeout` bounds each
   phase separately, and its read deadline restarts on every chunk received,
   so a host that trickles bytes never trips it. `bounded_await` puts one
   wall-clock deadline over a whole async operation, which is what actually
   enforces `config.total_timeout`. `build_timeout` alone never did.
"""

import asyncio
import json
import logging
import socket
import ssl
import threading
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from .errors import ProviderTimeoutError, ProviderUnreachableError

logger = logging.getLogger("zlibrary.sources")

# Machine marker deliberately carried in the gaierror text. httpcore/httpx
# discard the original exception type and cause on this path but preserve its
# message. Matching this exact token is stable and avoids guessing from broad
# human-readable timeout prose.
DNS_TIMEOUT_MARKER = "zlibrary_mcp_dns_timeout"

# Cached probe verdicts, keyed by (host, port), for the lifetime of the
# process. The bridge is a one-shot process per MCP call, so this only ever
# collapses the repeated probes of a single call (LibGen walks three mirrors)
# and cannot go stale across calls.
_probe_cache: Dict[Tuple[str, int], Optional[ProviderUnreachableError]] = {}


def host_of(url: str) -> str:
    """Extract the hostname from a URL, or '' if it has none."""
    return (urlsplit(url).hostname or "").lower()


def port_of(url: str) -> int:
    """The TCP port a URL addresses, explicit or implied by its scheme.

    The preflight opens a socket to this port, so it has to be the port the
    real request will use. Defaulting to 443 regardless made a base URL like
    `http://localhost:8080` probe `localhost:443` and report a reachable host
    as dead.

    Args:
        url: Absolute URL

    Returns:
        The explicit port if the URL carries one, else 80 for http and 443
        for anything else (https and unknown schemes alike).
    """
    parts = urlsplit(url)
    if parts.port:
        return parts.port
    return 80 if parts.scheme == "http" else 443


def proxy_in_use(url: str) -> bool:
    """Whether an outbound proxy would carry a request to this URL.

    httpx and requests both honour HTTP_PROXY / HTTPS_PROXY / ALL_PROXY by
    default (`trust_env` is True), but a raw socket does not. On a network
    where direct egress is blocked and a proxy is mandatory — corporate LANs,
    many container runtimes — the real request succeeds through the proxy
    while a direct probe cannot connect at all. Preflighting there would
    report every provider unreachable and stop the working request from ever
    being made, which is strictly worse than having no preflight.

    Args:
        url: Absolute URL the request will be sent to

    Returns:
        True if a proxy applies to this URL and the preflight must be skipped
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        # getproxies() reads the *_PROXY environment (plus system config on
        # macOS/Windows); proxy_bypass() applies NO_PROXY, so a host excluded
        # from the proxy is still probed directly, which is correct.
        proxies = urllib.request.getproxies()
        if not proxies:
            return False
        if host and urllib.request.proxy_bypass(host):
            return False
    except Exception:  # noqa: BLE001 - never let proxy detection break a search
        return False
    return parts.scheme in proxies or "all" in proxies


def build_timeout(config) -> httpx.Timeout:
    """Build an httpx timeout from a SourceConfig.

    Args:
        config: SourceConfig carrying connect_timeout / read_timeout

    Returns:
        httpx.Timeout with an explicit budget on every phase. httpx defaults
        pool/write to the same value so a saturated pool cannot hang either.
    """
    return httpx.Timeout(
        connect=config.connect_timeout,
        read=config.read_timeout,
        write=config.read_timeout,
        pool=config.connect_timeout,
    )


def classify_httpx_error(exc: BaseException) -> Tuple[str, str]:
    """Map an httpx/transport exception to a (reason, detail) pair.

    The cause chain matters more than the httpx class: httpx raises
    `ConnectError` for a failed DNS lookup, a refused connection, and a broken
    TLS handshake alike, and only `__cause__` tells those apart.

    Args:
        exc: Exception raised by an httpx call

    Returns:
        (reason code from errors.REASON_TEXT, free-text detail)
    """
    detail = type(exc).__name__

    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout", detail
    if isinstance(exc, httpx.PoolTimeout):
        return "connect_timeout", detail
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return "read_timeout", detail
    if isinstance(exc, httpx.TimeoutException):
        return "read_timeout", detail
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error", f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.DecodingError)):
        return "protocol_error", detail
    # response.json() on a non-JSON body. The host answered, so this is a
    # response problem, not a reachability one.
    if isinstance(exc, json.JSONDecodeError):
        return "protocol_error", f"{detail}: {exc}"

    if isinstance(exc, httpx.ConnectError):
        cause = exc.__cause__
        while cause is not None:
            if isinstance(cause, socket.gaierror):
                return "dns_failure", f"{detail}: {cause}"
            if isinstance(cause, ssl.SSLError):
                return "tls_error", f"{detail}: {type(cause).__name__}"
            if isinstance(cause, ConnectionRefusedError):
                return "connect_refused", detail
            cause = cause.__cause__
        # httpx strips the gaierror cause produced by its AnyIO transport, but
        # retains our exact machine marker in the ConnectError message.
        if DNS_TIMEOUT_MARKER in str(exc):
            return "dns_timeout", f"{detail}: DNS resolution deadline exceeded"
        # httpx does not always preserve other causes; fall back to the text.
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text:
            return "dns_failure", str(exc)
        if "refused" in text:
            return "connect_refused", detail
        return "connect_error", f"{detail}: {exc}"

    if isinstance(exc, ssl.SSLError):
        return "tls_error", detail
    if isinstance(exc, socket.gaierror):
        return "dns_failure", f"{detail}: {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return "connect_refused", detail
    if isinstance(exc, asyncio.TimeoutError):
        return "read_timeout", detail

    return "unknown", f"{detail}: {exc}"


def classify_requests_error(exc: BaseException) -> Tuple[str, str]:
    """Map an exception from the `requests`-based LibGen library.

    `libgen_api_enhanced` re-raises every transport failure as a bare
    `requests.exceptions.RequestException` carrying only a sentence, so the
    original class is usually gone by the time we see it. Match on the cause
    chain first and fall back to that sentence.

    Args:
        exc: Exception raised by a libgen_api_enhanced call

    Returns:
        (reason code from errors.REASON_TEXT, free-text detail)
    """
    detail = type(exc).__name__

    cause = exc.__cause__ or exc.__context__
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return "dns_failure", f"{detail}: {cause}"
        if isinstance(cause, ssl.SSLError):
            return "tls_error", f"{detail}: {type(cause).__name__}"
        if isinstance(cause, ConnectionRefusedError):
            return "connect_refused", detail
        cause = cause.__cause__ or cause.__context__

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "read_timeout", str(exc)
    if "failed to connect" in text or "connection" in text:
        return "connect_error", str(exc)
    if "http error" in text:
        return "http_error", str(exc)
    return "unknown", f"{detail}: {exc}"


async def probe_host(
    provider: str,
    host: str,
    port: int = 443,
    timeout: float = 5.0,
    use_cache: bool = True,
    scheme: str = "https",
) -> None:
    """Check that a host resolves and accepts a TCP connection.

    Runs before the real request so an unreachable provider costs one bounded
    probe instead of the full per-request budget (and, for LibGen, so we never
    enter the un-interruptible third-party call at all).

    DNS and connect are probed separately on purpose: they are the two failure
    modes measured on dionysus 2026-08-11 (annas-archive.org had no DNS record;
    libgen.is resolved to 193.218.118.42 but dropped every SYN) and they call
    for different remedies.

    Skipped entirely when a proxy applies: the probe is an optimisation, and an
    optimisation that vetoes a request the real client could have completed is
    worse than no optimisation at all.

    Args:
        provider: Provider name for error attribution
        host: Hostname to probe
        port: TCP port (default 443)
        timeout: Budget in seconds for EACH of the DNS and connect phases
        use_cache: Reuse a verdict already reached for this (host, port)
        scheme: URL scheme the real request will use, for proxy detection

    Raises:
        ProviderUnreachableError: If DNS or the TCP connect fails or times out
    """
    if proxy_in_use(f"{scheme}://{host}:{port}"):
        logger.debug(
            "%s: skipping preflight for %s — an outbound proxy is configured "
            "and the direct probe would not reflect the real request path",
            provider,
            host,
        )
        return

    key = (host, port)
    if use_cache and key in _probe_cache:
        cached = _probe_cache[key]
        if cached is not None:
            raise cached
        return

    error = await _probe_host_uncached(provider, host, port, timeout)
    _probe_cache[key] = error
    if error is not None:
        raise error


async def _probe_host_uncached(
    provider: str, host: str, port: int, timeout: float
) -> Optional[ProviderUnreachableError]:
    """Run the probe, returning the failure rather than raising it."""
    # DNS is a blocking libc/NSS call and cannot be cancelled. The event loop's
    # default executor uses non-daemon workers and joins them during
    # asyncio.run() shutdown, so wait_for(loop.getaddrinfo(...)) can return at
    # five seconds yet still keep the one-shot bridge alive until the resolver
    # eventually returns. Reuse the daemon-thread boundary that protects the
    # third-party LibGen client; translate its generic operation timeout into
    # the DNS-specific stable reason expected from a preflight.
    try:
        addresses = await run_bounded(
            lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout,
            provider=provider,
            host=host,
            operation="dns probe",
        )
    except ProviderTimeoutError:
        return ProviderUnreachableError(
            provider, host, f"no DNS answer within {timeout:g}s", reason="dns_timeout"
        )
    except socket.gaierror as exc:
        return ProviderUnreachableError(provider, host, str(exc), reason="dns_failure")
    except OSError as exc:
        return ProviderUnreachableError(
            provider, host, f"{type(exc).__name__}: {exc}", reason="dns_failure"
        )

    # TCP connect. Deliberately no TLS handshake: this only answers "is anything
    # listening", and a handshake would double the cost of the common case.
    async def connect_resolved():
        """Connect using numeric sockaddrs from the daemon-bounded lookup."""
        loop = asyncio.get_running_loop()
        last_error: Optional[OSError] = None
        for family, socktype, proto, _canonname, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
            try:
                return await _open_resolved_connection(loop, sock, sockaddr)
            except BaseException as exc:
                sock.close()
                if isinstance(exc, OSError):
                    last_error = exc
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise OSError("DNS returned no usable stream addresses")

    writer = None
    try:
        _, writer = await asyncio.wait_for(connect_resolved(), timeout)
    except asyncio.TimeoutError:
        return ProviderUnreachableError(
            provider,
            host,
            f"no TCP handshake within {timeout:g}s",
            reason="connect_timeout",
        )
    except ConnectionRefusedError:
        return ProviderUnreachableError(provider, host, "", reason="connect_refused")
    except OSError as exc:
        return ProviderUnreachableError(
            provider, host, f"{type(exc).__name__}: {exc}", reason="connect_error"
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    return None


async def _open_resolved_connection(loop, sock, sockaddr):
    """Connect a prepared socket to one numeric address tuple."""
    await loop.sock_connect(sock, sockaddr)
    return await asyncio.open_connection(sock=sock)


def reset_probe_cache() -> None:
    """Clear cached probe verdicts (used by tests)."""
    _probe_cache.clear()


async def run_bounded(
    func: Callable[[], Any],
    timeout: float,
    *,
    provider: str,
    host: str = "",
    operation: str = "request",
) -> Any:
    """Run a blocking callable on a daemon thread under a wall-clock budget.

    Use this instead of `asyncio.to_thread` for any third-party synchronous
    call whose own timeouts cannot be configured. Two properties matter:

    - The await returns at `timeout` whether or not the call has finished.
    - The thread is a daemon, so an abandoned call cannot keep the interpreter
      alive at shutdown. `asyncio.to_thread` gives neither: its worker threads
      are registered for join-at-exit, which is precisely how a hung LibGen
      search outlived its MCP request by nine hours.

    The abandoned thread still holds a socket until the process exits. That is
    acceptable here because the bridge is a one-shot process per MCP call; it
    would not be in a long-lived server.

    Args:
        func: Zero-argument blocking callable
        timeout: Wall-clock budget in seconds
        provider: Provider name for error attribution
        host: Hostname for error attribution
        operation: Short label for the error message

    Returns:
        Whatever `func` returns

    Raises:
        ProviderTimeoutError: If the budget elapses first
        Exception: Whatever `func` raised
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[Any]" = loop.create_future()

    def _deliver(setter: Callable[[Any], None], value: Any) -> None:
        # The loop may be gone, or the future already cancelled by the timeout,
        # by the time a straggling thread reports back. Neither is an error.
        try:
            loop.call_soon_threadsafe(_apply, setter, value)
        except RuntimeError:
            pass

    def _apply(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def _runner() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 - relayed to the awaiter
            _deliver(future.set_exception, exc)
        else:
            _deliver(future.set_result, result)

    thread = threading.Thread(
        target=_runner, name=f"{provider}-{operation}", daemon=True
    )
    thread.start()

    try:
        return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "%s %s exceeded its %.0fs budget; abandoning the worker thread",
            provider,
            operation,
            timeout,
        )
        raise ProviderTimeoutError(
            provider,
            host,
            f"{operation} exceeded {timeout:g}s",
            reason="search_timeout",
        ) from None


@asynccontextmanager
async def bounded_resolver(timeout: float):
    """Route event-loop hostname resolution through a daemon worker.

    httpx/AnyIO performs its own resolution after provider preflight. The
    default loop implementation delegates that libc/NSS call to a non-daemon
    executor which asyncio joins at shutdown, so cancelling the HTTP request
    cannot bound the one-shot interpreter. Replacing the loop method keeps the
    public signature AnyIO expects while reusing the daemon lifecycle boundary.
    """
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo

    async def getaddrinfo(
        host,
        port,
        *,
        family=0,
        type=0,
        proto=0,
        flags=0,
    ):
        try:
            return await run_bounded(
                lambda: socket.getaddrinfo(host, port, family, type, proto, flags),
                timeout,
                provider="network",
                host=str(host),
                operation="dns resolution",
            )
        except ProviderTimeoutError as exc:
            raise socket.gaierror(
                socket.EAI_AGAIN, f"{DNS_TIMEOUT_MARKER}:{timeout:g}s"
            ) from exc

    loop.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        loop.getaddrinfo = original


async def bounded_await(
    awaitable,
    timeout: float,
    *,
    provider: str,
    host: str = "",
    operation: str = "request",
):
    """Await a coroutine under a wall-clock budget.

    The async counterpart to `run_bounded`. `httpx.Timeout` bounds each phase
    of a request separately, and its read deadline restarts every time another
    chunk arrives — so a host that trickles bytes indefinitely never trips it
    and the operation runs past `total_timeout` unbounded. That is what this
    closes: one deadline over the whole operation, however many phases or
    redirects it spans.

    Unlike `run_bounded` this needs no thread. The work is already async, so
    `asyncio.wait_for` cancels it properly and no socket is left held.

    Args:
        awaitable: Coroutine to run
        timeout: Wall-clock budget in seconds
        provider: Provider name for error attribution
        host: Hostname for error attribution
        operation: Short label for the error message

    Returns:
        Whatever the coroutine returns

    Raises:
        ProviderTimeoutError: If the budget elapses first
        Exception: Whatever the coroutine raised
    """
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "%s %s exceeded its %.0fs total budget", provider, operation, timeout
        )
        raise ProviderTimeoutError(
            provider,
            host,
            f"{operation} exceeded {timeout:g}s",
            reason="search_timeout",
        ) from None
