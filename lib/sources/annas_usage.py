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
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _process_start_time(pid: int) -> Optional[float]:
    """Process start time in seconds since boot, or None if unavailable.

    Linux only, from `/proc/<pid>/stat` field 22. Used to tell a live lock
    owner from an unrelated process that happens to have inherited its PID
    after a reboot — without it, a reused PID makes a stale lock look live
    forever and no download can ever take the browser again (Codex on #150).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        ticks = float(fields[19])  # field 22, zero-indexed after the comm split
    except (OSError, IndexError, ValueError):
        return None
    try:
        hertz = os.sysconf("SC_CLK_TCK") or 100
    except (OSError, ValueError):
        hertz = 100
    return ticks / hertz


def _process_exists(pid: int) -> bool:
    """Whether a process id is live, without signalling it.

    **`os.kill(pid, 0)` is not a liveness probe on Windows.** CPython maps any
    signal other than the console-control values onto `TerminateProcess`, so
    the "harmless" zero signal *kills* the process it was meant to ask about
    (Codex on #150). Here that would mean a second download terminating the
    bridge holding the browser lock, and then waiting behind a lock whose owner
    it had just destroyed.

    POSIX keeps the cheap check. Windows uses `OpenProcess`, which is the
    read-only question actually being asked.
    """
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import ctypes  # noqa: PLC0415

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                # Cannot tell; treat as alive rather than reclaim a lock we do
                # not understand.
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
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


# How long to wait for another process to finish its read-modify-write. The
# critical section is two file operations, so anything beyond this means a
# crashed process left the directory behind rather than a genuine queue.
_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.02
_STALE_LOCK_AGE = 30.0

# How many multiples of `stale_after` a PID-alive lock is trusted for on
# platforms that cannot report process start times. Generous, because a live
# holder really can hold for a whole download; finite, because a PID reused
# after a reboot must not wedge the browser forever.
_PID_TRUST_CEILING = 8


def _system_uptime() -> Optional[float]:
    """Seconds since boot, or None where unavailable."""
    try:
        with open("/proc/uptime", "rb") as handle:
            return float(handle.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None


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
        if not _process_exists(pid):
            return False

        # A live PID is not proof of ownership. After a reboot or an unclean
        # shutdown the directory survives and its PID can belong to an
        # unrelated long-lived process, which would make the lock look live
        # forever (Codex on #150). Where the platform can tell us, a process
        # that started AFTER the lock was created cannot be its owner.
        started = _process_start_time(pid)
        if started is not None:
            try:
                lock_age = time.time() - self.path.stat().st_mtime
            except OSError:
                return True
            uptime = _system_uptime()
            if uptime is not None and (uptime - started) < lock_age:
                logger.warning(
                    "Lock at %s names PID %d, but that process is younger than "
                    "the lock — treating it as stale rather than live",
                    self.path,
                    pid,
                )
                return False
            return True

        # No start time available (non-Linux). Trust the PID, but not forever:
        # cap it so a reused PID cannot wedge the browser permanently.
        try:
            lock_age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return lock_age < (self.stale_after * _PID_TRUST_CEILING)

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
                observed_owner = self._owner_pid()
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
                    # Ownership-aware: two processes can both see the same
                    # stale lock, and if the first reclaims and recreates it,
                    # a blind `release()` from the second would delete the NEW
                    # owner's lock and hand the browser to two holders at once
                    # (Codex on #150). Only remove what we actually observed.
                    logger.warning(
                        "Reclaiming a browser lock at %s: owner is gone and the "
                        "lock is %.0fs old",
                        self.path,
                        age,
                    )
                    self._release_if_unchanged(observed_owner)
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)
            except OSError as exc:
                # Fail CLOSED, for the same reason the counter does. Returning
                # success here means "no lock exists, proceed anyway" — so two
                # bridge processes both launch Chrome against one profile and
                # the serialisation this class exists for is gone, usually
                # surfacing as a confusing profile-lock error rather than as
                # the policy violation it is (Codex on #150). An unwritable
                # lock path does not heal itself, so allowing the first request
                # allows every later one.
                logger.warning(
                    "Cannot create the browser lock at %s: %s", self.path, exc
                )
                return False

    def _release_if_unchanged(self, observed_owner: Optional[int]) -> None:
        """Remove the lock only if it still names the owner we saw.

        Between observing a stale lock and reclaiming it, another process may
        have reclaimed and re-taken it. Removing that one would leave two
        holders believing they own the browser.
        """
        if self._owner_pid() != observed_owner:
            logger.info("Not reclaiming %s: another process took it first", self.path)
            return
        self.release()

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
                logger.warning(
                    "Clock moved backwards past the usage window start; "
                    "re-anchoring without resetting the %d requests already spent",
                    spent,
                )
                window_started = now
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
