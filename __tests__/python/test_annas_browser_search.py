"""Offline contracts using Anna HTML captured on 2026-10-23.

The reader adds marked synthetic completion metadata to older trimmed fakes;
the original capture files remain unchanged.

Breaks caught: header rows leaking into results, raw-script wall matches,
opt-out launches, manual-check interaction, timeout/lock leaks, lost pacing.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from annas_browser_test_fixtures import read_browser_fixture

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from lib.sources.net import WalkDeadline

PAGES = Path(__file__).parent / "fixtures" / "annas-browser"


def saved(name):
    return read_browser_fixture(PAGES / name)


def config(tmp_path, **kwargs):
    return SourceConfig(
        annas_browser_enabled=True,
        annas_browser_profile_dir=str(tmp_path / "profile"),
        annas_browser_min_interval=0.001,
        annas_browser_settle_seconds=0.02,
        annas_browser_nav_timeout=0.1,
        **kwargs,
    )


class Page:
    """Browser boundary fake: only saved real documents; no interaction API."""

    def __init__(self, name="kant-settled.html", status=403):
        self.html = saved(name)
        self.status = status
        self.closed = False
        self.urls = []

    async def goto(self, url, **kwargs):
        self.urls.append(url)
        return SimpleNamespace(status=self.status)

    async def content(self):
        return self.html

    async def close(self):
        self.closed = True


def session_with_page(monkeypatch, cfg, page):
    from lib.sources.annas_browser import AnnasBrowserSession

    session = AnnasBrowserSession(cfg)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(session, "_ensure_context", AsyncMock(return_value=context))
    return session


@pytest.mark.parametrize(
    "name,count", [("frankenstein-settled.html", 50), ("kant-settled.html", 2)]
)
async def test_real_parser_excludes_recent_downloads(name, count):
    adapter = AnnasArchiveAdapter(SourceConfig())
    response = httpx.Response(
        200,
        text=saved(name),
        request=httpx.Request("GET", adapter.base_url + "/search"),
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: response)
    )
    try:
        rows = await adapter.search("fixture")
        assert len(rows) == count
        assert len({r.md5 for r in rows}) == count
        if name == "kant-settled.html":
            from dataclasses import asdict

            row = asdict(rows[1])
            assert row["md5"] == "636d44bc0b81f4b337cf6a85c9d507b0"
            assert row["title"] == "Critique of Pure Reason"
            assert row["author"] == "Immanuel Kant; Paul Guyer, Allen W. Wood"
            assert row["year"] == ""
            assert row["extension"] == ""
            assert "language" not in row["extra"]
            assert row["extra"]["publisher"].endswith(", 1998")
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "name,wall,human",
    [
        ("cold-human.html", True, True),
        ("frankenstein-settled.html", False, False),
        ("kant-settled.html", False, False),
        ("public-domain-detail-settled.html", False, False),
        ("public-domain-partner-settled.html", False, False),
    ],
)
def test_real_page_challenge_detection(name, wall, human):
    from lib.sources.annas_browser import is_browser_check, needs_human_check

    response = httpx.Response(
        200, text=saved(name), headers={"content-type": "text/html"}
    )
    assert is_browser_check(response) is wall
    assert needs_human_check(saved(name)) is human


@pytest.mark.parametrize(
    "enabled,status,name,want_browser",
    [
        (False, 403, "cold-human.html", False),
        (True, 403, "cold-human.html", True),
        (True, 200, "cold-human.html", True),
        (True, 200, "kant-settled.html", False),
        (True, 502, "kant-settled.html", False),
    ],
)
async def test_browser_is_only_used_for_opted_in_http_wall(
    tmp_path, enabled, status, name, want_browser
):
    cfg = config(tmp_path)
    cfg.annas_browser_enabled = enabled
    adapter = AnnasArchiveAdapter(cfg)
    calls = []
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (
                calls.append(str(req.url)) or httpx.Response(status, text=saved(name))
            )
        )
    )
    browser = SimpleNamespace(
        search_html=AsyncMock(return_value=saved("kant-settled.html")),
        close=AsyncMock(),
    )
    adapter._browser = browser
    try:
        if (not enabled and status == 403) or status == 502:
            with pytest.raises(ProviderResponseError) as caught:
                await adapter.search("kant + guyer")
            assert caught.value.reason == (
                "browser_check" if status == 403 else "http_error"
            )
        else:
            rows = await adapter.search("kant + guyer")
            assert len(rows) == 2
        assert len(calls) == 1
        assert browser.search_html.await_count == int(want_browser)
    finally:
        await adapter.close()


async def test_human_check_returns_existing_failure_and_backs_off(
    tmp_path, monkeypatch
):
    page = Page("cold-human.html")
    session = session_with_page(monkeypatch, config(tmp_path), page)
    with pytest.raises(ProviderResponseError) as caught:
        await session.search_html("kant")
    assert caught.value.reason == "browser_check"
    assert set(caught.value.to_dict()) == {
        "provider",
        "host",
        "reason",
        "detail",
        "message",
    }
    assert page.closed
    assert session._process_lock._fd is None
    from lib.sources.annas_usage import DailyUsage

    usage = DailyUsage(str(tmp_path / "annas-browser-usage.json"))
    allowed, remaining, wait = usage.spend(30, 20)
    assert allowed and remaining == 28 and wait > 290


async def test_settled_results_override_initial_403_and_encode_query(
    tmp_path, monkeypatch
):
    page = Page()
    session = session_with_page(monkeypatch, config(tmp_path), page)
    assert await session.search_html("kant & guyer") == saved("kant-settled.html")
    assert page.urls == ["https://annas-archive.gl/search?q=kant%20%26%20guyer"]
    assert page.closed and session._process_lock._fd is None


async def test_shared_deadline_bounds_stuck_browser_and_releases_lock(
    tmp_path, monkeypatch
):
    page = Page()
    session = session_with_page(monkeypatch, config(tmp_path), page)

    async def stuck(*args, **kwargs):
        await asyncio.sleep(5)

    page.goto = stuck
    with pytest.raises(ProviderTimeoutError):
        await session.search_html("kant", deadline=WalkDeadline(0.03))
    assert page.closed and session._process_lock._fd is None


def test_browser_settings_keep_branch_names_and_invalid_limits_fall_back(monkeypatch):
    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", " yes ")
    monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", "~/b4-test-profile")
    monkeypatch.setenv("ANNAS_BROWSER_DAILY_LIMIT", "0")
    monkeypatch.setenv("ANNAS_BROWSER_MIN_INTERVAL", "nan")
    cfg = get_source_config()
    assert cfg.annas_browser_enabled
    assert cfg.annas_browser_profile_dir == str(Path.home() / "b4-test-profile")
    assert cfg.annas_browser_daily_limit == 30
    assert cfg.annas_browser_min_interval == 20


@pytest.mark.parametrize(
    "name", ["public-domain-detail-settled.html", "public-domain-partner-settled.html"]
)
async def test_browser_rejects_nonsearch_pages(tmp_path, monkeypatch, name):
    page = Page(name, status=200)
    session = session_with_page(monkeypatch, config(tmp_path), page)
    with pytest.raises(ProviderResponseError) as caught:
        await session.search_html("kant")
    assert caught.value.reason == "protocol_error"
    assert page.closed and session._process_lock._fd is None


async def test_browser_retries_only_dom_read_during_automatic_navigation(
    tmp_path, monkeypatch
):
    page = Page()
    page.content = AsyncMock(
        side_effect=[RuntimeError("navigation in progress"), saved("kant-settled.html")]
    )
    session = session_with_page(monkeypatch, config(tmp_path), page)
    assert await session.search_html("kant") == saved("kant-settled.html")
    assert len(page.urls) == 1


async def test_launch_is_visible_lazy_and_closes_before_unlock(tmp_path, monkeypatch):
    from lib.sources.annas_browser import AnnasBrowserSession

    cfg = config(tmp_path)
    page = Page()
    session = AnnasBrowserSession(cfg)
    closed_with_lock = []

    async def close_context():
        closed_with_lock.append(session._process_lock._fd is not None)

    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), close=close_context
    )
    launch = AsyncMock(return_value=context)
    driver = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock()
    )
    start = AsyncMock(return_value=driver)
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=start)),
    )
    assert start.await_count == 0
    assert await session.search_html("kant") == saved("kant-settled.html")
    launch.assert_awaited_once_with(
        cfg.annas_browser_profile_dir,
        channel="chrome",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    assert closed_with_lock == [True]
    assert driver.stop.await_count == 1
    assert session._context is None and session._process_lock._fd is None


async def test_missing_optional_dependency_is_configuration_failure(
    tmp_path, monkeypatch
):
    from lib.sources.annas_browser import AnnasBrowserSession

    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    session = AnnasBrowserSession(config(tmp_path))
    with pytest.raises(Exception) as caught:
        await session.search_html("kant")
    assert caught.value.reason == "configuration_error"
    assert "annas-browser" in str(caught.value)
    assert session._process_lock._fd is None


async def test_spent_daily_budget_refuses_before_launch(tmp_path, monkeypatch):
    cfg = config(tmp_path, annas_browser_daily_limit=1)
    page = Page()
    first = session_with_page(monkeypatch, cfg, page)
    await first.search_html("kant")
    second = session_with_page(monkeypatch, cfg, Page())
    with pytest.raises(ProviderResponseError) as caught:
        await second.search_html("kant")
    assert caught.value.reason == "quota_exhausted"
    assert second._ensure_context.await_count == 0
    assert second._process_lock._fd is None


def test_counter_preserves_interval_and_backoff_between_instances(
    tmp_path, monkeypatch
):
    import lib.sources.annas_usage as usage_module

    monkeypatch.setattr(usage_module.time, "time", lambda: 100000.0)
    state = str(tmp_path / "usage.json")
    assert usage_module.DailyUsage(state).spend(30, 20) == (True, 29, 0.0)
    assert usage_module.DailyUsage(state).spend(30, 20) == (True, 28, 20.0)
    usage_module.DailyUsage(state).penalise(300)
    assert usage_module.DailyUsage(state).spend(30, 20) == (True, 27, 300.0)


def test_counter_stops_at_daily_limit_and_resets_after_24_hours(tmp_path, monkeypatch):
    import lib.sources.annas_usage as usage_module

    now = [100000.0]
    monkeypatch.setattr(usage_module.time, "time", lambda: now[0])
    state = str(tmp_path / "usage.json")
    for left in range(29, -1, -1):
        assert usage_module.DailyUsage(state).spend(30)[1] == left
    assert usage_module.DailyUsage(state).spend(30) == (False, 0, 0.0)
    now[0] += 86401
    assert usage_module.DailyUsage(state).spend(30) == (True, 29, 0.0)


def test_corrupt_counter_refuses_instead_of_resetting(tmp_path):
    from lib.sources.annas_usage import DailyUsage, UsageCounterUnavailableError

    state = tmp_path / "usage.json"
    state.write_text("not JSON")
    with pytest.raises(UsageCounterUnavailableError):
        DailyUsage(str(state)).spend(30)
    assert state.read_text() == "not JSON"


async def test_waiting_for_another_process_lock_respects_deadline(
    tmp_path, monkeypatch
):
    from lib.sources.annas_usage import CrossProcessLock

    cfg = config(tmp_path)
    holder = CrossProcessLock(str(tmp_path / "annas-browser.lock"))
    assert holder.acquire(0)
    session = session_with_page(monkeypatch, cfg, Page())
    try:
        with pytest.raises(ProviderTimeoutError):
            await session.search_html("kant", deadline=WalkDeadline(0.02))
        assert session._ensure_context.await_count == 0
        assert session._process_lock._fd is None
    finally:
        holder.release()
    assert await session.search_html("kant") == saved("kant-settled.html")


async def test_explicit_cancellation_closes_page_and_releases_lock(
    tmp_path, monkeypatch
):
    page = Page()
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    page.goto = wait
    session = session_with_page(monkeypatch, config(tmp_path), page)
    task = asyncio.create_task(session.search_html("kant"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert page.closed and session._process_lock._fd is None


async def test_counter_contention_refuses_without_blocking_the_event_loop(
    tmp_path, monkeypatch
):
    import time
    from lib.sources.annas_usage import CrossProcessLock

    cfg = config(tmp_path)
    holder = CrossProcessLock(str(tmp_path / "annas-browser-usage.json.lock"))
    assert holder.acquire(0)
    session = session_with_page(monkeypatch, cfg, Page())
    started = time.monotonic()
    try:
        with pytest.raises(ProviderConfigurationError) as caught:
            await session.search_html("kant", deadline=WalkDeadline(0.03))
        assert caught.value.reason == "configuration_error"
        assert time.monotonic() - started < 0.5
        assert session._ensure_context.await_count == 0
    finally:
        holder.release()


async def test_adapter_fallback_uses_real_session_and_same_row_shape(
    tmp_path, monkeypatch
):
    from dataclasses import asdict

    cfg = config(tmp_path)
    adapter = AnnasArchiveAdapter(cfg)
    session = session_with_page(monkeypatch, cfg, Page())
    adapter._browser = session
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, text=saved("cold-human.html"))
        )
    )
    try:
        browser_rows = await adapter.search("kant")
        http_rows = adapter._parse_search_html(saved("kant-settled.html"))
        assert [asdict(row) for row in browser_rows] == [
            asdict(row) for row in http_rows
        ]
        assert browser_rows[1].md5 == "636d44bc0b81f4b337cf6a85c9d507b0"
    finally:
        await adapter.close()
