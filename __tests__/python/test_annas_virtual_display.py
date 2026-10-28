"""No provider traffic: launch options and child ownership at the browser boundary."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.sources.annas_browser import AnnasBrowserSession, BrowserUnavailableError
from lib.sources.config import SourceConfig


async def test_missing_xvfb_is_named_without_headless_fallback(tmp_path, monkeypatch):
    cfg = SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    cfg.annas_browser_display = "virtual"
    cfg.annas_xvfb_path = "/does-not-exist/Xvfb"
    launch = AsyncMock()
    chromium = SimpleNamespace(launch_persistent_context=launch)
    playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(
            async_playwright=lambda: SimpleNamespace(
                start=AsyncMock(return_value=playwright)
            )
        ),
    )
    session = AnnasBrowserSession(cfg)
    try:
        with pytest.raises(BrowserUnavailableError, match="/does-not-exist/Xvfb"):
            await session._ensure_context()
        assert launch.await_count == 0
    finally:
        await session.close()


async def test_virtual_display_owned_and_reuse_context_is_ephemeral(
    tmp_path, monkeypatch
):
    # A tiny owned executable exercises readiness and cleanup without an X server.
    fake = tmp_path / "Xvfb"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys,time\nprint(177, flush=True)\ntime.sleep(60)\n"
    )
    fake.chmod(0o700)
    cfg = SourceConfig(annas_browser_profile_dir=str(tmp_path / "profile"))
    cfg.annas_browser_display = "virtual"
    cfg.annas_xvfb_path = str(fake)
    cfg.annas_session_reuse = True
    context = SimpleNamespace(close=AsyncMock())
    chrome = SimpleNamespace(
        new_context=AsyncMock(return_value=context), close=AsyncMock()
    )
    launch = AsyncMock(return_value=chrome)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=launch), stop=AsyncMock()
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(
            async_playwright=lambda: SimpleNamespace(
                start=AsyncMock(return_value=playwright)
            )
        ),
    )
    session = AnnasBrowserSession(cfg)
    inherited = os.environ.get("DISPLAY")
    try:
        assert await session._ensure_context() is context
        options = launch.call_args.kwargs
        assert options["headless"] is False
        assert options["env"]["DISPLAY"] == ":177"
        assert os.environ.get("DISPLAY") == inherited
        proc = session._display.process
        assert proc.returncode is None
    finally:
        await session.close()
    assert proc.returncode is not None
    assert chrome.close.await_count == 1
