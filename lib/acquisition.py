"""Durable per-destination acquisition receipts, independent of provider authentication.

The output-root guard is argument admission, not protection against an operator
concurrently swapping directories. Receipts are not authorization tokens.
"""

import asyncio
from contextlib import asynccontextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from lib.filename_utils import create_unified_filename, normalize_document_extension
from lib.sources.config import (
    DEFAULT_NODE_LONG_TIMEOUT_SECONDS,
    WALK_OVERHEAD_ALLOWANCE,
    WALK_BUDGET_SAFETY_MARGIN,
    _node_timeout_seconds,
)
from lib.sources.net import WalkDeadline

# Scope, not another timer: every helper consults this same WalkDeadline.
# Context-local because concurrent acquisitions must never share issuance/time.
_acquisition_scope = ContextVar("acquisition_scope", default=None)


def _mark_published():
    """The commit point ends cooperative refusal; the Node owner stays the hard stop."""
    scope = _acquisition_scope.get()
    if scope is not None:
        scope["published"] = True


def _check_deadline(phase=None, *, minimum=WALK_BUDGET_SAFETY_MARGIN):
    # After commit, even consulting the clock can only turn success into an
    # error or strand its receipt. Finish under the reserved slice/Node owner.
    scope = _acquisition_scope.get()
    if scope is None or scope["published"]:
        return None
    if phase is not None:
        scope["phase"] = phase
    left = scope["deadline"].remaining()
    if left < minimum:
        raise AcquisitionError(
            "walk_budget_exhausted",
            f"Acquisition deadline exhausted during {scope['phase']}",
            issued=scope["issued"],
        )
    return left


try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

_WINDOWS = os.name == "nt"


class AcquisitionError(ValueError):
    def __init__(self, reason, message, *, issued=False, occupied=False):
        super().__init__(message)
        self.reason = reason
        self.issued = issued
        self.occupied = occupied

    def to_dict(self):
        return {
            "reason": self.reason,
            "request_issued": self.issued,
            **({"destination_occupied": True} if self.occupied else {}),
        }


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def destination_name(book, filename=None):
    if filename is None:
        return create_unified_filename(
            book, year=book.get("year", ""), language=book.get("language", "")
        )
    if (
        not isinstance(filename, str)
        or filename in {"", ".", ".."}
        or any(c in filename for c in ("/", "\\", "\x00"))
    ):
        raise AcquisitionError("invalid_argument", "filename must be a basename")
    cleaned = re.sub(r"[^\w. -]", "_", filename).strip(" .")
    if not cleaned:
        raise AcquisitionError(
            "invalid_argument", "filename is empty after sanitization"
        )
    extension = normalize_document_extension(book.get("extension")) or "bin"
    return Path(cleaned).stem[:180] + "." + extension


def output_directory(output_dir):
    root_value = os.environ.get("ZLIBRARY_OUTPUT_ROOT")
    if root_value is not None:
        root = Path(root_value).expanduser()
        if not root_value or not root.is_absolute():
            raise AcquisitionError(
                "output_refused", "ZLIBRARY_OUTPUT_ROOT must be an absolute path"
            )
    else:
        data = (
            os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
            if _WINDOWS
            else os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        )
        root = Path(data) / "zlibrary-mcp" / "downloads"
        if not root.is_absolute():
            raise AcquisitionError(
                "output_refused", "Output data directory must be absolute"
            )
    root = root.resolve()
    value = os.fspath(output_dir) if output_dir else ""
    caller_path = Path(value)
    if (
        "\x00" in value
        or value.startswith("~")
        or ".." in caller_path.parts
        or (
            _WINDOWS
            and (caller_path.drive or caller_path.root)
            and not caller_path.is_absolute()
        )
    ):
        raise AcquisitionError(
            "output_refused",
            "output_dir must be relative or fully absolute, without NUL, '..' or leading '~'; use ZLIBRARY_OUTPUT_ROOT",
        )
    directory = (root / value).resolve()
    if os.environ.get(
        "ZLIBRARY_UNRESTRICTED_OUTPUT"
    ) != "1" and not directory.is_relative_to(root):
        raise AcquisitionError(
            "output_refused",
            "output_dir is outside ZLIBRARY_OUTPUT_ROOT; trusted operators may set ZLIBRARY_UNRESTRICTED_OUTPUT=1",
        )
    return directory


def _private_directory(directory):
    """Chmod only levels this call creates, including missing root ancestors."""
    if directory.is_dir():
        return
    _private_directory(directory.parent)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        if not directory.is_dir():
            raise
    else:
        directory.chmod(0o700)


def _prepare_directory(directory):
    """Create private levels, then reject a directory redirected during creation."""
    _private_directory(directory)
    if directory.resolve() != directory:
        raise AcquisitionError(
            "output_refused", "Output directory changed during creation"
        )


def _identity(book):
    source = str(book.get("source") or "zlibrary").lower()
    if source == "annas_archive":
        source = "annas"
    identifiers = (
        {
            "id": str(book.get("id", "")),
            "hash": book.get("hash") or book.get("book_hash"),
        }
        if source == "zlibrary"
        else {"md5": str(book.get("md5") or "").lower()}
    )
    if not all(identifiers.values()):
        raise AcquisitionError(
            "invalid_argument", "Selection has no usable source identity"
        )
    return source, identifiers


def _hash(path):
    _check_deadline()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _check_deadline()
            digest.update(chunk)
    _check_deadline()
    return digest.hexdigest()


def _read(path, *, issued=False):
    _check_deadline()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise AcquisitionError(
                "output_refused", "Receipt must be a regular file", issued=issued
            )
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except AcquisitionError:
        raise
    except (FileNotFoundError, ValueError):
        return {}
    except OSError as error:
        raise AcquisitionError(
            "output_refused", "Cannot read acquisition receipt", issued=issued
        ) from error


def _verified_artifact(record, directory):
    """A receipt is usable only when its current artifact still matches it."""
    if (
        record.get("record_version") != 1
        or record.get("state") not in {"complete", "intent"}
        or not isinstance(record.get("path"), str)
        or not record.get("sha256")
    ):
        return None
    path = Path(record["path"])
    if (
        path.resolve().parent == directory
        and not path.is_symlink()
        and path.is_file()
        and path.stat().st_size == record.get("bytes")
        and _hash(path) == record["sha256"]
    ):
        return path
    return None


def _atomic_json(path, record):
    _check_deadline()
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            _check_deadline()
            os.fsync(stream.fileno())
        _check_deadline()
        os.replace(temporary, path)
        # Persist the directory entry as well as the receipt bytes on POSIX.
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _alive(pid):
    if _WINDOWS:
        raise AcquisitionError(
            "output_refused", "POSIX PID liveness is unavailable on Windows"
        )
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _remove_stale(lock, budget):
    """Never steal an active local holder merely because a call was slow."""
    if _WINDOWS or fcntl is None:
        raise AcquisitionError(
            "output_refused", "Stale unlink requires the POSIX lease backend"
        )
    try:
        with lock.open("r+") as existing:
            if fcntl:
                try:
                    fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            try:
                record = json.load(existing)
            except ValueError:
                record = {}
            if not isinstance(record, dict):
                record = {}
            st = os.fstat(existing.fileno())
            pid = record.get("pid")
            # The OS lease, not an empty file's age, protects a paused creator.
            usable_pid = type(pid) is int and pid > 0
            stale = not _alive(pid) if usable_pid else True
            current = lock.stat()
            if stale and (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                lock.unlink()
    except FileNotFoundError:
        pass


class DestinationLease:
    """One non-waiting OS lease; Windows lock files are persistent coordination state."""

    def __init__(self, path, budget, *, issued=False):
        self.lock = Path(str(path) + ".lock")
        self.budget = budget
        self.issued = issued
        self.descriptor = None
        self.held = False
        self.windows = _WINDOWS

    def _busy(self):
        return AcquisitionError(
            "breaker_open",
            "Destination is still being acquired; retry after the active call completes",
            issued=self.issued,
        )

    def __enter__(self):
        if (self.windows and msvcrt is None) or (not self.windows and fcntl is None):
            raise AcquisitionError(
                "output_refused",
                "No supported destination lease backend",
                issued=self.issued,
            )
        try:
            if self.windows:
                # No O_EXCL, metadata liveness checks or unlink on this backend.
                self.descriptor = os.open(
                    self.lock,
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    # CRT _locking reports a conflicting region as EACCES/EDEADLK.
                    # An open failure, EBADF/ENOSYS etc. is not contention.
                    if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise self._busy() from error
                    raise
                self.held = True
            else:
                try:
                    self.descriptor = os.open(
                        self.lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                    )
                except FileExistsError:
                    _remove_stale(self.lock, self.budget)
                    try:
                        self.descriptor = os.open(
                            self.lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                        )
                    except FileExistsError as error:
                        raise self._busy() from error
                try:
                    fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise self._busy() from error
                self.held = True
                # A reaper may have replaced our empty inode before this flock.
                # Never write metadata or admit a holder detached from the path.
                held = os.fstat(self.descriptor)
                try:
                    current = self.lock.stat()
                except FileNotFoundError as error:
                    raise self._busy() from error
                if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                    raise self._busy()
                os.write(
                    self.descriptor,
                    json.dumps({"pid": os.getpid(), "time": _now()}).encode(),
                )
                os.fsync(self.descriptor)
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, OSError):
                raise AcquisitionError(
                    "output_refused",
                    "Cannot acquire destination lease",
                    issued=self.issued,
                ) from error
            raise

    def close(self):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        try:
            if self.held:
                if self.windows:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    try:
                        current, held = self.lock.stat(), os.fstat(descriptor)
                        if (current.st_dev, current.st_ino) == (
                            held.st_dev,
                            held.st_ino,
                        ):
                            self.lock.unlink()  # inode checked while flock is still held
                    except FileNotFoundError:
                        pass
        finally:
            self.held = False
            os.close(descriptor)  # must run even if unlock/stat/unlink raises

    def __exit__(self, *_):
        self.close()


@asynccontextmanager
async def destination_lock(path, budget):
    scope = _acquisition_scope.get()
    deadline = scope["deadline"] if scope else WalkDeadline(budget)
    while True:
        _check_deadline("destination lease")
        lease = DestinationLease(path, budget)
        try:
            lease.__enter__()
            break
        except AcquisitionError as error:
            if error.reason != "breaker_open" or deadline.expired():
                _check_deadline("destination lease")
                raise
            await asyncio.sleep(min(0.05, deadline.remaining()))
    try:
        yield lease
    finally:
        lease.close()


def _record(book, source, identifiers, destination, request):
    return {
        "record_version": 1,
        "state": "intent",
        "path": str(destination),
        "bytes": None,
        "sha256": None,
        "md5": book.get("md5"),
        "reused": False,
        "transfer_skipped": False,
        "downloaded_at": _now(),
        "source": source,
        "provenance": {"route": source, "mirror": None, "host": None},
        **{
            field: book.get(field) or None
            for field in [
                "title",
                "author",
                "year",
                "language",
                "publisher",
                "pages",
                "isbn",
                "series",
                "edition",
                "translator",
            ]
        },
        "format": book.get("extension") or None,
        "identifiers": identifiers,
        "request": {
            field: request.get(field)
            for field in ["ref", "query", "item_id", "why_chosen"]
        },
    }


def _reply(record, record_path, skipped):
    return {
        "path": record["path"],
        "bytes": record["bytes"],
        "reused": False if skipped else record["reused"],
        "transfer_skipped": skipped,
        "verified": "sha256",
        "record": str(record_path),
    }


def _same_identity(record, source, identifiers):
    return record.get("source") == source and record.get("identifiers") == identifiers


def _guard_target(path, directory, *, issued=False):
    path = Path(path)
    if path.is_symlink() or path.resolve().parent != directory:
        raise AcquisitionError(
            "output_refused",
            "Destination must remain inside the output directory",
            issued=issued,
        )
    return path.resolve()


def _refuse_foreign(record, target, directory, source, identifiers, *, issued=False):
    # A durable hashed intent reserves its canonical identity even before publication.
    reserved = (
        record.get("record_version") == 1
        and record.get("state") == "intent"
        and record.get("sha256")
        and record.get("path") == str(target)
    )
    if (
        reserved or _verified_artifact(record, directory) is not None
    ) and not _same_identity(record, source, identifiers):
        raise AcquisitionError(
            "output_refused",
            "Destination has a verified receipt or intent for another selection",
            issued=issued,
        )


def _refuse_occupied(target, *, issued=False):
    if target.exists():
        raise AcquisitionError(
            "output_refused",
            "Destination already holds a file with no matching receipt; move or rename it or choose another output_dir",
            issued=issued,
            occupied=True,
        )


def _long_bridge_budget():
    """Effective spawn owner (or long env timer), parsed as Node's positiveIntEnv.

    Anything but a plain positive integer of at most 2**31 - 1 ms falls back to
    the default, as the bridge deadline does, instead of refusing every download.
    """
    name = (
        "PYTHON_BRIDGE_CALL_TIMEOUT_MS"
        if "PYTHON_BRIDGE_CALL_TIMEOUT_MS" in os.environ
        else "PYTHON_BRIDGE_LONG_TIMEOUT"
    )
    return _node_timeout_seconds(name, DEFAULT_NODE_LONG_TIMEOUT_SECONDS)


def _finalization_reserve(scope):
    return min(135.0, scope["deadline"].budget / 8)


def _remaining_transfer_budget(configured):
    """Clamp inner work to the current acquisition, retaining its reply reserve.

    No scope means a direct Python source call retains its configured ceiling.
    Re-read on mirror admission too: suspending resolution for a transfer must
    never refund time to the outer whole-download clock.
    """
    scope = _acquisition_scope.get()
    if scope is None:
        return configured
    return min(
        configured,
        max(
            0.0,
            scope["deadline"].remaining()
            - _finalization_reserve(scope)
            - WALK_BUDGET_SAFETY_MARGIN,
        ),
    )


async def acquire(
    book_details,
    output_dir=None,
    filename=None,
    request=None,
    receipt_only=False,
    *,
    transfer,
):
    """One long deadline until commit, reserving time to finish receipt publication.

    Reserve bridge overhead exactly as the source walk does. Blocking filesystem
    syscalls are cooperative boundaries, not cancellable workers: no detached
    write may race the next holder after this call releases its destination.
    """
    owner = _long_bridge_budget()
    budget = max(
        0.0,
        owner - min(WALK_OVERHEAD_ALLOWANCE, owner / 8) - WALK_BUDGET_SAFETY_MARGIN,
    )
    token = _acquisition_scope.set(
        {
            "deadline": WalkDeadline(budget),
            "issued": False,
            "published": False,
            "phase": "admission",
        }
    )
    try:
        _check_deadline()
        return await _acquire(
            book_details, output_dir, filename, request, receipt_only, transfer=transfer
        )
    finally:
        _acquisition_scope.reset(token)


async def _acquire(
    book_details,
    output_dir=None,
    filename=None,
    request=None,
    receipt_only=False,
    *,
    transfer,
):
    """Lease initial and canonical destinations through publication and every receipt write.

    Verified artifact reuse is content- and metadata-read-only. With ``receipt_only``
    existing receipts are also read-only; otherwise ordinary calls may repair them.
    A receipt-only miss raises the usual pre-intent refusal or returns
    ``{"receipt_hit": False}`` before an intent is written or ``transfer`` is called.
    The parent uses it when its breaker refuses provider work.
    """
    book = dict(book_details)
    directory = output_directory(output_dir)
    destination = _guard_target(directory / destination_name(book, filename), directory)
    source, identifiers = _identity(book)
    _prepare_directory(directory)
    receipt_path = Path(str(destination) + ".acq.json")
    budget = _long_bridge_budget()
    async with destination_lock(destination, budget):
        with ExitStack() as leases:
            held = {destination}

            def canonical_lease(target, *, issued):
                target = _guard_target(target, directory, issued=issued)
                if target not in held:
                    # Never await a nested lease: opposing format corrections must not deadlock.
                    leases.enter_context(
                        DestinationLease(target, budget, issued=issued)
                    )
                    held.add(target)
                return target

            previous = _read(receipt_path, issued=False)
            if previous.get("record_version") == 1 and isinstance(
                previous.get("path"), str
            ):
                target = canonical_lease(previous["path"], issued=False)
                _refuse_foreign(previous, destination, directory, source, identifiers)
                canonical_path = Path(str(target) + ".acq.json")
                canonical = _read(
                    canonical_path, issued=False
                )  # inspect only after acquiring its lease
                _refuse_foreign(canonical, target, directory, source, identifiers)
                candidate = canonical if canonical else previous
                path = _verified_artifact(candidate, directory)
                if path == target and _same_identity(candidate, source, identifiers):
                    _mark_published()  # finish any receipt repair for this usable artifact
                    promoted = candidate["state"] == "intent"
                    if promoted:
                        candidate = {
                            **candidate,
                            "state": "complete",
                            "transfer_skipped": True,
                            "reused": False,
                        }
                    if receipt_only:
                        # Read-only: the next ordinary call repairs these receipts.
                        held_at = canonical_path if canonical else receipt_path
                        return _reply(candidate, held_at, True)
                    if promoted or not canonical:
                        _atomic_json(canonical_path, candidate)
                    if receipt_path != canonical_path and previous != candidate:
                        _atomic_json(receipt_path, candidate)
                    return _reply(candidate, canonical_path, True)
                if receipt_only:
                    return {"receipt_hit": False}
                _refuse_occupied(target)
            else:
                _refuse_foreign(previous, destination, directory, source, identifiers)

            if receipt_only:
                return {"receipt_hit": False}  # no intent, no provider work
            _refuse_occupied(destination)
            record = _record(book, source, identifiers, destination, request or {})
            _atomic_json(receipt_path, record)  # before authentication/provider work
            checkpoint_target = None
            checkpoint_count = 0
            checkpoint_valid = False
            authority = None

            def checkpoint(stage, target):
                nonlocal \
                    checkpoint_count, \
                    checkpoint_target, \
                    checkpoint_valid, \
                    authority, \
                    record
                _check_deadline("publication checkpoint")
                checkpoint_count += 1
                checkpoint_valid = False
                if checkpoint_count != 1:
                    raise AcquisitionError(
                        "internal",
                        "Acquisition requires exactly one publication checkpoint",
                        issued=True,
                    )
                target = canonical_lease(target, issued=True)
                stage = Path(stage)
                if stage.resolve() == target or not stage.is_file():
                    raise AcquisitionError(
                        "internal",
                        "Checkpoint must precede publication of a staged file",
                        issued=True,
                    )
                stage.chmod(0o600)
                canonical_path = Path(str(target) + ".acq.json")
                existing = _read(canonical_path, issued=True)
                _refuse_foreign(
                    existing, target, directory, source, identifiers, issued=True
                )
                digest, size = _hash(stage), stage.stat().st_size
                record.update(
                    path=str(target),
                    bytes=size,
                    sha256=digest,
                    format=normalize_document_extension(target.suffix) or None,
                )
                if _verified_artifact(existing, directory) == target and _same_identity(
                    existing, source, identifiers
                ):
                    if existing["sha256"] != digest or existing["bytes"] != size:
                        raise AcquisitionError(
                            "integrity_mismatch",
                            "Staged bytes differ from the verified canonical artifact",
                            issued=True,
                        )
                    authority = existing
                    if authority["state"] == "intent":
                        authority = {
                            **authority,
                            "state": "complete",
                            "transfer_skipped": True,
                            "reused": False,
                        }
                        _atomic_json(canonical_path, authority)
                    # A completed canonical receipt, including provenance, is immutable on reuse.
                    if canonical_path != receipt_path:
                        _atomic_json(receipt_path, authority)
                else:
                    _refuse_occupied(target, issued=True)
                    _atomic_json(
                        canonical_path, record
                    )  # canonical hashed intent FIRST
                    if canonical_path != receipt_path:
                        _atomic_json(receipt_path, record)
                checkpoint_target = target
                checkpoint_valid = True

            scope = _acquisition_scope.get()
            # Keep a bounded finalization slice inside the same clock, not a
            # fresh allowance after transfer. A sub-second transfer is not a
            # useful attempt (the provider walks make the same distinction).
            finalize_reserve = _finalization_reserve(scope)
            left = _check_deadline(
                "resolution and transfer", minimum=1.0 + finalize_reserve
            )
            scope["issued"] = True  # callback admission: never replay its outcome
            task = asyncio.create_task(
                transfer(
                    book, str(directory), filename=filename, publication_hook=checkpoint
                )
            )
            try:
                try:
                    # A timeout must inspect the commit point before cancelling:
                    # a committed transfer owns its finalization through reply.
                    result = await asyncio.wait_for(
                        asyncio.shield(task), left - finalize_reserve
                    )
                except asyncio.TimeoutError:
                    if task.done():
                        result = task.result()  # preserve a provider-owned TimeoutError
                    elif scope["published"]:
                        result = await task
                    else:
                        raise AcquisitionError(
                            "walk_budget_exhausted",
                            "Acquisition deadline exhausted during resolution and transfer",
                            issued=True,
                        ) from None
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if checkpoint_count != 1 or not checkpoint_valid:
                raise AcquisitionError(
                    "internal",
                    "Acquisition requires exactly one successful prepublication checkpoint",
                    issued=True,
                )
            final = _guard_target(result["file_path"], directory, issued=True)
            if final != checkpoint_target or not final.is_file():
                raise AcquisitionError(
                    "internal",
                    "Transfer returned a different or missing checkpoint target",
                    issued=True,
                )
            _mark_published()  # also support transfer callbacks publishing by other means
            if not result.get("publication_reused"):
                final.chmod(0o600)
            if (
                _hash(final) != record["sha256"]
                or final.stat().st_size != record["bytes"]
            ):
                raise AcquisitionError(
                    "integrity_mismatch",
                    "Published bytes differ from the acquisition checkpoint",
                    issued=True,
                )
            canonical_path = Path(str(final) + ".acq.json")
            if authority is None:
                record.update(
                    state="complete",
                    reused=bool(result.get("publication_reused", False)),
                    provenance=result.get("provenance") or record["provenance"],
                )
                _atomic_json(canonical_path, record)
            else:
                record = authority
            if (
                canonical_path != receipt_path
                and _read(receipt_path, issued=True) != record
            ):
                _atomic_json(receipt_path, record)
            reply = _reply(
                {**record, "reused": bool(result.get("publication_reused", False))},
                canonical_path,
                False,
            )
            if result.get("quota_estimate"):
                reply["quota_estimate"] = result["quota_estimate"]
            return reply
