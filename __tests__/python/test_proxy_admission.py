"""Offline #282 admission regressions; real proxy selection and fake I/O."""

from unittest.mock import MagicMock

import httpx
import pytest

from lib.sources import annas, libgen, net
from lib.sources.config import SourceConfig
from lib.sources.errors import AllSourcesFailedError, ProviderTimeoutError
from lib.sources.net import WalkDeadline


@pytest.fixture
def proxy_route(monkeypatch):
    """Use real probe_host/proxy_in_use, replacing only environment and sockets."""
    probes = []
    now = [0.0]

    def configure(proxies, bypass=(), probe_cost=0.0):
        monkeypatch.setattr(net.urllib.request, "getproxies", lambda: proxies)
        monkeypatch.setattr(
            net.urllib.request, "proxy_bypass", lambda host: host in bypass
        )

        async def probe(provider, host, port, timeout):
            probes.append((host, port))
            now[0] += probe_cost
            return None

        monkeypatch.setattr(net, "_probe_host_uncached", probe)
        net.reset_probe_cache()
        return now, probes

    yield configure
    net.reset_probe_cache()


@pytest.mark.real_preflight
@pytest.mark.parametrize("operation", ["search", "download"])
@pytest.mark.parametrize(
    "mode,left,expected_mirrors,expected_probes",
    [
        ("proxied", 2.0, ["li"], []),
        ("mixed", 9.0, ["li", "vg"], [("libgen.li", 443)]),
        ("direct", 2.0, [], []),
        ("bypassed", 2.0, [], []),
        ("disabled", 2.0, ["li"], []),
        ("too_short", 0.999, [], []),
    ],
)
async def test_libgen_admission_follows_each_mirrors_proxy_route(
    monkeypatch, proxy_route, operation, mode, left, expected_mirrors, expected_probes
):
    """Catch charging skipped preflight, or applying one mirror's route to all."""
    now, probes = proxy_route(
        {} if mode in ("direct", "disabled") else {"https": "http://proxy.invalid"},
        bypass=("libgen.li",) if mode in ("mixed", "bypassed") else (),
        probe_cost=5.0 if mode == "mixed" else 0.0,
    )
    adapter = libgen.LibgenAdapter(SourceConfig(preflight_enabled=mode != "disabled"))
    mirrors = []

    def search_factory(*, mirror):
        mirrors.append(mirror)
        if mode == "mixed" and mirror == "li":
            now[0] += 2.0
            raise libgen.ProviderUnreachableError("libgen", "libgen.li")
        client = MagicMock()
        client.search_default.return_value = []
        return client

    async def resolve(client, mirror, md5):
        mirrors.append(mirror)
        if mode == "mixed" and mirror == "li":
            now[0] += 2.0
        return (
            (None, "synthetic missing key")
            if mode == "mixed" and mirror == "li"
            else ("KEY", "")
        )

    async def serves_bytes(*args):
        return True, "fixture bytes"

    monkeypatch.setattr(libgen, "LibgenSearch", search_factory)
    monkeypatch.setattr(adapter, "_resolve_key", resolve)
    monkeypatch.setattr(adapter, "_serves_bytes", serves_bytes)
    # Keep production pacing; mirror fallback has already spent five clock seconds.
    monkeypatch.setattr(libgen.time, "time", lambda: 100.0 + now[0])
    deadline = WalkDeadline(left, clock=lambda: now[0])

    async def run():
        if operation == "search":
            assert await adapter.search("fixture", deadline=deadline) == []
        else:
            candidates = adapter.iter_download_candidates("a" * 32, deadline=deadline)
            try:
                result = await anext(candidates)
                assert result.url.startswith(
                    f"https://libgen.{expected_mirrors[-1]}/get.php?"
                )
            finally:
                await candidates.aclose()

    if expected_mirrors:
        await run()
    else:
        with pytest.raises(AllSourcesFailedError) as error:
            await run()
        assert error.value.failures[0].reason == "walk_budget_exhausted"
    assert mirrors == expected_mirrors
    assert probes == expected_probes


@pytest.mark.real_preflight
@pytest.mark.parametrize("operation", ["search", "get_download_url"])
@pytest.mark.parametrize(
    "mode,left,admitted",
    [
        ("proxied", 2.0, True),
        ("direct", 2.0, False),
        ("bypassed", 2.0, False),
        ("disabled", 2.0, True),
        ("too_short", 0.999, False),
    ],
)
async def test_annas_admission_follows_proxy_route(
    monkeypatch, proxy_route, operation, mode, left, admitted
):
    """Catch skipped preflight consuming the last useful Anna's slice."""
    _, probes = proxy_route(
        {} if mode in ("direct", "disabled") else {"https": "http://proxy.invalid"},
        bypass=("annas-archive.gl",) if mode == "bypassed" else (),
    )
    adapter = annas.AnnasArchiveAdapter(
        SourceConfig(
            preflight_enabled=mode != "disabled",
            # This case measures anonymous search admission, not member sign-in.
            annas_secret_key="synthetic" if operation == "get_download_url" else "",
        )
    )
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if operation == "search":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(
            200, json={"download_url": "https://download.invalid/book.pdf"}
        )

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        if admitted:
            result = await getattr(adapter, operation)(
                "a" * 32, deadline=WalkDeadline(left, clock=lambda: 0)
            )
            if operation == "search":
                assert result == []
            else:
                assert result.url == "https://download.invalid/book.pdf"
        else:
            with pytest.raises(ProviderTimeoutError) as error:
                await getattr(adapter, operation)(
                    "a" * 32, deadline=WalkDeadline(left, clock=lambda: 0)
                )
            assert error.value.reason == "walk_budget_exhausted"
    finally:
        await adapter.close()
    assert requests == (
        ["/search" if operation == "search" else "/dyn/api/fast_download.json"]
        if admitted
        else []
    )
    assert probes == []


@pytest.mark.real_preflight
async def test_annas_proxy_admission_uses_configured_scheme_and_port(proxy_route):
    """Catch assuming HTTPS/443 while probe_host skips an HTTP mirror probe."""
    _, probes = proxy_route({"http": "http://proxy.invalid"})
    adapter = annas.AnnasArchiveAdapter(
        SourceConfig(annas_base_url="http://mirror.invalid:8080")
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=""))
    )
    try:
        assert (
            await adapter.search("fixture", deadline=WalkDeadline(2, clock=lambda: 0))
            == []
        )
    finally:
        await adapter.close()
    assert probes == []
