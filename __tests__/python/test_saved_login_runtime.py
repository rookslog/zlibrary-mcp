"""Saved authentication must send cookies, renew once, and never log in implicitly."""

import httpx
import pytest
import python_bridge
from lib import login_store as store
from zlibrary.eapi import EAPIClient, EAPIAuthenticationError


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ZLIBRARY_EMAIL", "stale-env@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-stale-password")
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "fake.example.test")
    monkeypatch.delenv("ZLIBRARY_PREFER_ENV", raising=False)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    state = {"requests": [], "response": lambda request: {"success": 1, "books": []}}

    def handle(request):
        state["requests"].append((request.url.path, request.headers.get("cookie", "")))
        result = state["response"](request)
        return (
            result
            if isinstance(result, httpx.Response)
            else httpx.Response(200, json=result)
        )

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle), cookies=self._cookies
            )
        return self._client

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    store.save(
        identity="saved@example.test",
        user_id="42",
        user_key="old-key",
        domain="fake.example.test",
    )
    return state


async def test_saved_key_initializes_without_password_or_login(site, monkeypatch):
    monkeypatch.delenv("ZLIBRARY_EMAIL")
    monkeypatch.delenv("ZLIBRARY_PASSWORD")
    client = await python_bridge.initialize_eapi_client()
    try:
        assert await client.search("fake") == {"success": 1, "books": []}
        assert site["requests"] == [
            (
                "/eapi/book/search",
                "siteLanguageV2=en; remix_userid=42; remix_userkey=old-key",
            )
        ]
    finally:
        await client.close()


async def test_refused_key_reloads_new_generation_without_restart(site):
    def respond(request):
        if "old-key" in request.headers["cookie"]:
            store.save(
                identity="saved@example.test",
                user_id="42",
                user_key="new-key",
                domain="fake.example.test",
            )
            return {"success": 0, "error": "Please login"}
        return {"success": 1, "books": [{"id": "123"}]}

    site["response"] = respond
    client = await python_bridge.initialize_eapi_client()
    try:
        assert (await client.search("fake"))["books"] == [{"id": "123"}]
        assert len(site["requests"]) == 2
        assert "new-key" in site["requests"][-1][1]
        assert all(path != "/eapi/user/login" for path, _ in site["requests"])
    finally:
        await client.close()


@pytest.mark.parametrize("new_generation", [False, True])
async def test_rejection_stops_without_password_fallback_or_retry_loop(
    site, new_generation
):
    def respond(request):
        if new_generation:
            store.save(
                identity="saved@example.test",
                user_id="42",
                user_key="still-refused-key",
                domain="fake.example.test",
            )
        return {"success": 0, "error": "Please login"}

    site["response"] = respond
    client = await python_bridge.initialize_eapi_client()
    try:
        with pytest.raises(EAPIAuthenticationError, match="zlibrary login"):
            await client.search("fake")
        assert len(site["requests"]) == (2 if new_generation else 1)
        assert all(path != "/eapi/user/login" for path, _ in site["requests"])
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload", [{"success": 0, "error": "Unknown business error"}, {"success": 0}]
)
async def test_ambiguous_reply_does_not_reload_or_retry(site, payload):
    site["response"] = lambda request: payload
    client = await python_bridge.initialize_eapi_client()
    try:
        assert await client.search("fake") == payload
        assert len(site["requests"]) == 1
        assert store.load().generation == 1
    finally:
        await client.close()


async def test_new_identity_is_not_used_to_retry_old_operation(site):
    def respond(request):
        store.save(
            identity="other@example.test",
            user_id="43",
            user_key="other-key",
            domain="fake.example.test",
        )
        return {"success": 0, "error": "Please login"}

    site["response"] = respond
    client = await python_bridge.initialize_eapi_client()
    try:
        with pytest.raises(EAPIAuthenticationError, match="zlibrary login"):
            await client.search("fake")
        assert len(site["requests"]) == 1
    finally:
        await client.close()


async def test_ambiguous_profile_during_domain_recovery_is_not_rejection(
    site, monkeypatch
):
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")

    async def target(exclude=()):
        return "new.example.test"

    monkeypatch.setattr(python_bridge, "resolve_eapi_domain", target)
    client = await python_bridge.initialize_eapi_client()
    site["response"] = lambda request: {"success": 0, "error": "Try again later"}
    try:
        with pytest.raises(RuntimeError, match="Unexpected EAPI profile"):
            await client.on_domain_failed()
        assert store.load().generation == 1
    finally:
        await client.close()


async def test_saved_identity_is_checked_against_private_session(site, monkeypatch):
    monkeypatch.setattr(
        python_bridge,
        "_session_input",
        {"identity": "stale-env@example.test", "action": "operation"},
    )
    with pytest.raises(ValueError, match="identity"):
        await python_bridge.initialize_eapi_client()
    assert not site["requests"]


async def test_saved_snapshot_cannot_silently_fall_back_after_logout(site, monkeypatch):
    monkeypatch.setenv("ZLIBRARY_LOGIN_EXPECTED_ORIGIN", "saved")
    monkeypatch.setenv("ZLIBRARY_LOGIN_EXPECTED_IDENTITY", "saved@example.test")
    store.logout()
    with pytest.raises(store.StoreError, match="Saved login changed"):
        await python_bridge.initialize_eapi_client()
    assert not site["requests"]
