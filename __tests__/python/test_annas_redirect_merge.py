"""Member merge boundaries with real HTTP clients and two loopback listeners."""

from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError, SourceError
from test_credential_redirects import loopback
from zlibrary.eapi import RedirectRefusedError

BASE = "https://annas-archive.gl"
KEY = "merge-key-canary-731"  # gitleaks:allow -- synthetic loopback-only value
COOKIE = "merge-cookie-canary-931"
HTML = '<a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">A merge book</a>'


@asynccontextmanager
async def member_site(
    *,
    account_status=302,
    fresh_cookie=True,
    account_foreign=False,
    page_hops=0,
    page_escape=False,
    api_foreign=False,
):
    requests = []

    def sink(_head, _body):
        return 403, {}, b'{"error":"Invalid API key"}'

    async with loopback("127.0.0.2", sink) as (receiver, leaked):
        foreign = "https://sub.annas-archive.gl/private?key=" + KEY

        def origin_handler(head, body):
            lines = head.decode().split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            requests.append((method, target, headers.get("Cookie", ""), body))
            path = urlsplit(target).path
            if method == "POST":
                assert path == "/account/"
                assert parse_qs(body.decode()) == {"key": [KEY]}
                response_headers = {
                    "Location": foreign
                    if account_foreign
                    else "/after-account?key=" + KEY
                }
                if fresh_cookie:
                    response_headers["Set-Cookie"] = (
                        f"aa_account_id2={COOKIE}; Domain=.annas-archive.gl; Path=/; HttpOnly"
                    )
                return account_status, response_headers, b"Invalid API key"
            if path == "/dyn/api/fast_download.json":
                return (
                    307,
                    {"Location": foreign if api_foreign else "/after-api?key=" + KEY},
                    b"Invalid API key",
                )
            if path.startswith("/after-"):
                return 403, {}, b"Invalid API key"
            if page_escape:
                return 302, {"Location": foreign}, b"redirect"
            hop = int(path.rsplit("/", 1)[1]) if path.startswith("/page/") else 0
            if hop < page_hops:
                return 302, {"Location": f"/page/{hop + 1}"}, b"redirect"
            return 200, {}, HTML.encode()

        async with loopback("127.0.0.1", origin_handler) as (origin, _):

            class LocalTransport(httpx.AsyncBaseTransport):
                def __init__(self):
                    self.transport = httpx.AsyncHTTPTransport(trust_env=False)

                async def handle_async_request(self, request):
                    if request.url.host == "annas-archive.gl":
                        base = httpx.URL(origin)
                    elif request.url.host == "sub.annas-archive.gl":
                        base = httpx.URL(receiver)
                    else:
                        raise AssertionError("Non-loopback network forbidden")
                    local = httpx.Request(
                        request.method,
                        request.url.copy_with(
                            scheme="http", host=base.host, port=base.port
                        ),
                        headers=request.headers,
                        content=await request.aread(),
                    )
                    return await self.transport.handle_async_request(local)

                async def aclose(self):
                    await self.transport.aclose()

            adapter = AnnasArchiveAdapter(
                SourceConfig(
                    annas_secret_key=KEY, annas_base_url=BASE, preflight_enabled=False
                )
            )
            adapter._client = httpx.AsyncClient(
                transport=LocalTransport(), follow_redirects=True, trust_env=False
            )
            try:
                yield adapter, requests, leaked
            finally:
                await adapter.close()


@pytest.mark.parametrize("status", [302, 307, 308])
async def test_account_redirect_is_named_uncertainty_not_rejected_key(status):
    async with member_site(
        account_status=status, fresh_cookie=False, account_foreign=True
    ) as (adapter, requests, leaked):
        refusal = await adapter._signin(adapter._client)
        assert isinstance(refusal, RedirectRefusedError)
        assert refusal.status_code == status
        assert refusal.target_host == "sub.annas-archive.gl"
        assert KEY not in str(refusal) and COOKIE not in str(refusal)
        assert "/private" not in str(refusal) and "?key=" not in str(refusal)
        assert adapter.member_status()["member_session"] == "none"
        assert (await adapter.search("books"))[0].title == "A merge book"
        assert len([r for r in requests if r[0] == "POST"]) == 1
        assert not any(r[2] for r in requests if r[0] == "GET")
        assert leaked == []


async def test_account_refusal_can_accept_fresh_same_origin_cookie_without_following():
    async with member_site(page_hops=5) as (adapter, requests, leaked):
        refusal = await adapter._signin(adapter._client)
        assert isinstance(refusal, RedirectRefusedError)
        assert refusal.target_host == "annas-archive.gl"
        assert adapter.member_status()["member_session"] == "active"
        for _ in range(2):
            assert (await adapter.search("books"))[0].title == "A merge book"
        assert len([r for r in requests if r[0] == "POST"]) == 1
        gets = [r for r in requests if r[0] == "GET"]
        assert len(gets) == 12  # Two independent searches, each with five allowed hops.
        assert all(COOKIE in row[2] for row in gets)
        assert not any(row[1].startswith("/after-account") for row in gets)
        assert leaked == []


@pytest.mark.parametrize("params", [None, {"q": "books"}])
async def test_cookie_page_with_or_without_params_refuses_foreign_redirect(params):
    async with member_site(page_escape=True) as (adapter, requests, leaked):
        await adapter._signin(adapter._client)
        with pytest.raises(SourceError, match="Page redirect refused"):
            await adapter._fetch(adapter._client, BASE + "/search", params=params)
        assert len([r for r in requests if r[0] == "GET"]) == 1
        assert leaked == []


@pytest.mark.parametrize(
    "foreign", [False, True], ids=["same-origin", "foreign-origin"]
)
async def test_api_key_plus_member_cookie_never_follows_redirect(foreign):
    async with member_site(api_foreign=foreign) as (adapter, requests, leaked):
        await adapter._signin(adapter._client)
        with pytest.raises(ProviderResponseError, match="Redirect refused"):
            await adapter.get_download_url("a" * 32)
        api = [r for r in requests if r[1].startswith("/dyn/api/fast_download.json")]
        assert len(api) == 1 and KEY in api[0][1] and COOKIE in api[0][2]
        assert not any(row[1].startswith("/after-api") for row in requests)
        assert adapter.member_status()["member_session"] == "active"
        assert leaked == []


@pytest.mark.parametrize("params", [None, {"q": "books"}])
async def test_sixth_member_page_redirect_is_refused_before_seventh_request(params):
    async with member_site(page_hops=6) as (adapter, requests, leaked):
        await adapter._signin(adapter._client)
        with pytest.raises(SourceError, match="Page redirect refused"):
            await adapter._fetch(adapter._client, BASE + "/search", params=params)
        assert len([r for r in requests if r[0] == "GET"]) == 6
        assert leaked == []


async def test_parameterized_member_page_retains_five_same_origin_hops():
    async with member_site(page_hops=5) as (adapter, requests, leaked):
        await adapter._signin(adapter._client)
        response = await adapter._fetch(
            adapter._client, BASE + "/search", params={"q": "books"}
        )
        assert response.status_code == 200
        gets = [r for r in requests if r[0] == "GET"]
        assert len(gets) == 6 and all(COOKIE in row[2] for row in gets)
        assert parse_qs(urlsplit(gets[0][1]).query) == {"q": ["books"]}
        assert leaked == []
