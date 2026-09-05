"""Private, process-shared EAPI cookies for the short-lived Python bridge."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import time

import httpx

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger("zlibrary")
# A local brake, not a prediction of when the upstream limit will reset.
LOGIN_COOLDOWN_SECONDS = 300
LIMIT_MESSAGE = (
    "Too many logins. Z-Library temporarily rejected login; try again later."
)


def _private_directory() -> Path:
    root = Path(
        os.environ.get("ZLIBRARY_SESSION_DIR")
        or Path.home() / ".cache" / "zlibrary-mcp" / "sessions"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("EAPI session directory must not be a symlink")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError(
            "EAPI session directory must be owned by the current user with mode 0700"
        )
    return root


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (
        os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        os.close(fd)
        raise RuntimeError("EAPI session files must be private regular files (0600)")
    return fd


@asynccontextmanager
async def _locked(path: Path):
    fd = _open_private(path, os.O_CREAT | os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + 30
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        while not acquired:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for the EAPI session lock"
                    ) from None
                # Wait on lock availability, without blocking the event loop.
                await asyncio.sleep(0.05)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> dict:
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd) as handle:
        try:
            value = json.load(handle)
        except (ValueError, UnicodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict):
    fd, name = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _response_error(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
    except ValueError:
        return ""
    return payload.get("error", "") if isinstance(payload, dict) else ""


async def _is_authenticated(client) -> bool:
    try:
        profile = await client.get_profile()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401 or (
            exc.response.status_code == 400 and _response_error(exc) == "Please login"
        ):
            return False
        raise
    if profile.get("success") == 0 and profile.get("error") == "Please login":
        return False
    user = profile.get("user")
    if (
        profile.get("success") == 1
        and isinstance(user, dict)
        and str(user.get("id")) == str(client.remix_userid)
    ):
        return True
    raise RuntimeError(
        "Unexpected EAPI profile response; cached session retained without re-login"
    )


async def authenticated_client(email: str, password: str, login, client_factory):
    """Validate shared cookies, or serialize a single login and atomic save."""
    root = _private_directory()
    account = hashlib.sha256(email.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            [email, password, os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip()]
        ).encode()
    ).hexdigest()
    path = root / f"{account}.json"
    async with _locked(root / f"{account}.lock"):
        record = _read(path)
        if record.get("fingerprint") != fingerprint:
            record = {}
        retry_after = record.get("retry_after", 0)
        if isinstance(retry_after, (int, float)) and retry_after > time.time():
            raise RuntimeError(LIMIT_MESSAGE + " Local retry cooldown is active.")
        if all(
            isinstance(record.get(k), str) and record[k]
            for k in ("domain", "userid", "userkey")
        ):
            client = client_factory(
                record["domain"],
                remix_userid=record["userid"],
                remix_userkey=record["userkey"],
            )
            try:
                valid = await _is_authenticated(client)
            except BaseException:
                await client.close()
                raise
            if valid:
                logger.info("Reused cached EAPI session")
                return client
            await client.close()
            # The next caller must not retry a known invalid session.
            _write(path, {"fingerprint": fingerprint})
        try:
            client = await login()
        except httpx.HTTPStatusError as exc:
            error = _response_error(exc)
            if (
                exc.response.status_code in (400, 429)
                and isinstance(error, str)
                and error.startswith("Too many logins")
            ):
                _write(
                    path,
                    {
                        "fingerprint": fingerprint,
                        "retry_after": time.time() + LOGIN_COOLDOWN_SECONDS,
                    },
                )
                raise RuntimeError(LIMIT_MESSAGE) from None
            raise
        try:
            if not client.remix_userid or not client.remix_userkey:
                raise RuntimeError("EAPI login returned no session cookies")
            _write(
                path,
                {
                    "fingerprint": fingerprint,
                    "domain": client.domain,
                    "userid": str(client.remix_userid),
                    "userkey": str(client.remix_userkey),
                },
            )
        except BaseException:
            await client.close()
            raise
        return client
