"""Configured-domain fallback over real loopback HTTP, with no Location replay."""

import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
import python_bridge as bridge
from lib.auth_diagnostics import failure_diagnostics, failure_type
from zlibrary import eapi
from test_credential_redirects import KEY, PASSWORD, loopback


DOMAINS = ["z-library.sk", "1lib.sk", "z-library.ec"]


@pytest.fixture
def environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ZLIBRARY_EAPI_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.invalid")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", PASSWORD)
    monkeypatch.setattr(eapi, "DEFAULT_EAPI_DOMAINS", DOMAINS)


@asynccontextmanager
async def service(monkeypatch, **scenario):
    requests = []

    def sink(_head, _body):
        return 200, {}, b'{"success":0,"error":"Authentication required"}'

    async with loopback("127.0.0.2", sink) as (receiver, leaked):
        location = (
            receiver.replace("http://", f"http://user:{PASSWORD}@")
            + "/private?key="
            + KEY
        )

        def handle(head, body):
            lines = head.decode().split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            host, path = headers["Host"], urlsplit(target).path
            requests.append((host, method, path, headers.get("Cookie", ""), body))
            probe = path == "/eapi/book/search" and KEY not in headers.get("Cookie", "")
            kind = (
                "probe"
                if probe
                else {
                    "/eapi/user/login": "login",
                    "/eapi/info/domains": "domains",
                    "/eapi/user/profile": "profile",
                }.get(path, "operation")
            )
            if host in scenario.get(kind + "_redirect", ()):
                return (
                    307,
                    {"Location": location},
                    b'DiamWall {"success":0,"error":"Authentication required"}',
                )
            if host in scenario.get(kind + "_wall", ()):
                return 403, {}, b"DiamWall Access Denied"
            if host in scenario.get(kind + "_nonjson", ()):
                return 200, {"Content-Type": "text/html"}, b"<html>Unavailable</html>"
            if kind in ("profile", "login") and host in scenario.get(
                kind + "_rejected", ()
            ):
                return 401, {}, b'{"error":"Authentication required"}'
            payload = (
                {"success": 1, "user": {"id": "123", "remix_userkey": KEY}}
                if kind in ("login", "profile")
                else {"domains": scenario.get("advertised", [])}
                if kind == "domains"
                else {"success": 1, "books": []}
            )
            return 200, {}, json.dumps(payload).encode()

        async with loopback("127.0.0.1", handle) as (origin, _):
            real_client = httpx.AsyncClient

            class LocalTransport(httpx.AsyncBaseTransport):
                def __init__(self):
                    self.transport = httpx.AsyncHTTPTransport(trust_env=False)

                async def handle_async_request(self, request):
                    if request.url.host in DOMAINS:
                        base = httpx.URL(origin)
                        url = request.url.copy_with(
                            scheme="http", host=base.host, port=base.port
                        )
                    elif request.url.host == "127.0.0.2":
                        url = request.url
                    else:
                        raise AssertionError("Non-loopback network forbidden")
                    local = httpx.Request(
                        request.method,
                        url,
                        headers=request.headers,
                        content=await request.aread(),
                    )
                    return await self.transport.handle_async_request(local)

                async def aclose(self):
                    await self.transport.aclose()

            def client(*args, **kwargs):
                kwargs["transport"] = LocalTransport()
                kwargs["trust_env"] = False
                return real_client(*args, **kwargs)

            monkeypatch.setattr(httpx, "AsyncClient", client)
            yield requests, leaked


pytestmark = pytest.mark.usefixtures("environment")


def logins(requests):
    return [row[0] for row in requests if row[2] == "/eapi/user/login"]


async def test_probe_307_skips_two_candidates_before_login(monkeypatch):
    async with service(monkeypatch, probe_redirect=DOMAINS[:2]) as (requests, leaked):
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[2]
            assert logins(requests) == [DOMAINS[2]]
            assert leaked == []
        finally:
            await client.close()


async def test_actual_login_307_tries_next_configured_domain(monkeypatch):
    async with service(monkeypatch, login_redirect=DOMAINS[:2]) as (requests, leaked):
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[2]
            assert logins(requests) == DOMAINS
            assert all(
                PASSWORD.encode() in row[4]
                for row in requests
                if row[2] == "/eapi/user/login"
            )
            assert leaked == []
        finally:
            await client.close()


async def test_domains_307_moves_to_validated_next_domain(monkeypatch):
    async with service(monkeypatch, domains_redirect=DOMAINS[:2]) as (requests, leaked):
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[2]
            assert logins(requests) == DOMAINS[:1]
            assert [r[0] for r in requests if r[2] == "/eapi/user/profile"] == DOMAINS[
                1:
            ]
            assert [r[0] for r in requests if r[2] == "/eapi/info/domains"] == DOMAINS
            assert leaked == []
        finally:
            await client.close()


async def test_advertised_domain_probe_307_skips_to_next(monkeypatch):
    async with service(monkeypatch, probe_redirect=DOMAINS[:2], advertised=DOMAINS) as (
        requests,
        leaked,
    ):
        # Initial configured login is deliberately pinned by the resolver only;
        # advertised selection still uses the real candidate probes.
        async def resolve(*, exclude=()):
            return None if DOMAINS[2] in exclude else DOMAINS[2]

        monkeypatch.setattr(bridge, "resolve_eapi_domain", resolve)
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[2]
            assert logins(requests) == [DOMAINS[2]]
            assert leaked == []
        finally:
            await client.close()


@pytest.mark.parametrize("profile_redirect", [[], [DOMAINS[1]]])
async def test_session_recovery_307_uses_same_key_without_login(
    monkeypatch, profile_redirect
):
    async with service(
        monkeypatch, operation_redirect=[DOMAINS[0]], profile_redirect=profile_redirect
    ) as (requests, leaked):
        client = eapi.EAPIClient(DOMAINS[0], "123", KEY)
        bridge._attach_eapi_session(client)
        try:
            assert (await client.search("fixture"))["books"] == []
            assert (
                client.domain == DOMAINS[2]
                if profile_redirect
                else client.domain == DOMAINS[1]
            )
            assert client.remix_userkey == KEY
            assert logins(requests) == []
            assert leaked == []
        finally:
            await client.close()


async def test_recovery_profile_auth_rejection_does_not_try_another_domain(monkeypatch):
    async with service(
        monkeypatch, operation_redirect=[DOMAINS[0]], profile_rejected=[DOMAINS[1]]
    ) as (requests, leaked):
        client = eapi.EAPIClient(DOMAINS[0], "123", KEY)
        bridge._attach_eapi_session(client)
        try:
            with pytest.raises(eapi.EAPIAuthenticationError) as rejected:
                await client.search("fixture")
            assert failure_diagnostics(rejected.value)["failure_kind"] == "auth_rejected"
            assert all(r[0] != DOMAINS[2] for r in requests)
            assert leaked == []
        finally:
            await client.close()


@pytest.mark.parametrize("path", ["login", "domains", "operation"])
async def test_exhausted_or_pinned_refusal_is_named_sanitized_and_not_auth(
    monkeypatch, path
):
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", DOMAINS[0])
    async with service(monkeypatch, **{path + "_redirect": DOMAINS}) as (
        requests,
        leaked,
    ):
        client = eapi.EAPIClient(DOMAINS[0], "123", KEY)
        bridge._attach_eapi_session(client)
        try:
            with pytest.raises(Exception) as caught:
                if path == "login":
                    await client.login("reader@example.invalid", PASSWORD)
                elif path == "domains":
                    await client.get_domains()
                else:
                    await client.search("fixture")
            error = caught.value
            assert type(error).__name__ == "RedirectRefusedError"
            assert error.target_host == "127.0.0.2"
            assert error.status_code == 307
            assert "Redirect refused" in str(error) and "127.0.0.2" in str(error)
            for secret in (
                KEY,
                PASSWORD,
                "user:",
                "/private",
                "?key=",
                "Authentication required",
            ):
                assert secret not in str(error)
                assert secret not in repr(vars(error))
            assert failure_type(error) == "RedirectRefusedError"
            assert failure_diagnostics(error)["failure_kind"] == "transport"
            assert not isinstance(error, eapi.EAPIAuthenticationError)
            assert {r[0] for r in requests} == {DOMAINS[0]}
            assert leaked == []
        finally:
            await client.close()


async def test_login_refusal_exhausts_each_configured_domain_once(monkeypatch):
    async with service(monkeypatch, login_redirect=DOMAINS) as (requests, leaked):
        with pytest.raises(Exception) as caught:
            await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        assert type(caught.value).__name__ == "RedirectRefusedError"
        assert logins(requests) == DOMAINS
        assert leaked == []


async def test_real_login_auth_rejection_never_tries_another_domain(monkeypatch):
    async with service(monkeypatch, login_rejected=[DOMAINS[0]]) as (requests, leaked):
        with pytest.raises(eapi.EAPIAuthenticationError):
            await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        assert logins(requests) == DOMAINS[:1]
        assert all(row[0] != DOMAINS[1] for row in requests)
        assert leaked == []


async def test_refusal_diagnostics_do_not_inherit_an_active_auth_error():
    try:
        try:
            raise eapi.EAPIAuthenticationError("Authentication required")
        except eapi.EAPIAuthenticationError:
            raise eapi.RedirectRefusedError(
                "receiver.invalid", status_code=307
            ) from None
    except eapi.RedirectRefusedError as error:
        assert failure_diagnostics(error)["failure_kind"] == "transport"


def test_health_names_refusal_before_wall_words_in_the_hostname():
    error = eapi.RedirectRefusedError("diamwall.receiver.invalid", status_code=307)
    assert bridge._classify_health_error(error) == "redirect_refused"


async def test_pinned_discovery_does_not_move_domains(monkeypatch):
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", DOMAINS[0])
    async with service(monkeypatch, domains_redirect=DOMAINS) as (requests, leaked):
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[0]
            assert {row[0] for row in requests} == {DOMAINS[0]}
            assert leaked == []
        finally:
            await client.close()
