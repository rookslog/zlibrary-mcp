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

    Contains the resolved download URL and optional quota information
    (for Anna's Archive which has daily limits).
    """

    url: str
    source: SourceType
    quota_info: Optional[QuotaInfo] = None
    # Resolution identity, distinct from the host observed during transfer.
    mirror: str = ""
    has_more_candidates: bool = False  # Untried destinations, not prefetched URLs.
