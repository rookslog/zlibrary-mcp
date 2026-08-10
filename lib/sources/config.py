"""Configuration for multi-source book search system.

Environment variables:
    ANNAS_SECRET_KEY: API key for Anna's Archive fast downloads
    ANNAS_BASE_URL: Anna's Archive base URL (default: https://annas-archive.gl)
    LIBGEN_MIRROR: LibGen mirror to use (default: li)
    BOOK_SOURCE_DEFAULT: Default source selection (auto|annas|libgen)
    BOOK_SOURCE_FALLBACK_ENABLED: Enable fallback to other source (default: true)
"""

import os
from dataclasses import dataclass

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
    """

    annas_secret_key: str = ""
    annas_base_url: str = DEFAULT_ANNAS_BASE_URL
    libgen_mirror: str = DEFAULT_LIBGEN_MIRROR
    default_source: str = "auto"  # 'auto' | 'annas' | 'libgen'
    fallback_enabled: bool = True

    @property
    def has_annas_key(self) -> bool:
        """Check if Anna's Archive API key is configured."""
        return bool(self.annas_secret_key)


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
    )
