"""Closed, bounded EAPI diagnostics. No exception prose or response payloads."""

import json
import logging
import os
import ssl
from uuid import UUID, uuid4

import httpx

FAILURE_TYPES = frozenset(
    {
        "EAPIAuthenticationError",
        "HTTPStatusError",
        "RedirectRefusedError",
        "DiamWallError",
        "NonJSONResponseError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "ReadError",
        "WriteError",
        "ConnectionError",
        "TimeoutError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "AcquisitionError",
    }
)


def failure_type(error):
    name = type(error).__name__
    return name if name in FAILURE_TYPES else "RuntimeError"


def _causes(error):
    seen = set()
    pending = [error]
    while pending and len(seen) < 16:
        error = pending.pop(0)
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        yield error
        # httpcore retains the native TLS verdict in __context__, not __cause__.
        pending.extend((error.__cause__, error.__context__))


def certificate_failure(error):
    return any(
        isinstance(cause, ssl.SSLCertVerificationError) for cause in _causes(error)
    )


def encode_failure(info, *, mcp=False):
    """Bound the complete serialized envelope, not just its inner diagnostic record."""

    def encode(value):
        return json.dumps(
            {"content": [{"type": "text", "text": json.dumps(value)}]} if mcp else value
        )

    encoded = encode(info)
    if len(encoded.encode()) > 2048:
        encoded = encode(
            {
                "error": "EAPI bridge failure",
                "type": "RuntimeError",
                "diagnostics": read_diagnostics(info.get("diagnostics")),
            }
        )
    return encoded


def read_diagnostics(value):
    if not isinstance(value, dict) or set(value) - {
        "failure_kind",
        "http_status",
        "attempt",
        "refresh_count",
        "request_id",
    }:
        return None
    if value.get("failure_kind") not in (
        "auth_rejected",
        "transport",
        "wall",
        "ambiguous_status",
    ):
        return None
    for key, minimum in (("attempt", 1), ("refresh_count", 0)):
        number = value.get(key)
        if type(number) is not int or not minimum <= number <= 65535:
            return None
    if "http_status" in value and (
        type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599
    ):
        return None
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or len(request_id) != 36:
        return None
    try:
        parsed = UUID(request_id)
    except ValueError:
        return None
    if str(parsed) != request_id or parsed.version not in (1, 2, 3, 4, 5):
        return None
    if len(json.dumps(value, separators=(",", ":")).encode()) > 512:
        return None
    return dict(value)


def failure_diagnostics(error, *, refreshed=False):
    # Parent supplies only correlation metadata, never credentials, on this channel.
    try:
        candidate = os.environ.get("ZLIBRARY_DIAGNOSTIC_ID", "")
        identifier = UUID(candidate)
        if str(identifier) != candidate or identifier.version not in (1, 2, 3, 4, 5):
            raise ValueError()
        request_id = str(identifier)
    except ValueError:
        request_id = str(uuid4())
    causes = list(_causes(error))
    from zlibrary.eapi import (
        EAPIAuthenticationError,
        DiamWallError,
        RedirectRefusedError,
    )

    # The closest typed verdict wins. A replacement's real 401 must not be
    # hidden by the refusal that triggered recovery, or vice versa.
    verdict = next(
        (
            cause
            for cause in causes
            if isinstance(cause, (RedirectRefusedError, EAPIAuthenticationError))
        ),
        None,
    )
    kind = (
        "ambiguous_status"
        if isinstance(error, httpx.HTTPStatusError)
        else "transport"
        if isinstance(verdict, RedirectRefusedError)
        else "auth_rejected"
        if isinstance(verdict, EAPIAuthenticationError)
        else "wall"
        if any(isinstance(cause, DiamWallError) for cause in causes)
        else "transport"
        if any(
            isinstance(cause, (httpx.RequestError, ssl.SSLError)) for cause in causes
        )
        else "ambiguous_status"
    )
    status = None
    for cause in causes:
        candidate = getattr(cause, "status_code", None)
        if candidate is None:
            candidate = getattr(getattr(cause, "response", None), "status_code", None)
        if type(candidate) is int and 100 <= candidate <= 599:
            status = candidate
            break
    result = {
        "failure_kind": kind,
        "attempt": 1,
        "refresh_count": 0,
        "request_id": request_id,
    }
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    for key, env, low in (
        ("attempt", "ZLIBRARY_DIAGNOSTIC_ATTEMPT", 1),
        ("refresh_count", "ZLIBRARY_DIAGNOSTIC_REFRESH", 0),
    ):
        try:
            result[key] = min(65535, max(low, int(os.environ.get(env, str(low)))))
        except ValueError:
            pass
    if refreshed:
        result["refresh_count"] = max(1, result["refresh_count"])
    return read_diagnostics(result)


class DiagnosticLogFilter(logging.Filter):
    """Suppress legacy payload-bearing bridge logs, including exc_info rendering.

    This filter is installed only by the bridge, not by direct library consumers.
    Both the stderr and file handlers receive the same closed diagnostic record.
    """

    def filter(self, record):
        # Handler filters also see unrelated records propagated to root.
        if not (
            record.name == "zlibrary"
            or record.name.startswith("zlibrary.")
            or record.name == "lib.enhanced_metadata"
            or record.name.startswith("lib.enhanced_metadata.")
        ):
            return True
        diagnostics = read_diagnostics(getattr(record, "auth_diagnostics", None))
        if diagnostics is None:
            return False
        encoded = json.dumps(diagnostics, separators=(",", ":"))
        if len(encoded.encode()) > 512 or "\n" in encoded:
            return False
        record.msg, record.args = encoded, ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True
