"""Abstract base class for book source adapters.

All source adapters (Anna's Archive, LibGen) implement this interface
to provide consistent search and download behavior.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional

from .models import DownloadResult, UnifiedBookResult
from .net import WalkDeadline


class SourceAdapter(ABC):
    """Abstract base class for book source adapters.

    Implementations must provide:
        search: Find books matching a query
        get_download_url: Get download URL for a specific book
        iter_download_candidates: Override when a source has independent routes
        close: Clean up resources (httpx clients, etc.)
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        deadline: Optional[WalkDeadline] = None,
        **kwargs,
    ) -> List[UnifiedBookResult]:
        """Search for books matching query.

        Args:
            query: Search string (title, author, ISBN, etc.)
            deadline: Wall-clock ceiling for the WHOLE walk this attempt
                belongs to, shared across providers and mirrors. An adapter
                must draw its per-attempt budget from what this has left
                rather than starting a full-length attempt on a spent walk;
                None means the adapter is being used directly and its own
                configured budget applies.
            **kwargs: Source-specific search options

        Returns:
            List of unified book results from this source
        """
        pass

    @abstractmethod
    async def get_download_url(
        self,
        md5: str,
        deadline: Optional[WalkDeadline] = None,
    ) -> DownloadResult:
        """Get download URL for a book by MD5 hash.

        Args:
            md5: MD5 hash identifying the book
            deadline: As for `search`.

        Returns:
            DownloadResult with URL and optional quota info
        """
        pass

    async def iter_download_candidates(
        self,
        md5: str,
        deadline: Optional[WalkDeadline] = None,
    ) -> AsyncIterator[DownloadResult]:
        """Yield source-neutral download candidates for one book.

        Adapters with a single resolution path inherit this compatibility
        implementation. Providers with independent mirrors override it.
        """
        yield await self.get_download_url(md5, deadline=deadline)

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (httpx clients, etc.)."""
        pass
