"""Data models for multi-source book search system.

Provides unified result types that abstract away differences between
Anna's Archive and LibGen sources.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class SourceType(str, Enum):
    """Supported book sources."""

    ANNAS_ARCHIVE = "annas_archive"
    LIBGEN = "libgen"


@dataclass
class UnifiedBookResult:
    """Unified book search result from any source.

    Required fields:
        md5: Unique identifier (MD5 hash of book content)
        title: Book title
        source: Which source provided this result

    Optional fields (may not be available from all sources):
        author: Book author(s)
        year: Publication year
        extension: File extension (pdf, epub, etc.)
        size: Human-readable file size
        download_url: Direct download URL if available
        extra: Source-specific metadata
    """

    md5: str
    title: str
    source: SourceType
    author: str = ""
    year: str = ""
    extension: str = ""
    size: str = ""
    download_url: str = ""
    extra: Dict = field(default_factory=dict)


@dataclass
class QuotaInfo:
    """Download quota information from Anna's Archive.

    Anna's Archive API provides quota information with each download
    request. This tracks remaining downloads for the day.
    """

    downloads_left: int
    downloads_per_day: int
    downloads_done_today: int


@dataclass
class DownloadResult:
    """Result of a download URL request.

    Contains the resolved download URL, optional quota information (for
    Anna's Archive which has daily limits), and the provenance of the route
    that resolved it.

    `route`, `mirror` and `host` exist so acquisition can *return* what served
    the file instead of only logging it. Before #101 they did not, so the
    adapters wrote provenance to `logger.warning` on stderr and the caller
    never saw which mirror or CDN node had answered (found by review on #98).
    They default to empty so an adapter that genuinely has no mirror concept
    is not forced to invent one.

    Attributes:
        url: Resolved download URL
        source: Which source resolved it
        quota_info: Remaining daily downloads, where the source reports them
        route: The named route that resolved the URL, e.g. 'get.php'
        mirror: The mirror or domain the route was taken against, e.g. 'vg'
        host: The host expected to serve the bytes, where resolution observed
            it — a mirror can hand out a valid key while its CDN node is dead,
            so this is not always the mirror
    """

    url: str
    source: SourceType
    quota_info: Optional[QuotaInfo] = None
    route: str = ""
    mirror: str = ""
    host: str = ""
