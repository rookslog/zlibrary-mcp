"""Offline transport contracts; synthetic cookie, saved result HTML, local TCP only.

Mutants caught: bypassing limiter/lock, accepting a partial or rejected page,
retrying forever, retaining a rejected/expired session, persisting cookie state.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from annas_browser_test_fixtures import read_browser_fixture
from lib.sources.annas_browser import AnnasBrowserSession
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import ProviderResponseError

PAGES = Path(__file__).parent / "fixtures" / "annas-browser"
HTML = read_browser_fixture(PAGES / "kant-settled.html")
COOKIE = "synthetic-clearance-sentinel"
UA = "Synthetic exact browser UA/155"


@asynccontextmanager
async def server(monkeypatch, status=200, body=HTML, delay=0, headers=""):
    """Real loopback HTTP server; no provider requests or persisted headers."""
    requests = []
    tasks = set()

    async def serve(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request.decode("latin1"))
            if delay:
                await asyncio.sleep(delay)
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Length: {len(payload)}\r\n"
                f"Connection: close\r\n{headers}\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}", requests
    finally:
        listener.close()
        await listener.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def browser(tmp_path, monkeypatch, base):
    cfg = SourceConfig(
        annas_browser_enabled=True,
        annas_base_url=base,
        annas_browser_profile_dir=str(tmp_path / "profile"),
        annas_browser_min_interval=0.001,
        annas_browser_search_timeout=2,
        read_timeout=0.03,
        connect_timeout=0.03,
    )
    cfg.annas_session_reuse = True
    cfg.annas_session_ttl = 900
    session = AnnasBrowserSession(cfg)
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        content=AsyncMock(return_value=HTML),
        close=AsyncMock(),
        evaluate=AsyncMock(return_value=UA),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(),
        cookies=AsyncMock(
            return_value=[
                {
                    "name": "clearance",
                    "value": COOKIE,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "secure": False,
                    "expires": -1,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        ),
    )
    monkeypatch.setattr(session, "_ensure_context", AsyncMock(return_value=context))
    return session, page


async def test_reuses_exact_session_and_counts_both_methods(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        first = await session.search_html("first")
        second = await session.search_html("second")
        assert getattr(second, "method", None) == "session"
        assert getattr(first, "method", None) == "browser"
        assert len(requests) == 1
        assert f"clearance={COOKIE}" in requests[0]
        assert f"user-agent: {UA}".lower() in requests[0].lower()
        assert page.goto.await_count == 1
        assert session._limiter.remaining_today == 28
        assert "method=session" in caplog.text and "method=browser" in caplog.text
        assert COOKIE not in caplog.text and UA not in caplog.text
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert COOKIE.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "status,body,delay",
    [
        (403, HTML, 0),
        (200, "<html><title>DDoS-Guard</title></html>", 0),
        (200, "<html><form></form></html>", 0),
        (200, HTML, 0.2),
    ],
    ids=["403", "challenge", "unsettled", "timeout"],
)
async def test_rejection_falls_back_once(tmp_path, monkeypatch, status, body, delay):
    async with server(monkeypatch, status, body, delay) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        await session.search_html("first")
        result = await session.search_html("second")
        assert len(requests) == 1
        assert page.goto.await_count == 2
        assert getattr(result, "method", None) == "browser"
        assert session._limiter.remaining_today == 27


async def test_expiry_skips_http(tmp_path, monkeypatch):
    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        session.config.annas_session_ttl = 0.001
        await session.search_html("first")
        await asyncio.sleep(0.01)
        await session.search_html("second")
        assert not requests
        assert page.goto.await_count == 2
        assert getattr(session, "last_fetch_method", None) == "browser"


async def test_reuse_cannot_skip_pacing_or_daily_cap(tmp_path, monkeypatch):
    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        await session.search_html("first")
        session._limiter.min_interval = 20
        # The first spend used a short interval; reserve one warm request first.
        await session.search_html("second")
        with pytest.raises(ProviderResponseError):
            await session.search_html("third")
        assert len(requests) == 1
        session._limiter.daily_limit = 2
        with pytest.raises(ProviderResponseError):
            await session.search_html("fourth")
        assert len(requests) == 1
        assert page.goto.await_count == 1


def test_config_is_opt_in_with_finite_ttl(monkeypatch):
    monkeypatch.setenv("ANNAS_SESSION_REUSE", "true")
    monkeypatch.setenv("ANNAS_SESSION_TTL", "nan")
    monkeypatch.setenv("ANNAS_BROWSER_DISPLAY", "virtual")
    monkeypatch.setenv("ANNAS_XVFB_PATH", "/missing/Xvfb")
    cfg = get_source_config()
    assert getattr(cfg, "annas_session_reuse", False)
    assert cfg.annas_session_ttl == 900
    assert cfg.annas_browser_display == "virtual"
    assert cfg.annas_xvfb_path == "/missing/Xvfb"


async def test_adapter_tries_warm_session_before_unbudgeted_http(tmp_path, monkeypatch):
    from lib.sources.annas import AnnasArchiveAdapter

    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        await session.search_html("first")
        adapter = AnnasArchiveAdapter(session.config)
        adapter._browser = session

        async def forbidden(*args, **kwargs):
            pytest.fail("warm session must run before ordinary HTTP")

        monkeypatch.setattr(adapter, "_fetch", forbidden)
        try:
            rows = await adapter.search("second")
            assert len(rows) == 2
            assert adapter.last_fetch_method == "session"
            assert len(requests) == 1 and page.goto.await_count == 1
        finally:
            await adapter.close()


async def test_local_tcp_server_when_permitted():
    try:
        listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    except OSError as exc:
        pytest.skip(f"local socket bind unavailable: {exc}")
    listener.close()
    await listener.wait_closed()


async def test_cancel_while_waiting_for_pacing_discards_session(tmp_path, monkeypatch):
    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        await session.search_html("first")
        assert session._earned_session is not None
        entered = asyncio.Event()

        async def wait_for_pacing(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(session._limiter, "acquire", wait_for_pacing)
        pending = asyncio.create_task(session.search_html("second"))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert session._earned_session is None
        assert not session._lock.locked()
        assert not requests


async def test_reuse_waits_for_same_one_at_a_time_lock(tmp_path, monkeypatch):
    async with server(monkeypatch) as (base, requests):
        session, page = browser(tmp_path, monkeypatch, base)
        await session.search_html("first")
        await session._lock.acquire()
        pending = asyncio.create_task(session.search_html("second"))
        try:
            await asyncio.sleep(0.03)
            assert not requests
            assert not pending.done()
        finally:
            session._lock.release()
        assert (await pending).method == "session"
        assert len(requests) == 1


async def test_redirect_cannot_disclose_session_to_another_origin(
    tmp_path, monkeypatch
):
    async with server(monkeypatch) as (other, other_requests):
        async with server(
            monkeypatch, status=302, headers=f"Location: {other}/search\r\n"
        ) as (base, requests):
            session, page = browser(tmp_path, monkeypatch, base)
            await session.search_html("first")
            result = await session.search_html("second")
            assert result.method == "browser"
            assert len(requests) == 1
            assert not other_requests
            assert page.goto.await_count == 2


def test_flags_default_off(monkeypatch):
    for name in ("ANNAS_SESSION_REUSE", "ANNAS_BROWSER_DISPLAY", "ANNAS_SESSION_TTL"):
        monkeypatch.delenv(name, raising=False)
    cfg = get_source_config()
    assert cfg.annas_session_reuse is False
    assert cfg.annas_browser_display == "desktop"
    assert cfg.annas_session_ttl == 900
