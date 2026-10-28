"""Offline route decisions and budget reporting; no live browser or provider."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from lib import python_bridge as bridge
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.annas_usage import DailyUsage, UsageCounterUnavailableError
from lib.sources.config import SourceConfig
from lib.sources.router import SourceRouter


@pytest.mark.asyncio
@pytest.mark.parametrize("challenge,route", [(False, "html"), (True, "browser")])
async def test_search_reports_decided_route_even_when_browser_enabled(
    monkeypatch, tmp_path, challenge, route
):
    cfg = SourceConfig(
        annas_browser_enabled=True,
        preflight_enabled=False,
        annas_browser_profile_dir=str(tmp_path / "profile"),
    )
    adapter = AnnasArchiveAdapter(cfg)
    html = (
        "<html><title>Checking your browser</title></html>"
        if challenge
        else "<html><title>Fixture - Anna's Archive</title></html>"
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html))
    )
    adapter._browser = SimpleNamespace(
        search_html=AsyncMock(return_value="<html></html>"), close=AsyncMock()
    )
    router = SourceRouter(cfg)
    router._annas = adapter
    monkeypatch.setattr(bridge, "get_source_router", AsyncMock(return_value=router))
    try:
        result = await bridge.search_multi_source("fixture", source="annas")
        assert result["books"] == []
        assert result["via"][0]["source"] == "annas"
        assert result["via"][0]["route"] == route
        assert result["via"][0]["ms"] >= 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_zlibrary_search_reports_api(monkeypatch):
    eapi = SimpleNamespace(search=AsyncMock(return_value={"books": []}))
    monkeypatch.setattr(bridge, "get_eapi_client", AsyncMock(return_value=eapi))
    result = await bridge.search("fixture")
    assert result["via"][0]["route"] == "api"
    assert result["via"][0]["source"] == "zlibrary"


@pytest.mark.parametrize(
    "age,spent,next_allowed,want_used,want_wait",
    [(10, 12, 1006, 12, 6), (86401, 12, 1006, 0, 6), (-10, 12, 1016, 12, 6)],
)
def test_budget_snapshot_reads_window_without_writing(
    tmp_path, monkeypatch, age, spent, next_allowed, want_used, want_wait
):
    monkeypatch.setattr("lib.sources.annas_usage.time.time", lambda: 1000)
    state = tmp_path / "cache" / "annas-browser-usage.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {"window_started": 1000 - age, "spent": spent, "next_allowed": next_allowed}
        )
    )
    before = state.read_bytes()
    result = DailyUsage(str(state)).snapshot(50)
    assert result == {"used_today": want_used, "next_slot_in_s": want_wait}
    assert state.read_bytes() == before
    assert sorted(p.name for p in state.parent.iterdir()) == [
        "annas-browser-usage.json"
    ]


def test_missing_and_corrupt_counter_never_create_or_reset_state(tmp_path):
    state = tmp_path / "absent" / "annas-browser-usage.json"
    assert DailyUsage(str(state)).snapshot(50) == {"used_today": 0, "next_slot_in_s": 0}
    assert not state.parent.exists()
    state.parent.mkdir()
    state.write_text("corrupt")
    with pytest.raises(UsageCounterUnavailableError):
        DailyUsage(str(state)).snapshot(50)
    assert state.read_text() == "corrupt"


@pytest.mark.asyncio
async def test_budget_dispatch_uses_configured_cache_and_never_launches(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", "true")
    monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ANNAS_BROWSER_DAILY_LIMIT", "50")
    result = await bridge._dispatch_bridge_function("get_annas_browser_limits", {})
    assert result["used_today"] == 0
    assert result["daily_cap"] == 50
    assert result["min_interval_s"] == 20
    assert result["next_slot_in_s"] == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_acquisition_reuse_has_empty_via(tmp_path):
    from test_interface_acquisition import BOOK, transfer
    from lib.acquisition import acquire

    first = await acquire(BOOK, str(tmp_path), transfer=transfer)
    assert first["via"] == []  # fake provider did not issue a source request
    second = await acquire(BOOK, str(tmp_path), transfer=transfer)
    assert second["transfer_skipped"] is True
    assert second["via"] == []


@pytest.mark.asyncio
async def test_rejected_key_download_records_browser_not_configured_api(
    monkeypatch, tmp_path
):
    from lib.sources.route_trace import collect_routes

    cfg = SourceConfig(
        annas_browser_enabled=True, annas_secret_key="fixture", preflight_enabled=False
    )
    adapter = AnnasArchiveAdapter(cfg)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(401))
    )

    async def urls(*args, **kwargs):
        yield "https://files.invalid/fixture.pdf"

    adapter._browser = SimpleNamespace(iter_download_urls=urls, close=AsyncMock())
    router = SourceRouter(cfg)
    router._annas = adapter
    try:
        with collect_routes() as via:
            stream = router.iter_download_candidates("a" * 32, source="annas")
            result = await anext(stream)
            await stream.aclose()
        assert result.url == "https://files.invalid/fixture.pdf"
        assert via[0]["route"] == "browser"
        assert via[0]["ms"] >= 0
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_pacing_wait_contributes_queue_time(tmp_path, monkeypatch):
    from lib.sources.annas_browser import _RateLimiter
    from lib.sources.route_trace import collect_routes, route_attempt

    usage = DailyUsage(str(tmp_path / "usage.json"))
    usage.spend(50, 0.02)
    limiter = _RateLimiter(0.02, 50, usage)
    with collect_routes() as via:
        with route_attempt("annas", "browser"):
            await limiter.acquire()
    assert via[0]["queued_ms"] >= 1
    assert via[0]["ms"] >= via[0]["queued_ms"]


def test_local_budget_never_requires_eapi():
    assert bridge._requires_eapi_client("get_annas_browser_limits", {}) is False


@pytest.mark.asyncio
async def test_budget_dispatch_reads_shared_counter_and_marks_corruption_unknown(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", "true")
    monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ANNAS_BROWSER_DAILY_LIMIT", "50")
    (tmp_path / "profile").mkdir()
    state = tmp_path / "annas-browser-usage.json"
    import time

    state.write_text(
        json.dumps(
            {
                "window_started": time.time(),
                "spent": 17,
                "next_allowed": time.time() + 20,
            }
        )
    )
    before = state.read_bytes()
    result = await bridge._dispatch_bridge_function("get_annas_browser_limits", {})
    assert result["used_today"] == 17
    assert 19 <= result["next_slot_in_s"] <= 20
    assert state.read_bytes() == before
    state.write_text("broken")
    result = await bridge._dispatch_bridge_function("get_annas_browser_limits", {})
    assert result["used_today"] is None
    assert result["next_slot_in_s"] is None
    assert result["counter_status"] == "unavailable"
    assert state.read_text() == "broken"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "annas-browser-usage.json",
        "profile",
    ]


@pytest.mark.asyncio
async def test_counter_is_visible_before_new_profile_directory_exists(
    monkeypatch, tmp_path
):
    import time

    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", "true")
    monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(tmp_path / "new-profile"))
    state = tmp_path / "annas-browser-usage.json"
    state.write_text(
        json.dumps({"window_started": time.time(), "spent": 17, "next_allowed": 0})
    )
    before = state.read_bytes()
    result = await bridge._dispatch_bridge_function("get_annas_browser_limits", {})
    assert result["used_today"] == 17
    assert state.read_bytes() == before
    assert not (tmp_path / "new-profile").exists()


@pytest.mark.asyncio
async def test_eapi_failure_main_exports_only_route_observations(monkeypatch, capsys):
    import sys

    eapi = SimpleNamespace(
        search=AsyncMock(side_effect=httpx.ReadTimeout("fixture")),
        close=AsyncMock(),
        needs_profile_validation=False,
        remix_userkey="",
    )
    monkeypatch.setattr(bridge, "_eapi_client", eapi)
    monkeypatch.setattr(bridge, "_session_input", None)
    monkeypatch.setattr(bridge, "initialize_eapi_client", AsyncMock())
    monkeypatch.setattr(bridge, "get_eapi_client", AsyncMock(return_value=eapi))
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", json.dumps({"query": "fixture"})]
    )
    with pytest.raises(SystemExit):
        await bridge.main()
    output = capsys.readouterr()
    envelope = json.loads(output.err.splitlines()[-1])
    assert envelope["details"]["via"][0]["source"] == "zlibrary"
    assert envelope["details"]["via"][0]["route"] == "api"
    assert envelope["details"]["via"][0]["ms"] >= 0


@pytest.mark.asyncio
async def test_counter_read_matches_browser_parent_of_symlink_profile(
    monkeypatch, tmp_path
):
    import time

    actual = tmp_path / "actual"
    (actual / "profile").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "profile").symlink_to(actual / "profile", target_is_directory=True)
    state = actual / "annas-browser-usage.json"
    state.write_text(
        json.dumps({"window_started": time.time(), "spent": 17, "next_allowed": 0})
    )
    monkeypatch.setenv("ANNAS_BROWSER_ENABLED", "true")
    monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(alias / "profile"))
    before = state.read_bytes()
    result = await bridge._dispatch_bridge_function("get_annas_browser_limits", {})
    assert result["used_today"] == 17
    assert state.read_bytes() == before
    assert list(alias.iterdir()) == [alias / "profile"]
