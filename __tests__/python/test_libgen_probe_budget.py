"""Real preflight verdicts must precede a comparison slice's backstop."""

import asyncio
import socket
import threading

import pytest

from lib.sources import libgen, net
from lib.sources.config import SourceConfig
from lib.sources.errors import AllSourcesFailedError, ProviderUnreachableError

pytestmark = [pytest.mark.unit, pytest.mark.real_preflight]


@pytest.fixture
def wire(monkeypatch):
    """Replace only DNS/TCP I/O; retain probe, thread and deadline machinery."""
    net.reset_probe_cache()
    release = threading.Event()
    state = {"phase": "connect", "healthy": False, "dns": 0, "tcp": 0}
    monkeypatch.setattr(net, "proxy_in_use", lambda _url: False)

    def resolve(*_args, **_kwargs):
        state["dns"] += 1
        if state["phase"] == "dns" and not state["healthy"]:
            release.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

    class Writer:
        def __init__(self, sock):
            self.sock = sock

        def close(self):
            self.sock.close()

        async def wait_closed(self):
            pass

    async def connect(_loop, sock, _address):
        state["tcp"] += 1
        if not state["healthy"]:
            await asyncio.Event().wait()
        return object(), Writer(sock)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(net, "_open_resolved_connection", connect)
    yield state
    release.set()
    net.reset_probe_cache()


def adapter(monkeypatch, *, preflight=0.35, slice_seconds=0.35):
    instance = libgen.LibgenAdapter(
        SourceConfig(preflight_enabled=True, preflight_timeout=preflight)
    )
    monkeypatch.setattr(instance, "_mirror_candidates", lambda: ["li"])
    # Equal configured clocks reproduce the default5s/5s race in less time.
    monkeypatch.setattr(libgen, "MULTI_SOURCE_MIRROR_SECONDS", slice_seconds)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dns", "connect"])
async def test_j1_real_probe_typed_timeout_beats_equal_mirror_slice(
    monkeypatch, wire, phase
):
    wire["phase"] = phase
    instance = (
        adapter(
            monkeypatch,
            preflight=SourceConfig().preflight_timeout,
            slice_seconds=libgen.MULTI_SOURCE_MIRROR_SECONDS,
        )
        if phase == "connect"
        else adapter(monkeypatch)
    )
    with pytest.raises(AllSourcesFailedError) as failure:
        await instance.search("Solaris", multi_source=True)
    assert [item.reason for item in failure.value.failures] == [f"{phase}_timeout"]


@pytest.mark.asyncio
async def test_j1_clipped_failure_cannot_poison_a_later_full_budget_probe(
    monkeypatch, wire
):
    instance = adapter(monkeypatch)
    with pytest.raises(AllSourcesFailedError):
        await instance.search("Solaris", multi_source=True)
    before = wire["dns"]
    wire["healthy"] = True
    # This must really probe again rather than rethrowing the clipped verdict.
    await instance._preflight("li")
    assert wire["dns"] == before + 1


@pytest.mark.asyncio
async def test_j1_unclipped_probe_retains_existing_failure_cache(monkeypatch, wire):
    instance = adapter(monkeypatch, preflight=0.1)
    with pytest.raises(AllSourcesFailedError) as failure:
        await instance.search("Solaris", multi_source=True)
    assert failure.value.failures[0].reason == "connect_timeout"
    before = wire["dns"]
    wire["healthy"] = True
    with pytest.raises(ProviderUnreachableError, match="TCP"):
        await instance._preflight("li")
    assert wire["dns"] == before


@pytest.mark.asyncio
async def test_j1_clipped_success_does_not_hide_a_later_host_failure(monkeypatch, wire):
    instance = adapter(monkeypatch)
    wire["healthy"] = True
    await instance._preflight("li", 0.15)
    wire["healthy"] = False
    with pytest.raises(ProviderUnreachableError, match="TCP"):
        await instance._preflight("li")


@pytest.mark.asyncio
async def test_j1_clipped_probe_can_reuse_an_existing_full_budget_verdict(
    monkeypatch, wire
):
    instance = adapter(monkeypatch)
    wire["healthy"] = True
    await instance._preflight("li")
    before = wire["dns"]
    wire["healthy"] = False
    await instance._preflight("li", 0.15)
    assert wire["dns"] == before


@pytest.mark.asyncio
async def test_j1_no_probe_headroom_is_local_refusal_without_network(monkeypatch, wire):
    instance = adapter(monkeypatch, slice_seconds=0.08)
    with pytest.raises(AllSourcesFailedError) as failure:
        await instance.search("Solaris", multi_source=True)
    assert failure.value.failures[0].reason == "mirror_slice_exhausted"
    assert wire["dns"] == 0


@pytest.mark.asyncio
async def test_j1_outer_backstop_stays_local_when_preflight_does_not_settle(
    monkeypatch,
):
    instance = adapter(monkeypatch, slice_seconds=0.08)

    async def stalled(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(instance, "_preflight", stalled)
    with pytest.raises(AllSourcesFailedError) as failure:
        await instance.search("Solaris", multi_source=True)
    assert failure.value.failures[0].reason == "mirror_slice_exhausted"
