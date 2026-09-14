"""Persistent EAPI session cache: reuse login cookies across bridge runs.

Every MCP tool call spawns a fresh bridge process (``src/lib/python-runner.ts``),
so without a cache each call pays one ``/eapi/user/login``. Login is the one
EAPI endpoint that is rate-limited upstream: a session doing search ->
download -> metadata can exhaust its login budget and start seeing 502s
within a minute. The cookies Z-Library sets on login (``remix_userid`` /
``remix_userkey``) *are* the login result in durable form, so they are cached
here and validated with a cheap authenticated profile read before reuse
(:func:`python_bridge.initialize_eapi_client` does the validation).

The cache file holds bearer-equivalent secrets: it is written with mode 0600,
and nothing in it is ever logged.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("zlibrary")

_SESSION_ENV_VAR = "ZLIBRARY_SESSION_FILE"
_SESSION_FILE_NAME = "eapi-session.json"

_REQUIRED_KEYS = ("domain", "remix_userid", "remix_userkey")


def default_session_path() -> Path:
    """Where the session cache lives.

    ``ZLIBRARY_SESSION_FILE`` overrides the location (tests and sandboxes
    point it somewhere disposable); the default keeps the project's state
    under the user's config directory.
    """
    override = os.environ.get(_SESSION_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "zlibrary-mcp" / _SESSION_FILE_NAME


def load_eapi_session(path: Optional[Path] = None) -> Optional[dict]:
    """Read a cached session, or ``None`` when there is nothing usable.

    Any malformed file — unreadable, not JSON, not an object, or missing any
    required field — yields ``None`` rather than an error: a cache that
    cannot be understood is a cache that gets rewritten on next login.
    """
    session_path = Path(path) if path else default_session_path()
    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning(
            "Ignoring unreadable EAPI session cache %s: %s", session_path, exc
        )
        return None
    if not isinstance(raw, dict):
        return None
    session = {key: str(raw.get(key) or "").strip() for key in _REQUIRED_KEYS}
    if not all(session.values()):
        return None
    return session


def save_eapi_session(
    domain: str,
    remix_userid: object,
    remix_userkey: object,
    path: Optional[Path] = None,
) -> None:
    """Persist a login result atomically. Best-effort: never raises.

    A failed cache write must not fail the tool call that produced the
    session — the next call would simply log in again, which is the
    pre-cache behaviour, not an outage.
    """
    values = (
        str(domain or "").strip(),
        str(remix_userid or "").strip(),
        str(remix_userkey or "").strip(),
    )
    if not all(values):
        return
    session_path = Path(path) if path else default_session_path()
    payload = json.dumps(
        {
            "domain": values[0],
            "remix_userid": values[1],
            "remix_userkey": values[2],
        }
    )
    try:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".eapi-session-", dir=str(session_path.parent)
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, session_path)
        finally:
            # After a successful os.replace this is already gone; on any
            # failure before it, drop the half-written temporary.
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    except OSError as exc:
        logger.warning(
            "Could not persist EAPI session cache at %s: %s", session_path, exc
        )


def clear_eapi_session(path: Optional[Path] = None) -> None:
    """Drop the cache — called when a restored session fails validation.

    Keeps a dead cookie pair from being re-validated (one wasted profile
    request) on every subsequent call until it happens to expire properly.
    """
    session_path = Path(path) if path else default_session_path()
    try:
        session_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not remove EAPI session cache at %s: %s", session_path, exc
        )
