"""Offline member-route contracts against an in-process fake HTTP server."""

import asyncio
import json
import logging
from pathlib import Path
import traceback
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, quote_plus

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import SourceError

BASE = "https://annas-archive.gl"
KEY = "member-canary+/ key-739!"
COOKIE = "cookie-canary-839"
HTML = '<html><a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">A book</a></html>'
CHECK = "<html><title>DDoS-Guard</title><p>Checking your browser</p></html>"


class FakeSite:
    def __init__(self, login="ok", refuse=0):
        self.login = login
        self.refuse = refuse
        self.posts = 0
        self.gets = 0
        self.foreign = 0
        self.cookies = []

    async def __call__(self, request):
        if request.url.host != "annas-archive.gl":
            self.foreign += 1
            return httpx.Response(200, text=HTML)
        if request.method == "POST":
            self.posts += 1
            assert request.url.path == "/account/"
            assert not request.url.query
            assert parse_qs(request.content.decode()) == {"key": [KEY]}
            await asyncio.sleep(0.005)
            if self.login == "error":
                raise httpx.ConnectError(KEY + COOKIE, request=request)
            if self.login == "redirect":
                return httpx.Response(
                    307, headers={"location": "https://other.invalid/account/"}
                )
            if self.login in ("reject", "reject-get-error"):
                return httpx.Response(200, text="<form>Sign in</form>")
            if self.login == "wrong-cookie-domain":
                return httpx.Response(
                    302,
                    headers={
                        "set-cookie": f"aa_account_id2={COOKIE}; Domain=other.invalid; Path=/"
                    },
                )
            return httpx.Response(
                302,
                headers={
                    "location": "/",
                    "set-cookie": f"aa_account_id2={COOKIE}; Path=/; HttpOnly",
                },
            )
        self.gets += 1
        self.cookies.append(request.headers.get("cookie", ""))
        if self.login in ("get-error", "reject-get-error"):
            raise httpx.RemoteProtocolError(KEY + COOKIE, request=request)
        if self.login == "get-redirect":
            return httpx.Response(
                302, headers={"location": "https://other.invalid/search"}
            )
        if self.login == "echo":
            return httpx.Response(200, text=f'<a href="/md5/abc">{KEY} {COOKIE}</a>')
        if self.login == "escaped-echo":
            text = "".join(f"&#{ord(char)};" for char in KEY + COOKIE)
            return httpx.Response(200, text=f'<a href="/md5/abc">{text}</a>')
        if self.refuse:
            self.refuse -= 1
            return httpx.Response(403, text=CHECK)
        if self.login == "challenge":
            return httpx.Response(200, text=CHECK)
        return httpx.Response(200, text=HTML)


@pytest.fixture
async def make_adapter():
    clients = []

    def make(site, key=KEY, base=BASE):
        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_secret_key=key, annas_base_url=base, preflight_enabled=False
            )
        )
        adapter._client = httpx.AsyncClient(
            transport=httpx.MockTransport(site), follow_redirects=True
        )
        clients.append(adapter)
        return adapter

    yield make
    for adapter in clients:
        await adapter.close()


async def test_lazy_success_and_session_reuse(make_adapter):
    site = FakeSite()
    adapter = make_adapter(site)
    assert adapter.member_status() == {
        "member_key_configured": True,
        "member_session": "none",
    }
    assert site.posts == 0
    for _ in range(2):
        assert (await adapter.search("books"))[0].title == "A book"
    assert site.posts == 1
    assert all(f"aa_account_id2={COOKIE}" in c for c in site.cookies)
    assert adapter.member_status()["member_session"] == "active"
    await adapter.close()
    assert adapter.member_status()["member_session"] == "none"


@pytest.mark.parametrize(
    "mode,expected", [("reject", "rejected"), ("error", "none"), ("redirect", "none")]
)
async def test_rejected_or_failed_login_backs_off_and_falls_back(
    make_adapter, mode, expected
):
    site = FakeSite(login=mode)
    adapter = make_adapter(site)
    for _ in range(3):
        assert (await adapter.search("books"))[0].title == "A book"
    assert site.posts == 1
    assert site.foreign == 0
    assert adapter.member_status()["member_session"] == expected
    assert not any(site.cookies)


async def test_no_key_never_posts(make_adapter):
    site = FakeSite()
    adapter = make_adapter(site, key="")
    await adapter.search("books")
    assert site.posts == 0
    assert adapter.member_status() == {
        "member_key_configured": False,
        "member_session": "none",
    }


async def test_untrusted_host_never_receives_key(make_adapter):
    site = FakeSite()
    adapter = make_adapter(site, base="https://other.invalid")
    await adapter.search("books")
    assert site.posts == 0


async def test_refused_session_refreshes_once(make_adapter):
    site = FakeSite(refuse=1)
    adapter = make_adapter(site)
    assert (await adapter.search("books"))[0].title == "A book"
    assert site.posts == 2
    assert site.gets == 2


async def test_repeated_refusal_never_loops(make_adapter):
    site = FakeSite(refuse=100)
    adapter = make_adapter(site)
    for _ in range(3):
        with pytest.raises(SourceError):
            await adapter.search("books")
    assert site.posts == 2
    assert site.gets == 4
    assert adapter.member_status()["member_session"] == "rejected"


async def test_concurrent_searches_share_signin(make_adapter):
    site = FakeSite()
    adapter = make_adapter(site)
    results = await asyncio.gather(*(adapter.search("books") for _ in range(12)))
    assert all(r[0].title == "A book" for r in results)
    assert site.posts == 1


async def test_live_session_challenge_is_an_error_not_empty(make_adapter):
    site = FakeSite(login="challenge")
    adapter = make_adapter(site)
    with pytest.raises(SourceError) as caught:
        await adapter.search("books")
    assert "browser" in str(caught.value).lower()
    assert caught.value.reason == "http_error"
    assert site.posts == 2


async def test_member_page_cross_host_redirect_is_refused(make_adapter):
    site = FakeSite(login="get-redirect")
    adapter = make_adapter(site)
    with pytest.raises(SourceError):
        await adapter.search("books")
    assert site.foreign == 0


async def test_expired_cookie_signs_in_again(make_adapter):
    site = FakeSite()
    adapter = make_adapter(site)
    await adapter.search("books")
    adapter._client.cookies.clear()
    assert adapter.member_status()["member_session"] == "none"
    await adapter.search("books")
    assert site.posts == 2


async def test_rejected_login_can_retry_after_cooldown(make_adapter, monkeypatch):
    site = FakeSite(login="reject")
    adapter = make_adapter(site)
    await adapter.search("books")
    site.login = "ok"
    future = adapter._member_retry_at + 1
    monkeypatch.setattr(
        "lib.sources.annas.time", SimpleNamespace(monotonic=lambda: future)
    )
    await adapter.search("books")
    assert site.posts == 2
    assert adapter.member_status()["member_session"] == "active"


async def test_debug_transport_logs_cannot_emit_credentials(make_adapter, caplog):
    adapter = make_adapter(FakeSite())
    with caplog.at_level(logging.DEBUG):
        await adapter.search("books")
        # Model httpcore's debug response-header event and echoed transport error.
        logging.getLogger("httpcore.http11").debug(
            "receive_response_headers.complete %r", [(b"set-cookie", COOKIE.encode())]
        )
        logging.getLogger("httpcore.connection").debug("connect_tcp.failed %s", KEY)
    assert COOKIE not in caplog.text
    assert KEY not in caplog.text
    await adapter.close()


@pytest.mark.parametrize("mode", ["echo", "escaped-echo"])
async def test_provider_echo_cannot_turn_credentials_into_metadata(make_adapter, mode):
    adapter = make_adapter(FakeSite(login=mode))
    with pytest.raises(SourceError) as caught:
        await adapter.search("books")
    assert KEY not in str(caught.value)
    assert COOKIE not in str(caught.value)


async def test_concurrent_refusals_share_one_refresh(make_adapter):
    class RefusedGeneration(FakeSite):
        async def __call__(self, request):
            if request.method == "GET" and self.posts == 1:
                await asyncio.sleep(0.01)
                return httpx.Response(403, text=CHECK)
            return await super().__call__(request)

    site = RefusedGeneration()
    adapter = make_adapter(site)
    results = await asyncio.gather(*(adapter.search("books") for _ in range(10)))
    assert all(r[0].title == "A book" for r in results)
    assert site.posts == 2


async def test_cookie_for_wrong_domain_is_not_an_active_session(make_adapter):
    site = FakeSite(login="wrong-cookie-domain")
    adapter = make_adapter(site)
    await adapter.search("books")
    assert adapter.member_status()["member_session"] == "none"
    await adapter.search("books")
    assert site.posts == 1


async def test_timed_out_signin_backs_off_before_next_search(make_adapter):
    class SlowLogin(FakeSite):
        async def __call__(self, request):
            if request.method == "POST" and self.posts == 0:
                self.posts += 1
                await asyncio.sleep(5)
            return await super().__call__(request)

    site = SlowLogin()
    adapter = make_adapter(site)
    adapter.config.total_timeout = 0.05
    with pytest.raises(SourceError):
        await adapter.search("books")
    assert (await adapter.search("books"))[0].title == "A book"
    assert site.posts == 1
    assert adapter.member_status()["member_session"] == "none"


async def test_canaries_absent_from_real_acquisition_receipts(
    make_adapter, tmp_path, caplog, capsys
):
    from lib import acquisition as acq
    from lib import python_bridge as bridge

    row = (await make_adapter(FakeSite()).search("books"))[0]
    book = {**vars(row), **row.extra, "source": "annas_archive", "extension": "pdf"}
    target = tmp_path / "chosen.pdf"

    async def transfer(book, directory, *, filename, publication_hook):
        stage = tmp_path / ".stage"
        stage.write_bytes(b"%PDF-1.7\nsynthetic member receipt payload")
        publication_hook(stage, target)
        bridge._publish_or_reuse(stage, target)
        return {"file_path": str(target)}

    reply = await acq.acquire(
        book, str(tmp_path), filename="chosen.pdf", transfer=transfer
    )
    assert json.loads(Path(reply["record"]).read_text())["state"] == "complete"
    artifacts = [json.dumps(reply), caplog.text]
    artifacts += [path.read_text() for path in tmp_path.rglob("*.json")]
    output = capsys.readouterr()
    artifacts += [output.out, output.err]
    for artifact in artifacts:
        for token in (KEY, COOKIE, quote(KEY, safe=""), quote_plus(KEY, safe="")):
            assert token not in artifact


@pytest.mark.parametrize(
    "mode",
    ["error", "get-error", "reject-get-error", "challenge", "redirect", "reject"],
)
async def test_canaries_absent_from_output_errors_logs_and_receipts(
    make_adapter, mode, caplog, capsys
):
    site = FakeSite(login=mode)
    adapter = make_adapter(site)
    artifacts = []
    try:
        rows = await adapter.search("books")
        artifacts.append(repr(rows))
    except SourceError as error:
        artifacts.extend(
            [json.dumps(error.to_dict()), "".join(traceback.format_exception(error))]
        )
        assert error.__cause__ is None
        assert error.__context__ is None
    artifacts.append(json.dumps(adapter.member_status()))
    output = capsys.readouterr()
    artifacts.extend([caplog.text, output.out, output.err])
    for artifact in artifacts:
        for secret in (KEY, COOKIE, quote(KEY, safe=""), quote_plus(KEY, safe="")):
            assert secret not in artifact
