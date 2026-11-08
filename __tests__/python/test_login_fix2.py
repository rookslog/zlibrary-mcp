"""Generation-loss recovery and truthful account-failure diagnostics."""

import json

import httpx
import pytest
from __tests__.fixtures.saved_renewal_transport import renewal_failure
from lib import login_cli, login_store as store
from lib.auth_diagnostics import failure_diagnostics
from zlibrary import eapi


def save():
    return store.save(
        identity="reader@example.test",
        user_id="42",
        user_key="synthetic-store-key",
        domain="origin.example.test",
    )


@pytest.mark.parametrize("repair_entry", ["store", "cli"])
async def test_empty_existing_counter_requires_explicit_history_repair(
    repair_entry, monkeypatch, capsys
):
    assert save().generation == 1
    assert save().generation == 2
    store.logout()
    lock = store.path().parent / ".zlibrary.lock"
    lock.write_bytes(b"")
    with pytest.raises(store.StoreCounterError, match="logout.*restart"):
        save()
    assert store.load() is None
    prompts = []
    monkeypatch.setattr(
        login_cli.getpass, "getpass", lambda prompt: prompts.append(prompt)
    )
    assert await login_cli.run(["login", "--json"]) == 1
    assert prompts == []
    assert "logout" in json.loads(capsys.readouterr().err)["error"]
    if repair_entry == "store":
        assert store.logout() == {"removed": False, "counter_repaired": True}
    else:
        assert await login_cli.run(["logout", "--json"]) == 0
        assert (
            "restart running servers" in json.loads(capsys.readouterr().out)["message"]
        )
    # Explicit repair cannot recover the lost history; it warns before restarting.
    assert save().generation == 1


async def test_new_saved_key_transport_failure_has_no_old_auth_context():
    error = await renewal_failure()
    assert error.__context__ is None
    diagnostic = failure_diagnostics(error)
    assert diagnostic["failure_kind"] == "transport"
    assert "http_status" not in diagnostic


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("reply", ["redirect", "wrong-password"])
async def test_login_names_redirect_without_retry_advice_and_retains_password_message(
    json_mode, reply, monkeypatch, capsys
):
    for name in ("ZLIBRARY_EAPI_DOMAIN", "ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    hosts = ["one.example.test", "two.example.test", "three.example.test"]
    monkeypatch.setattr(eapi, "DEFAULT_EAPI_DOMAINS", hosts)
    sent = []

    def handle(request):
        assert request.url.host in hosts, (
            "Credential redirect reached an unconfigured host"
        )
        if request.url.path == "/eapi/book/search":
            return httpx.Response(200, json={"success": 1, "books": []})
        assert request.url.path == "/eapi/user/login"
        sent.append(request.url.host)
        if reply == "redirect":
            return httpx.Response(
                307,
                headers={
                    "Location": "https://user:synthetic-password@receiver.example.test/private?key=secret"
                },
            )
        return httpx.Response(401, json={"error": "Please login"})

    original_init = httpx.AsyncClient.__init__

    def offline_client(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_client)
    answers = iter(["reader@example.test", "synthetic-password"])
    monkeypatch.setattr(login_cli.getpass, "getpass", lambda _prompt: next(answers))
    assert await login_cli.run(["login", *(["--json"] if json_mode else [])]) == 1
    output = capsys.readouterr()
    message = json.loads(output.err)["error"] if json_mode else output.err.strip()
    if reply == "redirect":
        assert sent == hosts
        assert message == "Redirect refused (HTTP 307) to host receiver.example.test"
        assert "retry" not in message.lower() and "again" not in message.lower()
    else:
        assert sent == hosts[:1]
        assert message == "Login failed; check setup before trying again."
    assert not output.out
    assert "synthetic-password" not in message and "/private" not in message
    assert store.load() is None
