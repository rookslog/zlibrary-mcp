"""Credential destination, cookie acceptance and refusal text contracts."""

from contextlib import asynccontextmanager

import httpx
import pytest
import python_bridge as bridge

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError
from test_annas_redirect_merge import BASE, COOKIE, KEY
from test_credential_redirects import PASSWORD
from test_redirect_recovery import DOMAINS, environment as environment, logins, service
from zlibrary.eapi import RedirectRefusedError, raise_for_refused_redirect

pytestmark = pytest.mark.usefixtures("environment")


@pytest.mark.parametrize("failure", ["redirect", "wall", "nonjson"])
async def test_login_stops_after_one_post_when_remaining_probes_fail(
    monkeypatch, failure
):
    async with service(
        monkeypatch, probe_redirect=DOMAINS, **{f"login_{failure}": DOMAINS}
    ) as (requests, leaked):
        with pytest.raises(Exception):
            await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        assert logins(requests) == DOMAINS[:1]
        assert leaked == []


async def test_login_skips_failed_probe_and_uses_next_healthy_host(monkeypatch):
    async with service(
        monkeypatch, login_redirect=DOMAINS[:1], probe_redirect=DOMAINS[1:2]
    ) as (requests, leaked):
        client = await bridge._login_eapi_client("reader@example.invalid", PASSWORD)
        try:
            assert client.domain == DOMAINS[2]
            assert logins(requests) == [DOMAINS[0], DOMAINS[2]]
            assert leaked == []
        finally:
            await client.close()


@asynccontextmanager
async def account_reply(location, *, fresh=True, prior=False):
    requests = []

    def site(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/account/"
        headers = {"location": location}
        if fresh:
            headers["set-cookie"] = f"aa_account_id2={COOKIE}; Path=/"
        return httpx.Response(302, headers=headers)

    adapter = AnnasArchiveAdapter(
        SourceConfig(annas_secret_key=KEY, annas_base_url=BASE, preflight_enabled=False)
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(site), follow_redirects=True, trust_env=False
    )
    if prior:
        adapter._client.cookies.set(
            "aa_account_id2", COOKIE, domain="annas-archive.gl", path="/"
        )
    try:
        yield adapter, requests
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "location", ["http://annas-archive.gl/", "https://annas-archive.gl:444/"]
)
async def test_account_cookie_requires_configured_scheme_and_port(location):
    async with account_reply(location) as (adapter, requests):
        await adapter._signin(adapter._client)
        assert adapter.member_status()["member_session"] == "none"
        assert not adapter._has_member_cookie(adapter._client)
        assert len(requests) == 1


async def test_old_jar_cookie_cannot_replace_fresh_signin_cookie():
    async with account_reply("/", fresh=False, prior=True) as (adapter, requests):
        assert adapter._has_member_cookie(adapter._client)
        await adapter._signin(adapter._client)
        assert adapter.member_status()["member_session"] == "none"
        assert not adapter._has_member_cookie(adapter._client)
        assert len(requests) == 1


def test_refused_redirect_rejects_hostname_characters_outside_allowlist():
    response = httpx.Response(
        307,
        headers={"location": "https://bad_host.invalid/private"},
        request=httpx.Request("GET", "https://source.invalid/"),
    )
    with pytest.raises(RedirectRefusedError) as caught:
        raise_for_refused_redirect(response)
    assert caught.value.target_host == "<invalid>"
    assert "bad_host" not in str(caught.value)


async def test_annas_api_refusal_never_echoes_key_in_destination_hostname():
    requests = []

    def site(request):
        requests.append(request)
        return httpx.Response(
            307, headers={"location": f"https://{KEY}.receiver.invalid/private"}
        )

    adapter = AnnasArchiveAdapter(
        SourceConfig(annas_secret_key=KEY, annas_base_url=BASE, preflight_enabled=False)
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(site), follow_redirects=True, trust_env=False
    )
    try:
        with pytest.raises(ProviderResponseError) as caught:
            await adapter.get_download_url("a" * 32)
        assert caught.value.detail == "Redirect refused (HTTP 307)"
        assert KEY not in str(caught.value)
        assert KEY not in repr(caught.value.__dict__)
        assert len(requests) == 1
    finally:
        await adapter.close()
