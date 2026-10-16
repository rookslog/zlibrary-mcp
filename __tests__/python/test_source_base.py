"""
Tests for lib/sources/base.py — SourceAdapter abstract base class.

Verifies the ABC contract: concrete subclasses must implement search,
get_download_url, and close. Also tests that incomplete implementations
raise TypeError.
"""

import pytest
from lib.sources.base import SourceAdapter
from lib.sources.models import DownloadResult, SourceType, UnifiedBookResult

pytestmark = pytest.mark.unit


class TestSourceAdapterABC:
    """Tests for the abstract base class contract."""

    def test_cannot_instantiate_directly(self):
        """SourceAdapter is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            SourceAdapter()

    def test_incomplete_subclass_raises(self):
        """A subclass that doesn't implement all methods raises TypeError."""

        class IncompleteAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return []

            # Missing get_download_url and close

        with pytest.raises(TypeError):
            IncompleteAdapter()

    def test_partial_implementation_raises(self):
        """A subclass implementing only two of three methods raises TypeError."""

        class PartialAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return []

            async def get_download_url(self, md5, **kwargs):
                return DownloadResult(
                    url="http://example.com", source=SourceType.LIBGEN
                )

            # Missing close

        with pytest.raises(TypeError):
            PartialAdapter()

    def test_complete_subclass_instantiates(self):
        """A subclass implementing all abstract methods can be instantiated."""

        class CompleteAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return [
                    UnifiedBookResult(
                        md5="abc123",
                        title="Test Book",
                        source=SourceType.LIBGEN,
                    )
                ]

            async def get_download_url(self, md5, **kwargs):
                return DownloadResult(
                    url="http://example.com", source=SourceType.LIBGEN
                )

            async def close(self):
                pass

        adapter = CompleteAdapter()
        assert isinstance(adapter, SourceAdapter)

    @pytest.mark.asyncio
    async def test_complete_subclass_methods_work(self):
        """A complete subclass's methods are callable and return expected types."""

        class WorkingAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return [
                    UnifiedBookResult(
                        md5="abc",
                        title=query,
                        source=SourceType.ANNAS_ARCHIVE,
                    )
                ]

            async def get_download_url(self, md5, **kwargs):
                return DownloadResult(
                    url=f"http://dl.example.com/{md5}",
                    source=SourceType.ANNAS_ARCHIVE,
                )

            async def close(self):
                self.closed = True

        adapter = WorkingAdapter()

        results = await adapter.search("Hegel")
        assert len(results) == 1
        assert results[0].title == "Hegel"
        assert results[0].source == SourceType.ANNAS_ARCHIVE

        dl = await adapter.get_download_url("abc123")
        assert "abc123" in dl.url

        await adapter.close()
        assert adapter.closed is True

    def test_subclass_with_extra_methods(self):
        """Subclass can have additional methods beyond the ABC contract."""

        class ExtendedAdapter(SourceAdapter):
            async def search(self, query, **kwargs):
                return []

            async def get_download_url(self, md5, **kwargs):
                return DownloadResult(url="", source=SourceType.LIBGEN)

            async def close(self):
                pass

            def custom_method(self):
                return "custom"

        adapter = ExtendedAdapter()
        assert adapter.custom_method() == "custom"
