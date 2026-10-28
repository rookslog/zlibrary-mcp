"""FU-1 falsifiers. Real saved HTML; browser/transport boundaries are fakes."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.annas_browser import AnnasBrowserSession, is_browser_check
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError

PAGES = Path(__file__).parent / "fixtures" / "annas-browser"


def saved(name):
    return (PAGES / name).read_text()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("status", [403, 429, 502, 503])
async def test_guarded_ordinary_error_never_launches_browser(enabled, status, tmp_path):
    config = SourceConfig(
        annas_browser_enabled=enabled,
        annas_browser_profile_dir=str(tmp_path / "profile"),
    )
    adapter = AnnasArchiveAdapter(config)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                status,
                text=saved("kant-settled.html"),
                headers={"server": "ddos-guard"},
            )
        )
    )
    browser = SimpleNamespace(
        search_html=AsyncMock(return_value=saved("kant-settled.html")),
        close=AsyncMock(),
    )
    adapter._browser = browser
    try:
        with pytest.raises(ProviderResponseError) as caught:
            await adapter.search("kant")
        # Integration decision: retain the live public cause and status while
        # ordinary origin errors still never qualify for browser launch.
        assert caught.value.reason == "browser_check"
        assert caught.value.status_code == status
        assert caught.value.to_dict()["status_code"] == status
        assert browser.search_html.await_count == 0
    finally:
        await adapter.close()


@pytest.mark.parametrize("status", [200, 403, 502])
@pytest.mark.parametrize(
    "mime", ["text/html", "text/plain", "application/octet-stream", ""]
)
def test_real_challenge_kept_without_header(status, mime):
    response = httpx.Response(
        status, text=saved("cold-human.html"), headers={"content-type": mime}
    )
    assert is_browser_check(response)


@pytest.mark.parametrize("bad_path", ["parent", "profile", "lock", "lock_operation"])
async def test_unusable_paths_are_immediate_configuration_errors(
    tmp_path, monkeypatch, bad_path
):
    import errno
    import time
    import lib.sources.annas_usage as usage_module
    from lib.sources.errors import ProviderConfigurationError
    from lib.sources.net import WalkDeadline

    profile = tmp_path / "state" / "profile"
    if bad_path == "parent":
        (tmp_path / "state").write_text("occupied")
    elif bad_path == "profile":
        profile.parent.mkdir()
        profile.write_text("occupied")
    elif bad_path == "lock":
        profile.parent.mkdir()
        (profile.parent / "annas-browser.lock").mkdir()
    else:

        def unsupported(fd):
            raise OSError(errno.ENOTSUP, "locking unsupported")

        monkeypatch.setattr(usage_module, "_try_lock", unsupported)
    session = AnnasBrowserSession(SourceConfig(annas_browser_profile_dir=str(profile)))
    launch = AsyncMock()
    monkeypatch.setattr(session, "_ensure_context", launch)
    started = time.monotonic()
    with pytest.raises(ProviderConfigurationError):
        await session.search_html("kant", deadline=WalkDeadline(0.2))
    assert time.monotonic() - started < 0.1
    assert launch.await_count == 0
    assert session._process_lock._fd is None


@pytest.mark.parametrize("wait_kind", ["penalty", "spacing"])
async def test_wait_beyond_deadline_refuses_without_spending(
    tmp_path, monkeypatch, wait_kind
):
    import time
    import lib.sources.annas_browser as browser_module
    from lib.sources.annas_usage import DailyUsage
    from lib.sources.net import WalkDeadline

    state = tmp_path / "annas-browser-usage.json"
    usage = DailyUsage(str(state))
    if wait_kind == "penalty":
        usage.penalise(300)
    else:
        usage.spend(30, 20)
    before = state.read_bytes()
    session = AnnasBrowserSession(
        SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    )
    launch = AsyncMock()
    monkeypatch.setattr(session, "_ensure_context", launch)
    proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    proxy.sleep = AsyncMock(side_effect=AssertionError("refusal must not sleep"))
    monkeypatch.setattr(browser_module, "asyncio", proxy)
    started = time.monotonic()
    for _ in range(2):
        with pytest.raises(ProviderResponseError) as caught:
            await session.search_html("kant", deadline=WalkDeadline(0.2))
        assert caught.value.reason == "browser_check"
        assert "retry" in caught.value.detail.lower() and "UTC" in caught.value.detail
        assert state.read_bytes() == before
    print(
        f"immediate backoff refusal elapsed={time.monotonic() - started:.6f}s; no sleep requested"
    )
    assert proxy.sleep.await_count == 0
    assert launch.await_count == 0


async def test_settled_result_manual_phrase_is_not_a_human_check(tmp_path, monkeypatch):
    from lib.sources.annas_browser import needs_human_check

    page_html = saved("kant-manual-phrase-variant.html")
    assert not needs_human_check(page_html)
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=403)),
        content=AsyncMock(return_value=page_html),
        close=AsyncMock(),
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    session = AnnasBrowserSession(
        SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    )
    monkeypatch.setattr(session, "_ensure_context", AsyncMock(return_value=context))
    assert await session.search_html("kant") == page_html
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["spent"] == 1
    assert state["next_allowed"] - state["window_started"] < 25


@pytest.mark.parametrize("stuck_navigation", [False, True])
async def test_slow_closes_fit_owner_clock_and_force_before_unlock(
    tmp_path, monkeypatch, stuck_navigation
):
    import time
    from lib.sources.net import WalkDeadline
    from lib.sources.errors import ProviderTimeoutError

    session = AnnasBrowserSession(
        SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    )
    events = []

    async def slow_close(name):
        events.append((name, session._process_lock._fd is not None))
        await asyncio.sleep(0.15)

    async def goto(*args, **kwargs):
        if stuck_navigation:
            await asyncio.sleep(5)
        return SimpleNamespace(status=403)

    page = SimpleNamespace(
        goto=goto,
        content=AsyncMock(return_value=saved("kant-settled.html")),
        close=lambda: slow_close("page"),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), close=lambda: slow_close("context")
    )
    session._context = context
    session._playwright = SimpleNamespace(stop=lambda: slow_close("driver"))

    def force(*args):
        events.append(("force", session._process_lock._fd is not None))

    monkeypatch.setattr(session, "_force_kill_owned", force, raising=False)
    if not stuck_navigation:
        # Isolate close timing from synchronous parsing of the large capture.
        monkeypatch.setattr(
            session, "_visit", AsyncMock(return_value=saved("kant-settled.html"))
        )
    started = time.monotonic()
    if stuck_navigation:
        with pytest.raises(ProviderTimeoutError):
            await session.search_html("kant", deadline=WalkDeadline(0.12))
    else:
        assert await session.search_html("kant", deadline=WalkDeadline(0.12)) == saved(
            "kant-settled.html"
        )
    elapsed = time.monotonic() - started
    print(
        f"cleanup owner=0.12s elapsed={elapsed:.6f}s stuck_navigation={stuck_navigation} events={events}"
    )
    assert elapsed < 0.22
    assert [name for name, _ in events] == ["page", "context", "driver", "force"]
    assert all(held for _, held in events)
    assert session._process_lock._fd is None
    assert session._context is None and session._playwright is None


@pytest.mark.parametrize(
    "enabled,setting,expected",
    [(False, "120", 45), (True, "120", 120), (True, "180", 180)],
)
async def test_browser_budget_covers_l6_without_increasing_http_budget(
    tmp_path, monkeypatch, enabled, setting, expected
):
    from lib.sources.config import get_source_config
    from lib.sources.net import WalkDeadline
    import lib.sources.annas as adapter_module

    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("ANNAS_BROWSER_SEARCH_TIMEOUT", setting)
    config = get_source_config()
    assert config.total_timeout == 45
    adapter = AnnasArchiveAdapter(config)
    seen = []
    original_bound = adapter_module.bounded_await

    async def record_bound(awaitable, timeout, **kwargs):
        seen.append(timeout)
        return await original_bound(awaitable, timeout, **kwargs)

    monkeypatch.setattr(adapter_module, "bounded_await", record_bound)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, text=saved("cold-human.html"))
        )
    )
    remaining = []

    async def search_html(query, deadline):
        remaining.append(deadline.remaining())
        return saved("kant-settled.html")

    adapter._browser = SimpleNamespace(search_html=search_html, close=AsyncMock())
    try:
        if enabled:
            await adapter.search("kant", deadline=WalkDeadline(210))
            assert expected - 2 < remaining[0] <= expected
            assert remaining[0] > 84.883  # Largest measured L6 capture upper bound.
        else:
            with pytest.raises(ProviderResponseError):
                await adapter.search("kant", deadline=WalkDeadline(210))
        assert 44 < seen[0] <= 45
    finally:
        await adapter.close()


async def test_default_settle_can_cover_simulated_85_second_capture(
    tmp_path, monkeypatch
):
    import lib.sources.annas_browser as browser_module

    clock = [0.0]
    original_asyncio = asyncio

    async def advance(seconds):
        clock[0] += seconds
        await original_asyncio.sleep(0)

    proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    proxy.get_running_loop = lambda: SimpleNamespace(time=lambda: clock[0])
    proxy.sleep = advance
    monkeypatch.setattr(browser_module, "asyncio", proxy)

    async def content():
        if clock[0] < 85:
            raise RuntimeError("automatic navigation not settled")
        return saved("kant-settled.html")

    session = AnnasBrowserSession(
        SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    )
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=403)), content=content
    )
    assert await session._visit(
        page, "https://annas-archive.gl/search?q=kant"
    ) == saved("kant-settled.html")
    assert clock[0] == 85


@pytest.mark.parametrize("parent_exited", [False, True])
def test_force_cleanup_targets_only_pinned_owned_processes(monkeypatch, parent_exited):
    import os
    import select
    import signal

    driver = SimpleNamespace(
        pid=701,
        returncode=None,
        kill=lambda: pytest.fail("unexpected raw PID fallback"),
    )
    records = {
        "/proc/701/stat": f"701 (node) S {os.getpid()}",
        "/proc/701/task/701/children": "702 999",
        "/proc/702/stat": "702 (chrome) S 701",
        "/proc/702/task/702/children": "703",
        "/proc/703/stat": "703 (chrome) S 702",
        "/proc/703/task/703/children": "",
        "/proc/999/stat": "999 (unrelated) S 1",
    }
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: records[str(self)])
    opened, closed, signals = [], [], []

    def pin(pid):
        opened.append(pid)
        return pid + 1000

    monkeypatch.setattr(os, "pidfd_open", pin, raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(
        select, "select", lambda ready, *a: (ready if parent_exited else [], [], [])
    )
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, sig: signals.append((fd, sig)),
        raising=False,
    )
    pinned = AnnasBrowserSession._pin_owned_processes(driver)
    AnnasBrowserSession._force_kill_owned(pinned, driver)
    if parent_exited:
        assert pinned == [1701] and opened == [701]
    else:
        assert pinned == [1701, 1702, 1703]
        assert closed == [1999]  # Mismatched parent identity is never accepted.
    assert signals == [(fd, signal.SIGKILL) for fd in reversed(pinned)]


def test_non_pidfd_fallback_kills_only_the_owned_child_driver():
    killed = []
    driver = SimpleNamespace(returncode=None, kill=lambda: killed.append("driver"))
    AnnasBrowserSession._force_kill_owned([], driver)
    assert killed == ["driver"]
