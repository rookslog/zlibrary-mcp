"""CLI contract: closed diagnostics, hidden prompts, and no bearer output."""

import base64
import html
import importlib
import io
import json
from urllib.parse import quote, quote_plus

import pytest
from lib import login_store as store


def cli_module():
    try:
        return importlib.import_module("lib.login_cli")
    except ModuleNotFoundError:
        pytest.fail("Login command handling is not implemented")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("ZLIBRARY_EMAIL", raising=False)
    monkeypatch.delenv("ZLIBRARY_PASSWORD", raising=False)
    return cli_module()


class Client:
    domain = "fake.example.test"
    remix_userid = "42"
    remix_userkey = 'fake+key/<&"é'
    closed = False

    async def close(self):
        self.closed = True

    async def get_profile(self):
        return {"success": 1, "user": {"id": "42"}}


async def test_login_hidden_input_saves_key_and_never_password(
    cli, monkeypatch, capsys
):
    client = Client()
    answers = iter(["reader@example.test", "fake-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))

    async def login(identity, password):
        assert (identity, password) == ("reader@example.test", "fake-password")
        return client

    monkeypatch.setattr(cli, "authenticate", login)
    assert await cli.run(["login", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["origin"] == "saved"
    assert data["identity"] == "reader@example.test"
    assert data["expiry"] == "unknown"
    assert "fake-password" not in store.path().read_text()
    assert client.closed


async def test_password_stdin_is_not_a_prompt_or_argument(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "reader@example.test")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("fake-stdin-password\n"))

    async def login(identity, password):
        assert password == "fake-stdin-password"
        return Client()

    monkeypatch.setattr(cli, "authenticate", login)
    assert await cli.run(["login", "--password-stdin"]) == 0
    assert "fake-stdin-password" not in capsys.readouterr().out
    assert "fake-stdin-password" not in store.path().read_text()


async def test_status_and_logout_report_provenance_without_secret(
    cli, monkeypatch, capsys
):
    store.save(
        identity="reader@example.test",
        user_id="42",
        user_key=Client.remix_userkey,
        domain=Client.domain,
    )

    async def validate(login):
        return "working"

    monkeypatch.setattr(cli, "validate", validate)
    assert await cli.run(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert {
        k: status[k] for k in ("identity", "origin", "backend", "works", "expiry")
    } == {
        "identity": "reader@example.test",
        "origin": "saved",
        "backend": "file",
        "works": "working",
        "expiry": "unknown",
    }
    assert await cli.run(["logout"]) == 0
    assert "not cancelled" in capsys.readouterr().out
    assert not store.path().exists()


@pytest.mark.parametrize(
    "encoder",
    [
        str,
        lambda x: json.dumps(x)[1:-1],
        html.escape,
        quote,
        quote_plus,
        lambda x: base64.b64encode(x.encode()).decode(),
    ],
)
async def test_encoded_provider_secrets_absent_from_output_logs_and_errors(
    cli, monkeypatch, capsys, caplog, encoder
):
    secret = 'fake+password/<&"é'
    key = Client.remix_userkey
    answers = iter(["reader@example.test", secret])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))

    async def bad_login(identity, password):
        import logging

        logging.getLogger("fake-provider").warning(encoder(secret) + encoder(key))
        raise RuntimeError(encoder(secret) + encoder(key))

    monkeypatch.setattr(cli, "authenticate", bad_login)
    assert await cli.run(["login", "--json"]) == 1
    streams = capsys.readouterr()
    combined = streams.out + streams.err + caplog.text
    assert secret not in combined and key not in combined
    assert encoder(secret) not in combined and encoder(key) not in combined
    decoded_error = json.loads(streams.err.strip().splitlines()[-1])["error"]
    assert secret not in decoded_error and key not in decoded_error
    assert encoder(secret) not in decoded_error and encoder(key) not in decoded_error
    assert "Login failed" in combined
    assert not store.path().exists()


@pytest.mark.parametrize(
    "args",
    [
        ["login", "--password", "do-not-echo"],
        ["login", "do-not-echo"],
        ["status", "--password-stdin"],
    ],
)
async def test_unsupported_arguments_are_refused_without_echo(cli, args, capsys):
    assert await cli.run(args) == 2
    streams = capsys.readouterr()
    assert "do-not-echo" not in streams.out + streams.err
    assert not store.path().exists()


async def test_status_validation_distinguishes_refused_from_ambiguous(cli, monkeypatch):
    from zlibrary.eapi import EAPIClient
    import httpx

    saved = store.save(
        identity="reader@example.test",
        user_id="42",
        user_key="fake-status-key",
        domain="fake.example.test",
    )
    state = {"payload": {"success": 0, "error": "Try again later"}}

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=state["payload"])
                )
            )
        return self._client

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    assert await cli.validate(saved) == "unknown"
    state["payload"] = {"success": 0, "error": "Please login"}
    assert await cli.validate(saved) == "refused"
    state["payload"] = {"success": 1, "user": {"id": "42"}}
    assert await cli.validate(saved) == "working"


@pytest.mark.parametrize("password_stdin", [False, True])
def test_real_terminal_never_echoes_password(cli, tmp_path, password_stdin):
    import os
    import pty
    import select
    import signal
    import sys
    import time
    from pathlib import Path

    runner = tmp_path / "fake_prompt.py"
    runner.write_text("""import asyncio, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import login_cli
async def close(): pass
async def authenticate(identity, password):
    assert identity == 'fake-reader@example.test'
    assert password == 'fake-hidden-password'
    return SimpleNamespace(domain='fake.example.test', remix_userid='42', remix_userkey='fake-hidden-key', close=close)
login_cli.authenticate = authenticate
raise SystemExit(asyncio.run(login_cli.run(sys.argv[2:])))
""")
    read_pipe, write_pipe = os.pipe()
    pid, terminal = pty.fork()
    if pid == 0:
        if password_stdin:
            os.dup2(read_pipe, 0)
        os.close(read_pipe)
        os.close(write_pipe)
        os.execv(
            sys.executable,
            [
                sys.executable,
                str(runner),
                str(Path(__file__).resolve().parents[2] / "lib"),
                "login",
                *(["--password-stdin"] if password_stdin else []),
            ],
        )
    os.close(read_pipe)
    captured = bytearray()

    def until(marker):
        deadline = time.monotonic() + 10
        while marker not in captured:
            assert time.monotonic() < deadline, "Prompt/output was not received"
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if ready:
                try:
                    captured.extend(os.read(terminal, 4096))
                except OSError:
                    raise AssertionError(
                        "CLI exited before the expected prompt or success marker"
                    ) from None
                assert b"fake-hidden-password" not in captured, (
                    "Terminal echoed the password"
                )
                assert b"fake-hidden-key" not in captured, "Terminal printed the key"

    try:
        until(b"Email: ")
        os.write(terminal, b"fake-reader@example.test\n")
        if password_stdin:
            os.write(write_pipe, b"fake-hidden-password\n")
            os.close(write_pipe)
            write_pipe = None
        else:
            until(b"Password: ")
            os.write(terminal, b"fake-hidden-password\n")
        until(b"Login saved.")
        assert b"fake-hidden-password" not in captured
        assert b"fake-hidden-key" not in captured
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        pid = None
    finally:
        if pid:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
        if write_pipe is not None:
            os.close(write_pipe)
