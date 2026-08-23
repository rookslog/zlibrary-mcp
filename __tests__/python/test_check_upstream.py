"""Tests for the upstream reachability probe.

The network calls themselves are not exercised here — they are the point of the
scheduled job. What is tested is the reporting contract the CI workflow depends
on: which failures are actionable, and the exact `$GITHUB_OUTPUT` encoding.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

MODULE_PATH = Path(__file__).parent.parent.parent / "scripts" / "check_upstream.py"


@pytest.fixture(scope="module")
def check_upstream():
    spec = importlib.util.spec_from_file_location("check_upstream", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # The module uses `from __future__ import annotations` with @dataclass, and
    # dataclasses resolves those string annotations via sys.modules — so the module
    # must be registered before exec_module, not after.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[spec.name]
        raise
    yield module
    del sys.modules[spec.name]


def test_required_failure_is_actionable(check_upstream):
    """Z-Library failures must be reported as FAIL, not warnings."""
    result = check_upstream.ProbeResult(
        name="zlibrary:eapi/book/search", ok=False, detail="boom", required=True
    )
    assert result.symbol == "FAIL"


def test_optional_failure_is_a_warning(check_upstream):
    """LibGen/Anna's are fallbacks; their absence must not read as a hard failure."""
    result = check_upstream.ProbeResult(
        name="libgen:search", ok=False, detail="boom", required=False
    )
    assert result.symbol == "WARN"


def test_passing_probe_reports_ok(check_upstream):
    result = check_upstream.ProbeResult(name="x", ok=True, detail="fine")
    assert result.symbol == "OK"


def test_render_summarises_required_and_optional_failures(check_upstream):
    results = [
        check_upstream.ProbeResult("a", True, "fine"),
        check_upstream.ProbeResult("b", False, "broken", required=True),
        check_upstream.ProbeResult("c", False, "broken", required=False),
    ]
    report = check_upstream.render(results)
    assert "1 passing, 1 required failing, 1 optional failing" in report
    assert "FAIL" in report and "WARN" in report and "OK" in report


def test_github_output_uses_heredoc_for_multiline_report(
    check_upstream, tmp_path, monkeypatch
):
    """A bare `report=<multi-line>` assignment breaks the workflow parser."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    check_upstream.emit_github_output("line one\nline two", failed=True)

    written = out.read_text(encoding="utf-8")
    assert "failed=true\n" in written
    assert "report<<PROBE_EOF\n" in written
    assert "line one\nline two\n" in written
    assert written.rstrip().endswith("PROBE_EOF")


def test_github_output_is_a_noop_outside_ci(check_upstream, monkeypatch):
    """Running `npm run doctor` locally must not fail for lack of GITHUB_OUTPUT."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    check_upstream.emit_github_output("anything", failed=False)  # must not raise


def test_probe_targets_match_runtime_defaults(check_upstream):
    """The probe is only useful if it checks what the server actually contacts.

    The Anna's/LibGen targets are derived from lib/sources/config.py at import
    time (get_source_config applies the env overrides), so the probe cannot
    drift from the runtime adapters again.
    """
    from lib.sources.config import get_source_config

    from zlibrary.eapi import DEFAULT_EAPI_DOMAINS

    runtime = get_source_config()
    # The runtime resolves a domain from ZLIBRARY_EAPI_DOMAIN or (by probing)
    # DEFAULT_EAPI_DOMAINS; the doctor mirrors that by importing the same list.
    assert check_upstream.ZLIB_DOMAIN == os.environ.get(
        "ZLIBRARY_EAPI_DOMAIN", DEFAULT_EAPI_DOMAINS[0]
    )
    assert check_upstream.ANNAS_BASE_URL == runtime.annas_base_url
    # LibgenAdapter passes the mirror suffix to LibgenSearch, which builds
    # https://libgen.{suffix}/ — the probe must target that same host (it
    # previously hardcoded libgen.is while the runtime used libgen.li).
    assert check_upstream.LIBGEN_BASE_URL == f"https://libgen.{runtime.libgen_mirror}"


def test_blocked_probe_reports_block_not_fail(check_upstream):
    """An IP-level refusal is not drift; it must render as BLOCK, not FAIL."""
    result = check_upstream.ProbeResult(
        name="zlibrary:eapi/book/search",
        ok=False,
        detail="network-level block",
        required=True,
        blocked=True,
    )
    assert result.symbol == "BLOCK"


def test_blocked_probe_is_not_actionable(check_upstream):
    """Blocked probes must not set the failed flag that files the drift issue —
    otherwise CI's datacenter address keeps the rolling issue open forever."""
    results = [
        check_upstream.ProbeResult("a", True, "fine"),
        check_upstream.ProbeResult("b", False, "walled", required=True, blocked=True),
    ]
    assert check_upstream.actionable_failures(results) == []
    results.append(check_upstream.ProbeResult("c", False, "drift", required=True))
    assert [r.name for r in check_upstream.actionable_failures(results)] == ["c"]


def test_render_counts_blocked_separately(check_upstream):
    results = [
        check_upstream.ProbeResult("a", True, "fine"),
        check_upstream.ProbeResult("b", False, "walled", required=True, blocked=True),
    ]
    report = check_upstream.render(results)
    assert "BLOCK" in report
    assert "1 passing, 0 required failing, 0 optional failing" in report
    assert "1 blocked (network-level, not drift)" in report


def test_block_detail_classifies_bare_403(check_upstream):
    """GitHub-hosted runners get a bare 403 with no DiamWall markers from every
    Z-Library domain; that must classify as a block, with the caveat that only
    a residential probe can distinguish IP blocking from a global wall."""
    detail = check_upstream._block_detail(httpx.Response(403, text="Forbidden"))
    assert detail is not None
    assert "network-level block" in detail
    assert "HTTP 403" in detail
    assert "not upstream drift" in detail


def test_block_detail_names_diamwall_when_present(check_upstream):
    body = '<html><script src="https://cdn.diamwall.com/x.js"></script></html>'
    detail = check_upstream._block_detail(httpx.Response(517, text=body))
    assert detail is not None and "DiamWall" in detail


def test_block_detail_passes_healthy_response(check_upstream):
    assert check_upstream._block_detail(httpx.Response(200, text='{"ok":1}')) is None


def test_github_output_carries_zlib_blocked_flag(check_upstream, tmp_path, monkeypatch):
    """The live-suite job gates on zlib_blocked to avoid logging in through a
    wall (spurious failure + burns the ~10/hour login rate limit)."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    check_upstream.emit_github_output("report", failed=False, zlib_blocked=True)

    written = out.read_text(encoding="utf-8")
    assert "failed=false\n" in written
    assert "zlib_blocked=true\n" in written


ADS_PAGE = (
    '<html><body><table><tr><td><a href="get.php?md5={md5}&amp;key=GST1V9KIA7FWM2JQ">'
    "<h2>GET</h2></a></td></tr></table></body></html>"
)


@pytest.fixture
def libgen_download(check_upstream, monkeypatch):
    """Drive probe_libgen_download over a MockTransport — no network, no sleeps.

    Returns a callable taking a `handler(httpx.Request) -> httpx.Response` and
    running the probe against it.
    """
    # The probe spaces its real requests >= 2s apart; that spacing is not what
    # these tests are about, so it is zeroed rather than waited out.
    monkeypatch.setattr(check_upstream, "LIBGEN_PROBE_DELAY_SECONDS", 0)

    def run(handler, mirrors=("li", "vg")):
        monkeypatch.setattr(check_upstream, "LIBGEN_MIRROR_CANDIDATES", tuple(mirrors))

        async def _go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ) as client:
                return await check_upstream.probe_libgen_download(client)

        return asyncio.run(_go())

    return run


def _pdf_response(request):
    return httpx.Response(
        206,
        headers={"content-type": "application/pdf"},
        content=b"%PDF-1.6\n" + b"x" * 128,
        request=request,
    )


def test_libgen_download_probe_reports_first_working_mirror(
    check_upstream, libgen_download
):
    """The happy path: ads.php yields a key, get.php redirects to a CDN node,
    and the CDN hands back real PDF bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5),
            )
        if request.url.host == "libgen.li" and request.url.path == "/get.php":
            return httpx.Response(
                307, headers={"location": "https://cdn3.booksdl.lc/get.php?x=1"}
            )
        return _pdf_response(request)

    result = libgen_download(handler)
    assert result.ok is True
    assert result.symbol == "OK"
    assert result.required is False
    # The detail must name both the mirror and the CDN node it reached — the
    # two hops that fail independently.
    assert "li:" in result.detail
    assert "cdn3.booksdl.lc" in result.detail
    assert "PDF" in result.detail


def test_libgen_download_probe_requests_only_a_small_range(
    check_upstream, libgen_download
):
    """The probe must not download a whole book to prove downloads work."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        seen["range"] = request.headers.get("Range")
        return _pdf_response(request)

    assert libgen_download(handler).ok is True
    assert seen["range"] == f"bytes=0-{check_upstream.LIBGEN_PROBE_RANGE_BYTES - 1}"


def test_libgen_download_probe_detects_expired_key_bounce(
    check_upstream, libgen_download
):
    """An expired key does not error — get.php 307s back to the /ads.php
    interstitial and serves HTTP 200 HTML. Only the redirect target tells you."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get.php":
            return httpx.Response(
                307,
                headers={
                    "location": f"https://{request.url.host}/ads.php"
                    f"?md5={check_upstream.LIBGEN_PROBE_MD5}&key=STALE"
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5),
        )

    result = libgen_download(handler)
    assert result.ok is False
    assert result.symbol == "WARN"
    assert "/ads.php" in result.detail
    assert "expired" in result.detail
    # Both mirrors bounced, and neither was a network-level refusal.
    assert result.blocked is False
    assert result.detail.count("bounced back") == 2


def test_libgen_download_probe_rejects_html_served_as_the_file(
    check_upstream, libgen_download
):
    """HTTP 200 plus a page body is the reachability-vs-capability trap: the
    request 'succeeded' and delivered no file."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            text="<html><body>Download limit reached</body></html>",
            request=request,
        )

    result = libgen_download(handler)
    assert result.ok is False
    assert "served a page, not a file" in result.detail


def test_libgen_download_probe_rejects_non_pdf_bytes(check_upstream, libgen_download):
    """A non-HTML content-type is not enough; the magic bytes must be checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        return httpx.Response(
            206,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x00not-a-pdf",
            request=request,
        )

    result = libgen_download(handler)
    assert result.ok is False
    assert "not a PDF" in result.detail


def test_libgen_download_probe_falls_over_to_a_healthy_mirror(
    check_upstream, libgen_download
):
    """Mirrors hand off to different CDN nodes that fail independently, so a
    dead node on the default mirror must not condemn the whole source."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        if request.url.host == "libgen.li":
            raise httpx.ConnectError("wrong version number", request=request)
        return _pdf_response(request)

    result = libgen_download(handler)
    assert result.ok is True
    assert result.detail.startswith("vg:")


def test_libgen_download_probe_reports_every_mirror_when_all_fail(
    check_upstream, libgen_download
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        raise httpx.ConnectError("wrong version number", request=request)

    result = libgen_download(handler, mirrors=("li", "vg", "la"))
    assert result.ok is False
    assert result.detail.startswith("no mirror delivered a file")
    for mirror in ("li:", "vg:", "la:"):
        assert mirror in result.detail
    assert result.extra["mirrors_tried"] == ["li", "vg", "la"]


def test_libgen_download_probe_reports_dom_drift_when_key_is_gone(
    check_upstream, libgen_download
):
    """No key-bearing GET anchor means the ads.php scrape has drifted — a
    different failure from a dead CDN, and the report must say so."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html><body><a href='/foo'>GET</a></body></html>"
        )

    result = libgen_download(handler)
    assert result.ok is False
    assert "DOM drift" in result.detail


def test_libgen_download_probe_classifies_a_wall_as_blocked(
    check_upstream, libgen_download
):
    """An IP-level refusal from every mirror is not drift and must not read as
    a capability failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    result = libgen_download(handler)
    assert result.ok is False
    assert result.blocked is True
    assert result.symbol == "BLOCK"


def test_libgen_download_probe_is_not_blocked_when_a_mirror_answered(
    check_upstream, libgen_download
):
    """A mix of a wall and a real failure is a real failure: something answered
    and could not deliver."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "libgen.li":
            return httpx.Response(403, text="Forbidden")
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text=ADS_PAGE.format(md5=check_upstream.LIBGEN_PROBE_MD5)
            )
        raise httpx.ConnectError("wrong version number", request=request)

    result = libgen_download(handler)
    assert result.ok is False
    assert result.blocked is False
    assert result.symbol == "WARN"


def test_libgen_key_extraction_takes_the_key_bearing_anchor(check_upstream):
    get_url, key = check_upstream._extract_libgen_key(
        "https://libgen.vg/ads.php?md5=abc",
        ADS_PAGE.format(md5="abc"),
    )
    assert get_url == "https://libgen.vg/get.php?md5=abc&key=GST1V9KIA7FWM2JQ"
    assert key == "GST1V9KIA7FWM2JQ"


def test_libgen_key_extraction_returns_nothing_without_a_key(check_upstream):
    assert check_upstream._extract_libgen_key(
        "https://libgen.vg/ads.php?md5=abc",
        '<html><a href="get.php?md5=abc">GET</a></html>',
    ) == (None, None)


def test_libgen_download_probe_targets_the_configured_mirror_first(check_upstream):
    """Failover is only informative if the mirror the runtime actually uses is
    the one reported first."""
    from lib.sources.config import get_source_config

    assert (
        check_upstream.LIBGEN_MIRROR_CANDIDATES[0] == get_source_config().libgen_mirror
    )
    assert len(set(check_upstream.LIBGEN_MIRROR_CANDIDATES)) == len(
        check_upstream.LIBGEN_MIRROR_CANDIDATES
    )


def test_annas_probe_rejects_md5_links_without_an_extracted_title(check_upstream):
    """An empty cover link is reachable but cannot produce a book result title."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            text='<a href="/md5/deadbeef"><img alt="Cover"></a>',
        )

    async def probe() -> object:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await check_upstream.probe_annas(client)

    result = asyncio.run(probe())

    assert result.ok is False
    assert "no non-empty title" in result.detail


def test_annas_probe_accepts_and_reports_an_extracted_title(check_upstream):
    """A text-bearing result proves the field assertion can also succeed."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            text=(
                '<a href="/md5/deadbeef"><img alt="Cover"></a>'
                '<a href="/md5/deadbeef"> Critique of Pure Reason </a>'
            ),
        )

    async def probe() -> object:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await check_upstream.probe_annas(client)

    result = asyncio.run(probe())

    assert result.ok is True
    assert result.detail == "search extracted title: 'Critique of Pure Reason'"


def test_annas_parking_page_is_reported_as_parked(check_upstream):
    """A parked domain returns HTTP 200 with no /md5/ links; the report must say
    'parked', not just 'no links found', so the operator knows the domain is gone."""
    assert any("trellian" in m for m in check_upstream.PARKING_MARKERS)
    # The markers list is what probe_annas scans the body with; keep them
    # lowercase because the body is lowercased before matching.
    assert all(m == m.lower() for m in check_upstream.PARKING_MARKERS)


class TestLibgenSearchProbeUsesProductionAdapter:
    """The search probe must exercise the production adapter, not a
    hand-rolled fetch: on 2026-08-17 (#124) a transport-level probe passed
    (admitted UA, 200, >500 bytes) while production parsed nothing.
    """

    def _run(self, check_upstream, search_impl):
        import asyncio
        from unittest.mock import patch

        async def go():
            with patch("lib.sources.libgen.LibgenAdapter.search", new=search_impl):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen(client)

        return asyncio.run(go())

    @staticmethod
    def _book(md5="a" * 32, title="Pride and Prejudice"):
        from types import SimpleNamespace

        return SimpleNamespace(md5=md5, title=title)

    def test_parsed_results_report_ok(self, check_upstream):
        async def fake_search(self, query, **kwargs):
            return [self_or_none._book(), self_or_none._book(md5="b" * 32)]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is True
        assert "2 usable result(s)" in result.detail
        assert result.required is False

    def test_nonempty_but_unusable_rows_fail(self, check_upstream):
        """A parser regression that emits rows with empty md5/title (the #132
        column-shift shape) must not pass the canary (Codex on #128)."""

        async def fake_search(self, query, **kwargs):
            return [
                self_or_none._book(md5="", title=""),
                self_or_none._book(md5="", title="some citation text"),
            ]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "NONE usable" in result.detail

    def test_mixed_rows_count_only_usable(self, check_upstream):
        async def fake_search(self, query, **kwargs):
            return [
                self_or_none._book(),
                self_or_none._book(md5="", title=""),
            ]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is True
        assert "1 usable result(s) (32-hex md5 + title) of 2 parsed" in result.detail

    def test_isbn_shaped_md5_is_not_usable(self, check_upstream):
        """A column-shifted row can put an ISBN or a citation in the md5 slot;
        the downloader passes it to ads.php?md5= and resolves nothing, so
        truthiness is not enough — require the 32-hex shape (Codex on #133)."""

        async def fake_search(self, query, **kwargs):
            return [
                self_or_none._book(md5="978-0-14-143951-8"),
                self_or_none._book(md5="Austen, J. (1813). Pride and Prejudice."),
                self_or_none._book(md5="a" * 31),
                self_or_none._book(md5="g" * 32),
                self_or_none._book(md5=" " + "a" * 32 + " "),
            ]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "NONE usable" in result.detail

    def test_blank_title_with_valid_md5_is_not_usable(self, check_upstream):
        async def fake_search(self, query, **kwargs):
            return [self_or_none._book(title="   ")]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "NONE usable" in result.detail

    def test_uppercase_hex_md5_is_usable(self, check_upstream):
        async def fake_search(self, query, **kwargs):
            return [self_or_none._book(md5="ABCDEF0123456789" * 2)]

        self_or_none = self
        result = self._run(check_upstream, fake_search)
        assert result.ok is True

    def test_canary_carries_its_own_deadline(self, check_upstream):
        """A hung adapter search must fail the probe, not hang the doctor
        (Codex on #128). Patch the probe's timeout down so the test is fast."""
        import asyncio as _asyncio

        async def hung_search(self, query, **kwargs):
            await _asyncio.sleep(30)

        real_wait_for = _asyncio.wait_for

        def short_wait_for(awaitable, timeout):
            return real_wait_for(awaitable, timeout=0.05)

        from unittest.mock import patch

        async def go():
            with (
                patch("lib.sources.libgen.LibgenAdapter.search", new=hung_search),
                patch.object(check_upstream.asyncio, "wait_for", short_wait_for),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen(client)

        result = _asyncio.run(go())
        assert result.ok is False
        assert "TimeoutError" in result.detail or "CancelledError" in result.detail

    def test_probe_deadline_covers_the_documented_worst_case(self, check_upstream):
        """90s cancelled a legitimate three-mirror failover walk, whose
        documented default worst case is ~165s (Codex on #133)."""
        from lib.sources.config import get_source_config
        from lib.sources.libgen import LibgenAdapter

        deadline = check_upstream.libgen_probe_timeout(
            get_source_config(), LibgenAdapter.MIN_REQUEST_INTERVAL
        )
        # 3 mirrors x (2 x 5s preflight phases + 45s total + 2s rate limit)
        assert deadline >= 165
        assert deadline == pytest.approx(3 * 57 + check_upstream.LIBGEN_PROBE_MARGIN)

    def test_probe_deadline_follows_operator_tuning(self, check_upstream, monkeypatch):
        """An operator who raises the per-provider budget must not thereby make
        the canary cancel searches production would complete."""
        from lib.sources.config import get_source_config

        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "180")
        monkeypatch.setenv("BOOK_SOURCE_PREFLIGHT_TIMEOUT", "20")
        deadline = check_upstream.libgen_probe_timeout(get_source_config(), 2.0)
        assert deadline == pytest.approx(
            3 * (180 + 2 + 40) + check_upstream.LIBGEN_PROBE_MARGIN
        )

    def test_probe_passes_the_computed_deadline_to_wait_for(self, check_upstream):
        from types import SimpleNamespace
        from unittest.mock import patch

        from lib.sources.config import get_source_config
        from lib.sources.libgen import LibgenAdapter

        captured = {}
        real_wait_for = asyncio.wait_for

        def spy_wait_for(awaitable, timeout):
            captured["timeout"] = timeout
            return real_wait_for(awaitable, timeout=timeout)

        async def fake_search(self, query, **kwargs):
            return [SimpleNamespace(md5="a" * 32, title="Pride and Prejudice")]

        async def go():
            with (
                patch("lib.sources.libgen.LibgenAdapter.search", new=fake_search),
                patch.object(check_upstream.asyncio, "wait_for", spy_wait_for),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen(client)

        result = asyncio.run(go())
        assert result.ok is True
        assert captured["timeout"] == pytest.approx(
            check_upstream.libgen_probe_timeout(
                get_source_config(), LibgenAdapter.MIN_REQUEST_INTERVAL
            )
        )

    def test_zero_parsed_results_fail_even_on_http_200(self, check_upstream):
        """The old byte-count threshold admitted the 639-byte failure stub;
        zero parsed results for the canary must never report ok."""

        async def fake_search(self, query, **kwargs):
            return []

        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "0 parsed results" in result.detail

    def test_adapter_exception_reports_failure_detail(self, check_upstream):
        async def fake_search(self, query, **kwargs):
            raise RuntimeError("no results table (title 'Welcome to nginx!')")

        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "Welcome to nginx!" in result.detail


class TestAnnasDownloadDomProbe:
    """The browser route's DOM shape needs its own drift check (#150).

    `probe_annas` only exercises `/search`. Anna's could change the book page
    and every browser download would fail while the doctor still reported the
    adapter healthy — the reachability-vs-capability gap this script exists to
    close, reintroduced on a new surface.
    """

    @pytest.fixture(autouse=True)
    def _isolated_usage_counter(self, tmp_path, monkeypatch):
        """Never spend the operator's real Anna's budget from a test.

        The probe is metered by the same persistent counter as production
        (#144), so without this a test run consumes real daily slots and, once
        they are gone, every later test sees "budget spent" instead of the
        behaviour it is checking. Found exactly that way.
        """
        monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
        monkeypatch.setenv("ANNAS_BROWSER_MIN_INTERVAL", "0.001")

    PARTNER_PAGE = (
        '<html><body><a href="/faq">FAQ</a>'
        '<a href="https://cdn9.example.net/d/x/Book.pdf?sig=1">Download now</a>'
        "</body></html>"
    )

    def _run(self, check_upstream, handler):
        import asyncio

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await check_upstream.probe_annas_download_dom(client)

        return asyncio.run(go())

    def _two_page(self, book, partner=None, payload_status=206):
        """Book page, then partner page, then the payload range request."""
        partner = self.PARTNER_PAGE if partner is None else partner

        def handler(request):
            url = str(request.url)
            if "cdn9.example.net" in url:
                return httpx.Response(payload_status, content=b"%PDF-1.5" + b"\0" * 64)
            if "/slow_download/" in url:
                return httpx.Response(200, text=partner)
            return httpx.Response(200, text=book)

        return handler

    def test_partner_links_in_the_expected_shape_pass(self, check_upstream):
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        page = (
            f'<html><body><a href="/slow_download/{md5}/0/0">Server 1</a>'
            f'<a href="/slow_download/{md5}/0/1">Server 2</a></body></html>'
        )

        result = self._run(check_upstream, self._two_page(page))

        assert result.ok is True
        assert "2 partner-server link" in result.detail
        assert "cdn9.example.net" in result.detail, (
            "the probe must report the payload host it actually reached, or "
            "green says nothing about the half of the flow that moves the file"
        )

    def test_a_page_without_partner_links_is_drift_not_a_block(self, check_upstream):
        """Loaded, unchallenged, and missing the links the extractor needs."""
        page = "<html><body><h1>The Feynman Lectures</h1><p>No links.</p></body></html>"

        result = self._run(check_upstream, lambda r: httpx.Response(200, text=page))

        assert result.ok is False
        assert result.blocked is False
        assert "DOM drift" in result.detail

    def test_a_challenge_reports_block_and_says_the_shape_is_unverified(
        self, check_upstream
    ):
        """Being walled is not evidence the DOM changed, and must not say so."""
        page = (
            "<html><head><title>Just a moment...</title></head><body>"
            "<h1>Checking your browser before accessing</h1></body></html>"
        )

        result = self._run(check_upstream, lambda r: httpx.Response(200, text=page))

        assert result.ok is False
        assert result.blocked is True
        assert result.symbol == "BLOCK"
        assert "UNVERIFIED" in result.detail
        assert "not \nevidence" in result.detail or "not " in result.detail

    def test_annas_own_script_comments_do_not_read_as_a_challenge(self, check_upstream):
        """The live bug from #150, guarded on this surface too.

        Every Anna's page carries `// "text/css" for DDOS-GUARD caching.` in a
        script block. A probe that matched raw HTML would report BLOCK on every
        healthy run, which is worse than no probe: it teaches the operator to
        ignore the row.
        """
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        page = (
            f'<html><body><a href="/slow_download/{md5}/0/0">Server 1</a>'
            '<script>// "text/css" for DDOS-GUARD caching.\n'
            'fetch("/dyn/recent_downloads/");</script></body></html>'
        )

        result = self._run(check_upstream, self._two_page(page))

        assert result.ok is True
        assert result.blocked is False

    def test_a_partner_page_without_a_payload_link_is_drift(self, check_upstream):
        """The half of the flow that was previously unmonitored (#150).

        A book page can be perfectly intact while the partner page redesigns
        underneath it, and the probe used to return green on the book page
        alone — reporting the adapter healthy while every download failed.
        """
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        book = f'<html><body><a href="/slow_download/{md5}/0/0">S1</a></body></html>'
        redesigned = '<html><body><button id="dl">Get it</button></body></html>'

        result = self._run(check_upstream, self._two_page(book, redesigned))

        assert result.ok is False
        assert result.blocked is False
        assert "partner-page drift" in result.detail

    def test_a_walled_partner_page_is_blocked_not_drift(self, check_upstream):
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        book = f'<html><body><a href="/slow_download/{md5}/0/0">S1</a></body></html>'
        wall = "<html><body><h1>Checking your browser</h1></body></html>"

        result = self._run(check_upstream, self._two_page(book, wall))

        assert result.blocked is True
        assert result.symbol == "BLOCK"
        assert "UNVERIFIED" in result.detail

    def test_an_unfetchable_payload_is_not_healthy(self, check_upstream):
        """Extraction is not the end of the flow (#150).

        The browser hands the URL to httpx, and that handoff is what every
        download depends on. A signed URL that started requiring browser
        cookies or a referrer would extract cleanly while every production
        transfer failed — so a probe that stopped at extraction would report
        green through a total outage of the thing it exists to watch.
        """
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        book = f'<html><body><a href="/slow_download/{md5}/0/0">S1</a></body></html>'

        result = self._run(check_upstream, self._two_page(book, payload_status=403))

        assert result.ok is False
        assert result.blocked is False
        assert "outside the browser" in result.detail

    def test_a_fetchable_payload_is_named_in_the_detail(self, check_upstream):
        md5 = check_upstream.ANNAS_DOM_CANARY_MD5
        book = f'<html><body><a href="/slow_download/{md5}/0/0">S1</a></body></html>'

        result = self._run(check_upstream, self._two_page(book))

        assert result.ok is True
        assert "plain httpx" in result.detail, (
            "green must state that the handoff was exercised, or it reads as "
            "a claim about extraction alone"
        )


class TestRunProbesReturnsEveryProbe:
    """`run_probes` must unpack and return every probe it gathers.

    Codex on #150: a probe was added to the `gather()` call without a target,
    so the tuple unpack raised and `npm run doctor` plus the scheduled
    upstream check failed before producing a report — the health check
    disabled by an addition to it. The per-probe tests all passed, because
    they call the probe functions directly and never go through here.

    This test is deliberately structural rather than behavioural: it counts
    what goes in against what comes out, so the next probe added without a
    target fails here instead of in CI.
    """

    def test_every_gathered_probe_appears_in_the_result(self, check_upstream):
        import asyncio
        import inspect

        source = inspect.getsource(check_upstream.run_probes)
        import re

        gathered = re.findall(r"\b(probe_\w+)\(client\)", source)
        assert len(gathered) >= 5, f"expected the full probe set, saw {gathered}"

        async def stub(_client):
            return check_upstream.ProbeResult(
                name="stub", ok=True, detail="", required=False
            )

        async def stub_list(_client):
            return [
                check_upstream.ProbeResult(
                    name="zlibrary:stub", ok=True, detail="", required=False
                )
            ]

        import unittest.mock as mock

        patches = {name: stub for name in gathered}
        patches["probe_zlibrary_eapi"] = stub_list
        with mock.patch.multiple(check_upstream, **patches):
            results = asyncio.run(check_upstream.run_probes())

        assert len(results) == len(gathered), (
            f"{len(gathered)} probes gathered but {len(results)} returned — a "
            f"probe is being dropped, or the unpack will raise"
        )


def test_libgen_block_does_not_set_the_zlibrary_blocked_flag(check_upstream):
    """A walled LibGen must not suppress the Z-Library drift report.

    `zlib_blocked` gates whether upstream-check.yml files the Z-Library drift
    issue. #141 made a LibGen UA block set `blocked=True` too, so an unscoped
    `any(...)` would silence a genuine Z-Library drift report because a
    different source was walled (Codex on #146).
    """
    results = [
        check_upstream.ProbeResult(
            name="libgen:download", ok=False, detail="nginx stub", blocked=True
        ),
        check_upstream.ProbeResult(
            name="zlibrary:eapi/book/search", ok=False, detail="drift", blocked=False
        ),
    ]
    assert check_upstream.zlibrary_blocked(results) is False


def test_zlibrary_block_still_sets_the_flag(check_upstream):
    results = [
        check_upstream.ProbeResult(
            name="libgen:search", ok=True, detail="fine", blocked=False
        ),
        check_upstream.ProbeResult(
            name="zlibrary:eapi/info/domains", ok=False, detail="403", blocked=True
        ),
    ]
    assert check_upstream.zlibrary_blocked(results) is True


def test_annas_block_does_not_set_the_zlibrary_flag_either(check_upstream):
    """The scope is Z-Library specifically, not 'any source but LibGen'."""
    results = [
        check_upstream.ProbeResult(
            name="annas-archive:search", ok=False, detail="DDoS-Guard", blocked=True
        ),
    ]
    assert check_upstream.zlibrary_blocked(results) is False


class TestLibgenSearchProbeReportsUserAgentBlocks:
    """A UA-blocked search must read as BLOCK, not as upstream drift.

    Codex on #146: `probe_libgen` caught the adapter's failure as an ordinary
    optional failure, so one doctor run reported `libgen:search` WARN and
    `libgen:download` BLOCK for the identical refusal — and the summary counted
    an optional failure while the message said it was not drift. The two probes
    have to agree, or the operator has to guess which one is lying.
    """

    def _run(self, check_upstream, search_impl):
        import asyncio
        from unittest.mock import patch

        async def go():
            with patch("lib.sources.libgen.LibgenAdapter.search", new=search_impl):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen(client)

        return asyncio.run(go())

    def test_blocked_failure_sets_blocked_and_reads_as_block(self, check_upstream):
        from lib.sources.errors import AllSourcesFailedError
        from lib.sources.libgen import LibgenUserAgentBlocked

        async def blocked(_self, _query, **_kwargs):
            raise AllSourcesFailedError(
                "LibGen search",
                [
                    LibgenUserAgentBlocked(
                        "libgen",
                        "libgen.li",
                        "search page served nginx's default stub",
                        reason="protocol_error",
                    )
                ],
            )

        result = self._run(check_upstream, blocked)

        assert result.ok is False
        assert result.blocked is True
        assert result.symbol == "BLOCK"

    def test_ordinary_upstream_failure_is_not_reported_as_blocked(self, check_upstream):
        """The classification must stay narrow or it hides real drift.

        A timeout or a DOM change reported as BLOCK would suppress the drift
        issue the doctor exists to raise.
        """
        from lib.sources.errors import AllSourcesFailedError, ProviderResponseError

        async def drifted(_self, _query, **_kwargs):
            raise AllSourcesFailedError(
                "LibGen search",
                [
                    ProviderResponseError(
                        "libgen",
                        "libgen.li",
                        "search page had no results table — parse failure",
                        reason="protocol_error",
                    )
                ],
            )

        result = self._run(check_upstream, drifted)

        assert result.ok is False
        assert result.blocked is False
        assert result.symbol == "WARN"

    def test_a_libgen_block_does_not_imply_a_zlibrary_block(self, check_upstream):
        """`zlibrary_blocked` must stay source-scoped (#141).

        The new `blocked=True` on a LibGen probe would otherwise start
        triggering the Z-Library block path, which is the exact conflation the
        scoping fix on this branch removed.
        """
        results = [
            check_upstream.ProbeResult(
                name="libgen:search", ok=False, detail="UA blocked", blocked=True
            )
        ]

        assert check_upstream.zlibrary_blocked(results) is False

    def test_a_mixed_failure_set_is_not_reported_as_blocked(self, check_upstream):
        """One stubbed mirror plus one real failure is not a wall.

        Codex round 4 on #146: `any()` would have marked the whole probe BLOCK,
        dropping the genuine transport failure out of the optional-failure
        count and describing drift as network-level. `probe_libgen_download`
        requires every mirror to be blocked; the search probe must match, or
        the doctor contradicts itself in the other direction.
        """
        from lib.sources.errors import AllSourcesFailedError, ProviderUnreachableError
        from lib.sources.libgen import LibgenUserAgentBlocked

        async def mixed(_self, _query, **_kwargs):
            raise AllSourcesFailedError(
                "LibGen search",
                [
                    LibgenUserAgentBlocked(
                        "libgen", "libgen.li", "nginx stub", reason="protocol_error"
                    ),
                    ProviderUnreachableError(
                        "libgen", "libgen.vg", "no route", reason="connect_timeout"
                    ),
                ],
            )

        result = self._run(check_upstream, mixed)

        assert result.ok is False
        assert result.blocked is False
        assert result.symbol == "WARN"

    def test_an_empty_failure_list_is_not_reported_as_blocked(self, check_upstream):
        """`all()` over an empty list is True, which would invent a wall."""
        from lib.sources.errors import AllSourcesFailedError

        async def empty(_self, _query, **_kwargs):
            raise AllSourcesFailedError("LibGen search", [])

        result = self._run(check_upstream, empty)

        assert result.blocked is False


class TestAnnasProbeDiagnosticsNameAnnas:
    """A block report that names the wrong provider is worse than none (#150).

    `_block_detail` hardcodes the Z-Library domain and tells the operator to
    export `ZLIBRARY_EAPI_DOMAIN`. Reusing it for Anna's reported a Z-Library
    host and an irrelevant remedy in the exact anti-bot scenario the probe
    exists to distinguish.
    """

    @pytest.fixture(autouse=True)
    def _isolated_usage_counter(self, tmp_path, monkeypatch):
        """Never spend the operator's real Anna's budget from a test.

        The probe is metered by the same persistent counter as production
        (#144), so without this a test run consumes real daily slots and, once
        they are gone, every later test sees "budget spent" instead of the
        behaviour it is checking. Found exactly that way.
        """
        monkeypatch.setenv("ANNAS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
        monkeypatch.setenv("ANNAS_BROWSER_MIN_INTERVAL", "0.001")

    def _run(self, check_upstream, handler):
        import asyncio

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await check_upstream.probe_annas_download_dom(client)

        return asyncio.run(go())

    def test_a_403_names_annas_not_zlibrary(self, check_upstream):
        result = self._run(check_upstream, lambda r: httpx.Response(403, text="nope"))

        assert result.blocked is True
        assert "annas" in result.detail.lower()
        assert "zlibrary_eapi_domain" not in result.detail.lower(), (
            "sending an operator to change a Z-Library variable for an Anna's "
            "block is an actively wrong remedy"
        )
        assert "z-library" not in result.detail.lower()

    def test_the_probe_uses_the_production_challenge_classifier(self, check_upstream):
        """A partial marker copy called a challenge page DOM drift.

        Production `_classify_page` knows five challenge markers; the probe
        carried three. A 200 page saying only "enable JavaScript and cookies"
        was therefore reported as a layout change.
        """
        page = (
            "<html><body><p>Please enable JavaScript and cookies to continue</p>"
            "</body></html>"
        )

        result = self._run(check_upstream, lambda r: httpx.Response(200, text=page))

        assert result.blocked is True, (
            "a challenge marker production recognises must not read as drift"
        )
        assert "UNVERIFIED" in result.detail


class TestTheProbeReusesProductionRatherThanRedefiningIt:
    """Six round-7 findings on #150 were one mistake: the probe re-derived
    every property the production route already has — backoff, serialisation,
    refusal statuses, body validation, user agent, bounded reads — and got each
    of them wrong once. These assert the reuse rather than the behaviour, so a
    future edit that forks the logic again fails here.
    """

    def test_the_probe_uses_productions_refusal_statuses(self, check_upstream):
        import inspect

        source = inspect.getsource(check_upstream._annas_block_detail)

        assert "_REFUSAL_STATUSES" in source, (
            "reusing Z-Library's WALLED_STATUS_CODES missed a bare 429/503, so "
            "Anna's throttling was reported as DOM drift"
        )

    def test_the_probe_takes_the_production_browser_lock(self, check_upstream):
        import inspect

        source = inspect.getsource(check_upstream.probe_annas_download_dom)

        assert "CrossProcessLock" in source
        assert "browser_lock.release()" in source, (
            "every early return sits inside walk(); the release has to be in "
            "the outer finally or a blocked probe wedges the browser"
        )

    def test_the_probe_backs_off_on_a_wall(self, check_upstream):
        import inspect

        source = inspect.getsource(check_upstream.probe_annas_download_dom)

        assert "usage.penalise(" in source, (
            "production penalises on a wall; a probe that does not lets the "
            "next request hit Anna's after only the spacing interval"
        )

    def test_the_payload_probe_is_streamed_and_bounded(self, check_upstream):
        import inspect

        source = inspect.getsource(check_upstream.probe_annas_download_dom)

        assert "client.stream(" in source, (
            "a plain get() buffers the whole file when a CDN ignores Range — "
            "a 4KB probe that can pull 30MB is not a 4KB probe"
        )
        assert "DEFAULT_BROWSER_USER_AGENT" in source, (
            "the doctor client identifies as zlibrary-mcp-upstream-check; a CDN "
            "that blocks it would report a healthy handoff as broken"
        )

    def test_an_html_body_at_200_is_not_a_healthy_handoff(self, check_upstream):
        """Production rejects an HTML body; the probe must ask the same question."""
        assert check_upstream._looks_like_html(b"<!DOCTYPE html><html>...")
        assert check_upstream._looks_like_html(b"  \n<html><body>login</body>")
        assert not check_upstream._looks_like_html(b"%PDF-1.5\n%\xe2\xe3")
        assert not check_upstream._looks_like_html(b"The_Feynman\x00\x00BOOKMOBI")
