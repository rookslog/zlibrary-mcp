"""FU-3: real acquisition, parsing and admission; fake browser/HTTP/DNS only."""

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from bs4 import BeautifulSoup
import httpx
import pytest

from lib import python_bridge as bridge
from lib.acquisition import acquire
from lib.sources.annas_browser import _partner_countdown
from lib.sources.errors import AllSourcesFailedError
from test_annas_browser_b5_fu2_addendum import poll_clock
from test_annas_browser_download import (
    BOOK,
    DETAIL,
    DIGEST,
    MD5,
    PARTNERS,
    PAYLOAD,
    browser,
    first_url,
    integration_route,
    payload_variant,
    saved,
    spent,
)


@pytest.mark.parametrize("phase", ["first", "redirect", "resume"])
async def test_idn_guard_checks_ascii_connection_name(tmp_path, monkeypatch, phase):
    """Checking Unicode via IDNA-2003 instead of raw_host admits the private IDN."""
    target = "http://xn--strae-oqa.evil.example/"
    lookups = []
    idn_requests = []

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PAYLOAD[:65536]
            raise httpx.RemoteProtocolError("synthetic interrupted body")

    def serve(request):
        if request.url.raw_host == b"wbsg8v.xyz":
            return httpx.Response(302, headers={"location": target})
        idn_requests.append(request)
        if phase == "resume" and "range" not in request.headers:
            return httpx.Response(200, stream=Interrupted())
        if "range" in request.headers:
            return httpx.Response(
                206,
                content=PAYLOAD[65536:],
                headers={
                    "content-range": f"bytes 65536-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
                },
            )
        return httpx.Response(200, content=PAYLOAD)

    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        serve,
        annas_browser_max_servers=1,
        download_resume_attempts=1,
    )
    if phase != "redirect":
        io.pages[PARTNERS[0].replace(MD5, DIGEST)] = payload_variant(target).replace(
            MD5, DIGEST
        )

    async def resolve(host, port, **kwargs):
        # Reproduce socket's IDNA-2003 conversion, without a real DNS lookup.
        dns_name = host.encode("idna").decode("ascii")
        lookups.append((host, dns_name))
        private = dns_name == "xn--strae-oqa.evil.example" and (
            phase != "resume" or bool(idn_requests)
        )
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1" if private else "8.8.8.8", port),
            )
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as caught:
            await acquire(
                BOOK, str(output), filename="fu3.pdf", transfer=bridge.download_book
            )
        assert [failure.reason for failure in caught.value.failures] == [
            "protocol_error"
        ]
        assert len(requests) == (0 if phase == "first" else 1)
        assert len(idn_requests) == (1 if phase == "resume" else 0)
        assert lookups[-1] == (
            "xn--strae-oqa.evil.example",
            "xn--strae-oqa.evil.example",
        )
        assert not (output / "fu3.pdf").exists() and not list(output.glob(".source-*"))
        assert all(
            json.loads(p.read_text())["state"] == "intent"
            for p in output.glob("*.acq.json")
        )
    finally:
        await router.close()


@pytest.mark.parametrize(
    "location", ["http://10.0.0.1/file.pdf", "http://[::1]/file.pdf"]
)
async def test_private_literal_redirect_refuses_before_transfer(
    tmp_path, monkeypatch, location
):
    """Dropping the literal-address check admits a private redirect without DNS."""
    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        lambda request: (
            httpx.Response(302, headers={"location": location})
            if request.url.raw_host == b"wbsg8v.xyz"
            else httpx.Response(200, content=PAYLOAD)
        ),
        annas_browser_max_servers=1,
    )
    lookups = []

    async def resolve(host, port, **kwargs):
        lookups.append(host)
        assert host == "wbsg8v.xyz"  # A literal must never use DNS.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as caught:
            await acquire(
                BOOK, str(output), filename="fu3.pdf", transfer=bridge.download_book
            )
        assert [failure.reason for failure in caught.value.failures] == [
            "protocol_error"
        ]
        assert [request.url.raw_host for request in requests] == [b"wbsg8v.xyz"]
        assert lookups == ["wbsg8v.xyz"] and len(io.visits) == 2
        assert not (output / "fu3.pdf").exists() and not list(output.glob(".source-*"))
    finally:
        await router.close()


def countdown_page(shape):
    if shape == "original_fixture":
        soup = BeautifulSoup(saved("b5/partner-countdown.html"), "html.parser")
        soup.select_one(".js-download-countdown").attrs.pop("class", None)
        return str(soup)
    card = "<article><h3>Book card</h3><p>Ready in 30 Minutes</p></article>"
    if shape == "main_only":
        return f"<main>{card}<p>Please wait 5 seconds</p></main>"
    if shape == "main_title":
        return f"<main><h1>Download from partner website</h1>{card}<p>Please wait 5 seconds</p></main>"
    if shape == "section":
        return f"<main>{card}<section><p>Please wait 5 seconds</p><h2>Slow Partner Server #1</h2><div>Loading</div></section></main>"
    if shape == "heading":
        return f"<main>{card}<h2>Slow Partner Server #1</h2><div>Loading</div><p>Please wait 5 seconds</p><h2>Other section</h2></main>"
    return f"<main>{card}<p>Slow Partner Server #1</p><div>Loading</div><p>Please wait 5 seconds</p></main>"


@pytest.mark.parametrize(
    "shape",
    [
        "original_fixture",
        "later_sibling",
        "main_only",
        "main_title",
        "section",
        "heading",
    ],
)
async def test_whole_partner_countdown_survives_403(tmp_path, monkeypatch, shape):
    """The one-sibling scope loses these countdowns and confirms a 403 refusal."""
    page = countdown_page(shape)
    assert _partner_countdown(BeautifulSoup(page, "html.parser")) is True
    clock, _ = poll_clock(monkeypatch)
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: [page] * 20 + [saved("public-domain-partner-settled.html")]},
    )
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert clock[0] >= 1004  # Wait spans the refusal confirmation interval.
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] < time.time() + 1
    assert session._context is None and session._process_lock._fd is None


@pytest.mark.parametrize(
    "card",
    [
        "<article><p>Ready in 30 Minutes</p></article>",
        "<div><a class='custom-a' href='/md5/123'>Book</a><p>Please wait 10 minutes</p></div>",
    ],
)
@pytest.mark.parametrize("heading", [False, True])
def test_card_wait_text_does_not_become_partner_countdown(card, heading):
    """A main-wide free-text scan lets the book card disguise a settled page."""
    marker = "<p>Slow Partner Server #1</p>" if heading else ""
    soup = BeautifulSoup(
        f"<main>{card}{marker}<div>Loading</div></main>", "html.parser"
    )
    assert _partner_countdown(soup) is False


@pytest.mark.parametrize("next_heading", ["h1", "h2"])
def test_partner_countdown_stops_at_next_peer_or_higher_heading(next_heading):
    """Reading past the partner section lets unrelated waits mask a refusal."""
    soup = BeautifulSoup(
        f"<main><h2>Slow Partner Server #1</h2><div>Loading</div><{next_heading}>Other</{next_heading}><p>Please wait 5 seconds</p></main>",
        "html.parser",
    )
    assert _partner_countdown(soup) is False


async def test_reservation_race_after_launch_refuses_without_spending_or_transfer(
    tmp_path, monkeypatch
):
    """Skipping the postlaunch recheck admits a visit after the last slot is taken."""
    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(200, content=PAYLOAD),
        annas_browser_daily_limit=1,
    )
    session = router._annas._browser
    launch = io.launch
    opponent_state = []

    async def competing_launch():
        assert not (tmp_path / "annas-browser-usage.json").exists()
        context = await launch()
        # Another real process wins the last slot using the production counter.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from lib.sources.annas_usage import DailyUsage; "
                "assert DailyUsage(sys.argv[2]).spend(1, 0)[:2] == (True, 0)",
                str(Path(bridge.__file__).resolve().parent.parent),
                str(tmp_path / "annas-browser-usage.json"),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        opponent_state.append((tmp_path / "annas-browser-usage.json").read_bytes())
        return context

    monkeypatch.setattr(session, "_ensure_context", competing_launch)
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as caught:
            await acquire(
                BOOK, str(output), filename="fu3.pdf", transfer=bridge.download_book
            )
        assert [failure.reason for failure in caught.value.failures] == [
            "quota_exhausted"
        ]
        assert "this session's own daily ceiling of 1" in str(caught.value)
        assert io.opens == 1 and io.visits == [] and requests == []
        assert spent(tmp_path) == 1
        assert (tmp_path / "annas-browser-usage.json").read_bytes() == opponent_state[0]
        assert io.events == [("page", True), ("context", True), ("driver", True)]
        assert session._context is None and session._playwright is None
        assert session._process_lock._fd is None and not session._lock.locked()
        assert not (output / "fu3.pdf").exists() and not list(output.glob(".source-*"))
    finally:
        await router.close()
