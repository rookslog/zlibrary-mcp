"""Multi-source book search adapters.

This package provides a unified interface for searching and downloading
books from multiple sources (Anna's Archive, LibGen).

Usage:
    from lib.sources import UnifiedBookResult, SourceAdapter, get_source_config

    # Get configuration
    config = get_source_config()

    # All adapters return UnifiedBookResult
    # Adapters implement SourceAdapter ABC
"""

from .annas import AnnasArchiveAdapter, QuotaExhaustedError
from .base import SourceAdapter
from .config import SourceConfig, get_source_config
from .errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
    SourceError,
)
from .libgen import LibgenAdapter
from .models import DownloadResult, QuotaInfo, SourceType, UnifiedBookResult
from .router import SourceRouter

__all__ = [
    "UnifiedBookResult",
    "DownloadResult",
    "QuotaInfo",
    "SourceType",
    "SourceConfig",
    "get_source_config",
    "SourceAdapter",
    "AnnasArchiveAdapter",
    "QuotaExhaustedError",
    "LibgenAdapter",
    "SourceRouter",
    "SourceError",
    "ProviderUnreachableError",
    "ProviderTimeoutError",
    "ProviderResponseError",
    "AllSourcesFailedError",
]
