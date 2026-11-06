"""Real HTTP clients, synthetic credentials and two loopback hosts only.

Re-enabling automatic redirects on any credential-bearing path must let the
receiver observe a sentinel and fail these regressions.
"""

import asyncio
import runpy
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError
from zlibrary.eapi import EAPIClient, RedirectRefusedError
from zlibrary.libasync import AsyncZlib
from zlibrary.util import GET_request, GET_request_cookies, POST_request


PASSWORD = "synthetic-redirect-password"
KEY = "synthetic-redirect-key"
COOKIES = {"remix_userid": "123", "remix_userkey": KEY}
STATUSES = [301, 302, 303, 307, 308]


@asynccontextmanager
async def loopback(host, handler):
    seen = []

    async def serve(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            body = await reader.readexactly(length)
            seen.append(head + body)
            status, headers, payload = handler(head, body)
            writer.write(
                f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n".encode()
                + b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items())
                + b"\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, host, 0)
    try:
        yield f"http://{host}:{server.sockets[0].getsockname()[1]}", seen
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def redirect_pair(status, same_host):
    def success(_head, _body):
        return 200, {}, b'{"success":1,"user":{"id":123,"remix_userkey":"fixture"}}'

    async with loopback("127.0.0.2", success) as (receiver, received):
        target = receiver + "/target?key=" + KEY

        def redirect(head, body):
            if head.split(b" ")[1].startswith(b"/target"):
                received.append(head + body)
                return success(head, body)
            return status, {"Location": target}, b"redirect fixture"

        async with loopback("127.0.0.1", redirect) as (origin, sent):
            if same_host:
                target = origin + "/target?key=" + KEY
            yield origin, sent, received


def eapi_at(monkeypatch, origin, *, authenticated=True):
    # Change only the test endpoint scheme; retain production client creation.
    monkeypatch.setattr(EAPIClient, "base_url", property(lambda self: origin))
    return (
        EAPIClient("fixture.invalid", "123", KEY)
        if authenticated
        else EAPIClient("fixture.invalid")
    )


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # Ambient proxies must never see even the synthetic credentials.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
async def test_login_password_is_not_redirected(monkeypatch, status, same_host):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        client = eapi_at(monkeypatch, origin, authenticated=False)
        try:
            with pytest.raises(RedirectRefusedError):
                await client.login("reader@example.invalid", PASSWORD)
        finally:
            await client.close()
        assert PASSWORD.encode() in sent[0]
        assert received == []


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_eapi_cookie_is_not_redirected(monkeypatch, status, same_host, method):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        client = eapi_at(monkeypatch, origin)
        try:
            with pytest.raises(RedirectRefusedError):
                if method == "GET":
                    await client.get_profile()
                else:
                    await client.search("fixture")
        finally:
            await client.close()
        assert KEY.encode() in sent[0]
        assert received == []


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
@pytest.mark.parametrize("route", ["eapi", "legacy"])
async def test_file_host_cookie_is_not_redirected(tmp_path, status, same_host, route):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        client = EAPIClient("api.example.invalid", "123", KEY)
        client.get_download_link = AsyncMock(
            return_value={"downloadLink": origin + "/file"}
        )
        if route == "eapi":
            operation = client.download_file(123, "fixture", str(tmp_path))
        else:
            legacy = AsyncZlib()
            legacy.profile = object()
            legacy._eapi = client
            operation = legacy.download_book(
                {"id": "123", "hash": "fixture"}, str(tmp_path)
            )
        with pytest.raises(RedirectRefusedError):
            await operation
        assert KEY.encode() in sent[0]  # Existing initial file-host disclosure.
        assert received == []
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
@pytest.mark.parametrize("route", ["get", "cookie-get", "post"])
async def test_legacy_credentials_are_not_redirected(status, same_host, route):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        with pytest.raises(RedirectRefusedError):
            if route == "get":
                await GET_request(origin + "/file", cookies=COOKIES)
            elif route == "cookie-get":
                await GET_request_cookies(
                    origin + "/?remix_userkey=" + KEY, cookies=COOKIES
                )
            else:
                await POST_request(origin + "/login", {"password": PASSWORD})
        assert received == []
        assert (PASSWORD if route == "post" else KEY).encode() in sent[0]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
async def test_annas_api_key_is_not_redirected(monkeypatch, status, same_host):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        # Authorize only the fake initial host, without touching production policy.
        monkeypatch.setattr("lib.sources.annas.ANNAS_TRUSTED_HOSTS", {"127.0.0.1"})
        adapter = AnnasArchiveAdapter(
            SourceConfig(
                annas_base_url=origin, annas_secret_key=KEY, preflight_enabled=False
            )
        )
        try:
            with pytest.raises(ProviderResponseError):
                await adapter.get_download_url("a" * 32)
        finally:
            await adapter.close()
        assert KEY.encode() in sent[0]
        assert received == []


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("same_host", [False, True], ids=["cross-host", "same-host"])
async def test_archived_keyed_experiment_does_not_redirect(
    monkeypatch, status, same_host
):
    async with redirect_pair(status, same_host) as (origin, sent, received):
        # The archived script imports dotenv at module scope. Stub it before
        # loading any code so tests never open a credential file.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setenv("ANNAS_SECRET_KEY", KEY)
        monkeypatch.setenv("ANNAS_BASE_URL", origin)
        script = runpy.run_path(
            str(
                Path(__file__).resolve().parents[2]
                / "scripts/archive/experiments/test_annas_fast_download.py"
            )
        )
        result = await asyncio.to_thread(script["test_fast_download"], "a" * 32)
        assert KEY.encode() in sent[0]
        assert received == []
        assert result == {
            "status": "redirect_refused",
            "code": status,
            "host": "127.0.0.1" if same_host else "127.0.0.2",
        }


async def test_direct_login_and_file_download_still_work(tmp_path, monkeypatch):
    def handler(head, body):
        if b"/eapi/user/login" in head:
            assert PASSWORD.encode() in body
            return (
                200,
                {},
                b'{"success":1,"user":{"id":123,"remix_userkey":"synthetic-redirect-key"}}',
            )
        assert KEY.encode() in head
        return 200, {}, b"synthetic book bytes"

    async with loopback("127.0.0.1", handler) as (origin, seen):
        client = eapi_at(monkeypatch, origin, authenticated=False)
        try:
            assert (await client.login("reader@example.invalid", PASSWORD))[
                "success"
            ] == 1
            client.get_download_link = AsyncMock(
                return_value={"downloadLink": origin + "/file"}
            )
            result = await client.download_file(123, "fixture", str(tmp_path))
            assert Path(result).read_bytes() == b"synthetic book bytes"
            assert len(seen) == 2
        finally:
            await client.close()
