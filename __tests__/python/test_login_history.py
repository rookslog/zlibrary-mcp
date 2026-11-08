"""Missing counters preserve saved generations and concurrent repair writes."""

import json
import subprocess
import sys

import pytest
from lib import login_cli, login_store as store


def save():
    return store.save(
        identity="reader@example.test",
        user_id="42",
        user_key="synthetic-history-key",
        domain="origin.example.test",
    )


@pytest.mark.parametrize("entry", ["store", "cli"])
async def test_deleted_lock_logout_preserves_highest_saved_generation(entry, capsys):
    assert save().generation == 1
    assert save().generation == 2
    (store.path().parent / ".zlibrary.lock").unlink()
    if entry == "store":
        assert store.logout() == {"removed": True, "counter_repaired": True}
    else:
        assert await login_cli.run(["logout", "--json"]) == 0
        assert (
            "restart running servers" in json.loads(capsys.readouterr().out)["message"]
        )
    assert save().generation == 3


@pytest.mark.parametrize(
    "existing", [False, True], ids=["first-creation", "saved-history"]
)
def test_logout_before_creator_locks_never_resets_repaired_history(
    existing, monkeypatch
):
    if existing:
        assert save().generation == 1
        assert save().generation == 2
        (store.path().parent / ".zlibrary.lock").unlink()
    # A announces creation immediately before its first flock. B repairs while
    # A waits on stdin; only then may A take the same lock and continue saving.
    script = """
import fcntl, json, sys
from lib import login_store as store
original = fcntl.flock
first = True
def pause(fd, operation):
 global first
 if first:
  first = False
  print('created', flush=True)
  assert sys.stdin.readline().strip() == 'continue'
 return original(fd, operation)
fcntl.flock = pause
saved = store.save(identity='reader@example.test', user_id='42', user_key='synthetic-race-key', domain='origin.example.test')
print(json.dumps({'generation':saved.generation}), flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, "Lock creator did not reach its synchronization point"
        assert child.stdout.readline().strip() == "created"
        outcome = store.logout()
        assert outcome["counter_repaired"] is True
        retained = (store.path().parent / ".zlibrary.lock").read_bytes()
        expected = 3 if existing else 1
        output, error = child.communicate("continue\n", timeout=10)
        assert child.returncode == 0, error
        assert json.loads(output)["generation"] == expected
        history = (store.path().parent / ".zlibrary.lock").read_bytes()
        assert history.startswith(retained)
        assert save().generation == expected + 1
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
