"""M1/M2/L1: saved DOM variants and three explicitly synthetic partner fixtures.

No fixture is represented as a captured live countdown/refusal. Browser, poll
clock and HTTP boundaries are fake; the session, counter, locks and parser run.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import pytest

from lib.sources import annas_browser as browser_module
from lib.sources.annas_browser import _direct_file_url, _partner_countdown
from lib.sources.errors import ProviderResponseError, ProviderTimeoutError
from lib.sources.net import WalkDeadline
from test_annas_browser_download import (
    DETAIL,
    MD5,
    PARTNERS,
    browser,
    first_url,
    saved,
    spent,
)


def poll_clock(monkeypatch):
    clock, waits = [1000.0], []
    real_loop = asyncio.get_running_loop()

    class LoopClock:
        def time(self):
            return clock[0]

        def __getattr__(self, name):
            return getattr(real_loop, name)

    proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    proxy.get_running_loop = lambda: LoopClock()

    async def advance(seconds):
        waits.append(seconds)
        clock[0] += seconds
        await asyncio.sleep(0)

    proxy.sleep = advance
    monkeypatch.setattr(browser_module, "asyncio", proxy)
    return clock, waits


def observe_partner_reads(session, io, monkeypatch, clock):
    reads = []
    launch = io.launch

    async def observed_launch():
        context = await launch()
        page = await context.new_page()
        goto, content = page.goto, page.content
        current = [None]

        async def navigate(url, **kwargs):
            current[0] = urlsplit(url).path
            return await goto(url, **kwargs)

        async def read():
            if current[0] == PARTNERS[0]:
                reads.append(clock())
            return await content()

        page.goto, page.content = navigate, read
        return context

    monkeypatch.setattr(session, "_ensure_context", observed_launch)
    return reads


def partner_page(text):
    return f"""<main><div><article><h3>Book card</h3></article>
        <p>Slow Partner Server #1</p><div>{text}</div></div></main>"""


@pytest.mark.parametrize(
    "title", ["Ready in 30 Minutes: Quick Meals", "Please wait 10 minutes"]
)
@pytest.mark.parametrize("link_kind", ["block", "download_now"])
@pytest.mark.parametrize("status", [200, 403])
async def test_m1_ready_link_wins_over_card_and_countdown_text(
    tmp_path, monkeypatch, title, link_kind, status
):
    """A page-wide wait veto must not hide a usable selected partner link."""
    soup = BeautifulSoup(saved("public-domain-partner-settled.html"), "html.parser")
    card = soup.select_one("a.custom-a[href^='/md5/']").parent
    title_node = soup.new_tag("p")
    title_node.string = title
    card.append(title_node)
    if link_kind == "block":
        for anchor in soup.select("main a[href]"):
            if "wbsg8v.xyz" in anchor["href"]:
                anchor.string = "Get file"
    countdown = soup.new_tag("p", attrs={"class": "js-download-countdown"})
    countdown.string = "Please wait 5 seconds"
    soup.main.append(countdown)
    page = str(soup)
    url = _direct_file_url(page, "annas-archive.gl")
    assert url is not None and url.startswith("https://wbsg8v.xyz/")
    session, io = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: page}, {PARTNERS[0]: status}
    )
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    assert session._process_lock._fd is None and not session._lock.locked()


@pytest.mark.parametrize("title", ["Ready in 30 Minutes", "Please wait 10 minutes"])
async def test_m1_card_wait_text_cannot_disguise_a_403_refusal(
    tmp_path, monkeypatch, title
):
    clock, _ = poll_clock(monkeypatch)
    page = partner_page("Downloads refused. Please try again later.").replace(
        "Book card", title
    )
    assert _partner_countdown(BeautifulSoup(page, "html.parser")) is False
    session, io = browser(tmp_path, monkeypatch, {PARTNERS[0]: page})
    reads = observe_partner_reads(session, io, monkeypatch, lambda: clock[0])
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session)
    assert caught.value.reason == "browser_check"
    assert len(reads) >= 2 and reads[-1] - reads[0] >= 3
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2


@pytest.mark.parametrize(
    "fixture", ["partner-countdown-js.html", "partner-countdown-start.html"]
)
async def test_m2_delayed_countdown_at_403_settles_without_backoff(
    tmp_path, monkeypatch, fixture
):
    clock, _ = poll_clock(monkeypatch)
    countdown = saved(f"b5fu2/{fixture}")
    assert _partner_countdown(BeautifulSoup(countdown, "html.parser")) is True
    session, io = browser(
        tmp_path,
        monkeypatch,
        {
            PARTNERS[0]: [
                partner_page("Loading…"),
                countdown,
                saved("public-domain-partner-settled.html"),
            ]
        },
    )
    reads = observe_partner_reads(session, io, monkeypatch, lambda: clock[0])
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert len(reads) >= 3
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] < time.time() + 1
    assert session._process_lock._fd is None and session._context is None


async def test_m2_countdown_resets_refusal_confirmation(tmp_path, monkeypatch):
    clock, _ = poll_clock(monkeypatch)
    empty = partner_page("Loading…")
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: [empty, saved("b5fu2/partner-countdown-js.html"), empty, empty]},
    )
    reads = observe_partner_reads(session, io, monkeypatch, lambda: clock[0])
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session)
    assert caught.value.reason == "browser_check"
    assert len(reads) >= 4 and reads[-1] - reads[2] >= 3
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2


@pytest.mark.parametrize("marker", [False, True])
async def test_l1_rate_refusal_beats_wait_text_and_stale_marker(
    tmp_path, monkeypatch, marker
):
    clock, _ = poll_clock(monkeypatch)
    soup = BeautifulSoup(saved("b5fu2/partner-refusal-wait.html"), "html.parser")
    if marker:
        soup.select_one(".partner-download")["class"] = [
            "partner-download",
            "js-download-countdown",
        ]
    assert _partner_countdown(soup) is False
    session, io = browser(tmp_path, monkeypatch, {PARTNERS[0]: str(soup)})
    reads = observe_partner_reads(session, io, monkeypatch, lambda: clock[0])
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session)
    assert caught.value.reason == "browser_check"
    assert len(reads) >= 2 and reads[-1] - reads[0] >= 3
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] - state["window_started"] >= 299
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    assert session._process_lock._fd is None and session._context is None
    opens = io.opens
    with pytest.raises(ProviderResponseError):
        await first_url(session, deadline=WalkDeadline(2))
    assert spent(tmp_path) == 2 and io.opens == opens


async def test_m2_real_poll_spacing_precedes_confirmed_refusal(tmp_path, monkeypatch):
    """Keep a real clock/sleep witness in addition to simulated polling."""
    session, io = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: partner_page("Downloads refused.")}
    )
    # The real timing target is the partner wait, independent of book-card size.
    io.pages[DETAIL] = f"""<main><div id="cover_aarecord_id__md5:{MD5}"></div>
        <a href="{PARTNERS[0]}">Slow partner</a></main>"""
    reads = observe_partner_reads(session, io, monkeypatch, time.monotonic)
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session, deadline=WalkDeadline(10))
    assert caught.value.reason == "browser_check"
    assert len(reads) >= 2 and reads[-1] - reads[0] >= 3
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2


async def test_m2_short_settle_window_cannot_confirm_refusal(tmp_path, monkeypatch):
    poll_clock(monkeypatch)
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: partner_page("Loading…")},
        annas_browser_settle_seconds=0.5,
        annas_browser_max_servers=1,
    )
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session)
    assert caught.value.reason == "protocol_error"
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] < time.time() + 1
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2


async def test_m2_confirmation_wait_respects_caller_deadline(tmp_path, monkeypatch):
    session, io = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: partner_page("Loading…")}
    )
    io.pages[DETAIL] = f"""<main><div id="cover_aarecord_id__md5:{MD5}"></div>
        <a href="{PARTNERS[0]}">Slow partner</a></main>"""
    with pytest.raises(ProviderTimeoutError):
        await first_url(session, deadline=WalkDeadline(2))
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] < time.time() + 1
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    assert session._process_lock._fd is None and session._context is None
