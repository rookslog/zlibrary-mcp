"""TDD tests for LibGen adapter.

Tests verify LibgenAdapter implements SourceAdapter interface correctly,
returning UnifiedBookResult with source=LIBGEN and wrapping sync
LibgenSearch calls in asyncio.to_thread().
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from lib.sources.config import SourceConfig
from lib.sources.errors import AllSourcesFailedError, ProviderUnreachableError
from lib.sources.models import SourceType

pytestmark = pytest.mark.unit


class TestLibgenAdapterSearch:
    """Tests for LibgenAdapter.search() method."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.fixture
    def mock_book(self):
        """Create a mock book result from LibgenSearch."""
        book = MagicMock()
        book.md5 = "abc123def45600000000000000000000"
        book.title = "Python Programming"
        book.author = "John Doe"
        book.year = "2023"
        book.extension = "pdf"
        book.size = "5 MB"
        book.tor_download_link = "https://example.com/download/abc123def456"
        book.id = "12345"
        book.language = "English"
        book.pages = "500"
        return book

    @pytest.mark.asyncio
    async def test_search_returns_results(self, config, mock_book):
        """Search should return list of UnifiedBookResult with source=LIBGEN."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            results = await adapter.search("python")

            assert len(results) == 1
            assert results[0].md5 == "abc123def45600000000000000000000"
            assert results[0].title == "Python Programming"
            assert results[0].author == "John Doe"
            assert results[0].source == SourceType.LIBGEN
            # Search deliberately carries no download URL: the only one it could
            # offer is a .onion needing Tor, and a clearnet key expires in under
            # 2.5h. Callers resolve on demand via get_download_url.
            assert results[0].download_url == ""

    @pytest.mark.asyncio
    async def test_search_empty_returns_empty_list(self, config):
        """Search with no results should return empty list."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = []
            mock_search_class.return_value = mock_instance

            results = await adapter.search("nonexistent_book_xyz123")

            assert results == []

    @pytest.mark.asyncio
    async def test_search_runs_under_a_wall_clock_budget(self, config, mock_book):
        """Search must go through run_bounded, not asyncio.to_thread.

        libgen_api_enhanced calls requests.get with no timeout, and
        asyncio.to_thread's workers are joined at interpreter exit — together
        that kept whole bridge processes alive for hours after their MCP call
        was gone. The blocking call has to run somewhere it can be abandoned.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            with patch("lib.sources.libgen.run_bounded") as mock_run_bounded:

                async def call_func(func, timeout, **kwargs):
                    call_func.timeout = timeout
                    return func()

                mock_run_bounded.side_effect = call_func

                await adapter.search("python")

                mock_run_bounded.assert_called_once()
                assert call_func.timeout == config.total_timeout

    @pytest.mark.asyncio
    async def test_search_does_not_use_asyncio_to_thread(self, config, mock_book):
        """asyncio.to_thread must not reappear in the search path.

        Regression guard for the orphaned-process bug: to_thread's pool threads
        are non-daemon, so an abandoned request keeps the interpreter alive.
        """
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = [mock_book]
            mock_search_class.return_value = mock_instance

            with patch("asyncio.to_thread") as mock_to_thread:
                await adapter.search("python")

            mock_to_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_handles_missing_attributes(self, config):
        """Search should handle books with missing attributes gracefully."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        # Book with only an md5 (an md5-less book is dropped, not defaulted —
        # see TestMd5lessRowsFiltered); every OTHER missing attribute still
        # maps to an empty default rather than raising.
        minimal_book = MagicMock(spec=["md5"])
        minimal_book.md5 = "a" * 32

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = [minimal_book]
            mock_search_class.return_value = mock_instance

            results = await adapter.search("python")

            # Should not raise, should use empty defaults
            assert len(results) == 1
            assert results[0].md5 == "a" * 32
            assert results[0].title == ""
            assert results[0].source == SourceType.LIBGEN


MD5 = "abc123def456"
ADS_PAGE_WITH_KEY = (
    "<html><body><table><tr><td>"
    f'<a href="/get.php?md5={MD5}&key=TESTKEY123">GET</a>'
    "</td></tr></table></body></html>"
)
ADS_PAGE_NO_KEY = "<html><body><p>No download links here</p></body></html>"
PDF_BYTES = b"%PDF-1.6" + b"\x00" * 2040


class TestLibgenAdapterDownload:
    """Tests for LibgenAdapter.get_download_url().

    The adapter resolves downloads over HTTP against ads.php rather than
    through LibgenSearch, so these mock httpx transport — mocking LibgenSearch
    would leave the requests hitting the live service.
    """

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.fixture
    def adapter(self, config):
        """Adapter with rate limiting neutralised so tests do not sleep."""
        from lib.sources.libgen import LibgenAdapter

        instance = LibgenAdapter(config)
        instance.MIN_REQUEST_INTERVAL = 0
        return instance

    @staticmethod
    def _patched_client(handler):
        """Patch httpx.AsyncClient so the adapter's own client uses `handler`."""
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        return patch("lib.sources.libgen.httpx.AsyncClient", factory)

    @pytest.mark.asyncio
    async def test_resolves_key_from_ads_page(self, adapter):
        """A GET anchor's key becomes the get.php download URL."""

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url == f"https://libgen.li/get.php?md5={MD5}&key=TESTKEY123"
        assert result.source == SourceType.LIBGEN
        assert result.quota_info is None  # LibGen has no quota

    @pytest.mark.asyncio
    async def test_candidate_iteration_resumes_at_the_next_unique_mirror(self, adapter):
        """Restarting the mirror walk would retry li after its full transfer failed."""
        ads_hosts = []

        def handler(request):
            if "ads.php" in request.url.path:
                ads_hosts.append(request.url.host)
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            candidates = adapter.iter_download_candidates(MD5)
            first = await anext(candidates)
            second = await anext(candidates)
            await candidates.aclose()

        assert first.url.startswith("https://libgen.li/")
        assert second.url.startswith("https://libgen.vg/")
        assert ads_hosts == ["libgen.li", "libgen.vg"]

    @pytest.mark.asyncio
    async def test_falls_over_when_a_mirror_errors(self, adapter):
        """A mirror that fails at the network level is skipped, not fatal."""
        seen = []

        def handler(request):
            host = request.url.host
            seen.append(host)
            if host == "libgen.li":
                raise httpx.ConnectError("TLS failure", request=request)
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")
        assert "libgen.li" in seen

    @pytest.mark.asyncio
    async def test_falls_over_when_mirror_resolves_but_cdn_is_dead(self, adapter):
        """Resolving a key is not evidence the CDN behind it can serve.

        Regression guard for the 2026-08-10 measurement: libgen.li handed out
        a valid key while its CDN node failed TLS.
        """

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if request.url.host == "libgen.li":
                return httpx.Response(
                    200,
                    text="<html>error</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")

    @pytest.mark.asyncio
    async def test_range_ignoring_probe_stops_after_the_inspection_window(
        self, adapter
    ):
        """A 200 probe must not buffer the complete book before accepting it."""

        class LargeBookStream(httpx.AsyncByteStream):
            def __init__(self):
                self.chunks_read = 0
                self.closed = False

            async def __aiter__(self):
                for index in range(64):
                    self.chunks_read += 1
                    prefix = b"%PDF" if index == 0 else b"xxxx"
                    yield prefix + (b"x" * 1020)

            async def aclose(self):
                self.closed = True

        body = LargeBookStream()

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            return httpx.Response(
                200,
                stream=body,
                headers={"content-type": "application/pdf"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.li/")
        assert body.chunks_read <= 3
        assert body.closed is True

    @pytest.mark.asyncio
    async def test_redirected_http_error_retains_cdn_host_and_closes_stream(
        self, adapter
    ):
        """An HTTP response from a redirected CDN is typed and closed early."""

        class ErrorStream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                yield b"upstream unavailable"

            async def aclose(self):
                self.closed = True

        streams = []

        def handler(request):
            host = request.url.host
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if host.startswith("libgen."):
                mirror = host.rsplit(".", 1)[-1]
                return httpx.Response(
                    307,
                    headers={"location": f"https://cdn-{mirror}.booksdl.test/book.pdf"},
                )
            stream = ErrorStream()
            streams.append(stream)
            return httpx.Response(
                503,
                stream=stream,
                headers={"content-type": "text/plain"},
            )

        with self._patched_client(handler):
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.get_download_url(MD5)

        assert [failure.host for failure in excinfo.value.failures] == [
            "cdn-li.booksdl.test",
            "cdn-vg.booksdl.test",
            "cdn-la.booksdl.test",
        ]
        assert {failure.reason for failure in excinfo.value.failures} == {"http_error"}
        assert len(streams) == 3
        assert all(stream.closed for stream in streams)

    @pytest.mark.asyncio
    async def test_redirected_transport_failure_retains_cdn_host(self, adapter):
        """A redirect target, not its mirror origin, owns transport failure."""

        def handler(request):
            host = request.url.host
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if host.startswith("libgen."):
                mirror = host.rsplit(".", 1)[-1]
                return httpx.Response(
                    307,
                    headers={"location": f"https://cdn-{mirror}.booksdl.test/book.pdf"},
                )
            raise httpx.ConnectTimeout("redirected CDN stalled", request=request)

        with self._patched_client(handler):
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.get_download_url(MD5)

        assert [failure.host for failure in excinfo.value.failures] == [
            "cdn-li.booksdl.test",
            "cdn-vg.booksdl.test",
            "cdn-la.booksdl.test",
        ]
        assert {failure.reason for failure in excinfo.value.failures} == {
            "connect_timeout"
        }

    @pytest.mark.asyncio
    async def test_cdn_transport_failure_retains_its_host_and_reason(self, adapter):
        """A failed byte probe is transport evidence, not a protocol response."""

        def handler(request):
            if "ads.php" in request.url.path:
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            raise httpx.ConnectTimeout("cdn stalled", request=request)

        with self._patched_client(handler):
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.get_download_url(MD5)

        assert [failure.host for failure in excinfo.value.failures] == [
            "libgen.li",
            "libgen.vg",
            "libgen.la",
        ]
        assert {failure.reason for failure in excinfo.value.failures} == {
            "connect_timeout"
        }

    @pytest.mark.asyncio
    async def test_expired_key_bounce_is_treated_as_failure(self, adapter):
        """An expired key 307s back to ads.php rather than erroring."""

        class BounceStream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                yield ADS_PAGE_WITH_KEY.encode()

            async def aclose(self):
                self.closed = True

        bounce_stream = BounceStream()
        ads_visits = 0

        def handler(request):
            nonlocal ads_visits
            if "ads.php" in request.url.path:
                ads_visits += 1
                if ads_visits == 2:
                    return httpx.Response(
                        200,
                        stream=bounce_stream,
                        headers={"content-type": "text/html"},
                    )
                return httpx.Response(200, text=ADS_PAGE_WITH_KEY)
            if request.url.host == "libgen.li":
                return httpx.Response(
                    307, headers={"location": f"https://libgen.li/ads.php?md5={MD5}"}
                )
            return httpx.Response(
                206,
                content=PDF_BYTES,
                headers={"content-type": "application/octet-stream"},
            )

        with self._patched_client(handler):
            result = await adapter.get_download_url(MD5)

        assert result.url.startswith("https://libgen.vg/")
        assert bounce_stream.closed is True

    @pytest.mark.asyncio
    async def test_raises_when_no_mirror_yields_a_key(self, adapter):
        """DOM drift on every mirror surfaces as an error naming the attempts."""

        def handler(request):
            return httpx.Response(200, text=ADS_PAGE_NO_KEY)

        with self._patched_client(handler):
            with pytest.raises(AllSourcesFailedError, match="every source"):
                await adapter.get_download_url(MD5)

    @pytest.mark.asyncio
    async def test_retains_each_unreachable_mirror_as_a_structured_failure(
        self, adapter
    ):
        """A failed mirror walk must keep the host and stable reason per attempt."""

        async def unreachable(mirror):
            host = f"libgen.{mirror}"
            raise ProviderUnreachableError("libgen", host, reason="connect_timeout")

        with patch.object(adapter, "_preflight", side_effect=unreachable):
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.get_download_url(MD5)

        assert [failure.host for failure in excinfo.value.failures] == [
            "libgen.li",
            "libgen.vg",
            "libgen.la",
        ]
        assert {failure.reason for failure in excinfo.value.failures} == {
            "connect_timeout"
        }

    @pytest.mark.asyncio
    async def test_tries_configured_mirror_first_without_duplicates(self, adapter):
        """Mirror order starts at the configured one and repeats none."""
        adapter.mirror = "vg"

        assert adapter._mirror_candidates() == ["vg", "li", "la"]

    @pytest.mark.asyncio
    async def test_resolution_walk_obeys_each_mirrors_total_deadline(self, adapter):
        """A trickling CDN must not defer failure to the outer bridge budget."""
        adapter.config.total_timeout = 0.02

        async def trickle(*_args, **_kwargs):
            await asyncio.sleep(30)

        with (
            patch.object(adapter, "_preflight", new=AsyncMock(return_value=None)),
            patch.object(adapter, "_rate_limit", new=AsyncMock(return_value=None)),
            patch.object(
                adapter, "_resolve_key", new=AsyncMock(return_value=("KEY", ""))
            ),
            patch.object(adapter, "_serves_bytes", side_effect=trickle),
        ):
            with pytest.raises(AllSourcesFailedError, match="every source"):
                await asyncio.wait_for(adapter.get_download_url(MD5), timeout=0.2)


class TestLibgenAdapterRateLimiting:
    """Tests for LibgenAdapter rate limiting."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.mark.asyncio
    async def test_rate_limiting_enforced(self, config):
        """Second request should wait for MIN_REQUEST_INTERVAL."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_instance = MagicMock()
            mock_instance.search_default.return_value = []
            mock_search_class.return_value = mock_instance

            with patch("lib.sources.libgen.asyncio.sleep") as mock_sleep:
                # First request
                await adapter.search("python")

                # Force _last_request to be recent
                import time

                adapter._last_request = time.time()

                # Second request should trigger sleep
                await adapter.search("java")

                # Sleep should have been called
                mock_sleep.assert_called()


class TestLibgenAdapterInterface:
    """Tests verifying LibgenAdapter implements SourceAdapter interface."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    def test_implements_source_adapter(self, config):
        """LibgenAdapter should implement SourceAdapter ABC."""
        from lib.sources.base import SourceAdapter
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        assert isinstance(adapter, SourceAdapter)

    @pytest.mark.asyncio
    async def test_close_is_callable(self, config):
        """LibgenAdapter.close() should be callable without error."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        await adapter.close()  # Should not raise


class TestLibgenAdapterUserAgent:
    """The mirror UA-blocklists tool defaults (#124): python-requests' and
    python-httpx's default UAs get the default-nginx stub (HTTP 200, ~640
    bytes, no results table) while an identifying UA gets the real page.
    These tests pin the shim that carries the identifying UA into
    libgen-api-enhanced, which exposes no headers hook of its own.
    """

    def test_shim_is_installed_in_library_module(self):
        """libgen_api_enhanced.search_request must resolve `requests` to our shim."""
        from libgen_api_enhanced import search_request as lge_search_request

        from lib.sources import libgen as libgen_mod

        assert lge_search_request.requests is libgen_mod._search_requests

    def test_shim_get_sends_identifying_user_agent(self, monkeypatch):
        """Shim .get() must add USER_AGENT (and never the requests default)."""
        from lib.sources import libgen as libgen_mod

        captured = {}

        def fake_get(url, headers=None, **kwargs):
            captured["headers"] = headers
            response = MagicMock()
            response.status_code = 200
            response.text = '<table id="tablelibgen"></table>'
            return response

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        libgen_mod._search_requests.get("https://libgen.li/index.php")

        assert captured["headers"]["User-Agent"] == libgen_mod.USER_AGENT
        assert "python-requests" not in captured["headers"]["User-Agent"]

    def test_shim_preserves_caller_supplied_user_agent(self, monkeypatch):
        """An explicit UA from the library must not be overwritten."""
        from lib.sources import libgen as libgen_mod

        captured = {}

        def fake_get(url, headers=None, **kwargs):
            captured["headers"] = headers
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        libgen_mod._search_requests.get(
            "https://libgen.li/index.php", headers={"User-Agent": "custom"}
        )

        assert captured["headers"]["User-Agent"] == "custom"


STUB_PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>Welcome to nginx!</title>"
    "</head><body><h1>Welcome to nginx!</h1></body></html>"
)


class TestLibgenAdapterParseFailure:
    """Empty result vs unparseable page must be distinguishable (#124):
    a genuinely-empty search still renders the (empty) results table, so a
    page WITHOUT `tablelibgen` is a parse failure, never "no matches".

    #124 signalled this with a standalone `SourceParseError`. Folded into
    #106's taxonomy it is a typed per-mirror `ProviderResponseError`
    (`protocol_error`), which is strictly more useful: the mirror walk can
    route around one stubbed mirror, and only an all-mirror stub is fatal.
    """

    @pytest.fixture
    def config(self):
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @staticmethod
    def _stub_page():
        page = MagicMock()
        page.status_code = 200
        page.text = STUB_PAGE_HTML
        return page

    @pytest.mark.asyncio
    async def test_stub_page_on_every_mirror_raises_typed_parse_failure(self, config):
        """Zero results + no results table anywhere must raise, not return []."""
        from lib.sources import libgen as libgen_mod
        from lib.sources.errors import ProviderResponseError
        from lib.sources.libgen import LibgenAdapter, get_user_agent

        adapter = LibgenAdapter(config)
        adapter.MIN_REQUEST_INTERVAL = 0
        stub_page = self._stub_page()

        def fake_search_default(query):
            # Mirror reality: the library fetches through the shim (setting
            # last_response) and parses no table from the stub.
            libgen_mod._search_requests.last_response = stub_page
            return []

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.return_value.search_default.side_effect = (
                fake_search_default
            )
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.search("anything")

        # #124's message content survives the fold: title, status, byte count.
        assert "Welcome to nginx!" in str(excinfo.value)
        assert f"{len(STUB_PAGE_HTML)} bytes" in str(excinfo.value)
        assert "HTTP 200" in str(excinfo.value)

        # #141 sharpened the diagnosis rather than restating it. "no results
        # table" was true of the stub but described the symptom; the message
        # must now name the blocklisted UA and rule out the two wrong readings
        # a sweep actually made — that LibGen was down, or lacked the books.
        assert "blocklisted" in str(excinfo.value)
        assert "NOT an outage" in str(excinfo.value)
        assert "LIBGEN_USER_AGENT" in str(excinfo.value)
        assert get_user_agent() in str(excinfo.value)

        failures = excinfo.value.failures
        assert failures, "the stub must be recorded as a typed failure"
        assert all(isinstance(f, ProviderResponseError) for f in failures)
        assert all(f.reason == "protocol_error" for f in failures)
        # Attribution is per mirror, which the untyped SourceParseError lost.
        assert {f.host for f in failures} == {"libgen.li", "libgen.vg", "libgen.la"}

    @pytest.mark.asyncio
    async def test_stub_on_one_mirror_fails_over_to_a_working_mirror(self, config):
        """A stubbed mirror is not fatal — that is what the typed fold buys."""
        from lib.sources import libgen as libgen_mod
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        adapter.MIN_REQUEST_INTERVAL = 0
        stub_page = self._stub_page()

        good_page = MagicMock()
        good_page.status_code = 200
        good_page.text = '<html><table id="tablelibgen">...</table></html>'

        book = MagicMock()
        book.md5 = "abc123def45600000000000000000000"
        book.title = "Found On The Second Mirror"

        def search_for(mirror):
            def _search_default(query):
                if mirror == "li":
                    libgen_mod._search_requests.last_response = stub_page
                    return []
                libgen_mod._search_requests.last_response = good_page
                return [book]

            instance = MagicMock()
            instance.search_default.side_effect = _search_default
            return instance

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.side_effect = lambda mirror: search_for(mirror)
            results = await adapter.search("anything")

        assert len(results) == 1
        assert results[0].title == "Found On The Second Mirror"

    @pytest.mark.asyncio
    async def test_empty_table_returns_empty_list(self, config):
        """Zero results + a page WITH the results table is a real empty result."""
        from lib.sources import libgen as libgen_mod
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        empty_results_page = MagicMock()
        empty_results_page.status_code = 200
        empty_results_page.text = '<html><table id="tablelibgen"></table></html>'

        def fake_search_default(query):
            libgen_mod._search_requests.last_response = empty_results_page
            return []

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.return_value.search_default.side_effect = (
                fake_search_default
            )
            results = await adapter.search("xqzv nonexistent qqqzzz")

        assert results == []

    @pytest.mark.asyncio
    async def test_no_fetch_recorded_returns_empty_list(self, config):
        """If no page was fetched (fully mocked library), [] stays []."""
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.return_value.search_default.return_value = []
            results = await adapter.search("anything")

        assert results == []


class TestShimDefaultTimeout:
    """The shim must default a timeout on library calls that omit one — a
    mirror that accepts and never responds must not hold the search thread
    forever (Codex on #128)."""

    def _capture(self, monkeypatch):
        from lib.sources import libgen as libgen_mod

        captured = {}

        def fake_get(url, headers=None, **kwargs):
            captured.update(kwargs)
            return MagicMock(status_code=200, text='<table id="tablelibgen"></table>')

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        return libgen_mod, captured

    def test_default_timeout_applied(self, monkeypatch):
        libgen_mod, captured = self._capture(monkeypatch)
        libgen_mod._search_requests.get("https://libgen.li/index.php")
        # (connect, read) from the defaults in lib/sources/config.py
        assert captured["timeout"] == (10.0, 30.0)

    def test_configured_timeout_respected(self, monkeypatch):
        """An operator who raises the read budget must actually get it: the
        hard-coded 30s defeated BOOK_SOURCE_READ_TIMEOUT tuning (Codex #133)."""
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", "20")
        monkeypatch.setenv("BOOK_SOURCE_READ_TIMEOUT", "120")
        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "200")
        libgen_mod, captured = self._capture(monkeypatch)
        libgen_mod._search_requests.get("https://libgen.li/index.php")
        assert captured["timeout"] == (20.0, 120.0)

    def test_default_never_exceeds_total_budget(self, monkeypatch):
        """The call runs under run_bounded(config.total_timeout); a per-request
        budget above that could never be reached."""
        monkeypatch.setenv("BOOK_SOURCE_READ_TIMEOUT", "300")
        monkeypatch.setenv("BOOK_SOURCE_CONNECT_TIMEOUT", "300")
        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "12")
        libgen_mod, captured = self._capture(monkeypatch)
        libgen_mod._search_requests.get("https://libgen.li/index.php")
        assert captured["timeout"] == (12.0, 12.0)

    def test_unreadable_config_still_bounds_the_call(self, monkeypatch):
        """A config fault must degrade to a finite timeout, never to none."""
        from lib.sources import config as config_mod

        libgen_mod, captured = self._capture(monkeypatch)

        def boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(config_mod, "get_source_config", boom)
        libgen_mod._search_requests.get("https://libgen.li/index.php")
        assert captured["timeout"] == libgen_mod.SHIM_FALLBACK_TIMEOUT
        assert captured["timeout"] == 30.0

    def test_caller_timeout_preserved(self, monkeypatch):
        from lib.sources import libgen as libgen_mod

        captured = {}

        def fake_get(url, headers=None, **kwargs):
            captured.update(kwargs)
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        libgen_mod._search_requests.get("https://libgen.li/index.php", timeout=5)
        assert captured["timeout"] == 5


class TestMd5lessRowsFiltered:
    """Column-shifted journal-article rows (#132) arrive with an empty md5
    and can never be downloaded; _to_unified drops them so #134's wider
    search_default results stay usable."""

    @pytest.fixture
    def config(self):
        return SourceConfig(
            libgen_mirror="li",
            default_source="libgen",
            fallback_enabled=False,
        )

    @pytest.fixture
    def mock_book(self):
        book = MagicMock()
        book.md5 = "abc123def45600000000000000000000"
        book.title = "Python Programming"
        book.author = "John Doe"
        book.year = "2023"
        book.extension = "pdf"
        book.size = "5 MB"
        book.id = "12345"
        book.language = "English"
        book.pages = "500"
        return book

    def test_md5less_rows_dropped_books_kept(self, config, mock_book):
        from lib.sources.libgen import LibgenAdapter

        shifted = MagicMock()
        shifted.md5 = ""
        shifted.title = ""
        shifted.author = "Journal of X pp.193-224 Some citation aa 62395571"
        shifted.year = ""
        shifted.extension = "389 kB"
        shifted.size = "0"
        shifted.id = "68653542"
        shifted.language = "2003"
        shifted.pages = "English"

        adapter = LibgenAdapter(config)
        unified = adapter._to_unified([mock_book, shifted])

        assert len(unified) == 1
        assert unified[0].md5 == mock_book.md5


class TestLibgenBlockedUserAgent:
    """A blocklisted UA must be named, never reported as drift or emptiness.

    LibGen refuses a blocklisted User-Agent with HTTP 200 and nginx's default
    page. That is the same shape as an empty catalogue on the search path and
    the same shape as markup drift on the download path, so without explicit
    classification the operator is told the wrong thing twice. #141 records
    what that cost: a sweep read "no results table" as LibGen being down and
    concluded the catalogue lacked books it in fact had.
    """

    STUB = (
        "<html><head><title>Welcome to nginx!</title></head>"
        "<body><h1>Welcome to nginx!</h1></body></html>"
    )

    @pytest.fixture
    def adapter(self):
        from lib.sources.libgen import LibgenAdapter

        return LibgenAdapter(SourceConfig(libgen_mirror="li", default_source="libgen"))

    def test_user_agent_defaults_to_the_admitted_browser_string(self, monkeypatch):
        from lib.sources.config import DEFAULT_LIBGEN_USER_AGENT
        from lib.sources.libgen import (
            get_user_agent,
        )

        monkeypatch.delenv("LIBGEN_USER_AGENT", raising=False)
        assert get_user_agent() == DEFAULT_LIBGEN_USER_AGENT

    def test_user_agent_honours_the_operator_override(self, monkeypatch):
        from lib.sources.libgen import (
            get_user_agent,
        )

        monkeypatch.setenv("LIBGEN_USER_AGENT", "my-crawler/2.0")
        assert get_user_agent() == "my-crawler/2.0"

    def test_blank_override_falls_back_rather_than_sending_nothing(self, monkeypatch):
        """An empty env var must not become an empty UA header."""
        from lib.sources.config import DEFAULT_LIBGEN_USER_AGENT
        from lib.sources.libgen import (
            get_user_agent,
        )

        monkeypatch.setenv("LIBGEN_USER_AGENT", "   ")
        assert get_user_agent() == DEFAULT_LIBGEN_USER_AGENT

    def test_override_is_read_per_request_not_pinned_at_import(self, monkeypatch):
        """The override exists to answer the next widening without a release."""
        from lib.sources.libgen import (
            get_user_agent,
        )

        monkeypatch.setenv("LIBGEN_USER_AGENT", "first/1.0")
        assert get_user_agent() == "first/1.0"
        monkeypatch.setenv("LIBGEN_USER_AGENT", "second/2.0")
        assert get_user_agent() == "second/2.0"

    def test_nginx_stub_recognised_regardless_of_length(self):
        """Matched on the title: byte count is incidental and vhost-specific."""
        from lib.sources.libgen import (
            _nginx_stub,
        )

        assert _nginx_stub(self.STUB)
        assert _nginx_stub(self.STUB + "x" * 5000)
        assert not _nginx_stub("<html><title>Library Genesis</title></html>")

    def test_search_stub_is_reported_as_a_blocked_ua(self, monkeypatch):
        from lib.sources.libgen import (
            _unparseable_search_page,
        )

        monkeypatch.setenv("LIBGEN_USER_AGENT", "blocked-agent/1.0")
        page = MagicMock(text=self.STUB, status_code=200)
        detail = _unparseable_search_page(page)
        assert "blocked-agent/1.0" in detail
        assert "blocklisted" in detail
        assert "NOT an outage" in detail
        assert "LIBGEN_USER_AGENT" in detail

    def test_search_non_stub_parse_failure_keeps_its_own_wording(self):
        """A redesign is not a block; the two must stay distinguishable."""
        from lib.sources.libgen import (
            _unparseable_search_page,
        )

        page = MagicMock(
            text="<html><title>Library Genesis</title><body>redesigned</body></html>",
            status_code=200,
        )
        detail = _unparseable_search_page(page)
        assert "parse failure, not an empty result" in detail
        assert "blocklisted" not in detail

    def test_genuinely_empty_search_is_not_a_failure(self):
        """An empty catalogue hit still renders the table, and must pass through."""
        from lib.sources.libgen import (
            _unparseable_search_page,
        )

        page = MagicMock(text="<table id='tablelibgen'></table>", status_code=200)
        assert _unparseable_search_page(page) is None

    @pytest.mark.asyncio
    async def test_ads_php_stub_is_reported_as_a_blocked_ua(self, monkeypatch):
        """The detail names the UA the adapter actually sent.

        Supplied through the adapter's own `SourceConfig` rather than the
        environment: since #146 an adapter honours the config it was
        constructed with, so a later env change is deliberately NOT what this
        request used, and naming the env value would misreport the wire.
        """
        from lib.sources.libgen import LibgenAdapter

        monkeypatch.setenv("LIBGEN_USER_AGENT", "not-what-this-adapter-uses/1.0")
        adapter = LibgenAdapter(
            SourceConfig(
                libgen_mirror="li",
                default_source="libgen",
                libgen_user_agent="blocked-agent/1.0",
            )
        )
        client = MagicMock()
        client.get = AsyncMock(
            return_value=MagicMock(text=self.STUB, raise_for_status=MagicMock())
        )
        key, detail = await adapter._resolve_key(client, "li", MD5)
        assert key is None
        assert "blocked-agent/1.0" in detail
        assert "not-what-this-adapter-uses/1.0" not in detail
        assert "blocklisted" in detail
        assert "DOM drift" not in detail

    @pytest.mark.asyncio
    async def test_ads_php_without_anchor_still_reports_dom_drift(self, adapter):
        """Real markup drift must not be relabelled as a UA block."""

        client = MagicMock()
        client.get = AsyncMock(
            return_value=MagicMock(
                text="<html><title>Library Genesis</title><a href='/x'>Other</a></html>",
                raise_for_status=MagicMock(),
            )
        )
        key, detail = await adapter._resolve_key(client, "li", MD5)
        assert key is None
        assert "DOM drift" in detail
        assert "blocklisted" not in detail

    @pytest.mark.asyncio
    async def test_ads_php_with_key_returns_it(self, adapter):
        client = MagicMock()
        client.get = AsyncMock(
            return_value=MagicMock(
                text=(
                    "<html><a href='get.php?md5=" + MD5 + "&key=ABC123'>GET</a></html>"
                ),
                raise_for_status=MagicMock(),
            )
        )
        key, detail = await adapter._resolve_key(client, "li", MD5)
        assert key == "ABC123"
        assert detail == ""


class TestUserAgentReachesEveryRequest:
    """The override must apply to every LibGen request, not most of them.

    Codex on #146 found the first cut honoured `LIBGEN_USER_AGENT` on search
    and key resolution while the actual file transfer still sent the
    compiled-in default. An override that covers every request except the one
    that moves the file is worse than none: the operator sets it, search
    starts working, and downloads keep failing for a reason the config appears
    to have addressed.
    """

    CUSTOM = "custom-agent/9.9"

    def test_supplied_config_wins_over_the_environment(self, monkeypatch):
        """An adapter's own SourceConfig is not overridden by ambient env."""
        from lib.sources.libgen import get_user_agent

        monkeypatch.setenv("LIBGEN_USER_AGENT", "env-agent/1.0")
        assert (
            get_user_agent(SourceConfig(libgen_user_agent=self.CUSTOM)) == self.CUSTOM
        )

    def test_blank_config_value_falls_back_rather_than_sending_nothing(self):
        from lib.sources.config import DEFAULT_LIBGEN_USER_AGENT
        from lib.sources.libgen import get_user_agent

        assert (
            get_user_agent(SourceConfig(libgen_user_agent=""))
            == DEFAULT_LIBGEN_USER_AGENT
        )

    def test_no_config_still_reads_the_environment(self, monkeypatch):
        """The module-level search shim has no config and must keep working."""
        from lib.sources.libgen import get_user_agent

        monkeypatch.setenv("LIBGEN_USER_AGENT", "env-agent/1.0")
        assert get_user_agent() == "env-agent/1.0"

    def test_search_sends_the_adapters_own_configured_agent(self, monkeypatch):
        """The shim is a module singleton; the adapter's UA must still reach it."""
        import lib.sources.libgen as libgen_mod

        seen = {}

        def fake_get(url, headers=None, **kwargs):
            seen["ua"] = (headers or {}).get("User-Agent")

            class _Resp:
                status_code = 200
                text = "<html><table class='tablelibgen'></table></html>"

            return _Resp()

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        monkeypatch.setenv("LIBGEN_USER_AGENT", "env-agent/1.0")

        adapter = libgen_mod.LibgenAdapter(
            SourceConfig(libgen_mirror="li", libgen_user_agent=self.CUSTOM)
        )
        libgen_mod._search_requests.user_agent = libgen_mod.get_user_agent(
            adapter.config
        )
        libgen_mod._search_requests.get("https://libgen.li/index.php")

        assert seen["ua"] == self.CUSTOM, (
            "the adapter's configured UA must reach the search shim, "
            "not the ambient environment"
        )

    def test_shim_falls_back_to_env_when_no_agent_was_set(self, monkeypatch):
        import lib.sources.libgen as libgen_mod

        seen = {}

        def fake_get(url, headers=None, **kwargs):
            seen["ua"] = (headers or {}).get("User-Agent")

            class _Resp:
                status_code = 200
                text = ""

            return _Resp()

        monkeypatch.setattr(libgen_mod.requests, "get", fake_get)
        monkeypatch.setenv("LIBGEN_USER_AGENT", "env-agent/1.0")
        libgen_mod._search_requests.user_agent = None
        libgen_mod._search_requests.get("https://libgen.li/index.php")

        assert seen["ua"] == "env-agent/1.0"


class TestNginxStubMatchesTitleOnly:
    """A real record mentioning nginx is not a block.

    The classifier's own docstring said "matched on the title"; the first cut
    matched the whole body. A LibGen record whose title or description carries
    the phrase would have had its perfectly good GET anchor discarded and been
    reported to the operator as a blocked UA (Codex on #146).
    """

    def test_real_page_mentioning_nginx_in_content_is_not_a_stub(self):
        from lib.sources.libgen import _nginx_stub

        page = (
            "<html><head><title>Nginx HTTP Server - Third Edition</title></head>"
            "<body><p>Welcome to nginx! and other greetings, chapter 2</p>"
            "<a href='/get.php?md5=abc&key=K'>GET</a></body></html>"
        )
        assert _nginx_stub(page) is False

    def test_actual_stub_is_still_recognised(self):
        from lib.sources.libgen import _nginx_stub

        assert (
            _nginx_stub(
                "<html><head><title>Welcome to nginx!</title></head><body></body></html>"
            )
            is True
        )

    def test_title_match_is_case_insensitive_and_length_independent(self):
        from lib.sources.libgen import _nginx_stub

        assert _nginx_stub("<TITLE>WELCOME TO NGINX!</TITLE>" + "x" * 50_000) is True


class TestBlankConfiguredUserAgentFallsBack:
    """A whitespace-only injected UA must not reach the wire.

    Codex on #146: `get_source_config()` strips `LIBGEN_USER_AGENT` before
    storing it, so the environment path already treated "   " as absent. A
    caller constructing `SourceConfig` directly bypassed that strip, and the
    truthiness test then let whitespace through as a valid header — earning the
    nginx stub, which is the exact failure the override exists to escape, while
    the configuration looked like it had addressed it.
    """

    def test_whitespace_only_config_value_yields_the_default(self):
        from lib.sources.config import DEFAULT_LIBGEN_USER_AGENT, SourceConfig
        from lib.sources.libgen import get_user_agent

        blank = SourceConfig(libgen_user_agent="   ")

        assert get_user_agent(blank) == DEFAULT_LIBGEN_USER_AGENT

    def test_a_real_value_is_still_stripped_not_discarded(self):
        from lib.sources.config import SourceConfig
        from lib.sources.libgen import get_user_agent

        padded = SourceConfig(libgen_user_agent="  operator/9.9  ")

        assert get_user_agent(padded) == "operator/9.9"

    def test_whitespace_only_environment_value_yields_the_default(self, monkeypatch):
        from lib.sources.config import DEFAULT_LIBGEN_USER_AGENT
        from lib.sources.libgen import get_user_agent

        monkeypatch.setenv("LIBGEN_USER_AGENT", "   ")

        assert get_user_agent() == DEFAULT_LIBGEN_USER_AGENT


class TestSearchUserAgentBlockIsTyped:
    """A UA-blocked search must be distinguishable by type, not by message.

    Codex on #146: the doctor catches the search adapter's failure as an
    ordinary optional failure, so a refusal every mirror served came back as
    WARN from `libgen:search` while `libgen:download` reported BLOCK on the
    identical refusal in the same run. Sniffing the message across that
    boundary is not a contract; a type is.
    """

    @pytest.fixture
    def config(self):
        return SourceConfig(
            libgen_mirror="li", default_source="libgen", fallback_enabled=False
        )

    @pytest.mark.asyncio
    async def test_nginx_stub_raises_the_blocked_subclass(self, config):
        from lib.sources import libgen as libgen_mod
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        adapter.MIN_REQUEST_INTERVAL = 0
        stub_page = MagicMock()
        stub_page.status_code = 200
        stub_page.text = STUB_PAGE_HTML

        def fake_search_default(query):
            libgen_mod._search_requests.last_response = stub_page
            return []

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.return_value.search_default.side_effect = (
                fake_search_default
            )
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.search("anything")

        assert excinfo.value.failures, "the stub must be recorded as a failure"
        assert all(
            isinstance(f, libgen_mod.LibgenUserAgentBlocked)
            for f in excinfo.value.failures
        ), (
            "a stub-serving mirror must produce LibgenUserAgentBlocked; the "
            "doctor has no other way to tell a UA block from upstream drift"
        )

    @pytest.mark.asyncio
    async def test_a_layout_change_does_not_claim_a_ua_block(self, config):
        """The subclass must be narrow, or it becomes a second false diagnosis.

        A page that simply lost its results table is DOM drift, and calling it
        a UA block would send an operator to change LIBGEN_USER_AGENT for a
        problem no header can fix.
        """
        from lib.sources import libgen as libgen_mod
        from lib.sources.libgen import LibgenAdapter

        adapter = LibgenAdapter(config)
        adapter.MIN_REQUEST_INTERVAL = 0
        redesigned = MagicMock()
        redesigned.status_code = 200
        redesigned.text = "<html><title>Library Genesis</title><main></main></html>"

        def fake_search_default(query):
            libgen_mod._search_requests.last_response = redesigned
            return []

        with patch("lib.sources.libgen.LibgenSearch") as mock_search_class:
            mock_search_class.return_value.search_default.side_effect = (
                fake_search_default
            )
            with pytest.raises(AllSourcesFailedError) as excinfo:
                await adapter.search("anything")

        assert not any(
            isinstance(f, libgen_mod.LibgenUserAgentBlocked)
            for f in excinfo.value.failures
        )

    def test_the_blocked_subclass_is_still_a_provider_response_error(self):
        from lib.sources.errors import ProviderResponseError
        from lib.sources.libgen import LibgenUserAgentBlocked

        assert issubclass(LibgenUserAgentBlocked, ProviderResponseError), (
            "existing handlers catch ProviderResponseError; narrowing the "
            "hierarchy here would silently change failover behaviour"
        )
