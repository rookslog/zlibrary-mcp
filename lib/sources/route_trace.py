"""Per-call route observations; no configuration, URLs or provider payloads."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
from time import monotonic

_routes = ContextVar("route_observations", default=None)
_attempt = ContextVar("route_attempt", default=None)


@contextmanager
def collect_routes():
    entries = []
    token = _routes.set(entries)
    try:
        yield entries
    finally:
        _routes.reset(token)


@contextmanager
def route_attempt(source, route=None):
    entry = {"source": source}
    if route is not None:
        entry["route"] = route
    token = _attempt.set(entry)
    started = monotonic()
    try:
        yield
    finally:
        entry["ms"] = max(0, round((monotonic() - started) * 1000))
        entries = _routes.get()
        if entries is not None and "route" in entry:
            entries.append(entry)
        _attempt.reset(token)


def decided_route(route):
    entry = _attempt.get()
    if entry is not None:
        entry["route"] = route


def queued_since(started):
    entry = _attempt.get()
    elapsed = max(0, round((monotonic() - started) * 1000))
    if entry is not None and elapsed > 0:
        entry["queued_ms"] = entry.get("queued_ms", 0) + elapsed


def reply_with_via(function):
    @wraps(function)
    async def observed(*args, **kwargs):
        with collect_routes() as via:
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                exc.via = via
                raise
        # A receipt-only miss is an internal admission signal, not a tool success.
        if result.get("receipt_hit") is False:
            return result
        return {**result, "via": via}

    return observed


def api_attempt(function):
    @wraps(function)
    async def observed(*args, **kwargs):
        with route_attempt("zlibrary", "api"):
            return await function(*args, **kwargs)

    return observed


def public_routes(value):
    """Closed failure projection, bounded to one observation per source."""
    if not isinstance(value, list) or len(value) > 3:
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            return []
        if item.get("source") not in {"libgen", "annas", "zlibrary"} or item.get(
            "route"
        ) not in {"api", "html", "browser"}:
            return []
        entry = {key: item[key] for key in ("source", "route")}
        for key in ("ms", "queued_ms"):
            if key == "queued_ms" and key not in item:
                continue
            number = item.get(key)
            if (
                type(number) not in (int, float)
                or not math.isfinite(number)
                or number < 0
            ):
                return []
            entry[key] = number
        result.append(entry)
    return result
