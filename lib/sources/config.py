"""Configuration for multi-source book search system.

Environment variables:
    ANNAS_SECRET_KEY: API key for Anna's Archive fast downloads
    ANNAS_BASE_URL: Anna's Archive base URL (default: https://annas-archive.gl)
    LIBGEN_MIRROR: LibGen mirror to use (default: li)
    BOOK_SOURCE_DEFAULT: Default source selection (auto|annas|libgen)
    BOOK_SOURCE_FALLBACK_ENABLED: Enable fallback to other source (default: true)
    BOOK_SOURCE_CONNECT_TIMEOUT: TCP/TLS connect budget, seconds (default: 10)
    BOOK_SOURCE_READ_TIMEOUT: Response-read budget, seconds (default: 30)
    BOOK_SOURCE_TOTAL_TIMEOUT: Per-provider wall-clock budget, seconds (default: 45)
    BOOK_SOURCE_DOWNLOAD_TIMEOUT: Full source-file transfer budget, seconds (default: 1500)
    BOOK_SOURCE_PREFLIGHT: Probe host reachability before searching (default: true)
    BOOK_SOURCE_PREFLIGHT_TIMEOUT: Budget per probe phase, seconds (default: 5)
"""

import math
import os
from dataclasses import dataclass

# Timeout defaults. Every outbound call in this package is bounded by these;
# nothing here may fall back to "no timeout". The per-provider TOTAL budget
# exists because a per-request timeout does not bound a call that walks several
# mirrors, and because the LibGen library's own request cannot be interrupted
# once started (see lib/sources/net.run_bounded).
#
# These compose, and for months this comment tried to state how. It said one
# provider attempt costs 2*preflight + total = 55s, that LibGen walks three
# mirrors, that an `auto` search adds Anna's, and therefore 4 x 55s = 220s
# against a 240s PYTHON_BRIDGE_TIMEOUT. Every clause was true and the total was
# wrong: a custom LIBGEN_MIRROR prepends a fourth mirror, making it 275s, so the
# Node side killed the subprocess and the operator got a generic timeout instead
# of the per-mirror failures the walk had collected (#152).
#
# The repair is not a better sum. A walk costs providers x mirrors, both of
# which move with configuration, so any hand-maintained product drifts the
# moment either factor does. The walk is bounded by a WALL CLOCK instead:
# `WalkDeadline` (net.py) is created once per walk and every attempt draws from
# it, so the worst case is WALK_BUDGET regardless of how many mirrors or
# providers exist. The numbers below therefore size individual ATTEMPTS; only
# WALK_BUDGET sizes the walk, and only it has to stay under the Node budget.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_TOTAL_TIMEOUT = 45.0
# A source URL can legitimately serve a multi-hundred-megabyte book. Keep its
# full-transfer wall clock finite but separate from the per-attempt search and
# URL-resolution budget. The 40-minute Node budget splits into DEFAULT_WALK_
# BUDGET for resolution, this for transfer, OCR_ALLOWANCE and
# FINALIZE_ALLOWANCE; `worst_case_download_seconds()` adds them up and a test
# compares the total to PYTHON_BRIDGE_LONG_TIMEOUT.
DEFAULT_DOWNLOAD_TIMEOUT = 1500.0
DEFAULT_PREFLIGHT_TIMEOUT = 5.0

# The ceiling on a whole search or download-resolution walk, however many
# providers and mirrors it spans. ONE number covers both, and 165s is what both
# Node budgets already allow it:
#
#   search   PYTHON_BRIDGE_TIMEOUT      240s = 165 + 75s of bridge overhead
#   download PYTHON_BRIDGE_LONG_TIMEOUT 2400s = 165 resolution + 1500 transfer
#                                              + 600 OCR + 135 finalization
#
# Both sums are asserted by tests that read the TypeScript constants, so this
# cannot drift out of step with either. It is also, deliberately, the same
# clock the capped three-mirror walk used to cost — the change is that the cap
# is gone, so a mirror that fails its preflight in 5s now leaves room for the
# fourth instead of consuming one of only three slots.
DEFAULT_WALK_BUDGET = 165.0

# What the bridge costs outside the walk: spawning python, importing the source
# package, and serializing results back over stdout. Generous on purpose — the
# margin absorbs a slow cold start, it is not a number to tune.
WALK_OVERHEAD_ALLOWANCE = 30.0

# The rest of the long (download) budget, named so the sum can be computed
# instead of asserted in prose: OCR is bounded by rag_processing, and
# finalization covers hashing, staging-to-final rename and envelope writing.
OCR_ALLOWANCE = 600.0
FINALIZE_ALLOWANCE = 135.0

# Hosts operated by the Anna's Archive project, per the mirror list the live
# site itself advertises (verified 2026-07-24: .gl/.pk/.gd serve real search
# results). These three are also the domains Wikipedia's infobox lists as
# active (checked 2026-08-10).
#
# annas-archive.is was REMOVED 2026-08-10: it is not Anna's Archive. The
# discriminator is the endpoint this allowlist exists to authorize —
# /dyn/api/fast_download.json returns 401 on the genuine .gl (endpoint present,
# key rejected) and 404 on .is (endpoint absent). It also serves /books/{id}
# URLs instead of /md5/, runs a Google Analytics property, and hosts a
# secret-key "recovery" form. Its former justification here ("the domain the
# project's Telegram channel names as official") was unverifiable. Do not
# re-add it without reproducing the 401 on that endpoint.
#
# The former default annas-archive.li lapsed in March 2026 and is now PARKED
# by a domain squatter (Trellian/Above.com traffic-monetization page), and
# annas-archive.org/.se are NXDOMAIN. Parked, impersonating, or unknown hosts
# must NEVER receive ANNAS_SECRET_KEY — the fast-download API passes the key as
# a URL query parameter, so listing a host here discloses the key to whoever
# controls it. The adapter refuses to attach the key to any host not listed
# here (see AnnasArchiveAdapter.get_download_url).
ANNAS_TRUSTED_HOSTS = frozenset(
    {
        "annas-archive.gl",
        "annas-archive.pk",
        "annas-archive.gd",
    }
)

DEFAULT_ANNAS_BASE_URL = "https://annas-archive.gl"

# LibgenAdapter passes this suffix to LibgenSearch, which builds
# https://libgen.{mirror}/ — keep scripts/check_upstream.py deriving its probe
# host from here so the probe cannot drift from the runtime again.
DEFAULT_LIBGEN_MIRROR = "li"

# LibGen's blocklist is a moving target and it fails SILENTLY: a blocked UA
# gets HTTP 200 with nginx's ~640-byte default page, which is indistinguishable
# from an outage or an empty catalogue unless something classifies it (#141).
#
# #124 (2026-08-17) found an honest self-identifying UA was admitted where
# python-requests' default was not, and this default was set to that honest
# string. By 2026-08-23 the blocklist had widened to include it: measured
# against libgen.li, libgen.vg and libgen.la, the self-identifying UA returned
# the 641-byte stub for search AND the 637-byte stub for ads.php, while a
# desktop Firefox string returned 265 KB and a key-bearing download page.
#
# The honest string therefore now costs total failure and buys nothing, so the
# default is a browser string. `LIBGEN_USER_AGENT` exists so operators can set
# their own policy — including going back to an identifying string — without
# forking, and so the next widening is a config change rather than a release.
# The header every source sends unless something source-specific overrides it.
# Not LibGen-specific: httpx's and requests' default UAs are on more than one
# provider's blocklist, so "a browser string" is the neutral choice, not a
# LibGen accommodation.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

# LibGen starts from the neutral default and diverges only when an operator
# sets LIBGEN_USER_AGENT, which is scoped to LibGen requests alone.
DEFAULT_LIBGEN_USER_AGENT = DEFAULT_BROWSER_USER_AGENT


@dataclass
class SourceConfig:
    """Configuration for book source adapters.

    Attributes:
        annas_secret_key: API key for Anna's Archive fast downloads
        annas_base_url: Anna's Archive base URL
        libgen_mirror: LibGen mirror suffix (e.g., 'li', 'rs')
        default_source: Which source to try first ('auto', 'annas', 'libgen')
        fallback_enabled: Whether to try other source if primary fails
        connect_timeout: Seconds allowed for TCP/TLS connect
        read_timeout: Seconds allowed to read a response
        total_timeout: Seconds allowed for a whole provider operation
        download_timeout: Seconds allowed for a complete source-file transfer
        preflight_enabled: Probe host reachability before the real request
        preflight_timeout: Seconds allowed for each probe phase
        walk_budget: Seconds allowed for a WHOLE walk, across every
            provider and mirror it touches. The only budget the Node side
            has to be kept in step with.
    """

    annas_secret_key: str = ""
    annas_base_url: str = DEFAULT_ANNAS_BASE_URL
    libgen_mirror: str = DEFAULT_LIBGEN_MIRROR
    libgen_user_agent: str = DEFAULT_LIBGEN_USER_AGENT
    default_source: str = "auto"  # 'auto' | 'annas' | 'libgen'
    fallback_enabled: bool = True
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT
    preflight_enabled: bool = True
    preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT
    walk_budget: float = DEFAULT_WALK_BUDGET

    @property
    def has_annas_key(self) -> bool:
        """Check if Anna's Archive API key is configured."""
        return bool(self.annas_secret_key)


def worst_case_search_seconds(config: "SourceConfig") -> float:
    """Worst-case wall clock the Node side must allow for one bridge call.

    Now a sum of exactly two terms, neither of which depends on how many
    mirrors or providers exist: the walk's own ceiling, and what the bridge
    costs around it. That independence is the point. The previous version
    multiplied a per-attempt cost by a mirror count and a provider count, and
    was wrong for months because a custom `LIBGEN_MIRROR` changed one of the
    factors and nothing recomputed the product (#152). Adding Z-Library to the
    `auto` walk under #40 would have broken it again.

    A test compares this to `PYTHON_BRIDGE_TIMEOUT` in `python-runner.ts`, so
    raising `BOOK_SOURCE_WALK_BUDGET` past what the Node side allows fails the
    build instead of producing a killed subprocess in production.
    """
    return config.walk_budget + WALK_OVERHEAD_ALLOWANCE


def worst_case_download_seconds(config: "SourceConfig") -> float:
    """Worst-case wall clock for one acquisition, against the LONG budget.

    The same discipline as `worst_case_search_seconds`, for the other Node
    timeout. The allocation used to live only in a comment beside
    `DEFAULT_DOWNLOAD_TIMEOUT`, which is how #152 happened to the search side:
    a correct sentence nobody recomputed.
    """
    return (
        config.walk_budget
        + config.download_timeout
        + OCR_ALLOWANCE
        + FINALIZE_ALLOWANCE
    )


def _positive_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back on nonsense.

    A malformed or non-positive timeout must not disable the timeout — that is
    the failure this module exists to prevent — so it falls back to the default
    rather than to None.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def get_source_config() -> SourceConfig:
    """Load configuration from environment variables.

    Returns a fresh SourceConfig each call (not cached) to support
    environment variable changes during testing.
    """
    return SourceConfig(
        annas_secret_key=os.environ.get("ANNAS_SECRET_KEY", ""),
        annas_base_url=os.environ.get("ANNAS_BASE_URL", DEFAULT_ANNAS_BASE_URL),
        libgen_mirror=os.environ.get("LIBGEN_MIRROR", DEFAULT_LIBGEN_MIRROR),
        libgen_user_agent=(
            os.environ.get("LIBGEN_USER_AGENT", "").strip() or DEFAULT_LIBGEN_USER_AGENT
        ),
        default_source=os.environ.get("BOOK_SOURCE_DEFAULT", "auto"),
        fallback_enabled=os.environ.get("BOOK_SOURCE_FALLBACK_ENABLED", "true").lower()
        == "true",
        connect_timeout=_positive_float(
            "BOOK_SOURCE_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT
        ),
        read_timeout=_positive_float("BOOK_SOURCE_READ_TIMEOUT", DEFAULT_READ_TIMEOUT),
        total_timeout=_positive_float(
            "BOOK_SOURCE_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT
        ),
        download_timeout=_positive_float(
            "BOOK_SOURCE_DOWNLOAD_TIMEOUT", DEFAULT_DOWNLOAD_TIMEOUT
        ),
        preflight_enabled=os.environ.get("BOOK_SOURCE_PREFLIGHT", "true").lower()
        == "true",
        preflight_timeout=_positive_float(
            "BOOK_SOURCE_PREFLIGHT_TIMEOUT", DEFAULT_PREFLIGHT_TIMEOUT
        ),
        walk_budget=_positive_float("BOOK_SOURCE_WALK_BUDGET", DEFAULT_WALK_BUDGET),
    )
