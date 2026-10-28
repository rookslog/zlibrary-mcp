"""B5 FU-1: saved/derived DOM and real counters; fake only browser/HTTP/time."""

import asyncio
import logging
import json
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup

from lib.sources.annas_browser import (
    AnnasBrowserSession,
    _direct_file_url,
    _partner_countdown,
)
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError, ProviderTimeoutError
from lib.sources.net import WalkDeadline
from test_annas_browser_download import (
    DETAIL,
    MD5,
    PARTNERS,
    BrowserIO,
    adapter_browser,
    browser,
    first_url,
    saved,
    spent,
)


def partner_variant(*, countdown=False, incidental_pdf=False):
    soup = BeautifulSoup(saved("b5/partner-countdown.html"), "html.parser")
    if countdown:
        marker = next(
            p
            for p in soup.select("main p")
            if p.get_text(" ", strip=True) == "Slow Partner Server #1"
        )
        marker.find_next_sibling().append(
            soup.select_one(".js-download-countdown").extract()
        )
    if not countdown:
        soup.select_one(".js-download-countdown").decompose()
        p = soup.new_tag("p")
        p.string = "Downloads refused. Please try again later."
        soup.main.append(p)
    if incidental_pdf:
        link = soup.new_tag("a", href="https://notes.example.invalid/edition.pdf")
        link.string = "Edition notes"
        soup.select_one("a.custom-a[href^='/md5/']").parent.append(link)
    return str(soup)


async def test_normal_partner_heading_does_not_hide_a_403_refusal(
    tmp_path, monkeypatch
):
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: partner_variant()},
        # Use the production settle window: FU-2 M2 requires two complete
        # DOM reads at least 3 s apart, including parser time on this capture.
    )
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session)
    assert caught.value.reason == "browser_check"
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] - state["window_started"] >= 299


@pytest.mark.parametrize("text_only", [False, True])
async def test_incidental_pdf_cannot_end_a_partner_countdown(
    tmp_path, monkeypatch, text_only
):
    waiting = partner_variant(countdown=True, incidental_pdf=True)
    if text_only:
        soup = BeautifulSoup(waiting, "html.parser")
        del soup.select_one(".js-download-countdown")["class"]
        waiting = str(soup)
    assert _partner_countdown(BeautifulSoup(waiting, "html.parser")) is True
    assert _direct_file_url(waiting, "annas-archive.gl") is None
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: [waiting, saved("public-domain-partner-settled.html")]},
    )
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert io.visits == [DETAIL, PARTNERS[0]]


def test_download_now_beats_a_card_document_link():
    soup = BeautifulSoup(saved("public-domain-partner-settled.html"), "html.parser")
    link = soup.new_tag("a", href="https://notes.example.invalid/edition.pdf")
    link.string = "Download edition notes"
    soup.select_one("a.custom-a[href^='/md5/']").parent.append(link)
    assert _direct_file_url(str(soup), "annas-archive.gl").startswith(
        "https://wbsg8v.xyz/"
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_warns_with_source_and_never_key(
    tmp_path, monkeypatch, caplog, status
):
    canary = "offline-rejected-key-canary"
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_secret_key=canary)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status))
    )
    try:
        with caplog.at_level(logging.WARNING, logger="lib.sources.annas"):
            result = await adapter.get_download_url(MD5)
        assert result.url.startswith("https://wbsg8v.xyz/")
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "annas" in s.lower() and "key" in s.lower() and "reject" in s.lower()
            for s in warnings
        )
        assert canary not in caplog.text
        assert io.visits == [DETAIL, PARTNERS[0]]
    finally:
        await adapter.close()


def simulated_pacing_clock(monkeypatch):
    import lib.sources.annas_usage as usage_module
    import lib.sources.annas_browser as browser_module
    import time

    clock = [1000.0]
    waits = []
    real_asyncio = asyncio
    usage_time = SimpleNamespace(**{n: getattr(time, n) for n in dir(time)})
    usage_time.time = lambda: clock[0]
    usage_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(usage_module, "time", usage_time)
    proxy = SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio)})

    async def advance(seconds):
        waits.append(seconds)
        clock[0] += seconds
        await real_asyncio.sleep(0)

    proxy.sleep = advance
    monkeypatch.setattr(browser_module, "asyncio", proxy)
    return clock, waits


def paced_browser(tmp_path, monkeypatch):
    session = AnnasBrowserSession(
        SourceConfig(
            annas_browser_enabled=True,
            annas_browser_min_interval=20,
            annas_browser_profile_dir=str(tmp_path / "profile"),
        )
    )
    io = BrowserIO(session)
    monkeypatch.setattr(session, "_ensure_context", io.launch)
    return session, io


async def test_download_respects_twenty_second_spacing(tmp_path, monkeypatch):
    clock, waits = simulated_pacing_clock(monkeypatch)
    session, io = paced_browser(tmp_path, monkeypatch)
    visits = []
    launch = io.launch

    async def record_launch():
        context = await launch()
        page = await context.new_page()
        goto = page.goto

        async def record(url, **kwargs):
            visits.append(clock[0])
            return await goto(url, **kwargs)

        page.goto = record
        return context

    monkeypatch.setattr(session, "_ensure_context", record_launch)
    assert (
        await first_url(session, deadline=WalkDeadline(60, clock=lambda: clock[0]))
    ).startswith("https://wbsg8v.xyz/")
    assert visits == [1000, 1020] and waits == [20]
    assert spent(tmp_path) == 2


@pytest.mark.parametrize("wait_kind", ["spacing", "backoff"])
async def test_download_pacing_overrun_names_pacing_retry_and_spends_no_partner(
    tmp_path, monkeypatch, wait_kind
):
    clock, waits = simulated_pacing_clock(monkeypatch)
    session, io = paced_browser(tmp_path, monkeypatch)
    if wait_kind == "backoff":
        session._limiter.penalise(300)
    with pytest.raises(ProviderResponseError) as caught:
        await first_url(session, deadline=WalkDeadline(5, clock=lambda: clock[0]))
    message = str(caught.value)
    assert (
        "browser download pacing/backoff" in message
        and "retry at" in message
        and "UTC" in message
    )
    assert "DDoS-Guard" not in message and "manual check" not in message
    assert io.visits == ([] if wait_kind == "backoff" else [DETAIL])
    assert spent(tmp_path) == (0 if wait_kind == "backoff" else 1) and waits == []


async def test_download_timeout_names_download_not_search(tmp_path, monkeypatch):
    session, _ = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: partner_variant(countdown=True)}
    )
    with pytest.raises(ProviderTimeoutError) as caught:
        await first_url(session, deadline=WalkDeadline(0.3))
    assert (
        "browser download" in caught.value.detail
        and "browser search" not in caught.value.detail
    )
    assert "browser download" in str(caught.value)
    assert "search" not in str(caught.value)
    assert caught.value.reason == "search_timeout"


async def test_partner_200_is_deliberately_accepted(tmp_path, monkeypatch):
    session, io = browser(tmp_path, monkeypatch, statuses={PARTNERS[0]: 200})
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
