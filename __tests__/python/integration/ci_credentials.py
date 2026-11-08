"""Opaque integration credentials; raw values cross only the login boundary."""

from functools import partial
import json
import os
from pathlib import Path
from uuid import uuid4


class Secret:
    __slots__ = ("__value",)

    def __init__(self, value):
        self.__value = value

    def __repr__(self):
        return "***"

    def __str__(self):
        return "***"

    def reveal(self):
        return self.__value


def environment_credentials():
    return {
        "email": Secret(os.environ.get("ZLIBRARY_EMAIL", "")),
        "password": Secret(os.environ.get("ZLIBRARY_PASSWORD", "")),
    }


def session_capture_path():
    path = os.environ.get("ZLIBRARY_TEST_SESSION_VALUES")
    if not path:
        return None
    stamp = Path(path).absolute()
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
    if stamp.resolve().is_relative_to(workspace):
        raise ValueError("Session capture must be outside the workspace")
    return stamp


def record_session(stamp, record):
    if stamp is None:
        return
    data = (json.dumps(record) + "\n").encode("utf-8")
    fd = os.open(stamp, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o600)
        if os.write(fd, data) != len(data):
            raise OSError("Incomplete session capture")
    finally:
        os.close(fd)


async def capture_login(client, login, *args, **kwargs):
    stamp = session_capture_path()
    attempt = uuid4().hex
    record_session(stamp, {"attempt": attempt, "status": "pending"})
    try:
        return await login(*args, **kwargs)
    finally:
        record_session(
            stamp,
            {
                "attempt": attempt,
                "status": "complete",
                "values": [
                    str(getattr(client, "remix_userid", None) or ""),
                    str(getattr(client, "remix_userkey", None) or ""),
                ],
            },
        )


async def login_with_credentials(client, credentials):
    """Return a boolean so assertions cannot display a complete login reply."""
    try:
        login = client.login
        if not getattr(login, "_captures_session_values", False):
            login = partial(capture_login, client, login)
        return (
            await login(credentials["email"].reveal(), credentials["password"].reveal())
        ).get("success") == 1
    except Exception:
        raise RuntimeError("Integration login failed") from None
