"""Member HTTP search regressions; transport is synthetic or loopback."""

import asyncio
import socket

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderTimeoutError, SourceError
from test_annas_member import (
    BASE,
    COOKIE,
    HTML,
    KEY,
    FakeSite,
)

pytest_plugins = ["test_annas_member"]


@pytest.mark.parametrize("key", ["", KEY])
@pytest.mark.parametrize(
    "title",
    [
        "Browser verification of web applications",
        "Checking Your Browser: a field guide",
        "DDoS Guard research",
        "DDoS-Guard",
    ],
)
async def test_result_rows_override_challenge_words(make_adapter, key, title):
    async def site(request):
        if request.method == "POST":
            return await FakeSite()(request)
        return httpx.Response(
            200, text=f'<title>{title}</title><a href="/md5/abc">{title}</a>'
        )

    assert (await make_adapter(site, key=key).search("books"))[0].title == title


@pytest.mark.parametrize("key", ["", KEY])
@pytest.mark.parametrize(
    "page",
    [
        "<html></html>",
        "<title>Just a moment...</title>Verify you are human",
        "<title>A ddos guard book</title>",
    ],
)
async def test_no_rows_without_captured_empty_marker_is_unrecognised(
    make_adapter, key, page
):
    async def site(request):
        return (
            await FakeSite()(request)
            if request.method == "POST"
            else httpx.Response(200, text=page)
        )

    with pytest.raises(SourceError, match="Unrecognised page"):
        await make_adapter(site, key=key).search("books")


async def test_anonymous_timeout_keeps_master_detail(make_adapter):
    async def site(request):
        raise ProviderTimeoutError(
            "annas", "annas-archive.gl", "search exceeded 45s", reason="search_timeout"
        )

    with pytest.raises(SourceError) as caught:
        await make_adapter(site, key="").search("books")
    assert caught.value.detail == "search exceeded 45s"


async def test_anonymous_real_redirect_failure_preserves_transport_classification():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()

    async def redirect(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{dead_port}/end\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(redirect, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    adapter = AnnasArchiveAdapter(
        SourceConfig(annas_base_url=base, preflight_enabled=False)
    )
    adapter._client = httpx.AsyncClient(follow_redirects=True, trust_env=False)
    try:
        with pytest.raises(SourceError) as caught:
            await adapter.search("books")
        assert caught.value.reason in {"connect_error", "connect_refused"}
    finally:
        await adapter.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("hops", [1, 5])
async def test_same_origin_redirects_keep_member_session(make_adapter, hops):
    fake = FakeSite()
    paths = []

    async def site(request):
        if request.method == "POST":
            return await fake(request)
        paths.append(request.url.path)
        assert f"aa_account_id2={COOKIE}" in request.headers["cookie"]
        if len(paths) <= hops:
            return httpx.Response(302, headers={"location": f"/next/{len(paths)}"})
        return httpx.Response(200, text=HTML)

    adapter = make_adapter(site)
    assert (await adapter.search("books"))[0].title == "A book"
    assert len(paths) == hops + 1
    assert fake.posts == 1


@pytest.mark.parametrize(
    "location",
    [
        "http://annas-archive.gl/next",
        "https://annas-archive.gl:444/next",
        "https://other.invalid/next",
        "/loop",
    ],
)
async def test_foreign_origin_or_redirect_loop_stops(make_adapter, location):
    fake = FakeSite()
    gets = []

    async def site(request):
        if request.method == "POST":
            return await fake(request)
        gets.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(SourceError):
        await make_adapter(site).search("books")
    assert len(gets) <= 6
    assert all(url.startswith(BASE) and ":444" not in url for url in gets)


@pytest.mark.parametrize(
    "target",
    [
        "https://annas-archive.gl:444/next",
        "https://sub.annas-archive.gl/next",
        "http://annas-archive.gl/next",
    ],
)
async def test_anonymous_inflight_redirect_cannot_pick_up_new_member_cookie(
    make_adapter, target
):
    requests = []
    adapter = None

    async def site(request):
        requests.append((str(request.url), request.headers.get("cookie", "")))
        if request.url.path == "/search":
            # A concurrent sign-in completes while this anonymous GET is in flight.
            adapter._client.cookies.set(
                "aa_account_id2", COOKIE, domain=".annas-archive.gl", path="/"
            )
            adapter._member_state = "active"
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text=HTML)

    adapter = make_adapter(site)
    with pytest.raises(SourceError, match="Page redirect refused"):
        await adapter._fetch(adapter._client, BASE + "/search")
    assert requests == [(BASE + "/search", "")]


async def test_noncredential_cookies_are_not_echo_secrets(make_adapter):
    async def site(request):
        if request.method == "POST":
            return httpx.Response(
                302,
                headers=[
                    ("location", "/"),
                    ("set-cookie", f"aa_account_id2={COOKIE}; Path=/"),
                    ("set-cookie", "lang=en; Path=/"),
                    ("set-cookie", "aa_logged_in=1; Path=/"),
                ],
            )
        return httpx.Response(200, text='<a href="/md5/abc">en volume 1</a>')

    assert (await make_adapter(site).search("books"))[0].title == "en volume 1"


async def test_plain_http_never_posts_key(make_adapter):
    fake = FakeSite()
    adapter = make_adapter(fake, base="http://annas-archive.gl")
    await adapter.search("books")
    assert fake.posts == 0
    assert fake.foreign == 0
    assert adapter.member_status()["member_session"] == "none"


async def test_untrusted_signin_guard_is_observed(make_adapter):
    fake = FakeSite()
    adapter = make_adapter(fake, base="https://other.invalid")
    await adapter._signin(adapter._client)
    assert fake.foreign == 0  # Counts a leaked POST, unlike fake.posts.
    assert adapter.member_status()["member_session"] == "none"


async def test_foreign_redirect_with_valid_cookie_is_refused(make_adapter):
    class ForeignCookie(FakeSite):
        async def __call__(self, request):
            if request.method == "POST":
                self.posts += 1
                return httpx.Response(
                    307,
                    headers={
                        "location": "https://other.invalid/account/",
                        "set-cookie": f"aa_account_id2={COOKIE}; Path=/",
                    },
                )
            return await super().__call__(request)

    fake = ForeignCookie()
    adapter = make_adapter(fake)
    await adapter.search("books")
    assert fake.foreign == 0
    assert not any(fake.cookies)
    assert adapter.member_status()["member_session"] != "active"


async def test_transport_failure_is_unknown_not_rejected(make_adapter):
    fake = FakeSite(login="error")
    adapter = make_adapter(fake)
    await adapter.search("books")
    assert fake.posts == 1
    assert adapter.member_status()["member_session"] == "none"
    await adapter.search("books")
    assert fake.posts == 1
