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
# These compose. The preflight budget is per PHASE and there are two (DNS, then
# TCP), so one provider attempt costs at worst 2*preflight + total = 55s. LibGen
# walks up to three mirrors and an `auto` search adds Anna's on top, giving a
# worst case of 4 x 55s = 220s. PYTHON_BRIDGE_TIMEOUT on the Node side (240s)
# has to stay above that — a subprocess kill that fires first would preempt a
# legitimate slow walk rather than catch a hang. Raising any value here without
# raising that one narrows a margin that is already only 20s.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_TOTAL_TIMEOUT = 45.0
# A source URL can legitimately serve a multi-hundred-megabyte book. Keep its
# full-transfer wall clock finite but separate from the 45-second search and
# URL-resolution budget. The independent 40-minute Node budget allows 165
# seconds for worst-case LibGen resolution, 25 minutes for transfer, 10 minutes
# for OCR, and 135 seconds for finalization without coupling Python config to a
# TypeScript constant.
DEFAULT_DOWNLOAD_TIMEOUT = 1500.0
DEFAULT_PREFLIGHT_TIMEOUT = 5.0

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
    """

    annas_secret_key: str = ""
    annas_base_url: str = DEFAULT_ANNAS_BASE_URL
    libgen_mirror: str = DEFAULT_LIBGEN_MIRROR
    default_source: str = "auto"  # 'auto' | 'annas' | 'libgen'
    fallback_enabled: bool = True
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT
    preflight_enabled: bool = True
    preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT

    @property
    def has_annas_key(self) -> bool:
        """Check if Anna's Archive API key is configured."""
        return bool(self.annas_secret_key)


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
    )
