"""Account actions require writable storage and a single authentication owner."""

import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
import python_bridge
from lib import login_cli as cli, login_store as store
from lib.login_runtime import SavedLoginClient
from zlibrary.eapi import EAPIClient


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "pin.invalid")
    for name in (
        "ZLIBRARY_EMAIL",
        "ZLIBRARY_PASSWORD",
        "ZLIBRARY_PREFER_ENV",
        "ZLIBRARY_LOGIN_EXPECTED_ORIGIN",
        "ZLIBRARY_LOGIN_EXPECTED_IDENTITY",
        "ZLIBRARY_LOGIN_METADATA_REFUSED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    monkeypatch.setattr(python_bridge, "_lazy_session", False)


def saved():
    return store.save(
        identity="same@example.test",
        user_id="42",
        user_key="fake-saved-key",
        domain="saved.invalid",
    )


def transport(monkeypatch, reply):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=reply(request))

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle), cookies=self._cookies
            )
        return self._client

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    return requests


async def test_environment_status_does_not_authenticate(monkeypatch, capsys):
    monkeypatch.setenv("ZLIBRARY_EMAIL", "same@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-password")
    requests = transport(
        monkeypatch,
        lambda _: {"success": 1, "user": {"id": "42", "remix_userkey": "fake-key"}},
    )
    assert await cli.run(["status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["origin"] == "environment"
    assert result["works"] == "not_checked"
    assert requests == []


def account_actions(monkeypatch):
    actions = []

    def prompt(_):
        actions.append("prompt")
        return "fake-input"

    async def close():
        pass

    async def authenticate(*_):
        actions.append("login")
        return SimpleNamespace(
            remix_userid="42",
            remix_userkey="fake-key",
            domain="saved.invalid",
            close=close,
        )

    monkeypatch.setattr(cli, "_hidden", prompt)
    monkeypatch.setattr(cli, "authenticate", authenticate)
    return actions


@pytest.mark.parametrize(
    "problem",
    ["mode", "symlink", "hardlink", "partial", "garbage", "unwritable", "reservation"],
)
async def test_unusable_writer_refuses_before_prompts_or_authentication(
    problem, tmp_path, monkeypatch, capsys
):
    saved()
    lock = store.path().parent / ".zlibrary.lock"
    if problem == "mode":
        lock.chmod(0o644)
    elif problem == "symlink":
        moved = tmp_path / "moved-lock"
        lock.rename(moved)
        lock.symlink_to(moved)
    elif problem == "hardlink":
        os.link(lock, tmp_path / "extra-lock")
    elif problem in {"partial", "garbage"}:
        lock.write_bytes(b"1\n2" if problem == "partial" else b"garbage\n")
    elif problem == "reservation":

        def no_space(*_):
            raise OSError("fake full filesystem")

        monkeypatch.setattr(store.os, "write", no_space)
    else:
        original = os.open

        def read_only(path, flags, *args, **kwargs):
            if str(path).startswith(".login-") and flags & os.O_CREAT:
                raise PermissionError("fake read-only directory")
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(store.os, "open", read_only)
    actions = account_actions(monkeypatch)
    assert await cli.run(["login", "--json"]) == 1
    assert actions == []
    assert "run zlibrary login" not in capsys.readouterr().err.lower()


async def test_non_posix_refuses_before_prompts_or_authentication(monkeypatch, capsys):
    actions = account_actions(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(store.os, "name", "nt")
        result = await cli.run(["login", "--json"])
    assert result == 1
    assert actions == []
    assert "POSIX" in capsys.readouterr().err


@pytest.mark.parametrize("counter", [b"1\n2", b"garbage\n", b"1\n8\nbroken\n"])
async def test_logout_repairs_counter_before_another_login(
    counter, monkeypatch, capsys
):
    saved()
    lock = store.path().parent / ".zlibrary.lock"
    lock.write_bytes(counter)
    assert await cli.run(["logout", "--json"]) == 0
    message = json.loads(capsys.readouterr().out)["message"]
    assert "restart" in message.lower()
    account_actions(monkeypatch)
    assert await cli.run(["login", "--json"]) == 0
    assert store.load().generation > (
        8 if b"8\n" in counter else 2 if counter.endswith(b"2") else 1
    )


def test_busy_writer_refuses_with_bounded_wait_before_input(tmp_path):
    import fcntl
    import subprocess

    saved()
    lock = store.path().parent / ".zlibrary.lock"
    with lock.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        script = """import asyncio
from types import SimpleNamespace
from lib import login_cli as cli, login_store as store
store.LOCK_TIMEOUT_SECONDS = 0.05
def prompt(_):
    print('PROMPT', flush=True)
    return 'fake-input'
async def close(): pass
async def authenticate(*_):
    print('AUTH', flush=True)
    return SimpleNamespace(remix_userid='42', remix_userkey='fake-key', domain='saved.invalid', close=close)
cli._hidden = prompt
cli.authenticate = authenticate
raise SystemExit(asyncio.run(cli.run(['login', '--json'])))
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("Busy login storage did not refuse within the bounded wait")
    assert result.returncode == 1
    assert "PROMPT" not in result.stdout and "AUTH" not in result.stdout
    assert "busy" in result.stderr.lower()


async def test_logout_removes_private_orphans_and_distinguishes_absence(capsys):
    saved()
    orphan = store.path().parent / ".login-crashed"
    orphan.write_text("fake-orphan-key")
    orphan.chmod(0o600)
    assert await cli.run(["logout", "--json"]) == 0
    assert not orphan.exists()
    assert not store.path().exists()
    capsys.readouterr()
    assert await cli.run(["logout", "--json"]) == 0
    assert "No saved login" in json.loads(capsys.readouterr().out)["message"]


async def test_empty_logout_does_not_create_store(capsys):
    root = store.path().parents[1]
    assert await cli.run(["logout", "--json"]) == 0
    assert "No saved login" in json.loads(capsys.readouterr().out)["message"]
    assert not root.exists()


@pytest.mark.parametrize("action", ["login", "validate", "operation"])
async def test_same_identity_private_session_cannot_adopt_saved_key(
    action, monkeypatch
):
    saved()
    requests = transport(monkeypatch, lambda _: {"success": 1, "books": []})
    monkeypatch.setattr(
        python_bridge,
        "_session_input",
        {
            "identity": "same@example.test",
            "action": action,
            "generation": 1,
            "domain": "pin.invalid",
            "remix_userid": "42",
            "remix_userkey": "fake-env-key",
        },
    )
    with pytest.raises((ValueError, store.StoreError), match="[Pp]rivate session"):
        await python_bridge.initialize_eapi_client()
    assert requests == []


def test_saved_client_never_publishes_to_private_channel(monkeypatch):
    client = SavedLoginClient(saved())
    monkeypatch.setattr(python_bridge, "_eapi_client", client)
    monkeypatch.setattr(
        python_bridge,
        "_session_input",
        {"identity": "same@example.test", "generation": 1, "action": "operation"},
    )
    monkeypatch.setattr(python_bridge, "_session_published", None)
    writes = []
    monkeypatch.setattr(
        python_bridge.os, "write", lambda fd, data: writes.append((fd, data))
    )
    python_bridge._publish_eapi_session()
    assert writes == []


async def test_failed_metadata_refuses_direct_password_login(monkeypatch, capsys):
    monkeypatch.setenv("ZLIBRARY_EMAIL", "same@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-password")
    monkeypatch.setenv("ZLIBRARY_LOGIN_METADATA_REFUSED", "1")
    requests = transport(
        monkeypatch,
        lambda _: {"success": 1, "user": {"id": "42", "remix_userkey": "fake-key"}},
    )
    with pytest.raises(store.StoreError, match="metadata") as error:
        await python_bridge.initialize_eapi_client()
    assert error.value._eapi_local_refusal is True
    assert requests == []


async def test_changed_saved_snapshot_reports_local_advice_without_attempt(
    monkeypatch, capsys
):
    monkeypatch.setenv("ZLIBRARY_LOGIN_EXPECTED_ORIGIN", "saved")
    monkeypatch.setenv("ZLIBRARY_LOGIN_EXPECTED_IDENTITY", "same@example.test")
    monkeypatch.setattr(sys, "argv", ["python_bridge.py", "search", '{"query":"fake"}'])
    with pytest.raises(SystemExit) as exit_info:
        await python_bridge.main()
    assert exit_info.value.code == 1
    envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "Saved login changed" in envelope["error"]
    assert "diagnostics" not in envelope
    assert envelope["details"]["request_issued"] is False


def test_saved_identity_is_scrubbed_from_diagnostic_values(monkeypatch):
    monkeypatch.setattr(python_bridge, "_eapi_client", SavedLoginClient(saved()))
    result = python_bridge._safe_diagnostic(
        {"error": "account same@example.test failed"}
    )
    assert result == {"error": "account <redacted> failed"}


@pytest.mark.parametrize(
    "domain",
    [
        "https://other.invalid",
        "name@other.invalid",
        "host.invalid/path",
        "host\ninvalid",
    ],
)
def test_saved_domain_is_validated_when_read(domain):
    saved()
    data = json.loads(store.path().read_text())
    data["domain"] = domain
    store.path().write_text(json.dumps(data))
    with pytest.raises(store.StoreContentError):
        store.load()


def test_hidden_input_refuses_getpass_echo_fallback(tmp_path):
    import subprocess
    from pathlib import Path

    runner = tmp_path / "without_terminal.py"
    runner.write_text("""import asyncio, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from lib import login_cli as cli
async def close(): pass
async def authenticate(*_):
    print('AUTH-WAS-CALLED')
    return SimpleNamespace(remix_userid='42', remix_userkey='fake-key', domain='saved.invalid', close=close)
cli.authenticate = authenticate
raise SystemExit(asyncio.run(cli.run(['login', '--json'])))
""")
    result = subprocess.run(
        [sys.executable, str(runner), str(Path(__file__).resolve().parents[2])],
        input="fake-reader@example.test\nfake-hidden-input\n",
        text=True,
        capture_output=True,
        start_new_session=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "AUTH-WAS-CALLED" not in result.stdout + result.stderr
    assert "fake-hidden-input" not in result.stdout + result.stderr
    assert not store.path().exists()


async def test_saved_renewal_keeps_domain_override(monkeypatch):
    first = saved()

    def reply(request):
        if "fake-saved-key" in request.headers.get("cookie", ""):
            store.save(
                identity=first.identity,
                user_id="42",
                user_key="fake-new-key",
                domain="new.invalid",
            )
            return {"success": 0, "error": "Please login"}
        return {"success": 1, "books": []}

    requests = transport(monkeypatch, reply)
    client = await python_bridge.initialize_eapi_client()
    try:
        await client.search("fake")
        assert [request.url.host for request in requests] == [
            "pin.invalid",
            "pin.invalid",
        ]
        assert "fake-new-key" in requests[-1].headers["cookie"]
    finally:
        await client.close()


async def test_saved_client_renews_only_once_across_later_operations(monkeypatch):
    from zlibrary.eapi import EAPIAuthenticationError

    first = saved()
    state = {"rejections": 0}

    def reply(request):
        if len(requests) in (1, 3):
            state["rejections"] += 1
            store.save(
                identity=first.identity,
                user_id="42",
                user_key=f"fake-new-{state['rejections']}",
                domain="saved.invalid",
            )
            return {"success": 0, "error": "Please login"}
        return {"success": 1, "books": []}

    requests = transport(monkeypatch, reply)
    client = await python_bridge.initialize_eapi_client()
    try:
        await client.search("first")
        with pytest.raises(EAPIAuthenticationError):
            await client.search("second")
        assert len(requests) == 3
    finally:
        await client.close()
