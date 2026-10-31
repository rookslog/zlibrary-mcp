"""Offline regressions for #282 and #292; clocks never contact providers."""

from contextlib import asynccontextmanager
import json
import os
import subprocess

import pytest

from lib import acquisition as acq
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import AllSourcesFailedError, ProviderTimeoutError
from lib.sources.net import WalkDeadline


@pytest.mark.parametrize("download", [False, True])
def test_source_owner_under_supported_minimum_is_refused(monkeypatch, download):
    """Catch admitting the measured small-owner startup race at all."""
    name = "PYTHON_BRIDGE_LONG_TIMEOUT" if download else "PYTHON_BRIDGE_TIMEOUT"
    monkeypatch.setenv("BOOK_SOURCE_PREFLIGHT", "false")
    monkeypatch.setenv(name, "9999")
    with pytest.raises(ValueError, match=f"{name}.*10000"):
        get_source_config(for_download=download)
    monkeypatch.setenv(name, "10000")
    config = get_source_config(for_download=download)
    assert config.walk_budget > 6.0
    assert config.walk_budget + config.bridge_overhead < 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight,left", [(True, 5.999), (False, 0.999)])
@pytest.mark.parametrize("operation", ["search", "get_download_url"])
async def test_annas_refuses_useless_slice_without_probe_or_request(
    monkeypatch, preflight, left, operation
):
    """Catch reporting local admission as a breaker-counting provider timeout."""
    from lib.sources import annas

    async def forbidden(*args, **kwargs):
        pytest.fail("a useless slice reached provider work")

    adapter = annas.AnnasArchiveAdapter(
        SourceConfig(preflight_enabled=preflight, annas_secret_key="synthetic")
    )
    monkeypatch.setattr(annas, "probe_host", forbidden)
    monkeypatch.setattr(adapter, "_get_client", forbidden)
    with pytest.raises(ProviderTimeoutError) as error:
        await getattr(adapter, operation)(
            "a" * 32, deadline=WalkDeadline(left, clock=lambda: 0)
        )
    assert error.value.reason == "walk_budget_exhausted"
    assert error.value.provider == "annas"
    assert error.value.host == "annas-archive.gl"


def test_both_python_timeout_owners_match_compiled_node(monkeypatch):
    """Catch parser drift, including Python's leading-zero digit limit."""
    from lib.sources.config import _node_bridge_timeout_seconds

    # Import the compiled runner afresh per row: its constants invoke the real
    # private positiveIntEnv. No reimplementation of Node parsing in this test.
    values = [
        None,
        "",
        "0",
        "-1",
        "+1",
        "1.5",
        "1e4",
        "١٢٣",
        "2147483647",
        "2147483648",
        "\ufeff30000\u2029",
        "\u008530000",
        "\u001c30000",
        "9" * 5000,
        "0" * 5000 + "30000",
    ]
    monkeypatch.delenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", raising=False)
    for raw in values:
        env = dict(os.environ)
        for name in ("PYTHON_BRIDGE_TIMEOUT", "PYTHON_BRIDGE_LONG_TIMEOUT"):
            if raw is None:
                env.pop(name, None)
                monkeypatch.delenv(name, raising=False)
            else:
                env[name] = raw
                monkeypatch.setenv(name, raw)
        node = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {DEFAULT_BRIDGE_TIMEOUT_MS as s,LONG_BRIDGE_TIMEOUT_MS as l} "
                "from './dist/lib/python-runner.js'; console.log(JSON.stringify([s/1000,l/1000]));",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        short, long = json.loads(node.stdout)
        assert _node_bridge_timeout_seconds() == short
        assert _node_bridge_timeout_seconds(for_download=True) == long
        assert acq._long_bridge_budget() == long
        # Effective per-call owner takes precedence without changing source envs.
        monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "120000")
        monkeypatch.setenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", raw or "")
        assert acq._long_bridge_budget() == short
        monkeypatch.delenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS")


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer_spends_time", [False, True])
async def test_post_lease_walk_keeps_mirror_attribution(
    tmp_path, monkeypatch, transfer_spends_time
):
    """Catch a fresh inner walk admitting mirrors the outer owner cannot finish."""
    from lib import python_bridge as bridge
    from lib.sources.libgen import LibgenAdapter
    from lib.sources.router import SourceRouter
    from lib.sources.errors import ProviderUnreachableError

    now = [0.0]

    def clock(budget, **kwargs):
        return WalkDeadline(budget, clock=lambda: now[0], **kwargs)

    monkeypatch.setenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", "40000")
    monkeypatch.setattr(acq, "WalkDeadline", clock)
    monkeypatch.setattr("lib.sources.router.WalkDeadline", clock)
    monkeypatch.setattr("lib.sources.libgen.WalkDeadline", clock)
    lock = acq.destination_lock

    @asynccontextmanager
    async def delayed_lock(*args, **kwargs):
        async with lock(*args, **kwargs):
            # 34.999s outer budget, 4.374875s reserve. The second variant
            # spends another four seconds on a yielded transfer.
            now[0] = 25.0 if transfer_spends_time else 29.0
            yield

    config = SourceConfig(preflight_enabled=False, walk_budget=165.0)
    router = SourceRouter(config)
    adapter = LibgenAdapter(config)
    router._libgen = adapter

    async def get_router(**kwargs):
        return router

    async def resolve(*args):
        now[0] += 1.0
        return (
            "synthetic-key" if transfer_spends_time else None
        ), "synthetic missing key"

    async def serves_bytes(*args):
        return True, "fixture bytes"

    async def failed_transfer(*args, **kwargs):
        now[0] += 4.0
        raise ProviderUnreachableError("libgen", "libgen.li", reason="connect_failure")

    allocations = []
    bounded = bridge.bounded_await

    async def observe_chain(awaitable, timeout, **kwargs):
        allocations.append(timeout)
        return await bounded(awaitable, timeout, **kwargs)

    async def no_rate_limit(*args, **kwargs):
        return True

    monkeypatch.setattr(acq, "destination_lock", delayed_lock)
    monkeypatch.setattr(bridge, "get_source_router", get_router)
    monkeypatch.setattr(adapter, "_resolve_key", resolve)
    monkeypatch.setattr(adapter, "_rate_limit", no_rate_limit)
    monkeypatch.setattr(adapter, "_serves_bytes", serves_bytes)
    monkeypatch.setattr(bridge, "_download_url_to_file", failed_transfer)
    monkeypatch.setattr(bridge, "bounded_await", observe_chain)

    async def transfer(book, directory, **kwargs):
        return await bridge._fetch_from_source(book, directory)

    with pytest.raises(AllSourcesFailedError) as error:
        await acq.acquire(
            {"source": "libgen", "md5": "a" * 32, "extension": "pdf"},
            str(tmp_path),
            filename="chosen.pdf",
            transfer=transfer,
        )
    failures = error.value.failures
    assert [(f.host, f.reason) for f in failures] == [
        ("libgen.li", "connect_failure" if transfer_spends_time else "protocol_error"),
        ("", "walk_budget_exhausted"),
    ]
    assert allocations == [
        pytest.approx(5.623125 if transfer_spends_time else 1.623125)
    ]
    assert "vg" in failures[1].detail and "la" in failures[1].detail
    assert not (tmp_path / "chosen.pdf").exists()
    await router.close()
