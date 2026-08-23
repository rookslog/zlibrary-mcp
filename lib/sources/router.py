"""Source router for multi-source book search with fallback logic.

Routes search and download requests to Anna's Archive (primary) or LibGen (fallback)
based on configuration and availability. Provides automatic fallback when quota is
exhausted or errors occur.

Key decisions:
- SOURCE-ANNAS-PRIMARY: Anna's Archive is primary source
- SOURCE-LIBGEN-FALLBACK: LibGen is fallback when Anna's quota exhausted or unavailable
"""

import logging
import inspect
from typing import AsyncIterator, List, Literal, Optional

from .annas import AnnasArchiveAdapter, QuotaExhaustedError
from .config import SourceConfig, get_source_config
from .errors import AllSourcesFailedError, SourceError
from .libgen import LibgenAdapter
from .models import DownloadResult, UnifiedBookResult
from .net import WalkDeadline

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

    Fallback is tied to source='auto'. An explicitly named provider is the
    whole request, so its failure is raised with the provider, host and reason
    named rather than papered over with another provider's results — and no
    caller ends up waiting on a provider it did not ask for.

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

    def _search_candidates(self, source: SourceSelection) -> List[SourceSelection]:
        """Ordered providers to try for a search.

        `auto` means "give me results from wherever", so it gets the full list
        and the router walks it. An EXPLICIT source is a single-element list:
        asking for Anna's and silently receiving LibGen results is the bug #74
        fixed for the empty-result case, and quietly rerouting on a network
        failure reintroduces it — worse, it is how a request tagged
        `source="annas"` ended up hanging inside LibGen's search (2026-08-11).

        Args:
            source: Requested source

        Returns:
            Provider names in the order they should be attempted
        """
        primary = self._determine_source(source)
        if source != "auto" or not self.config.fallback_enabled:
            return [primary]
        return [primary] + [s for s in ("annas", "libgen") if s != primary]

    def _adapter_for(self, name: SourceSelection):
        """Get the adapter for a provider name."""
        return self._get_annas() if name == "annas" else self._get_libgen()

    async def search(
        self,
        query: str,
        source: SourceSelection = "auto",
        **kwargs,
    ) -> List[UnifiedBookResult]:
        """Search for books, falling back between providers when `source=auto`.

        Args:
            query: Search query string
            source: Source selection ('auto', 'annas', or 'libgen')
            **kwargs: Additional arguments passed to adapters

        Returns:
            List of UnifiedBookResult with source field indicating origin.
            An empty list means every attempted provider answered and none had
            a match — never that a provider was unreachable, and never that
            only some of them answered.

        Raises:
            AllSourcesFailedError: If any candidate provider failed without a
                later one producing results. That includes the partial case —
                one provider answering empty while another is unreachable — so
                a missing provider is never silently reported as "no matches".
                For an explicit source the failure set names just that
                provider, so the caller still learns which one broke and why.
        """
        candidates = self._search_candidates(source)
        failures: List[SourceError] = []
        answered = False
        # ONE clock for the whole walk, created here and spent by every
        # provider and every mirror beneath them. The alternative — each
        # attempt bounded on its own — makes the walk's cost a product of two
        # counts that move with configuration, and #152 is what that product
        # being wrong looks like from the operator's side: a killed bridge
        # process and a generic timeout in place of attributed failures.
        deadline = WalkDeadline(self.config.walk_budget)

        for name in candidates:
            try:
                results = await self._adapter_for(name).search(
                    query, deadline=deadline, **kwargs
                )
            except AllSourcesFailedError as exc:
                # A provider that walks its own mirrors (LibGen) reports a set.
                failures.extend(exc.failures)
                logger.warning(f"{name} search failed: {exc}")
                continue
            except SourceError as exc:
                failures.append(exc)
                logger.warning(f"{name} search failed: {exc}")
                continue
            except Exception as exc:
                failures.append(
                    SourceError(name, detail=f"{type(exc).__name__}: {exc}")
                )
                logger.warning(f"{name} search failed: {exc}")
                continue

            answered = True
            if results:
                return results
            logger.info(f"{name} returned no results")

        # An empty list is only honest when EVERY attempted provider answered.
        # A partial walk — LibGen returns no matches, then Anna's fails DNS —
        # would otherwise be reported as "no such book", when the truth is that
        # the provider most likely to have it was never successfully searched.
        # `answered` alone is not the test; the absence of failures is.
        if answered and not failures:
            return []

        raise AllSourcesFailedError("search", failures)

    def _download_candidates(self, source: SourceSelection) -> List[SourceSelection]:
        """Ordered providers to try for a download.

        Narrower than `_search_candidates`: Anna's fast-download API needs
        ANNAS_SECRET_KEY, so without one it is not a usable download fallback
        even though its search is.

        Args:
            source: Requested source

        Returns:
            Provider names in the order they should be attempted
        """
        primary = self._determine_source(source)
        if source != "auto" or not self.config.fallback_enabled:
            return [primary]
        if primary == "annas":
            return ["annas", "libgen"]
        return ["libgen"] + (["annas"] if self.config.has_annas_key else [])

    async def get_download_url(
        self,
        md5: str,
        source: SourceSelection = "auto",
    ) -> DownloadResult:
        """Get download URL, falling back between providers when `source=auto`.

        Args:
            md5: Book MD5 hash
            source: Source selection ('auto', 'annas', or 'libgen')

        Returns:
            DownloadResult with URL and quota info (if Anna's)

        Raises:
            SourceError: If the only candidate's quota is exhausted
            AllSourcesFailedError: If every candidate provider failed
        """
        stream = self.iter_download_candidates(md5, source=source)
        try:
            try:
                return await anext(stream)
            except AllSourcesFailedError as exc:
                if (
                    len(exc.failures) == 1
                    and exc.failures[0].reason == "quota_exhausted"
                ):
                    raise exc.failures[0] from exc
                raise
        finally:
            await stream.aclose()

    async def iter_download_candidates(
        self,
        md5: str,
        source: SourceSelection = "auto",
    ) -> AsyncIterator[DownloadResult]:
        """Flatten candidate streams, crossing providers only for ``auto``."""
        candidates = self._download_candidates(source)
        failures: List[SourceError] = []
        deadline = WalkDeadline(self.config.walk_budget)

        for name in candidates:
            provider_stream = None
            try:
                adapter = self._adapter_for(name)
                candidate_method = getattr(adapter, "iter_download_candidates", None)
                if candidate_method and inspect.isasyncgenfunction(candidate_method):
                    provider_stream = candidate_method(md5, deadline=deadline)
                else:

                    async def single_candidate():
                        yield await adapter.get_download_url(md5, deadline=deadline)

                    provider_stream = single_candidate()

                async for result in provider_stream:
                    if result.quota_info and result.quota_info.downloads_left == 0:
                        raise QuotaExhaustedError(f"{name} quota exhausted")
                    yield result
            except QuotaExhaustedError as exc:
                failures.append(
                    SourceError(name, detail=str(exc), reason="quota_exhausted")
                )
                logger.warning(f"{name} quota exhausted for {md5}")
            except AllSourcesFailedError as exc:
                failures.extend(exc.failures)
                logger.warning(f"{name} download failed: {exc}")
            except SourceError as exc:
                failures.append(exc)
                logger.warning(f"{name} download failed: {exc}")
            except Exception as exc:
                failures.append(
                    SourceError(name, detail=f"{type(exc).__name__}: {exc}")
                )
                logger.warning(f"{name} download failed: {exc}")
            finally:
                if provider_stream is not None:
                    close = getattr(provider_stream, "aclose", None)
                    if close is not None:
                        await close()

        if failures:
            raise AllSourcesFailedError("download", failures)

    async def close(self) -> None:
        """Clean up all adapter resources."""
        if self._annas:
            await self._annas.close()
            self._annas = None
        if self._libgen:
            await self._libgen.close()
            self._libgen = None
