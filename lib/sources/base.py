"""Abstract base class for book source adapters.

All source adapters (Anna's Archive, LibGen) implement this interface
to provide consistent search and download behavior.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, List

from .models import DownloadResult, UnifiedBookResult


class SourceAdapter(ABC):
    """Abstract base class for book source adapters.

    Implementations must provide:
        search: Find books matching a query
        get_download_url: Get download URL for a specific book
        iter_download_candidates: Override when a source has independent routes
        close: Clean up resources (httpx clients, etc.)
    """

    @abstractmethod
    async def search(self, query: str, **kwargs) -> List[UnifiedBookResult]:
        """Search for books matching query.

        Args:
            query: Search string (title, author, ISBN, etc.)
            **kwargs: Source-specific search options

        Returns:
            List of unified book results from this source
        """
        pass

    @abstractmethod
    async def get_download_url(self, md5: str) -> DownloadResult:
        """Get download URL for a book by MD5 hash.

        Args:
            md5: MD5 hash identifying the book

        Returns:
            DownloadResult with URL and optional quota info
        """
        pass

    async def iter_download_candidates(self, md5: str) -> AsyncIterator[DownloadResult]:
        """Yield source-neutral download candidates for one book.

        Adapters with a single resolution path inherit this compatibility
        implementation. Providers with independent mirrors override it.
        """
        yield await self.get_download_url(md5)

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (httpx clients, etc.)."""
        pass
