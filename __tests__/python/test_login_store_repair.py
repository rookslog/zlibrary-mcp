"""Corrupt content recovery, ancestor links, and resolver isolation."""

import json
import os
from pathlib import Path

import httpx
import pytest
from lib import login_cli, login_store as store
from lib.login_runtime import SavedLoginClient
from zlibrary.eapi import EAPIAuthenticationError, EAPIClient


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD", "ZLIBRARY_PREFER_ENV"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def save():
    return store.save(
        identity="fake@example.test",
        user_id="42",
        user_key="fake-store-key",
        domain="fake.example.test",
    )


def corrupt(content="{invalid json"):
    save()
    store.path().write_bytes(content.encode() if isinstance(content, str) else content)


@pytest.mark.parametrize(
    "content",
    ["{invalid json", "[]", '{"version":999}', b"\xff", "x" * 65537],
    ids=["json", "array", "schema", "encoding", "oversize"],
)
def test_l1_login_replaces_corrupt_content_and_preserves_generation(state, content):
    corrupt(content)
    repaired = save()
    assert repaired.generation == 2
    assert store.load().user_key.reveal() == "fake-store-key"


def test_l1_logout_removes_corrupt_content_without_losing_generation(state):
    corrupt()
    store.logout()
    assert not store.path().exists()
    assert save().generation == 2


async def test_l1_login_command_repairs_corruption(state, monkeypatch, capsys):
    corrupt()
    answers = iter(["fake@example.test", "fake-password"])
    monkeypatch.setattr(login_cli.getpass, "getpass", lambda prompt: next(answers))

    class Client:
        remix_userid, remix_userkey, domain = (
            "42",
            "fake-command-key",
            "fake.example.test",
        )

        async def close(self):
            pass

    async def authenticate(identity, password):
        assert (identity, password) == ("fake@example.test", "fake-password")
        return Client()

    monkeypatch.setattr(login_cli, "authenticate", authenticate)
    assert await login_cli.run(["login", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["generation"] == 2
    assert store.load().user_key.reveal() == "fake-command-key"


async def test_l1_logout_command_removes_corruption(state, capsys):
    corrupt()
    assert await login_cli.run(["logout", "--json"]) == 0
    assert "not cancelled" in json.loads(capsys.readouterr().out)["message"]
    assert not store.path().exists()


@pytest.mark.parametrize("link_at_root", [False, True])
def test_l2_symlink_above_state_root_is_supported(state, monkeypatch, link_at_root):
    real = state / "real"
    real.mkdir()
    link = state / "ancestor-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(link if link_at_root else link / "state"))
    assert save().generation == 1
    assert store.load().identity == "fake@example.test"
    assert (
        (real if link_at_root else real / "state") / "zlibrary/logins/zlibrary.json"
    ).exists()
    store.logout()
    assert save().generation == 2


@pytest.mark.parametrize("level", ["zlibrary", "logins", "file", "lock"])
def test_owned_symlinks_still_refuse_login_and_logout(state, level):
    save()
    target = {
        "zlibrary": store.path().parents[1],
        "logins": store.path().parent,
        "file": store.path(),
        "lock": store.path().parent / ".zlibrary.lock",
    }[level]
    moved = state / (level + "-moved")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(store.StoreError):
        save()
    with pytest.raises(store.StoreError):
        store.logout()
    assert target.is_symlink()
    if level != "lock":
        with pytest.raises(store.StoreError):
            store.load()


@pytest.mark.parametrize("unsafe", ["mode", "hardlink", "owner"])
def test_l1_recovery_never_ignores_safety_failures(state, monkeypatch, unsafe):
    corrupt()
    if unsafe == "mode":
        store.path().chmod(0o644)
    elif unsafe == "hardlink":
        os.link(store.path(), state / "extra-link")
    else:
        from types import SimpleNamespace
        import stat

        original = store.os.fstat

        def foreign_file(fd):
            info = original(fd)
            if stat.S_ISREG(info.st_mode):
                return SimpleNamespace(
                    st_mode=info.st_mode, st_uid=-1, st_nlink=info.st_nlink
                )
            return info

        monkeypatch.setattr(store.os, "fstat", foreign_file)
    with pytest.raises(store.StoreError):
        save()
    with pytest.raises(store.StoreError):
        store.logout()
    assert store.path().read_bytes() == b"{invalid json"


@pytest.mark.parametrize(
    "problem", ["corrupt", "mode", "relative", "home", "unreadable"]
)
@pytest.mark.parametrize("environment", [False, True])
def test_l3_unusable_store_returns_closed_notice_and_keeps_environment(
    state, monkeypatch, problem, environment
):
    corrupt()
    marker = "fake-secret-path-marker"
    if problem == "mode":
        store.path().chmod(0o644)
    elif problem == "relative":
        monkeypatch.setenv("XDG_STATE_HOME", marker)
    elif problem == "home":
        monkeypatch.delenv("XDG_STATE_HOME")

        def unavailable():
            raise RuntimeError(marker)

        monkeypatch.setattr(Path, "home", unavailable)
    elif problem == "unreadable":

        def refused(*args, **kwargs):
            raise PermissionError(marker)

        monkeypatch.setattr(store.os, "open", refused)
    if environment:
        monkeypatch.setenv("ZLIBRARY_EMAIL", "env@example.test")
        monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-env-password")
    login = store.resolve()
    assert login.origin == ("environment" if environment else "none")
    assert login.public()["configured"] is environment
    assert "store" in login.notice.lower() and login.notice
    assert marker not in login.notice and str(state) not in login.notice
    assert (
        "fake-env-password" not in login.notice and "fake-store-key" not in login.notice
    )
    if environment:
        assert login.password.reveal() == "fake-env-password"


@pytest.mark.parametrize("environment", [False, True])
def test_absent_store_has_no_notice(state, monkeypatch, environment):
    if environment:
        monkeypatch.setenv("ZLIBRARY_EMAIL", "env@example.test")
        monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-env-password")
    assert store.resolve().notice == ""


def test_relative_root_remains_refused_for_strict_operations(state, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    for operation in (store.load, save, store.logout):
        with pytest.raises(store.StoreError):
            operation()


@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
async def test_l3_status_reports_store_problem_without_paths_or_bearer(
    state, monkeypatch, capsys, environment, json_mode
):
    corrupt()
    if environment:
        monkeypatch.setenv("ZLIBRARY_EMAIL", "env@example.test")
        monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-env-password")

    async def validate(login):
        return "working" if login.origin == "environment" else "not_configured"

    monkeypatch.setattr(login_cli, "validate", validate)
    assert await login_cli.run(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["notice"] and "store" in data["notice"].lower()
    assert (
        str(state) not in data["notice"] and "fake-env-password" not in data["notice"]
    )


async def test_renewal_unusable_store_stops_as_auth_refusal_without_environment_fallback(
    state, monkeypatch
):
    record = save()
    calls = []

    def reject(request):
        calls.append(request.url.path)
        store.path().write_text("{invalid")
        return httpx.Response(200, json={"success": 0, "error": "Please login"})

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(reject), cookies=self._cookies
            )
        return self._client

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "env@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-env-password")
    client = SavedLoginClient(record)
    try:
        with pytest.raises(EAPIAuthenticationError, match="zlibrary login"):
            await client.search("fake")
        assert calls == ["/eapi/book/search"]
    finally:
        await client.close()
