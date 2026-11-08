"""Behavior checks: wrong precedence, loose modes and torn/lost updates must fail."""

import importlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor

import pytest


def store_module():
    try:
        return importlib.import_module("lib.login_store")
    except ModuleNotFoundError:
        pytest.fail("Python login store is not implemented")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("ZLIBRARY_EMAIL", raising=False)
    monkeypatch.delenv("ZLIBRARY_PASSWORD", raising=False)
    monkeypatch.delenv("ZLIBRARY_PREFER_ENV", raising=False)
    return store_module()


def save(store, identity="saved@example.test", key="synthetic-key"):
    return store.save(
        identity=identity, user_id="42", user_key=key, domain="fake.example.test"
    )


@pytest.mark.parametrize(
    "saved,environment,prefer,want",
    [
        (False, False, False, "none"),
        (False, True, False, "environment"),
        (True, False, False, "saved"),
        (True, True, False, "saved"),
        (True, True, True, "environment"),
    ],
)
def test_precedence_table(store, monkeypatch, saved, environment, prefer, want):
    if saved:
        save(store)
    if environment:
        monkeypatch.setenv("ZLIBRARY_EMAIL", "env@example.test")
        monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-env-password")
    if prefer:
        monkeypatch.setenv("ZLIBRARY_PREFER_ENV", "1")
    resolved = store.resolve()
    assert resolved.origin == want
    assert resolved.public()["origin"] == want
    assert resolved.public()["expiry"] == "unknown"
    assert resolved.public()["backend"] == "file"
    if saved and environment:
        assert "ZLIBRARY_PASSWORD" in resolved.public()["notice"]
    if want != "none":
        assert resolved.identity == (
            "saved@example.test" if want == "saved" else "env@example.test"
        )


def test_secret_representations_and_password_never_written(store, monkeypatch):
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "not-to-be-written")
    record = save(store)
    assert str(record.user_key) == "***"
    assert repr(record.user_key) == "***"
    assert "synthetic-key" not in repr(record)
    data = json.loads(store.path().read_text())
    assert set(data) == {
        "version",
        "source",
        "identity",
        "user_id",
        "user_key",
        "domain",
        "saved_at",
        "generation",
    }
    assert "not-to-be-written" not in store.path().read_text()
    assert data["user_key"] == "synthetic-key"
    assert store.path().stat().st_mode & 0o777 == 0o600
    assert store.path().parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "mode,directory", [(0o644, False), (0o666, False), (0o755, True)]
)
def test_loose_modes_refused_without_chmod(store, mode, directory):
    save(store)
    target = store.path().parent if directory else store.path()
    target.chmod(mode)
    with pytest.raises(store.StoreError, match="permissions"):
        store.load()
    assert target.stat().st_mode & 0o777 == mode


def test_symlink_and_hardlink_refused(store, tmp_path):
    save(store)
    target = tmp_path / "target"
    store.path().rename(target)
    store.path().symlink_to(target)
    with pytest.raises(store.StoreError):
        store.load()
    store.path().unlink()
    os.link(target, store.path())
    with pytest.raises(store.StoreError):
        store.load()


def _writer(root, key):
    os.environ["XDG_STATE_HOME"] = root
    import asyncio
    import time
    from types import SimpleNamespace
    from lib import login_cli, login_store

    original_read = os.read

    def delayed_read(fd, size):
        result = original_read(fd, size)
        time.sleep(0.05)  # Widen the lost-update window if the writer lock is removed.
        return result

    login_store.os.read = delayed_read
    answers = iter(["saved@example.test", "fake-process-password"])
    login_cli.getpass.getpass = lambda prompt: next(answers)

    async def close():
        pass

    async def authenticate(identity, password):
        return SimpleNamespace(
            remix_userid="42",
            remix_userkey=key,
            domain="fake.example.test",
            close=close,
        )

    login_cli.authenticate = authenticate
    assert asyncio.run(login_cli.run(["login"])) == 0


def test_two_process_logins_increment_and_reader_never_sees_partial_file(
    store, tmp_path
):
    save(store)

    def read_many():
        return [store.load().generation for _ in range(100)]

    with ThreadPoolExecutor() as readers:
        reading = readers.submit(read_many)
        processes = [
            multiprocessing.get_context("spawn").Process(
                target=_writer, args=(str(tmp_path), key)
            )
            for key in ("key-a", "key-b")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert set(reading.result()) <= {1, 2, 3}
    assert store.load().generation == 3
    assert store.load().user_key.reveal() in {"key-a", "key-b"}


def test_logout_then_login_never_reuses_generation(store):
    assert save(store).generation == 1
    store.logout()
    assert not store.path().exists()
    assert store.resolve().origin == "none"
    assert save(store).generation == 2


def test_reader_sees_old_complete_record_while_replace_is_pending(store, monkeypatch):
    from threading import Event

    save(store)
    ready, proceed = Event(), Event()
    original = os.replace

    def paused_replace(*args, **kwargs):
        ready.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.os, "replace", paused_replace)
    with ThreadPoolExecutor() as executor:
        writing = executor.submit(save, store, "saved@example.test", "new-key")
        try:
            assert ready.wait(5)
            try:
                record = store.load()
            except store.StoreError:
                pytest.fail(
                    "Concurrent reader observed an incomplete login before atomic replacement"
                )
            assert record.generation == 1
            assert record.user_key.reveal() == "synthetic-key"
        finally:
            proceed.set()
        assert writing.result().generation == 2
    assert store.load().user_key.reveal() == "new-key"


def test_failed_generation_reservation_cannot_reuse_a_logged_out_generation(
    store, monkeypatch
):
    assert save(store).generation == 1
    store.logout()

    def unavailable(fd, data):
        raise OSError("synthetic disk write refusal")

    with monkeypatch.context() as patch:
        patch.setattr(store.os, "write", unavailable)
        with pytest.raises(store.StoreError):
            save(store)
    assert save(store).generation == 2
