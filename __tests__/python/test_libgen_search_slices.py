"""Offline comparison-search mirror budgets, with one injected monotonic clock."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.sources import libgen
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderTimeoutError
from lib.sources.net import WalkDeadline

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.value = 100.0

    def now(self):
        return self.value

    def spend(self, cost):
        self.value += cost


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preflight", "pacing", "search", "slow_pacing"])
async def test_comparison_mirror_slice_covers_every_phase_and_reaches_second(
    monkeypatch, phase
):
    clock = Clock()
    adapter = libgen.LibgenAdapter(
        SourceConfig(preflight_enabled=True, total_timeout=45)
    )
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: ["li", "vg"])
    monkeypatch.setattr(libgen.time, "time", clock.now)
    probes, calls, bounds, sleeps = [], [], [], []
    current = {"mirror": None}

    async def preflight(mirror):
        probes.append(mirror)
        current["mirror"] = mirror
        clock.spend(
            10
            if phase == "preflight" and mirror == "li"
            else 0.5
            if phase == "pacing"
            else 0.2
        )

    async def sleep(delay):
        sleeps.append(delay)
        clock.spend(delay + 0.0001)

    async def bounded(awaitable, timeout, **kwargs):
        start = clock.now()
        bounds.append((current["mirror"], kwargs["operation"], timeout))
        result = await awaitable
        if clock.now() - start > timeout + 1e-8:
            clock.value = start + timeout
            raise ProviderTimeoutError(
                "libgen", kwargs["host"], "slice elapsed", reason="search_timeout"
            )
        return result

    book = SimpleNamespace(
        md5="a" * 32,
        title="Solaris",
        author="Lem",
        year="1961",
        extension="pdf",
        size="1 MB",
        tor_download_link="",
        id="1",
        language="French",
        pages="100",
    )

    class Search:
        def __init__(self, mirror):
            self.mirror = mirror

        def search_default(self, query):
            return [book]

    async def worker(func, timeout, **kwargs):
        calls.append((kwargs["host"], timeout))
        if kwargs["host"] == "libgen.li":
            clock.spend(timeout)
            raise ProviderTimeoutError(
                "libgen", kwargs["host"], "stalled search", reason="search_timeout"
            )
        clock.spend(0.1)
        return func()

    if phase == "pacing":
        adapter._last_request = clock.now()
    if phase == "slow_pacing":

        async def rate(deadline, **kwargs):
            clock.spend(10 if current["mirror"] == "li" else 0.1)
            return True

        monkeypatch.setattr(adapter, "_rate_limit", rate)
    monkeypatch.setattr(adapter, "_preflight", preflight)
    monkeypatch.setattr(libgen.asyncio, "sleep", sleep)
    monkeypatch.setattr(libgen, "bounded_await", bounded)
    monkeypatch.setattr(libgen, "run_bounded", worker)
    monkeypatch.setattr(libgen, "LibgenSearch", Search)
    rows = await adapter.search(
        "Solaris", deadline=WalkDeadline(12, clock=clock.now), multi_source=True
    )
    assert rows[0].title == "Solaris"
    assert probes == ["li", "vg"]
    assert clock.now() - 100 < 10.1
    assert calls[-1][0] == "libgen.vg"
    assert all(0 < budget <= 5 for _, budget in calls)
    assert bounds, "preflight and pacing must be explicitly bounded"
    if phase == "preflight":
        assert len(calls) == 1
    elif phase == "pacing":
        assert sleeps
        assert calls[0][1] == pytest.approx(3, abs=0.001)
    elif phase == "search":
        assert calls[0][1] == pytest.approx(4.8)
    elif phase == "slow_pacing":
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_single_source_retains_full_existing_attempt_budget(monkeypatch):
    adapter = libgen.LibgenAdapter(
        SourceConfig(preflight_enabled=False, total_timeout=45)
    )
    monkeypatch.setattr(adapter, "_preflight", AsyncMock())
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=True))
    captured = []

    async def worker(func, timeout, **kwargs):
        captured.append(timeout)
        return [], None, 0

    monkeypatch.setattr(libgen, "run_bounded", worker)
    assert await adapter.search("Solaris") == []
    assert captured == [45]


@pytest.mark.parametrize("parent_budget,child_budget", [(12, 3), (2, 3)])
def test_child_deadline_uses_parent_clock_and_never_extends_parent(
    parent_budget, child_budget
):
    clock = Clock()
    parent = WalkDeadline(parent_budget, clock=clock.now)
    child = parent.child(child_budget)
    clock.spend(1)
    assert child.remaining() == min(parent_budget, child_budget) - 1
    clock.spend(10)
    assert child.remaining() == 0


@pytest.mark.asyncio
async def test_bridge_forwards_opt_in_without_reinterpreting_router_source(monkeypatch):
    from lib import python_bridge

    router = SimpleNamespace(search=AsyncMock(return_value=[]))
    monkeypatch.setattr(
        python_bridge, "get_source_router", AsyncMock(return_value=router)
    )
    assert await python_bridge.search_multi_source(
        "Solaris", source="libgen", multi_source=True
    ) == {"books": [], "sources_used": []}
    router.search.assert_awaited_once_with(
        "Solaris", source="libgen", multi_source=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["warm", "starved"])
@pytest.mark.parametrize("real_clock", [False, True])
async def test_pacing_slack_reaches_healthy_second_mirror(
    monkeypatch, scenario, real_clock
):
    """A warm wait or quick first failure must leave 1.8 s for mirror two."""
    import asyncio
    import time

    clock = Clock()
    now = time.monotonic if real_clock else clock.now
    sleep = asyncio.sleep
    started = now()
    calls = []
    adapter = libgen.LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: ["li", "vg"])
    monkeypatch.setattr(adapter, "_preflight", AsyncMock())
    monkeypatch.setattr(libgen.time, "time", now)
    if scenario == "warm":
        adapter._last_request = now()

    async def overshooting_sleep(delay):
        if real_clock:
            await sleep(delay + 0.02)
        else:
            clock.spend(delay + 0.02)

    monkeypatch.setattr(libgen.asyncio, "sleep", overshooting_sleep)

    class Search:
        def __init__(self, mirror):
            self.mirror = mirror

        def search_default(self, query):
            calls.append((self.mirror, now() - started))
            cost = 0.2 if self.mirror == "li" else 1.8
            if real_clock:
                time.sleep(cost)
            else:
                clock.spend(cost)
            if self.mirror == "li":
                raise RuntimeError("synthetic first-mirror failure")
            return []

    monkeypatch.setattr(libgen, "LibgenSearch", Search)
    if not real_clock:

        async def worker(func, timeout, **kwargs):
            before = now()
            result = func()
            if now() - before > timeout:
                raise ProviderTimeoutError("libgen", kwargs["host"], "elapsed")
            return result

        monkeypatch.setattr(libgen, "run_bounded", worker)
    rows = await adapter.search(
        "Solaris", deadline=WalkDeadline(12, clock=now), multi_source=True
    )
    assert rows == []
    assert [mirror for mirror, _ in calls] == ["li", "vg"]
    assert calls[1][1] < 12
    assert now() - started < 12
    print(
        f"{scenario=} {real_clock=} mirror_two_s={calls[1][1]:.4f} total_s={now() - started:.4f}"
    )


@pytest.mark.asyncio
async def test_unissued_search_does_not_stamp_last_request(monkeypatch):
    clock = Clock()
    adapter = libgen.LibgenAdapter(SourceConfig(preflight_enabled=False))
    adapter._last_request = 98.0
    monkeypatch.setattr(libgen.time, "time", clock.now)
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: ["li", "vg"])
    monkeypatch.setattr(adapter, "_preflight", AsyncMock())

    async def sleep(delay):
        clock.spend(delay)

    async def exhausted(awaitable, timeout, **kwargs):
        result = await awaitable
        if kwargs["operation"] == "search pacing":
            clock.spend(timeout)
        return result

    monkeypatch.setattr(libgen.asyncio, "sleep", sleep)
    monkeypatch.setattr(libgen, "bounded_await", exhausted)
    worker = AsyncMock()
    monkeypatch.setattr(libgen, "run_bounded", worker)
    with pytest.raises(libgen.AllSourcesFailedError):
        await adapter.search(
            "Solaris", deadline=WalkDeadline(12, clock=clock.now), multi_source=True
        )
    worker.assert_not_awaited()
    assert adapter._last_request == 98.0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preflight", "pacing"])
async def test_g5_default_timeout_propagates_without_walking(monkeypatch, phase):
    """P6/P7: the default route must not swallow either admission timeout."""
    adapter = libgen.LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: ["li", "vg"])
    error = ProviderTimeoutError("libgen", "libgen.li", "synthetic timeout")
    preflight = AsyncMock(side_effect=error if phase == "preflight" else None)
    pacing = AsyncMock(side_effect=error if phase == "pacing" else None)
    worker = AsyncMock()
    monkeypatch.setattr(adapter, "_preflight", preflight)
    monkeypatch.setattr(adapter, "_rate_limit", pacing)
    monkeypatch.setattr(libgen, "run_bounded", worker)
    with pytest.raises(ProviderTimeoutError) as caught:
        await adapter.search("Solaris")
    assert caught.value is error
    assert preflight.await_count == 1
    worker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["exhausted", "refused", "parent_pacing"])
async def test_g5_comparison_admission_skips_only_current_mirror(monkeypatch, branch):
    """P8/P9 continue; P5 must refuse pacing that cannot fit the child slice."""
    clock = Clock()
    adapter = libgen.LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: ["li", "vg"])
    monkeypatch.setattr(libgen.time, "time", clock.now)
    current = {"mirror": None}
    sleeps, calls = [], []

    async def preflight(mirror):
        current["mirror"] = mirror
        if branch == "parent_pacing" and mirror == "li":
            clock.spend(3.5)
            adapter._last_request = clock.now()

    async def sleep(delay):
        sleeps.append(current["mirror"])
        clock.spend(delay + 0.0001)

    async def bounded(awaitable, timeout, **kwargs):
        start = clock.now()
        result = await awaitable
        if current["mirror"] == "li" and kwargs["operation"] == "search pacing":
            if branch == "exhausted":
                clock.spend(timeout)
            elif clock.now() - start > timeout:
                clock.value = start + timeout
                raise ProviderTimeoutError("libgen", kwargs["host"], "pacing elapsed")
        return result

    async def worker(func, timeout, **kwargs):
        calls.append(kwargs["host"])
        return [], None, 0

    if branch == "refused":

        async def pacing(*args, **kwargs):
            return current["mirror"] != "li"

        monkeypatch.setattr(adapter, "_rate_limit", pacing)
    monkeypatch.setattr(adapter, "_preflight", preflight)
    monkeypatch.setattr(libgen.asyncio, "sleep", sleep)
    monkeypatch.setattr(libgen, "bounded_await", bounded)
    monkeypatch.setattr(libgen, "run_bounded", worker)
    assert (
        await adapter.search(
            "Solaris", deadline=WalkDeadline(12, clock=clock.now), multi_source=True
        )
        == []
    )
    assert calls == ["libgen.vg"]
    assert "li" not in sleeps
    assert clock.now() - 100 < 12


@pytest.mark.asyncio
async def test_g5_comparison_uses_short_minimum_before_preflight(monkeypatch):
    """P10: a short remaining budget can still run a comparison mirror."""
    adapter = libgen.LibgenAdapter(
        SourceConfig(preflight_enabled=True, preflight_timeout=4)
    )
    monkeypatch.setattr(libgen, "proxy_in_use", lambda url: False)
    monkeypatch.setattr(adapter, "_preflight", AsyncMock())
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=True))
    worker = AsyncMock(return_value=([], None, 0))
    monkeypatch.setattr(libgen, "run_bounded", worker)
    assert (
        await adapter.search("Solaris", deadline=WalkDeadline(2), multi_source=True)
        == []
    )
    assert worker.await_count == 1
    assert 0 < worker.call_args.args[1] <= 2
