"""Symmetric per-source capability and limit reporting.

One vocabulary, two surfaces. The routing contract decided on #96 tells the
caller about sources in two moments that cost different things:

- inline, on every ``search_multi_source`` response (`routing.sources`) — only
  facts decidable from configuration, never a network round-trip;
- on demand, through ``get_download_limits`` — the same entries, with the
  quota that costs a round-trip filled in for whichever sources were asked
  for.

Both read their entries from here so the two surfaces cannot drift into
describing the same source two different ways.

Two rules govern the shape:

**Report, never rank (VISION invariant 4).** Every source gets the same key
set. A field that exists for one source and not another is a ranking dressed
as a schema, and the caller — not this server — chooses.

**A limit is three-valued, never null.** "This source has no daily limit",
"this source has one and we do not know it here", and "this source has one and
it is 7 of 10" are three different facts, and collapsing them onto ``null`` is
how a caller ends up treating LibGen's absence of a limit as an unknown quota
and paying for a round-trip that can never answer. Hence
:data:`LIMIT_NONE` / :data:`LIMIT_UNKNOWN` / :data:`LIMIT_KNOWN`, plus
:data:`LIMIT_NOT_APPLICABLE` for a source with no download route to limit.
"""

import os
from typing import Dict, Iterable, List, Optional, Sequence

from .config import SourceConfig, get_source_config

# Canonical source identities. These are what appears in `routing.sources`,
# `routing.served_by`, `provenance.source` and the `get_download_limits`
# response — one spelling everywhere, so a caller can key one dict off another.
SOURCE_ANNAS = "annas_archive"
SOURCE_LIBGEN = "libgen"
SOURCE_ZLIBRARY = "zlibrary"

#: Every source this server reads from, in a stable order.
KNOWN_SOURCES: Sequence[str] = (SOURCE_ANNAS, SOURCE_LIBGEN, SOURCE_ZLIBRARY)

# Accepted spellings, including the `source` selector values that
# `search_multi_source` has always taken ("annas" rather than "annas_archive").
_SOURCE_ALIASES: Dict[str, str] = {
    "annas": SOURCE_ANNAS,
    "annas_archive": SOURCE_ANNAS,
    "annas-archive": SOURCE_ANNAS,
    "annasarchive": SOURCE_ANNAS,
    "libgen": SOURCE_LIBGEN,
    "library_genesis": SOURCE_LIBGEN,
    "zlibrary": SOURCE_ZLIBRARY,
    "z-library": SOURCE_ZLIBRARY,
    "zlib": SOURCE_ZLIBRARY,
}

# Route names. A route is a thing the caller can ask this server to do with a
# source, not an internal code path.
ROUTE_SEARCH = "search"
ROUTE_DOWNLOAD = "download"

# `daily_limit.state` values.
LIMIT_NONE = "none"  # known to impose no daily limit
LIMIT_KNOWN = "known"  # concrete numbers, measured this call
LIMIT_UNKNOWN = "unknown"  # a limit exists but is not known here
LIMIT_NOT_APPLICABLE = "not_applicable"  # no download route to limit


def canonical_source(name: str) -> str:
    """Map any accepted spelling of a source onto its canonical identity.

    Args:
        name: Source name or selector value, in any accepted spelling

    Returns:
        One of :data:`KNOWN_SOURCES`

    Raises:
        ValueError: If the name is not a source this server reads from
    """
    key = str(name or "").strip().lower()
    try:
        return _SOURCE_ALIASES[key]
    except KeyError:
        raise ValueError(
            f"Unknown source '{name}'. Known sources: {', '.join(KNOWN_SOURCES)}."
        ) from None


def resolve_requested_sources(sources: Optional[Iterable[str]]) -> List[str]:
    """Normalize a caller's source selection, preserving :data:`KNOWN_SOURCES` order.

    ``None`` — the caller did not narrow the request — means every source. An
    explicit empty list is a caller asking about nothing and is rejected
    rather than silently widened to everything: a request that narrows to
    nothing is a mistake, and answering it with all three would spend a
    round-trip the caller was trying to avoid.

    Args:
        sources: Source names in any accepted spelling, or None for all

    Returns:
        Canonical source names, deduplicated, in canonical order

    Raises:
        ValueError: If a name is unknown, or the selection is explicitly empty
    """
    if sources is None:
        return list(KNOWN_SOURCES)
    if isinstance(sources, str):
        sources = [sources]

    selected = {canonical_source(name) for name in sources}
    if not selected:
        raise ValueError(
            "'sources' must name at least one source, or be omitted to report "
            f"all of them. Known sources: {', '.join(KNOWN_SOURCES)}."
        )
    return [name for name in KNOWN_SOURCES if name in selected]


def _daily_limit(
    state: str,
    *,
    total: Optional[int] = None,
    used: Optional[int] = None,
    remaining: Optional[int] = None,
    note: str = "",
) -> Dict:
    """One daily-limit report. Same keys whatever the state, so a caller can
    read `state` first and the numbers second without probing for keys."""
    return {
        "state": state,
        "total": total,
        "used": used,
        "remaining": remaining,
        "note": note,
    }


def no_daily_limit(note: str = "") -> Dict:
    """This source is known to impose no daily download limit."""
    return _daily_limit(LIMIT_NONE, note=note)


def unknown_daily_limit(note: str = "") -> Dict:
    """A daily limit exists, but its value is not known at this point.

    The note says what would answer it — a different tool, a configuration
    change, or a request that has not happened yet.
    """
    return _daily_limit(LIMIT_UNKNOWN, note=note)


def known_daily_limit(
    total: Optional[int], used: Optional[int], remaining: Optional[int], note: str = ""
) -> Dict:
    """Concrete numbers, measured on this call."""
    return _daily_limit(
        LIMIT_KNOWN, total=total, used=used, remaining=remaining, note=note
    )


def daily_limit_not_applicable(note: str = "") -> Dict:
    """No download route is available, so there is no quota to report."""
    return _daily_limit(LIMIT_NOT_APPLICABLE, note=note)


def zlibrary_credentials_configured() -> bool:
    """Whether Z-Library credentials are present in the environment.

    A local check by design: `routing` must cost no network, and an absent
    credential is decidable without one. Whether the credentials are *valid*
    is not knowable here and is not claimed.
    """
    return bool(
        os.environ.get("ZLIBRARY_EMAIL") and os.environ.get("ZLIBRARY_PASSWORD")
    )


def _entry(
    available: bool, routes: Sequence[str], daily_limit: Dict, note: str
) -> Dict:
    """One source entry. Identical key set for every source — see module docstring.

    ``available`` means "at least one route is usable right now", not "this
    source can do everything". A source that can be searched but not
    downloaded from is available, and its `routes` list says which half.
    """
    return {
        "available": available,
        "routes": list(routes),
        "daily_limit": daily_limit,
        "note": note,
    }


def describe_annas(config: SourceConfig) -> Dict:
    """Anna's Archive constraints, from configuration alone."""
    if config.has_annas_key:
        return _entry(
            available=True,
            routes=[ROUTE_SEARCH, ROUTE_DOWNLOAD],
            # Anna's reports the keyed quota in the fast-download response
            # itself, so there is no way to learn it without spending a
            # download. Reporting it as unknown-with-a-reason is the honest
            # answer; inventing a number or a round-trip is not.
            daily_limit=unknown_daily_limit(
                "Anna's reports the keyed quota with each fast download; it "
                "cannot be read without spending one"
            ),
            note="ANNAS_SECRET_KEY configured; keyed fast download available",
        )
    return _entry(
        available=True,
        routes=[ROUTE_SEARCH],
        daily_limit=daily_limit_not_applicable(
            "no download route without ANNAS_SECRET_KEY"
        ),
        note="no ANNAS_SECRET_KEY; search only, download unavailable",
    )


def describe_libgen(config: SourceConfig) -> Dict:
    """LibGen constraints, from configuration alone."""
    return _entry(
        available=True,
        routes=[ROUTE_SEARCH, ROUTE_DOWNLOAD],
        daily_limit=no_daily_limit("LibGen applies no daily download limit"),
        note=f"no account required; mirror {config.libgen_mirror} first",
    )


def describe_zlibrary(_config: SourceConfig) -> Dict:
    """Z-Library constraints, from configuration alone.

    The quota is deliberately `unknown` here rather than fetched: reading it
    costs an authenticated EAPI profile call, and making every LibGen-only
    search pay for one buys a symmetry nobody benefits from (#96).
    """
    if not zlibrary_credentials_configured():
        return _entry(
            available=False,
            routes=[],
            daily_limit=daily_limit_not_applicable(
                "ZLIBRARY_EMAIL / ZLIBRARY_PASSWORD not configured"
            ),
            note="no Z-Library credentials configured",
        )
    return _entry(
        available=True,
        routes=[ROUTE_SEARCH, ROUTE_DOWNLOAD],
        daily_limit=unknown_daily_limit(
            "costs an EAPI profile call; ask get_download_limits"
        ),
        note="credentials configured",
    )


_DESCRIBERS = {
    SOURCE_ANNAS: describe_annas,
    SOURCE_LIBGEN: describe_libgen,
    SOURCE_ZLIBRARY: describe_zlibrary,
}


def describe_sources(
    config: Optional[SourceConfig] = None,
    sources: Optional[Iterable[str]] = None,
) -> Dict[str, Dict]:
    """Locally knowable constraints for each source, keyed by canonical name.

    No network call happens here, and none may be added: this is what rides
    along with every search response.

    Args:
        config: Source configuration. Loaded from the environment if omitted.
        sources: Which sources to describe. All of them if omitted.

    Returns:
        Mapping of canonical source name to an entry with the identical key
        set described in the module docstring
    """
    resolved = config or get_source_config()
    return {
        name: _DESCRIBERS[name](resolved) for name in resolve_requested_sources(sources)
    }
