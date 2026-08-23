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
both write 30.

The lock was an atomic `os.mkdir`, chosen because it works the same on POSIX
and Windows. That bought atomic *creation* and no atomic *reclamation*: a
holder that dies leaves its directory behind, and every scheme for cleaning
one up is check-then-act — read the owner, decide it is dead, delete. Two
processes can pass that check together, and the second then deletes the lock
the first has already retaken, leaving two holders of one Chrome profile
(Codex on #150, twice). The PID files, liveness probes and staleness ages that
grew around it were all attempts to shrink that window rather than close it.

It is now an OS-held advisory lock — `fcntl.flock` on POSIX, `msvcrt.locking`
on Windows — because the kernel releases it when the holder dies. There is no
stale lock to reclaim, so the race has no window to occur in, and roughly
eighty lines of PID bookkeeping are gone with it. Both platforms are still
supported and nothing was added to the dependency set.

Nothing here is a security boundary. An operator who wants more can edit the
file or raise `ANNAS_BROWSER_DAILY_LIMIT`; the point is that they have to
decide to, rather than getting it by accident because the counter forgot.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# How long to wait for another process to finish its read-modify-write. The
# critical section is two file operations, so anything beyond this means a
# crashed process left the directory behind rather than a genuine queue.
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.02


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt  # noqa: PLC0415

    def _try_lock(fd: int) -> None:
        """Claim byte 0 exclusively, or raise OSError."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl  # noqa: PLC0415

    def _try_lock(fd: int) -> None:
        """Claim the whole file exclusively, or raise OSError."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class UsageCounterUnavailableError(RuntimeError):
    """The daily counter cannot be locked, so the ceiling cannot be enforced.

    Raised rather than swallowed: see the reasoning in `DailyUsage.spend`.
    """


class CrossProcessLock:
    """One holder at a time, across processes, for a named resource.

    An `asyncio.Lock` serialises coroutines inside one event loop, and every
    MCP operation runs in a **separate** `python_bridge.py` process — so two
    overlapping downloads each build their own session, their own lock, and
    navigate simultaneously (Codex on #150). Chrome would then also refuse the
    second launch against a profile the first one owns, producing a confusing
    browser error instead of a clear one.

    The claim is an OS advisory lock on an open file descriptor, so the kernel
    drops it when the holder exits — crash, kill -9 or clean return alike.
    That is the whole reason it replaced the lock directory this class used to
    manage by hand: there is no orphaned lock, therefore no staleness to judge,
    no owner PID to trust, and no check-then-act window in which two processes
    can both decide they are entitled to reclaim.

    Waiting is the correct behaviour here rather than a symptom: a browser walk
    legitimately takes a couple of minutes, so callers pass a generous timeout.

    The pid written into the file is diagnostics only. Nothing reads it to make
    a decision — that was the old design, and it is what went wrong.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self, timeout: float) -> bool:
        """Block up to `timeout` seconds for the lock. False if not taken.

        Fails CLOSED on an unusable lock path, for the same reason the counter
        does: returning success would mean "no lock exists, proceed anyway",
        two bridge processes would launch Chrome against one profile, and the
        serialisation this class exists for would be gone (Codex on #150). An
        unwritable path does not heal itself, so allowing the first request
        allows every later one.
        """
        if self._fd is not None:
            # Re-entrant acquisition would make the paired release() drop a
            # lock the caller still believes it holds.
            raise RuntimeError(f"lock at {self.path} is already held by this object")
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as exc:
                logger.warning("Cannot open the lock file at %s: %s", self.path, exc)
                return False
            try:
                _try_lock(fd)
            except OSError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL if timeout <= _LOCK_TIMEOUT else 0.25)
                continue
            except Exception:
                os.close(fd)
                raise
            self._fd = fd
            try:
                os.ftruncate(fd, 0)
                os.write(fd, str(os.getpid()).encode())
            except OSError:  # pragma: no cover - diagnostics only
                logger.debug("Could not record the lock owner in %s", self.path)
            return True

    def release(self) -> None:
        """Drop the lock. Safe to call when not held."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:  # pragma: no cover - the close below releases it anyway
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


class DailyUsage:
    """Persistent count of requests spent in the current 24-hour window."""

    def __init__(self, state_path: str):
        self.path = Path(state_path)
        self.lock_path = Path(str(state_path) + ".lock")
        # The same lock as the browser session uses, on a different path. It
        # used to be a second hand-rolled copy of the mkdir scheme, with its
        # own staleness age and its own version of the reclamation race.
        self._lock = CrossProcessLock(self.lock_path)

    # -- locking -----------------------------------------------------------

    def _acquire(self) -> bool:
        """Take the counter lock, or report failure so the caller can refuse.

        The critical section is two file operations, so `_LOCK_TIMEOUT` beyond
        it means genuine contention rather than a crashed holder — a crashed
        holder no longer leaves anything behind to wait for.
        """
        return self._lock.acquire(_LOCK_TIMEOUT)

    def _release(self) -> None:
        self._lock.release()

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
        except FileNotFoundError:
            # No file yet: a genuinely fresh window, which is the ordinary
            # first-run case.
            return time.time(), 0, 0.0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A file that EXISTS and cannot be read is a different situation
            # entirely. Resetting `spent` to zero there hands out a fresh
            # thirty requests, and the next write replaces the real count — so
            # a corrupt or unreadable state file silently raised the ceiling
            # instead of enforcing it (Codex on #150). Refuse, as with an
            # unlockable counter.
            raise UsageCounterUnavailableError(
                f"the Anna's daily-usage counter at {self.path} exists but "
                f"cannot be read ({type(exc).__name__}: {exc}). Refusing rather "
                f"than starting a fresh allowance on top of an unknown count. "
                f"Delete the file to reset the day deliberately."
            ) from None

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
            # Fail CLOSED. An earlier version allowed the request uncounted, on
            # the reasoning that a lock problem should not block the operator.
            # That was wrong in the way that matters: an unlockable path is not
            # transient — every later process takes the same branch, so the
            # ceiling is gone permanently and silently, and this route's scope
            # is conditional on the ceiling being real (Codex on #150).
            #
            # A refusal is visible, diagnosable, and fixable in one command. An
            # unbounded browser route against an anti-abuse control is neither.
            raise UsageCounterUnavailableError(
                f"cannot lock the Anna's daily-usage counter at {self.path}. "
                f"The browser route refuses to run without an enforceable "
                f"ceiling (#144) — an uncounted request would remove the limit "
                f"for every later call too, not just this one. Check that the "
                f"directory is writable, or set ANNAS_BROWSER_PROFILE_DIR "
                f"somewhere that is."
            )
        try:
            window_started, spent, next_allowed = self._read()
            now = time.time()
            # Forward past 24h: a genuine new window. Backward past the
            # window start: an NTP correction, NOT a new day — resetting there
            # granted a second full allowance inside one real 24-hour period,
            # repeatedly, for as long as the clock kept being corrected (Codex
            # on #150). Re-anchor the window instead, keeping the count.
            if now - window_started >= 86400:
                window_started, spent = now, 0
            elif now < window_started:
                # `next_allowed` is an ABSOLUTE timestamp on the pre-correction
                # clock, so re-anchoring the window alone leaves it stranded in
                # the future: a one-hour NTP correction turned "wait 20s" into
                # "wait an hour and 20s" (Codex on #150). Shift it by the same
                # delta the clock moved, which preserves whatever real delay
                # was left — including a long `penalise()` backoff, which must
                # survive this or the rollback would quietly cancel it.
                rollback = window_started - now
                logger.warning(
                    "Clock moved backwards %.0fs past the usage window start; "
                    "re-anchoring without resetting the %d requests already "
                    "spent, and shifting the request floor by the same amount",
                    rollback,
                    spent,
                )
                window_started = now
                next_allowed = max(0.0, next_allowed - rollback)
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
