"""Saved-account commands retain redirect refusals without replaying credentials."""

import json

import httpx
import pytest
import python_bridge
from lib import login_cli
from lib import login_store as store
from zlibrary.eapi import EAPIAuthenticationError, RedirectRefusedError


@pytest.mark.parametrize("entry", ["login", "status", "limits"])
@pytest.mark.parametrize("redirect_path", ["/eapi/user/login", "/eapi/user/profile"])
async def test_saved_account_never_replays_credentials_to_redirect_host(
    entry, redirect_path, tmp_path, monkeypatch, capsys
):
    # Restoring automatic redirects must expose a receiver request and fail.
    key = "synthetic-saved-redirect-key"
    password = "synthetic-saved-redirect-password"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "origin.example.test")
    monkeypatch.delenv("ZLIBRARY_PREFER_ENV", raising=False)
    monkeypatch.delenv("ZLIBRARY_EMAIL", raising=False)
    monkeypatch.delenv("ZLIBRARY_PASSWORD", raising=False)
    monkeypatch.delenv("ZLIBRARY_LOGIN_EXPECTED_ORIGIN", raising=False)
    monkeypatch.delenv("ZLIBRARY_LOGIN_METADATA_REFUSED", raising=False)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    monkeypatch.setattr(python_bridge, "_lazy_session", False)
    store.save(
        identity="reader@example.test",
        user_id="42",
        user_key=key,
        domain="origin.example.test",
    )
    before = store.load().generation
    requests = []

    def handle(request):
        requests.append(request)
        if (
            request.url.host == "origin.example.test"
            and request.url.path == redirect_path
        ):
            return httpx.Response(
                307,
                headers={
                    "Location": f"https://redirected.example.test/target?key={key}"
                },
                json={"success": 0, "error": "Please login"},
            )
        return httpx.Response(
            200,
            json={
                "success": 1,
                "user": {
                    "id": "42",
                    "remix_userkey": "synthetic-new-login-key",
                    "downloads_limit": 10,
                    "downloads_today": 3,
                },
            },
        )

    original_init = httpx.AsyncClient.__init__

    def offline_client(self, *args, **kwargs):
        # Keep production cookie construction and redirect policy intact.
        kwargs["transport"] = httpx.MockTransport(handle)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_client)
    answers = iter(["reader@example.test", password])
    monkeypatch.setattr(login_cli.getpass, "getpass", lambda _prompt: next(answers))
    exit_code = 0
    try:
        if entry == "limits":
            # This is the real bridge operation used by get_limits(mode="live").
            monkeypatch.setattr(
                python_bridge.sys,
                "argv",
                ["python_bridge.py", "get_download_limits", "{}"],
            )
            try:
                await python_bridge.main()
            except SystemExit as error:
                exit_code = error.code
        else:
            exit_code = await login_cli.run([entry, "--json"])
    finally:
        if python_bridge._eapi_client is not None:
            await python_bridge._eapi_client.close()

    output = capsys.readouterr()
    received = [r for r in requests if r.url.host != "origin.example.test"]
    assert not received, "A credential-bearing operation followed the redirect"
    assert all(key not in r.content.decode() for r in requests)
    assert key not in output.out + output.err
    assert password not in output.out + output.err
    assert not issubclass(RedirectRefusedError, EAPIAuthenticationError)
    assert [r.url.path for r in requests] == [
        "/eapi/user/login" if entry == "login" else "/eapi/user/profile"
    ]
    if entry == "login":
        assert password.encode() in requests[0].content
        assert key not in requests[0].headers.get("cookie", "")
        assert exit_code == (1 if redirect_path == "/eapi/user/login" else 0)
    else:
        assert key in requests[0].headers["cookie"]
        assert password.encode() not in requests[0].content
        assert store.load().generation == before
        if entry == "status":
            assert exit_code == 0
            assert json.loads(output.out)["works"] == (
                "unknown" if redirect_path == "/eapi/user/profile" else "working"
            )
        elif redirect_path == "/eapi/user/profile":
            assert exit_code == 1
            refusal = json.loads(output.err)
            assert refusal["type"] == "RedirectRefusedError"
            assert refusal["status_code"] == 307
            assert refusal["redirect_host"] == "redirected.example.test"
        else:
            assert exit_code == 0
            reply = json.loads(json.loads(output.out)["content"][0]["text"])
            assert reply["daily_remaining"] == 7
    if entry != "login" or redirect_path == "/eapi/user/login":
        assert store.load().generation == before
