"""Source router for multi-source book search with fallback logic.

Routes search and download requests to Anna's Archive (primary) or LibGen (fallback)
based on configuration and availability. Provides automatic fallback when quota is
exhausted or errors occur.

Key decisions:
- SOURCE-ANNAS-PRIMARY: Anna's Archive is primary source
- SOURCE-LIBGEN-FALLBACK: LibGen is fallback when Anna's quota exhausted or unavailable
"""

import logging
from typing import List, Literal, Optional

from .annas import AnnasArchiveAdapter, QuotaExhaustedError
from .config import SourceConfig, get_source_config
from .libgen import LibgenAdapter
from .models import DownloadResult, UnifiedBookResult

logger = logging.getLogger("zlibrary.sources")

SourceSelection = Literal["auto", "annas", "libgen"]


class SourceRouter:
    """Routes search and download requests to appropriate source with fallback.

    The router manages adapter lifecycle and provides automatic fallback
    when the primary source (Anna's Archive) fails or quota is exhausted.

    Configuration:
    - If ANNAS_SECRET_KEY is set, Anna's Archive is the primary source
    - If fallback_enabled=True (default), LibGen is used as fallback
    - 'auto' source selection picks Anna's if key exists, else LibGen

    An explicit source="annas" is honoured with or without a key, because Anna's
    search needs no credentials. 'auto' deliberately stays LibGen-first when no
    key is set: LibGen returns a resolvable download link, whereas an Anna's
    result without a key still needs a route the caller may not have.

    Usage:
        config = get_source_config()
        router = SourceRouter(config)
        results = await router.search("python programming")
        await router.close()
    """

    def __init__(self, config: Optional[SourceConfig] = None):
        """Initialize router with configuration.

        Args:
            config: SourceConfig instance. If None, loads from environment.
        """
        self.config = config or get_source_config()
        self._annas: Optional[AnnasArchiveAdapter] = None
        self._libgen: Optional[LibgenAdapter] = None

    def _get_annas(self) -> AnnasArchiveAdapter:
        """Get or create the Anna's Archive adapter.

        Constructed unconditionally. Anna's search is HTML scraping that carries
        no credentials, so it works without ANNAS_SECRET_KEY; only
        get_download_url needs the key, and it enforces that itself. Gating
        construction on the key put the check in the wrong place and made an
        explicit source="annas" silently return LibGen results instead (#74).

        Returns:
            AnnasArchiveAdapter instance (always available)
        """
        if self._annas is None:
            self._annas = AnnasArchiveAdapter(self.config)
        return self._annas

    def _get_libgen(self) -> LibgenAdapter:
        """Get or create LibGen adapter.

        Returns:
            LibgenAdapter instance (always available)
        """
        if self._libgen is None:
            self._libgen = LibgenAdapter(self.config)
        return self._libgen

    def _determine_source(self, source: SourceSelection) -> SourceSelection:
        """Determine actual source based on config and availability.

        Args:
            source: Requested source ('auto', 'annas', or 'libgen')

        Returns:
            Actual source to use based on configuration
        """
        if source == "auto":
            return "annas" if self.config.has_annas_key else "libgen"
        return source

    async def search(
        self,
        query: str,
        source: SourceSelection = "auto",
        **kwargs,
    ) -> List[UnifiedBookResult]:
        """Search for books with automatic fallback.

        Args:
            query: Search query string
            source: Source selection ('auto', 'annas', or 'libgen')
            **kwargs: Additional arguments passed to adapters

        Returns:
            List of UnifiedBookResult with source field indicating origin
        """
        actual_source = self._determine_source(source)

        if actual_source == "annas":
            try:
                results = await self._get_annas().search(query, **kwargs)
                if results:
                    return results
                logger.info("Anna's Archive returned no results")
            except Exception as e:
                logger.warning(f"Anna's Archive search failed: {e}")

            # Fallback to LibGen if enabled
            if self.config.fallback_enabled:
                logger.info("Falling back to LibGen")
                return await self._get_libgen().search(query, **kwargs)
            return []

        # Direct LibGen search
        return await self._get_libgen().search(query, **kwargs)

    async def get_download_url(
        self,
        md5: str,
        source: SourceSelection = "auto",
    ) -> DownloadResult:
        """Get download URL with automatic fallback on quota exhaustion.

        Args:
            md5: Book MD5 hash
            source: Source selection ('auto', 'annas', or 'libgen')

        Returns:
            DownloadResult with URL and quota info (if Anna's)

        Raises:
            QuotaExhaustedError: If Anna's quota exhausted and fallback disabled
            ValueError: If book not found in any source
        """
        actual_source = self._determine_source(source)

        if actual_source == "annas":
            annas = self._get_annas()
            if annas:
                try:
                    result = await annas.get_download_url(md5)
                    # Check quota - if 0 left, raise for fallback
                    if result.quota_info and result.quota_info.downloads_left == 0:
                        raise QuotaExhaustedError("Anna's Archive quota exhausted")
                    return result
                except QuotaExhaustedError:
                    if self.config.fallback_enabled:
                        logger.warning(
                            "Anna's Archive quota exhausted, falling back to LibGen"
                        )
                        return await self._get_libgen().get_download_url(md5)
                    raise
                except Exception as e:
                    if self.config.fallback_enabled:
                        logger.warning(
                            f"Anna's Archive download failed: {e}, falling back to LibGen"
                        )
                        return await self._get_libgen().get_download_url(md5)
                    raise

        return await self._get_libgen().get_download_url(md5)

    async def close(self) -> None:
        """Clean up all adapter resources."""
        if self._annas:
            await self._annas.close()
            self._annas = None
        if self._libgen:
            await self._libgen.close()
            self._libgen = None
