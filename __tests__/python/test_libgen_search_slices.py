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
        clock.spend(delay)

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
    assert clock.now() - 100 < 6.1
    assert calls[-1][0] == "libgen.vg"
    assert all(0 < budget <= 3 for _, budget in calls)
    assert bounds, "preflight and pacing must be explicitly bounded"
    if phase == "preflight":
        assert len(calls) == 1
    elif phase == "pacing":
        assert sleeps
        assert calls[0][1] == pytest.approx(1)
    elif phase == "search":
        assert calls[0][1] == pytest.approx(2.8)
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
