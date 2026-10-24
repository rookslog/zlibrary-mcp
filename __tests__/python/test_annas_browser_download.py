"""B5: real saved DOM/counters/router/transfer; fake only browser and HTTP I/O."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup

from lib.sources.annas_browser import AnnasBrowserSession
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from lib.sources.net import WalkDeadline
from lib.sources.router import SourceRouter

PAGES = Path(__file__).parent / "fixtures" / "annas-browser"
MD5 = "800de029cc07da5e574d5b859f6086e8"
DETAIL = f"/md5/{MD5}"
PARTNERS = [f"/slow_download/{MD5}/0/{n}" for n in range(16)]


def saved(name):
    return (PAGES / name).read_text()


def payload_variant(url, *, hidden=False):
    """Keep the captured partner DOM, replacing only its two payload anchors."""
    soup = BeautifulSoup(saved("public-domain-partner-settled.html"), "html.parser")
    anchors = [a for a in soup.select("main a[href]") if "wbsg8v.xyz" in a["href"]]
    anchors[0]["href"] = url
    if hidden:
        anchors[0]["hidden"] = ""
    anchors[1].decompose()
    return str(soup)


class BrowserIO:
    """A visible-browser boundary with no network, interaction or cookie APIs."""

    def __init__(self, session, pages=None, statuses=None):
        self.session = session
        self.pages = {
            DETAIL: saved("public-domain-detail-settled.html"),
            **{p: saved("public-domain-partner-settled.html") for p in PARTNERS},
            **(pages or {}),
        }
        self.statuses = statuses or {}
        self.visits = []
        self.events = []
        self.opens = 0

    async def launch(self):
        self.opens += 1
        current = [None]

        async def goto(url, **kwargs):
            assert self.session._process_lock._fd is not None
            path = urlsplit(url).path
            assert path in self.pages, path
            current[0] = path
            self.visits.append(path)
            return SimpleNamespace(status=self.statuses.get(path, 403))

        async def content():
            frames = self.pages[current[0]]
            if isinstance(frames, list):
                return frames.pop(0) if len(frames) > 1 else frames[0]
            return frames

        async def close(name):
            self.events.append((name, self.session._process_lock._fd is not None))

        page = SimpleNamespace(goto=goto, content=content, close=lambda: close("page"))

        async def new_page():
            return page

        context = SimpleNamespace(new_page=new_page, close=lambda: close("context"))
        self.session._context = context
        self.session._playwright = SimpleNamespace(stop=lambda: close("driver"))
        return context


def browser(tmp_path, monkeypatch, pages=None, statuses=None, **settings):
    config = SourceConfig(
        annas_browser_enabled=True,
        annas_browser_profile_dir=str(tmp_path / "profile"),
        annas_browser_min_interval=0,
        **settings,
    )
    session = AnnasBrowserSession(config)
    io = BrowserIO(session, pages, statuses)
    monkeypatch.setattr(session, "_ensure_context", io.launch)
    return session, io


async def first_url(session, *, md5=MD5, deadline=None):
    stream = session.iter_download_urls(md5, deadline=deadline)
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


def spent(tmp_path):
    return json.loads((tmp_path / "annas-browser-usage.json").read_text())["spent"]


async def test_captured_detail_partner_resolve_and_close_before_yield(
    tmp_path, monkeypatch
):
    """Returning a page URL or retaining Chrome/its lock across transfer is wrong."""
    session, io = browser(tmp_path, monkeypatch)
    url = await first_url(session, deadline=WalkDeadline(3))
    assert url.startswith("https://wbsg8v.xyz/") and url.endswith(".epub")
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    assert io.events == [("page", True), ("context", True), ("driver", True)]
    assert session._process_lock._fd is None and not session._lock.locked()
    assert session._context is None and session._playwright is None


async def test_missing_partner_refuses_without_visiting_another_book(
    tmp_path, monkeypatch
):
    session, io = browser(
        tmp_path, monkeypatch, {DETAIL: saved("b5/detail-no-partner.html")}
    )
    with pytest.raises(ProviderResponseError) as error:
        await first_url(session)
    assert error.value.reason == "not_found"
    assert io.visits == [DETAIL] and spent(tmp_path) == 1


async def test_countdown_is_polled_without_reloading_or_clicking(tmp_path, monkeypatch):
    session, io = browser(
        tmp_path,
        monkeypatch,
        {
            PARTNERS[0]: [
                saved("b5/partner-countdown.html"),
                saved("public-domain-partner-settled.html"),
            ]
        },
    )
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2


async def test_deadline_expires_mid_countdown_and_releases_owner(tmp_path, monkeypatch):
    session, io = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: saved("b5/partner-countdown.html")}
    )
    with pytest.raises(ProviderTimeoutError):
        await first_url(session, deadline=WalkDeadline(0.3))
    assert io.visits == [DETAIL, PARTNERS[0]] and spent(tmp_path) == 2
    assert session._process_lock._fd is None and not session._lock.locked()


async def test_partner_manual_check_stops_all_remaining_partners(tmp_path, monkeypatch):
    session, io = browser(
        tmp_path, monkeypatch, {PARTNERS[0]: saved("b5/partner-manual.html")}
    )
    with pytest.raises(ProviderResponseError) as error:
        await first_url(session)
    assert error.value.reason == "browser_check"
    assert io.visits == [DETAIL, PARTNERS[0]]
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] - state["window_started"] >= 299


@pytest.mark.parametrize("widget", ["ddg-iframe", "ddg-captcha"])
@pytest.mark.parametrize("kind", ["detail", "partner"])
async def test_structural_manual_widget_wins_over_ready_download_content(
    tmp_path, monkeypatch, widget, kind
):
    name = f"public-domain-{kind}-settled.html"
    soup = BeautifulSoup(saved(name), "html.parser")
    soup.main.insert(0, soup.new_tag("iframe", id=widget))
    path = DETAIL if kind == "detail" else PARTNERS[0]
    session, io = browser(tmp_path, monkeypatch, {path: str(soup)})
    with pytest.raises(ProviderResponseError) as error:
        await first_url(session)
    assert error.value.reason == "browser_check"
    assert io.visits == ([DETAIL] if kind == "detail" else [DETAIL, PARTNERS[0]])
    assert session._process_lock._fd is None
    state = json.loads((tmp_path / "annas-browser-usage.json").read_text())
    assert state["next_allowed"] - state["window_started"] >= 299


async def test_budget_exhaustion_stops_before_partner_navigation(tmp_path, monkeypatch):
    session, io = browser(tmp_path, monkeypatch, annas_browser_daily_limit=1)
    with pytest.raises(ProviderResponseError) as error:
        await first_url(session)
    assert error.value.reason == "quota_exhausted"
    assert io.visits == [DETAIL] and spent(tmp_path) == 1
    assert session._process_lock._fd is None


async def test_ordered_partner_stream_releases_lock_and_does_not_revisit_detail(
    tmp_path, monkeypatch
):
    session, io = browser(tmp_path, monkeypatch, annas_browser_max_servers=2)
    stream = session.iter_download_urls(MD5, deadline=WalkDeadline(4))
    try:
        await anext(stream)
        assert session._process_lock._fd is None
        await anext(stream)
        assert session._process_lock._fd is None
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
    assert io.visits == [DETAIL, PARTNERS[0], PARTNERS[1]]
    assert spent(tmp_path) == 3 and io.opens == 2


async def test_partner_http_failure_tries_next_in_site_order(tmp_path, monkeypatch):
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: saved("b5/partner-countdown.html")},
        {PARTNERS[0]: 502},
    )
    assert (await first_url(session)).startswith("https://wbsg8v.xyz/")
    assert io.visits == [DETAIL, PARTNERS[0], PARTNERS[1]] and spent(tmp_path) == 3


async def test_hidden_payload_waits_for_visible_link_and_preserves_signed_query(
    tmp_path, monkeypatch
):
    url = "https://bytes.example.invalid/book.epub?Policy=one&Signature=two"
    session, io = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: [payload_variant(url, hidden=True), payload_variant(url)]},
    )
    assert await first_url(session) == url
    assert io.visits == [DETAIL, PARTNERS[0]]


@pytest.mark.parametrize(
    "url",
    [
        "httpx://bytes.example.invalid/book.pdf",
        "javascript:alert(1)",
        "https://annas-archive.gl/another.pdf",
        "https://annas-archive.gd/another.pdf",
        "http://127.0.0.1/book.pdf",
        "http://[::1]/book.pdf",
        "http://localhost/book.pdf",
        "https://user:password@bytes.example.invalid/book.pdf",
    ],
)
async def test_unsafe_or_anna_page_links_are_not_download_candidates(
    tmp_path, monkeypatch, url
):
    session, _ = browser(
        tmp_path,
        monkeypatch,
        {PARTNERS[0]: payload_variant(url)},
        annas_browser_max_servers=1,
        annas_browser_settle_seconds=0.01,
    )
    with pytest.raises(ProviderResponseError) as error:
        await first_url(session)
    assert error.value.reason == "protocol_error"


async def test_invalid_download_md5_refuses_before_any_page_spend(
    tmp_path, monkeypatch
):
    session, io = browser(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        await first_url(session, md5="../not-a-record")
    assert io.visits == [] and not (tmp_path / "annas-browser-usage.json").exists()


def adapter_browser(tmp_path, monkeypatch, **settings):
    session, io = browser(tmp_path, monkeypatch, **settings)
    adapter = AnnasArchiveAdapter(session.config)
    adapter._browser = session
    return adapter, io


async def test_adapter_no_key_uses_browser_only_when_enabled(tmp_path, monkeypatch):
    adapter, io = adapter_browser(tmp_path, monkeypatch)
    adapter.config.annas_browser_enabled = False
    try:
        with pytest.raises(
            ProviderConfigurationError, match="ANNAS_SECRET_KEY not configured"
        ):
            await adapter.get_download_url(MD5)
        assert io.visits == []
        adapter.config.annas_browser_enabled = True
        result = await adapter.get_download_url(MD5, deadline=WalkDeadline(3))
        assert result.url.startswith("https://wbsg8v.xyz/")
        assert result.quota_info is None and spent(tmp_path) == 2
    finally:
        await adapter.close()


@pytest.mark.parametrize("remaining", [0, 4])
async def test_keyed_candidate_preserves_quota_and_never_opens_browser(
    tmp_path, monkeypatch, remaining
):
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_secret_key="offline-key")

    def api(request):
        assert request.url.path == "/dyn/api/fast_download.json"
        assert request.url.params["domain_index"] == "1"
        return httpx.Response(
            200,
            json={
                "download_url": "https://bytes.example.invalid/keyed.epub",
                "account_fast_download_info": {
                    "downloads_left": remaining,
                    "downloads_per_day": 25,
                    "downloads_done_today": 25 - remaining,
                },
            },
        )

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    try:
        results = [
            r
            async for r in adapter.iter_download_candidates(
                MD5, deadline=WalkDeadline(10)
            )
        ]
        assert len(results) == 1 and results[0].url.endswith("keyed.epub")
        assert results[0].quota_info.downloads_left == remaining
        assert io.visits == [] and not (tmp_path / "annas-browser-usage.json").exists()
    finally:
        await adapter.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_trusted_key_rejection_uses_browser_without_exporting_key(
    tmp_path, monkeypatch, status
):
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_secret_key="offline-key")
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status))
    )
    try:
        result = await adapter.get_download_url(MD5)
        assert (
            result.url.startswith("https://wbsg8v.xyz/")
            and "offline-key" not in result.url
        )
        assert io.visits == [DETAIL, PARTNERS[0]]
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"error": "quota exhausted"}),
        httpx.Response(200, json={}),
    ],
)
async def test_other_keyed_failures_do_not_fall_back(tmp_path, monkeypatch, response):
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_secret_key="offline-key")
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: response)
    )
    try:
        with pytest.raises(ProviderResponseError):
            await adapter.get_download_url(MD5)
        assert io.visits == []
    finally:
        await adapter.close()


async def test_untrusted_key_host_does_not_fall_back(tmp_path, monkeypatch):
    adapter, io = adapter_browser(
        tmp_path,
        monkeypatch,
        annas_secret_key="offline-key",
        annas_base_url="https://untrusted.example.invalid",
    )
    try:
        with pytest.raises(ProviderConfigurationError, match="unverified host"):
            await adapter.get_download_url(MD5)
        assert io.visits == []
    finally:
        await adapter.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_auto_router_adds_browser_fallback_only_when_opted_in(
    tmp_path, monkeypatch, enabled
):
    adapter, io = adapter_browser(tmp_path, monkeypatch)
    adapter.config.annas_browser_enabled = enabled
    router = SourceRouter(adapter.config)
    router._annas = adapter

    async def unavailable(md5, deadline=None):
        raise ProviderResponseError(
            "libgen", "libgen.li", "offline unavailable", reason="http_error"
        )
        yield

    async def close():
        pass

    router._libgen = SimpleNamespace(iter_download_candidates=unavailable, close=close)
    try:
        if enabled:
            result = await router.get_download_url(MD5)
            assert result.url.startswith("https://wbsg8v.xyz/")
            assert io.visits == [DETAIL, PARTNERS[0]]
        else:
            with pytest.raises(AllSourcesFailedError) as error:
                await router.get_download_url(MD5)
            assert [f.provider for f in error.value.failures] == ["libgen"]
            assert io.visits == []
    finally:
        await router.close()


async def test_router_accepts_final_paid_browser_visit_without_membership_quota(
    tmp_path, monkeypatch
):
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_browser_daily_limit=2)
    router = SourceRouter(adapter.config)
    router._annas = adapter
    try:
        result = await router.get_download_url(MD5, source="annas")
        assert (
            result.url.startswith("https://wbsg8v.xyz/") and result.quota_info is None
        )
        assert spent(tmp_path) == 2 and io.visits == [DETAIL, PARTNERS[0]]
    finally:
        await router.close()


async def test_search_and_download_spend_the_same_daily_counter(tmp_path, monkeypatch):
    adapter, io = adapter_browser(tmp_path, monkeypatch, annas_browser_daily_limit=3)
    io.pages["/search"] = saved("kant-settled.html")
    try:
        await adapter._browser.search_html("kant")
        await adapter.get_download_url(MD5)
        assert spent(tmp_path) == 3
        with pytest.raises(ProviderResponseError) as error:
            await adapter.get_download_url(MD5)
        assert error.value.reason == "quota_exhausted"
        assert io.visits == ["/search", DETAIL, PARTNERS[0]]
    finally:
        await adapter.close()


async def test_expired_candidate_clock_does_not_start_a_browser(tmp_path, monkeypatch):
    adapter, io = adapter_browser(tmp_path, monkeypatch)
    stream = adapter.iter_download_candidates(MD5, deadline=WalkDeadline(0))
    try:
        with pytest.raises(ProviderTimeoutError):
            await anext(stream)
        assert io.visits == [] and not (tmp_path / "annas-browser-usage.json").exists()
    finally:
        await stream.aclose()
        await adapter.close()


async def test_router_pauses_resolution_clock_while_consumer_transfers(
    tmp_path, monkeypatch
):
    adapter, io = adapter_browser(
        tmp_path, monkeypatch, walk_budget=0.8, annas_browser_max_servers=2
    )
    router = SourceRouter(adapter.config)
    router._annas = adapter
    stream = router.iter_download_candidates(MD5, source="annas")
    try:
        await anext(stream)
        assert adapter._browser._process_lock._fd is None
        await asyncio.sleep(
            0.9
        )  # Longer than the resolution clock, excluded by the router.
        result = await anext(stream)
        assert result.url.startswith("https://wbsg8v.xyz/")
        assert io.visits == [DETAIL, PARTNERS[0], PARTNERS[1]] and spent(tmp_path) == 3
    finally:
        await stream.aclose()
        await router.close()


PAYLOAD = b"%PDF-1.7\n" + b"B5 synthetic original file bytes\n" * 4000
DIGEST = hashlib.md5(PAYLOAD).hexdigest()
BOOK = {"md5": DIGEST, "source": "annas", "title": "Offline B5", "extension": "pdf"}


def integration_route(tmp_path, monkeypatch, handler, **settings):
    """Real adapter/router/session/transfer/publication; fake browser and HTTP only."""
    from lib import python_bridge as bridge

    adapter, io = adapter_browser(tmp_path, monkeypatch, **settings)
    io.pages = {
        key.replace(MD5, DIGEST): value.replace(MD5, DIGEST)
        for key, value in io.pages.items()
    }
    router = SourceRouter(adapter.config)
    router._annas = adapter
    monkeypatch.setattr(bridge, "get_source_router", AsyncMock(return_value=router))
    client_type = httpx.AsyncClient
    requests = []

    def serve(request):
        # No browser cookie/storage/credential API exists on BrowserIO, and
        # none of those headers may be manufactured at the transfer boundary.
        assert (
            "cookie" not in request.headers and "authorization" not in request.headers
        )
        requests.append(request)
        return handler(request)

    def client(**kwargs):
        assert "cookies" not in kwargs and "auth" not in kwargs
        return client_type(transport=httpx.MockTransport(serve), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return router, io, requests


@pytest.mark.parametrize(
    "mode,want_pages,want_http",
    [
        ("whole", 2, 1),
        ("resume", 2, 2),
        ("redirect", 2, 2),
        ("failover", 3, 2),
    ],
)
async def test_browser_url_uses_real_transfer_resume_receipt_and_reuse(
    tmp_path, monkeypatch, mode, want_pages, want_http
):
    """A browser-only mock transfer would miss validation, resume and receipt bugs."""
    from lib import python_bridge as bridge
    from lib.acquisition import acquire

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PAYLOAD[:65536]
            raise httpx.RemoteProtocolError("B5 interrupted body")

    def handler(request):
        if mode == "failover" and request.url.path == "/bad.epub":
            return httpx.Response(
                200, content=b"<!doctype html><html>expired link</html>"
            )
        if mode == "redirect" and request.url.host == "wbsg8v.xyz":
            return httpx.Response(
                302, headers={"location": "https://bytes.example.invalid/final.pdf"}
            )
        if mode == "resume" and "range" not in request.headers:
            return httpx.Response(
                200, stream=Interrupted(), headers={"content-length": str(len(PAYLOAD))}
            )
        if mode == "resume":
            assert request.headers["range"] == "bytes=65536-"
            return httpx.Response(
                206,
                content=PAYLOAD[65536:],
                headers={
                    "content-range": f"bytes 65536-{len(PAYLOAD) - 1}/{len(PAYLOAD)}",
                    "content-type": "application/pdf",
                },
            )
        return httpx.Response(
            200, content=PAYLOAD, headers={"content-type": "application/pdf"}
        )

    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        handler,
        annas_browser_daily_limit=want_pages,
        download_resume_attempts=1,
    )
    if mode == "failover":
        io.pages[PARTNERS[0].replace(MD5, DIGEST)] = payload_variant(
            "https://bytes.example.invalid/bad.epub"
        ).replace(MD5, DIGEST)
        io.pages[PARTNERS[1].replace(MD5, DIGEST)] = payload_variant(
            "https://bytes.example.invalid/good.epub"
        ).replace(MD5, DIGEST)
    try:
        result = await acquire(
            BOOK,
            str(tmp_path / "output"),
            filename="b5.pdf",
            transfer=bridge.download_book,
        )
        assert Path(result["path"]).read_bytes() == PAYLOAD
        record_path = Path(result["record"])
        receipt = record_path.read_bytes()
        record = json.loads(receipt)
        assert record["state"] == "complete"
        assert record["identifiers"]["md5"] == DIGEST
        assert record["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
        assert record["provenance"] == {
            "route": "mirror_download",
            "mirror": "annas-archive.gl",
            "host": "bytes.example.invalid"
            if mode in ("redirect", "failover")
            else "wbsg8v.xyz",
        }
        assert record.get("quota_estimate") is None
        # Existing optional bridge field: explicit unknown internally, stripped
        # by the public facade so it cannot reuse a membership estimate.
        assert "quota_estimate" in result and result["quota_estimate"] is None
        assert len(io.visits) == want_pages and len(requests) == want_http
        assert spent(tmp_path) == want_pages
        assert router._annas._browser._process_lock._fd is None
        before = (len(io.visits), len(requests))
        again = await acquire(
            BOOK,
            str(tmp_path / "output"),
            filename="b5.pdf",
            transfer=bridge.download_book,
        )
        assert (
            again["transfer_skipped"] is True
            and (len(io.visits), len(requests)) == before
        )
        assert record_path.read_bytes() == receipt
        assert "quota_estimate" in again and again["quota_estimate"] is None
        print(
            f"B5 {mode}: browser_pages={want_pages}; HTTP_requests={want_http}; reuse_extra=0"
        )
    finally:
        await router.close()


@pytest.mark.parametrize(
    "body,reason",
    [
        (b"<!doctype html><html><body>challenge</body></html>", "protocol_error"),
        (b"%PDF-1.7\nwrong selected edition bytes", "integrity_mismatch"),
    ],
)
async def test_browser_payload_rejection_cannot_publish_a_complete_receipt(
    tmp_path, monkeypatch, body, reason
):
    from lib import python_bridge as bridge
    from lib.acquisition import acquire

    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(200, content=body),
        annas_browser_max_servers=1,
    )
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as error:
            await acquire(
                BOOK, str(output), filename="b5.pdf", transfer=bridge.download_book
            )
        assert reason in [f.reason for f in error.value.failures]
        assert len(io.visits) == 2 and len(requests) == 1
        assert not (output / "b5.pdf").exists() and not list(output.glob(".source-*"))
        assert all(
            json.loads(p.read_text())["state"] == "intent"
            for p in output.glob("*.acq.json")
        )
    finally:
        await router.close()


async def test_public_acquire_dispatch_admits_only_enabled_browser_without_key(
    tmp_path, monkeypatch
):
    """The adapter is useless if acquire_book rejects it before URL resolution."""
    from lib import python_bridge as bridge
    from lib.acquisition import AcquisitionError

    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(
            200, content=PAYLOAD, headers={"content-type": "application/pdf"}
        ),
        annas_browser_daily_limit=2,
    )
    monkeypatch.delenv("ANNAS_SECRET_KEY", raising=False)
    monkeypatch.setattr(bridge, "get_source_config", lambda **kwargs: router.config)
    args = {
        "book_details": BOOK,
        "output_dir": str(tmp_path / "output"),
        "filename": "b5.pdf",
    }
    try:
        router.config.annas_browser_enabled = False
        with pytest.raises(AcquisitionError, match="Configure ANNAS_SECRET_KEY"):
            await bridge._dispatch_bridge_function("acquire_book", args)
        assert io.visits == [] and requests == []
        router.config.annas_browser_enabled = True
        result = await bridge._dispatch_bridge_function("acquire_book", args)
        assert Path(result["path"]).read_bytes() == PAYLOAD
        assert len(io.visits) == 2 and len(requests) == 1
    finally:
        await router.close()


@pytest.mark.parametrize("widget", ["ddg-iframe", "ddg-captcha"])
async def test_structural_widget_is_manual_even_on_settled_search(
    tmp_path, monkeypatch, widget
):
    from lib.sources.annas_browser import needs_human_check

    soup = BeautifulSoup(saved("kant-settled.html"), "html.parser")
    soup.body.insert(0, soup.new_tag("iframe", id=widget))
    page = str(soup)
    assert needs_human_check(page)
    session, io = browser(tmp_path, monkeypatch, {"/search": page})
    with pytest.raises(ProviderResponseError) as error:
        await session.search_html("kant")
    assert error.value.reason == "browser_check" and io.visits == ["/search"]
