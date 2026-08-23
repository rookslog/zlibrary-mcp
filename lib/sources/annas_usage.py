"""A daily request budget that survives the process it was spent in.

Every MCP operation starts a fresh `python_bridge.py`, so a counter held in a
`_RateLimiter` instance resets to zero on every call. A 30-per-day ceiling
stored that way is not a limit — it is a number in a docstring, and an operator
could issue unbounded downloads while the code claimed otherwise (Codex on
#150). Anna's browser route is in scope *on the condition* that the limits are
real, so this is the difference between the politeness claim being true and
being rhetorical.

The state is a small JSON file next to the browser profile, guarded by an
exclusive lock file so two concurrent bridge processes cannot both read 29 and
both write 30. The lock is a directory rather than `fcntl`: `os.mkdir` is
atomic on POSIX and Windows alike, needs no third-party dependency, and this
project supports both.

Nothing here is a security boundary. An operator who wants more can edit the
file or raise `ANNAS_BROWSER_DAILY_LIMIT`; the point is that they have to
decide to, rather than getting it by accident because the counter forgot.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# How long to wait for another process to finish its read-modify-write. The
# critical section is two file operations, so anything beyond this means a
# crashed process left the directory behind rather than a genuine queue.
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.02
_STALE_LOCK_AGE = 30.0


class CrossProcessLock:
    """One holder at a time, across processes, for a named resource.

    An `asyncio.Lock` serialises coroutines inside one event loop, and every
    MCP operation runs in a **separate** `python_bridge.py` process — so two
    overlapping downloads each build their own session, their own lock, and
    navigate simultaneously (Codex on #150). Chrome would then also refuse the
    second launch against a profile the first one owns, producing a confusing
    browser error instead of a clear one.

    Same atomic-`mkdir` mechanism as the usage counter, with a much longer
    timeout: a browser walk legitimately takes a couple of minutes, so waiting
    is the correct behaviour rather than a symptom.
    """

    def __init__(self, path: str, stale_after: float = 600.0):
        self.path = Path(path)
        self.owner_path = self.path / "owner"
        self.stale_after = stale_after

    def _owner_pid(self) -> Optional[int]:
        try:
            return int(self.owner_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _owner_is_alive(self) -> bool:
        """Whether the process that took this lock still exists.

        Age alone is not evidence of staleness here: a payload transfer may
        legitimately run for far longer than `stale_after` (the download budget
        allows 25 minutes) while the holder sits suspended, still owning the
        Chrome profile. Reclaiming on age would then launch a second Chrome
        against a profile in use — defeating the serialisation and breaking
        both downloads (Codex on #150). Liveness is the question that was
        actually being asked.
        """
        pid = self._owner_pid()
        if pid is None:
            # No owner recorded: an older lock, or one whose write was
            # interrupted. Fall back to age, which is what this class did
            # before liveness existed.
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Alive, owned by another user. Not ours to reclaim.
            return True
        except OSError:
            return True
        return True

    def acquire(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.path)
                try:
                    self.owner_path.write_text(str(os.getpid()))
                except OSError:  # pragma: no cover - lock still held, just anonymous
                    logger.debug(
                        "Could not record the lock owner at %s", self.owner_path
                    )
                return True
            except FileExistsError:
                if self._owner_is_alive():
                    # Held by a running process. Wait however long the caller
                    # allows; a live holder is never stale, whatever its age.
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.25)
                    continue
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.stale_after:
                    logger.warning(
                        "Reclaiming a browser lock at %s: owner is gone and the "
                        "lock is %.0fs old",
                        self.path,
                        age,
                    )
                    self.release()
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)
            except OSError:
                # Same reasoning as the counter: a lock we cannot take must not
                # block the operator's own downloads outright.
                return True

    def release(self) -> None:
        try:
            self.owner_path.unlink()
        except OSError:
            pass
        try:
            os.rmdir(self.path)
        except OSError:
            pass


class DailyUsage:
    """Persistent count of requests spent in the current 24-hour window."""

    def __init__(self, state_path: str):
        self.path = Path(state_path)
        self.lock_path = Path(str(state_path) + ".lock")

    # -- locking -----------------------------------------------------------

    def _acquire(self) -> bool:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.lock_path)
                return True
            except FileExistsError:
                # A process that died mid-write must not wedge the limiter
                # shut. Waiting forever would be a denial of the operator's own
                # tool; ignoring the lock entirely would defeat it.
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > _STALE_LOCK_AGE:
                    logger.warning(
                        "Removing a stale Anna's usage lock (%.0fs old) at %s",
                        age,
                        self.lock_path,
                    )
                    self._release()
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EROFS):
                    return False
                raise

    def _release(self) -> None:
        try:
            os.rmdir(self.lock_path)
        except OSError:
            pass

    # -- state -------------------------------------------------------------

    def _read(self) -> Tuple[float, int, float]:
        """`(window_started, spent, next_allowed)`, all wall-clock seconds.

        `next_allowed` is the earliest a request may start. It lives here
        rather than in the limiter for the same reason the count does: every
        MCP operation is a fresh bridge process, so an in-memory timestamp
        makes the minimum interval and the backoff hold *within* one call and
        vanish between calls — which is the normal case, not the edge case.
        """
        try:
            raw = json.loads(self.path.read_text())
            return (
                float(raw["window_started"]),
                int(raw["spent"]),
                float(raw.get("next_allowed", 0.0)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt or absent file starts a fresh window. It must not raise
            # — an unreadable counter that took the whole route down would make
            # the politeness layer a liability rather than a guard.
            return time.time(), 0, 0.0

    def _write(self, window_started: float, spent: int, next_allowed: float) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                {
                    "window_started": window_started,
                    "spent": spent,
                    "next_allowed": next_allowed,
                }
            )
        )
        os.replace(temporary, self.path)

    # -- the operations the limiter needs ---------------------------------

    def spend(
        self, limit: int, min_interval: float = 0.0
    ) -> Tuple[bool, Optional[int], float]:
        """Take one request from today's budget.

        Returns `(allowed, remaining_after, wait_seconds)`, where `remaining_after` is
        **None** when the count could not be read — unknown, not zero. A
        sentinel that looks like a number gets treated as one: `-1` reached
        `QuotaInfo.downloads_left`, the router read it as exhausted, and the
        fail-open path spent browser requests and then threw the result away
        (Codex on #150).

        The whole read-modify-write runs under the lock, because the
        interesting failure is two bridge processes each reading `limit - 1`
        and each deciding it may proceed.
        """
        if not self._acquire():
            # Failing open here is deliberate and narrow: the alternative is
            # that a permissions problem or a wedged lock silently blocks the
            # operator's own downloads. It is logged, and the in-process
            # spacing and backoff still apply.
            logger.warning(
                "Could not lock the Anna's usage counter at %s; this request is "
                "not counted against the daily ceiling",
                self.path,
            )
            return True, None, min_interval
        try:
            window_started, spent, next_allowed = self._read()
            now = time.time()
            if now - window_started >= 86400 or now < window_started:
                window_started, spent = now, 0
            if spent >= limit:
                return False, 0, 0.0
            start_at = max(now, next_allowed)
            # The next slot is reserved *before* the wait, under the lock, so
            # two processes queueing at once get consecutive slots rather than
            # both computing the same start time and going together.
            self._write(window_started, spent + 1, start_at + min_interval)
            return True, max(0, limit - (spent + 1)), max(0.0, start_at - now)
        finally:
            self._release()

    def penalise(self, seconds: float) -> None:
        """Push the earliest allowed request out, across processes.

        A backoff held in memory expired the moment the bridge process did, so
        "back off five minutes rather than retrying into the wall" was true of
        one call and false of the next one the operator made (#144).
        """
        if not self._acquire():
            logger.warning(
                "Could not lock the Anna's usage counter at %s; a backoff of "
                "%.0fs applies to this process only",
                self.path,
                seconds,
            )
            return
        try:
            window_started, spent, next_allowed = self._read()
            floor = time.time() + max(0.0, seconds)
            self._write(window_started, spent, max(next_allowed, floor))
        finally:
            self._release()

    def remaining(self, limit: int) -> int:
        """Budget left, without spending any. Never blocks on the lock."""
        window_started, spent, _ = self._read()
        now = time.time()
        if now - window_started >= 86400 or now < window_started:
            return limit
        return max(0, limit - spent)
