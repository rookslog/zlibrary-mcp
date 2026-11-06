"""Tests for the upstream reachability probe.

The network calls themselves are not exercised here — they are the point of the
scheduled job. What is tested is the reporting contract the CI workflow depends
on: which failures are actionable, and the exact `$GITHUB_OUTPUT` encoding.
"""

import asyncio
import json
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


def test_excluded_source_failure_is_a_warning(check_upstream, monkeypatch):
    """The operator can retain warning-only behaviour for a fallback."""
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "zlibrary")
    result = check_upstream.ProbeResult(
        name="libgen:search", ok=False, detail="boom", required=False
    )
    assert result.symbol == "WARN"


def test_passing_probe_reports_ok(check_upstream):
    result = check_upstream.ProbeResult(name="x", ok=True, detail="fine")
    assert result.symbol == "OK"


def test_render_summarises_required_and_optional_failures(check_upstream):
    results = [
        check_upstream.ProbeResult("zlibrary:a", True, "fine"),
        check_upstream.ProbeResult("zlibrary:b", False, "broken", required=True),
        check_upstream.ProbeResult("unused:c", False, "broken", required=False),
    ]
    report = check_upstream.render(results)
    assert "1 passing, 1 relied-on failing, 1 warnings" in report
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
    delimiter = next(
        line.removeprefix("report<<")
        for line in written.splitlines()
        if line.startswith("report<<")
    )
    assert delimiter != "PROBE_EOF"
    assert "line one\nline two\n" in written
    assert written.rstrip().endswith(delimiter)


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
    assert check_upstream.ZLIB_DOMAIN == (
        os.environ.get("ZLIBRARY_EAPI_DOMAIN") or DEFAULT_EAPI_DOMAINS[0]
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


def test_blocked_relied_on_probe_is_actionable(check_upstream):
    """Blocks alert independently of whether the router requires the source."""
    results = [
        check_upstream.ProbeResult("zlibrary:a", True, "fine"),
        check_upstream.ProbeResult(
            "zlibrary:b", False, "walled", required=True, blocked=True
        ),
    ]
    assert check_upstream.actionable_failures(results) == [results[1]]
    results.append(
        check_upstream.ProbeResult("libgen:c", False, "drift", required=False)
    )
    assert [r.name for r in check_upstream.actionable_failures(results)] == [
        "zlibrary:b",
        "libgen:c",
    ]


def test_render_counts_blocked_separately(check_upstream):
    results = [
        check_upstream.ProbeResult("zlibrary:a", True, "fine"),
        check_upstream.ProbeResult(
            "zlibrary:b", False, "walled", required=True, blocked=True
        ),
    ]
    report = check_upstream.render(results)
    assert "BLOCK" in report
    assert "1 passing, 1 relied-on failing, 0 warnings" in report
    assert "### Source blocked" in report
    assert "### Source changed" not in report


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

    check_upstream.emit_github_output("report", failed=False, zlib_blocked="true")

    written = out.read_text(encoding="utf-8")
    assert "failed=false\n" in written
    assert "zlib_blocked=true\n" in written
    assert f"zlib_domain={check_upstream.ZLIB_DOMAIN}\n" in written


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
    assert result.symbol == "FAIL"
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
    assert result.symbol == "FAIL"


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
        # 3 mirrors x (5s aggregate preflight + 45s total + 2s rate limit)
        assert deadline >= 165
        assert deadline == pytest.approx(3 * 52 + check_upstream.LIBGEN_PROBE_MARGIN)

    def test_probe_deadline_follows_operator_tuning(self, check_upstream, monkeypatch):
        """An operator who raises the per-provider budget must not thereby make
        the canary cancel searches production would complete."""
        from lib.sources.config import get_source_config

        monkeypatch.setenv("BOOK_SOURCE_TOTAL_TIMEOUT", "180")
        monkeypatch.setenv("BOOK_SOURCE_PREFLIGHT_TIMEOUT", "20")
        deadline = check_upstream.libgen_probe_timeout(get_source_config(), 2.0)
        assert deadline == pytest.approx(
            3 * (180 + 2 + 20) + check_upstream.LIBGEN_PROBE_MARGIN
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


class TestLibgenArticleSearchProbe:
    """The article canary is separate from the existing book-row canary."""

    def test_article_fixture_through_production_parser_and_probe(self, check_upstream):
        """Catch parser/conversion drift hidden by separately stubbed canary rows."""
        from unittest.mock import MagicMock, patch
        from lib.sources.config import SourceConfig

        fixture = (
            MODULE_PATH.parent.parent
            / "test_files/libgen/mixed_book_article_results.html"
        ).read_text()
        response = MagicMock(status_code=200, text=fixture)
        seen = []
        real_predicate = check_upstream._usable_article_canary_row

        def observe(row):
            if real_predicate(row):
                seen.append((row.md5, row.extra["id"], row.extra["content_type"]))
            return real_predicate(row)

        async def inline_worker(func, *_args, **_kwargs):
            # Worker deadlines/threads have separate tests; keep the parser
            # and canary integration deterministic on this test's thread.
            return func()

        async def go():
            with (
                patch.object(
                    check_upstream,
                    "get_source_config",
                    return_value=SourceConfig(preflight_enabled=False),
                ),
                patch("requests.get", return_value=response),
                patch("lib.sources.libgen.run_bounded", side_effect=inline_worker),
                patch.object(check_upstream, "_usable_article_canary_row", observe),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen_article(client)

        result = asyncio.run(go())
        assert result.ok is True
        assert result.name == "libgen:article-search"
        assert "1 usable article result(s)" in result.detail
        assert seen == [("e3f85ebc6faa062bceb95b349f369672", "76430726", "article")]

    def _run(self, check_upstream, search_impl):
        from unittest.mock import patch

        async def go():
            with patch("lib.sources.libgen.LibgenAdapter.search", new=search_impl):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(500))
                ) as client:
                    return await check_upstream.probe_libgen_article(client)

        return asyncio.run(go())

    def test_probe_queries_a_known_article_result(self, check_upstream):
        """The live canary must cover article markup, not only monograph rows.

        Restoring the former Pride and Prejudice query here leaves the article
        parser's selector, edition ID, and MD5 path outside drift detection.
        """
        from types import SimpleNamespace

        seen = {}

        async def fake_search(_self, query, **_kwargs):
            seen["query"] = query
            return [
                SimpleNamespace(
                    md5="e3f85ebc6faa062bceb95b349f369672",
                    title="The Inner Citadel P Hadot",
                    extra={"id": "76430726", "content_type": "article"},
                )
            ]

        result = self._run(check_upstream, fake_search)

        assert result.ok is True
        assert result.name == "libgen:article-search"
        assert seen["query"] == "The Inner Citadel Meditations Marcus Aurelius"

    @pytest.mark.parametrize("md5", ["", "978-0-14-143951-8", "a" * 31, "g" * 32])
    def test_shifted_article_md5_fails(self, check_upstream, md5):
        from types import SimpleNamespace

        async def fake_search(_self, _query, **_kwargs):
            return [
                SimpleNamespace(
                    md5=md5,
                    title="The Inner Citadel P Hadot",
                    extra={"id": "76430726", "content_type": "article"},
                )
            ]

        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert (
            "NONE usable article" in result.detail
            or "0 parsed results" in result.detail
        )
        assert result.blocked is False

    def test_dropped_article_is_drift(self, check_upstream):
        async def fake_search(_self, _query, **_kwargs):
            return []

        result = self._run(check_upstream, fake_search)
        assert result.ok is False
        assert "0 parsed results" in result.detail
        assert result.blocked is False

    def test_pre_repair_series_id_and_joined_title_do_not_clear_the_canary(
        self, check_upstream
    ):
        """The old series.php identity must fail despite a valid article MD5."""
        from types import SimpleNamespace

        async def fake_search(_self, _query, **_kwargs):
            return [
                SimpleNamespace(
                    md5="e3f85ebc6faa062bceb95b349f369672",
                    title=(
                        "The Classical Review 2001oct vol 51 iss 2 The Inner "
                        "Citadel P Hadot DOI 101093cr512298"
                    ),
                    extra={"id": "23549", "content_type": "article"},
                )
            ]

        result = self._run(check_upstream, fake_search)

        assert result.ok is False
        assert "NONE usable article" in result.detail

    def test_correct_edition_id_without_the_article_title_marker_fails(
        self, check_upstream
    ):
        """The article ID alone does not prove the edition title was selected."""
        from types import SimpleNamespace

        async def fake_search(_self, _query, **_kwargs):
            return [
                SimpleNamespace(
                    md5="e3f85ebc6faa062bceb95b349f369672",
                    title="The Classical Review October 2001",
                    extra={"id": "76430726", "content_type": "article"},
                )
            ]

        result = self._run(check_upstream, fake_search)

        assert result.ok is False
        assert "NONE usable article" in result.detail

    def test_valid_md5_book_row_does_not_clear_the_article_canary(self, check_upstream):
        """A book result proves neither the article selector nor its MD5 path."""
        from types import SimpleNamespace

        async def fake_search(_self, _query, **_kwargs):
            return [
                SimpleNamespace(
                    md5="3b516fa296c861195bc94fad9bddda9e",
                    title="The Inner Citadel",
                    extra={"content_type": "book"},
                )
            ]

        result = self._run(check_upstream, fake_search)

        assert result.ok is False
        assert "NONE usable article" in result.detail


@pytest.mark.parametrize(
    "entrypoint", ["probe_libgen", "probe_libgen_article", "probe_libgen_canaries"]
)
@pytest.mark.parametrize("cancel", [False, True])
def test_canaries_close_the_adapter_after_search_failure(
    check_upstream, monkeypatch, entrypoint, cancel
):
    from lib.sources.libgen import LibgenAdapter

    closed = []
    searched = []

    async def fail(adapter, _query, **_kwargs):
        searched.append(adapter)
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("offline search failure")

    async def close(adapter):
        closed.append(adapter)

    monkeypatch.setattr(LibgenAdapter, "search", fail)
    monkeypatch.setattr(LibgenAdapter, "close", close)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            return await getattr(check_upstream, entrypoint)(client)

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(go())
    else:
        result = asyncio.run(go())
        results = result if isinstance(result, tuple) else (result,)
        assert all(not r.ok and not r.blocked for r in results)
    assert searched
    assert all(adapter is searched[0] for adapter in searched)
    assert closed == [searched[0]]


def test_run_probes_includes_the_article_canary(check_upstream):
    """The article probe has to be wired into doctor and scheduled CI."""
    from unittest.mock import patch

    async def zlibrary(_client):
        return [check_upstream.ProbeResult("zlibrary:eapi", True, "ok")]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def libgen_canaries(_client):
        return (
            check_upstream.ProbeResult("libgen:search", True, "ok"),
            check_upstream.ProbeResult("libgen:article-search", True, "ok"),
        )

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    async def go():
        with (
            patch.object(check_upstream, "probe_zlibrary_eapi", zlibrary),
            patch.object(check_upstream, "probe_annas", annas),
            patch.object(check_upstream, "probe_libgen_canaries", libgen_canaries),
            patch.object(check_upstream, "probe_libgen_download", download),
        ):
            return await check_upstream.run_probes()

    results = asyncio.run(go())

    assert [result.name for result in results] == [
        "zlibrary:eapi",
        "annas-archive:search",
        "libgen:search",
        "libgen:article-search",
        "libgen:download",
    ]


def test_run_probes_starts_the_article_search_only_after_book_search_finishes(
    check_upstream,
):
    """The article canary starts only after the book canary returns."""
    from types import SimpleNamespace
    from unittest.mock import patch

    order = []

    async def zlibrary(_client):
        return [check_upstream.ProbeResult("zlibrary:eapi", True, "ok")]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    async def go():
        book_started = asyncio.Event()
        allow_book_finish = asyncio.Event()
        article_started = asyncio.Event()

        async def search(_adapter, query, **_kwargs):
            if query == "Pride and Prejudice":
                order.append("book:start")
                book_started.set()
                await allow_book_finish.wait()
                order.append("book:end")
                return [
                    SimpleNamespace(
                        md5="3b516fa296c861195bc94fad9bddda9e",
                        title="Pride and Prejudice",
                    )
                ]
            assert query == "The Inner Citadel Meditations Marcus Aurelius"
            order.append("article:start")
            article_started.set()
            return [
                SimpleNamespace(
                    md5="e3f85ebc6faa062bceb95b349f369672",
                    title="The Inner Citadel P Hadot",
                    extra={"id": "76430726", "content_type": "article"},
                )
            ]

        with (
            patch.object(check_upstream, "probe_zlibrary_eapi", zlibrary),
            patch.object(check_upstream, "probe_annas", annas),
            patch.object(check_upstream, "probe_libgen_download", download),
            patch("lib.sources.libgen.LibgenAdapter.search", new=search),
        ):
            probes = asyncio.create_task(check_upstream.run_probes())
            try:
                await asyncio.wait_for(book_started.wait(), timeout=5.0)
                await asyncio.sleep(0)
                assert article_started.is_set() is False
            finally:
                allow_book_finish.set()
                results = await asyncio.wait_for(probes, timeout=5.0)
        return results

    results = asyncio.run(go())

    assert [result.name for result in results] == [
        "zlibrary:eapi",
        "annas-archive:search",
        "libgen:search",
        "libgen:article-search",
        "libgen:download",
    ]
    assert order == ["book:start", "book:end", "article:start"]
    assert all(result.ok for result in results)


def test_run_probes_reuses_the_adapter_so_article_search_is_rate_limited(
    check_upstream,
):
    """The second canary must inherit the first request's pacing state."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from lib.sources import libgen as libgen_mod
    from lib.sources.config import SourceConfig

    config = SourceConfig(
        libgen_mirror="li",
        default_source="libgen",
        fallback_enabled=False,
        preflight_enabled=False,
    )
    search_adapters = []
    sleep_delays = []
    closed = []

    async def fake_search(adapter, query, **_kwargs):
        search_adapters.append(adapter)
        await adapter._rate_limit()
        if query == "Pride and Prejudice":
            return [
                SimpleNamespace(
                    md5="3b516fa296c861195bc94fad9bddda9e",
                    title="Pride and Prejudice",
                    extra={"id": "1342", "content_type": "book"},
                )
            ]
        return [
            SimpleNamespace(
                md5="e3f85ebc6faa062bceb95b349f369672",
                title="The Inner Citadel P Hadot",
                extra={"id": "76430726", "content_type": "article"},
            )
        ]

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    async def fake_close(adapter):
        closed.append(adapter)

    async def zlibrary(_client):
        return [check_upstream.ProbeResult("zlibrary:eapi", True, "ok")]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    async def go():
        with (
            patch.object(check_upstream, "get_source_config", return_value=config),
            patch.object(check_upstream, "probe_zlibrary_eapi", zlibrary),
            patch.object(check_upstream, "probe_annas", annas),
            patch.object(check_upstream, "probe_libgen_download", download),
            patch.object(libgen_mod.LibgenAdapter, "search", fake_search),
            patch.object(libgen_mod.LibgenAdapter, "close", fake_close),
            patch.object(libgen_mod.time, "time", side_effect=[100, 100, 101, 102]),
            patch.object(libgen_mod.asyncio, "sleep", fake_sleep),
        ):
            return await check_upstream.run_probes()

    results = asyncio.run(go())

    assert [result.name for result in results] == [
        "zlibrary:eapi",
        "annas-archive:search",
        "libgen:search",
        "libgen:article-search",
        "libgen:download",
    ]
    assert search_adapters[0] is search_adapters[1]
    assert sleep_delays == [1.0]
    assert closed == [search_adapters[0]]


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
        assert result.symbol == "FAIL"

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
        assert result.symbol == "FAIL"

    def test_an_empty_failure_list_is_not_reported_as_blocked(self, check_upstream):
        """`all()` over an empty list is True, which would invent a wall."""
        from lib.sources.errors import AllSourcesFailedError

        async def empty(_self, _query, **_kwargs):
            raise AllSourcesFailedError("LibGen search", [])

        result = self._run(check_upstream, empty)

        assert result.blocked is False


@pytest.mark.parametrize("status", [403, 429])
def test_annas_http_refusal_is_blocked(check_upstream, status):
    async def probe():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ) as client:
            return await check_upstream.probe_annas(client)

    result = asyncio.run(probe())
    assert not result.ok
    assert result.blocked
    assert f"HTTP {status}" in result.detail


@pytest.mark.parametrize(
    "relied,ok,blocked,failed",
    [
        (None, False, True, False),
        (None, False, False, False),
        ("zlibrary,libgen", False, True, False),
        ("zlibrary,annas-archive,libgen", False, True, True),
        ("annas-archive", False, False, True),
        ("zlibrary,libgen", False, False, False),
        (None, True, False, False),
        ("", False, False, False),
    ],
)
def test_reliance_controls_alerts_independently_of_router(
    check_upstream, monkeypatch, relied, ok, blocked, failed
):
    if relied is None:
        monkeypatch.delenv("UPSTREAM_RELIED_SOURCES", raising=False)
    else:
        monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", relied)
    result = check_upstream.ProbeResult(
        "annas-archive:search", ok, "test reason", required=False, blocked=blocked
    )
    assert bool(check_upstream.actionable_failures([result])) is failed
    report = check_upstream.render([result])
    if failed:
        assert ("### Source blocked" if blocked else "### Source changed") in report
    elif not ok:
        assert "WARN" in report


def test_invalid_reliance_is_rejected_before_probing(check_upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "annas-archve")
    with pytest.raises(ValueError, match="annas-archve"):
        check_upstream.actionable_failures([])


def test_first_seen_survives_issue_body_round_trip(check_upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "annas-archive")
    result = check_upstream.ProbeResult(
        "annas-archive:search", False, "HTTP 403", required=False, blocked=True
    )
    previous = check_upstream.render([result], today="2026-10-20")
    report = check_upstream.render([result], previous_body=previous, today="2026-10-23")
    assert "First seen: 2026-10-20" in report
    summary = check_upstream.summarize(
        [result], previous_body=report, today="2026-10-24"
    )
    row = summary["results"][0]
    assert {key: row[key] for key in ("source", "status", "reason", "first_seen")} == {
        "source": "annas-archive",
        "status": "blocked",
        "reason": "HTTP 403",
        "first_seen": "2026-10-20",
    }
    # A healthy intervening run removes the failure from persistent state.
    healthy = check_upstream.render([], previous_body=report, today="2026-10-24")
    again = check_upstream.summarize(
        [result], previous_body=healthy, today="2026-10-25"
    )
    assert again["results"][0]["first_seen"] == "2026-10-25"


@pytest.mark.parametrize(
    "previous",
    ["", "Old issue with no source dates", "<!-- upstream-source-status: {bad} -->"],
)
def test_old_issue_does_not_backdate_failure(check_upstream, previous):
    result = check_upstream.ProbeResult(
        "annas-archive:search", False, "403", blocked=True
    )
    summary = check_upstream.summarize(
        [result], previous_body=previous, today="2026-10-23"
    )
    assert summary["results"][0]["first_seen"] == "2026-10-23"


@pytest.mark.parametrize("ok,expected_exit", [(False, 1), (True, 0)])
def test_main_writes_text_and_json_on_success_and_failure(
    check_upstream, monkeypatch, tmp_path, capsys, ok, expected_exit
):
    import json

    async def probes():
        return [
            check_upstream.ProbeResult(
                "annas-archive:search", ok, "fixture", blocked=not ok
            )
        ]

    monkeypatch.setattr(check_upstream, "run_probes", probes)
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "annas-archive")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    previous = tmp_path / "previous.md"
    previous.write_text("Old issue")
    target = tmp_path / "upstream-report.txt"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_upstream.py",
            "--github-output",
            "--report-file",
            str(target),
            "--previous-issue",
            str(previous),
        ],
    )
    assert check_upstream.main() == expected_exit
    summary = json.loads(target.with_suffix(".json").read_text())
    assert summary["failed"] is (not ok)
    assert summary["results"][0]["status"] == ("passed" if ok else "blocked")
    if ok:
        assert summary["results"][0]["first_seen"] is None
    else:
        assert summary["results"][0]["first_seen"]
    assert capsys.readouterr().out == ""
    assert (
        read_workflow_outputs(tmp_path / "github-output")["report"]
        == target.read_text().rstrip()
    )
    assert (
        f"failed={'false' if ok else 'true'}"
        in (tmp_path / "github-output").read_text()
    )


@pytest.mark.parametrize(
    "body,headers,expected_ok,expected_blocked",
    [
        ('<a href="/md5/abc">DDoS-Guard: a study</a>', {}, True, False),
        ('<a href="/md5/abc">A real result</a>', {"server": "ddos-guard"}, True, False),
        ('<script src="/.well-known/ddos-guard/check.js"></script>', {}, False, True),
        ("Check your browser", {"server": "ddos-guard"}, False, True),
    ],
)
def test_annas_distinguishes_catalog_text_from_challenge(
    check_upstream, body, headers, expected_ok, expected_blocked
):
    async def probe():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body, headers=headers)
            )
        ) as client:
            return await check_upstream.probe_annas(client)

    result = asyncio.run(probe())
    assert result.ok is expected_ok
    assert result.blocked is expected_blocked


@pytest.mark.parametrize(
    "leading",
    [
        '<!-- upstream-source-status: {"annas-archive:search":"2020-01-01"} -->',
        "<!-- upstream-source-status: {broken} -->",
    ],
)
def test_last_issue_marker_controls_first_seen(check_upstream, leading):
    previous = (
        leading
        + '\n<!-- upstream-source-status: {"annas-archive:search":"2026-10-23"} -->'
    )
    result = check_upstream.ProbeResult(
        "annas-archive:search", False, "HTTP 403", blocked=True
    )
    summary = check_upstream.summarize(
        [result], previous_body=previous, today="2026-10-24"
    )
    assert summary["results"][0]["first_seen"] == "2026-10-23"


@pytest.mark.parametrize("ok", [True, False])
def test_upstream_text_cannot_escape_report_code_blocks(
    check_upstream, monkeypatch, ok
):
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "annas-archive")
    import json
    import re

    # An upstream title tries to close an ordinary fence, mention a user,
    # render HTML, and forge both a comment closer and a state marker.
    title = (
        "unsafe\n```\n@name <img src=x> -->\n"
        '<!-- upstream-source-status: {"annas-archive:search":"2020-01-01"} -->\n````'
    )
    result = check_upstream.ProbeResult("annas-archive:search", ok, title)
    report = check_upstream.render([result], today="2026-10-24")
    # Interpret CommonMark backtick fence boundaries independently of the
    # renderer. Everything upstream-controlled must be inside a code block.
    outside = []
    fence_length = 0
    for line in report.splitlines():
        match = re.fullmatch(r"(`{3,})(.*)", line)
        if not fence_length and match:
            fence_length = len(match[1])
        elif fence_length:
            if match and len(match[1]) >= fence_length and not match[2].strip():
                fence_length = 0
        else:
            outside.append(line)
    assert fence_length == 0
    assert "@name" not in "\n".join(outside)
    assert "<img" not in "\n".join(outside)
    assert "unsafe" not in "\n".join(outside)
    markers = re.findall(r"<!-- upstream-source-status: (.*?) -->", report)
    assert len(markers) == 1
    assert json.loads(markers[0]) == (
        {} if ok else {"annas-archive:search": "2026-10-24"}
    )
    assert "&lt;!--" in report and "--&gt;" in report
    if not ok:
        assert "### Source changed" in outside
        assert "#### annas-archive:search" in outside
        assert any(line.startswith("First seen: 2026-10-24") for line in outside)


@pytest.mark.parametrize("json_stdout", [False, True])
@pytest.mark.parametrize("ok", [False, True])
def test_doctor_probe_without_report_flag_does_not_write_files(
    check_upstream, monkeypatch, tmp_path, capsys, json_stdout, ok
):
    import json

    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", "annas-archive")

    async def probes():
        return [check_upstream.ProbeResult("annas-archive:search", ok, "fixture")]

    def refused_write(*args, **kwargs):
        raise AssertionError("Report writes require an explicit --report-file")

    monkeypatch.setattr(check_upstream, "run_probes", probes)
    monkeypatch.setattr(Path, "write_text", refused_write)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["check_upstream.py"] + (["--json"] if json_stdout else [])
    )
    assert check_upstream.main() == (0 if ok else 1)
    printed = capsys.readouterr().out
    if json_stdout:
        assert json.loads(printed)["failed"] is (not ok)
    else:
        assert "annas-archive:search" in printed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_blank_policy_uses_master_default(check_upstream, monkeypatch, value):
    monkeypatch.setenv("UPSTREAM_RELIED_SOURCES", value)
    results = [
        check_upstream.ProbeResult(f"{source}:search", False, "fixture", blocked=True)
        for source in ("zlibrary", "annas-archive", "libgen")
    ]
    assert [r.name for r in check_upstream.actionable_failures(results)] == [
        "zlibrary:search",
        "libgen:search",
    ]


def test_default_annas_warning_keeps_first_seen(check_upstream, monkeypatch):
    monkeypatch.delenv("UPSTREAM_RELIED_SOURCES", raising=False)
    result = check_upstream.ProbeResult(
        "annas-archive:search", False, "HTTP 403", blocked=True
    )
    first = check_upstream.render([result], today="2026-11-01")
    report = check_upstream.render([result], previous_body=first, today="2026-11-05")
    summary = check_upstream.summarize(
        [result], previous_body=report, today="2026-11-06"
    )
    assert summary["relied_sources"] == ["libgen", "zlibrary"]
    assert summary["failed"] is False
    assert summary["results"][0]["first_seen"] == "2026-11-01"
    assert "WARN" in report
    assert "### Source warnings" in report
    assert "First seen: 2026-11-01" in report


# Workflow outputs are parsed independently of the producer.
def read_workflow_outputs(path):
    lines = path.read_text().splitlines()
    result = {}
    iterator = iter(lines)
    for line in iterator:
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            body = []
            for item in iterator:
                if item == delimiter:
                    break
                body.append(item)
            result[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            result[name] = value
    return result


@pytest.mark.parametrize(
    "case,expected_state",
    [
        ("403", "true"),
        ("429", "unknown"),
        ("503", "unknown"),
        ("wall", "true"),
        ("html", "unknown"),
        ("connect", "unknown"),
        ("read", "unknown"),
        ("healthy", "false"),
        ("search-zero-refusal", "unknown"),
        ("search-false", "unknown"),
        ("search-books-null", "unknown"),
        ("search-books-object", "unknown"),
        ("domains-zero-refusal", "unknown"),
        ("domains-string", "unknown"),
        ("domains-empty", "unknown"),
        ("domains-off-host", "unknown"),
        ("search-off-host", "unknown"),
        ("both-off-host", "unknown"),
        ("search-auth-required", "false"),
        ("search-login-required", "false"),
        ("domains-auth-required", "false"),
        ("search-string-zero", "unknown"),
        ("search-null-success", "unknown"),
        ("search-negative-success", "unknown"),
        ("search-text-success", "unknown"),
        ("search-error-with-books", "unknown"),
        ("search-error-with-success", "unknown"),
        ("domains-text-success", "unknown"),
        ("domains-null-entry", "unknown"),
        ("domains-empty-entry", "unknown"),
        ("domains-whitespace-entry", "unknown"),
        ("domains-missing-key", "unknown"),
        ("domains-null-value", "unknown"),
        ("domains-real-shape", "false"),
    ],
)
def test_validate_workflow_outputs_through_real_zlibrary_probe(
    check_upstream, monkeypatch, tmp_path, capsys, case, expected_state
):
    import subprocess
    import json

    domain = check_upstream.DEFAULT_EAPI_DOMAINS[0]
    monkeypatch.setattr(check_upstream, "ZLIB_DOMAIN", domain)
    domain_payloads = {
        "domains-text-success": {"success": "0", "error": "x", "domains": [domain]},
        "domains-null-entry": {"domains": [None]},
        "domains-empty-entry": {"domains": [""]},
        "domains-whitespace-entry": {"domains": [" "]},
        "domains-missing-key": {"domains": [{"other": domain}]},
        "domains-null-value": {"domains": [{"domain": None}]},
        "domains-real-shape": {
            "success": 1,
            "domains": [
                {"domain": domain, "contentAvailable": True, "isRedirector": False}
            ],
        },
        "domains-zero-refusal": {"success": 0, "domains": [domain]},
        "domains-string": {"domains": "x"},
        "domains-empty": {"domains": []},
        "domains-auth-required": {
            "success": 0,
            "error": "Authentication required",
            "domains": [domain],
        },
    }
    search_payloads = {
        "search-string-zero": {"success": "0", "error": "Too many requests"},
        "search-null-success": {"success": None, "error": "Too many requests"},
        "search-negative-success": {"success": -1, "error": "Too many requests"},
        "search-text-success": {"success": "false"},
        "search-error-with-books": {"books": [], "error": "Too many requests"},
        "search-error-with-success": {"success": 1, "error": "Too many requests"},
        "search-zero-refusal": {"success": 0, "error": "Too many requests"},
        "search-false": {"success": False},
        "search-books-null": {"books": None},
        "search-books-object": {"books": {}},
        "search-auth-required": {"success": 0, "error": "Authentication required"},
        "search-login-required": {"success": 0, "error": "Please login"},
    }

    def respond(request):
        if case == "connect":
            raise httpx.ConnectError("offline request", request=request)
        if case == "read":
            raise httpx.ReadTimeout("offline request", request=request)
        if case == "html":
            return httpx.Response(200, text="<html>Check your browser</html>")
        if case == "wall":
            return httpx.Response(200, text="<html>DiamWall</html>")
        if case in ("403", "429", "503"):
            return httpx.Response(int(case))
        redirect = (
            case == "both-off-host"
            or (case == "domains-off-host" and request.method == "GET")
            or (case == "search-off-host" and request.method == "POST")
        )
        if redirect and request.url.host == domain:
            return httpx.Response(
                307, headers={"location": "https://other.invalid" + request.url.path}
            )
        payload = (
            domain_payloads.get(case, {"domains": [domain]})
            if request.method == "GET"
            else search_payloads.get(case, {"books": []})
        )
        return httpx.Response(200, json=payload)

    async def probes():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            return await check_upstream.probe_zlibrary_eapi(client)

    monkeypatch.setattr(check_upstream, "run_probes", probes)
    output = tmp_path / "probe-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["check_upstream.py", "--github-output"])
    assert check_upstream.main() == (0 if expected_state == "false" else 1)
    capsys.readouterr()
    values = read_workflow_outputs(output)
    assert values["zlib_blocked"] == expected_state
    assert values["zlib_domain"] in check_upstream.DEFAULT_EAPI_DOMAINS
    workflow = subprocess.check_output(
        [
            "node",
            "--input-type=module",
            "-e",
            "import fs from 'node:fs';import {parse} from 'yaml';process.stdout.write(JSON.stringify(parse(fs.readFileSync('.github/workflows/upstream-check.yml','utf8'))));",
        ],
        cwd=MODULE_PATH.parent.parent,
        text=True,
    )
    step = next(
        x
        for x in json.loads(workflow)["jobs"]["live_tests"]["steps"]
        if x.get("id") == "live"
    )
    live_output = tmp_path / "live-output"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 89\n")
    fake_uv.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZLIBRARY_")}
    env.update(
        PATH=f"{tmp_path}:{env['PATH']}",
        GITHUB_OUTPUT=str(live_output),
        ZLIB_BLOCKED=values["zlib_blocked"],
        ZLIB_PROBE_DOMAIN=values["zlib_domain"],
        GITHUB_WORKSPACE=str(MODULE_PATH.parent.parent),
    )
    child = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0
    assert (
        read_workflow_outputs(live_output)["status"]
        == {"false": "no_account", "true": "blocked", "unknown": "unknown"}[
            expected_state
        ]
    )


def test_validate_workflow_outputs_use_per_run_delimiters(
    check_upstream, monkeypatch, tmp_path
):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for _ in range(2):
        check_upstream.emit_github_output(
            "line one\nPROBE_EOF\nline two", failed=True, zlib_blocked="true"
        )
    delimiters = [
        x for x in output.read_text().splitlines() if x.startswith("report<<")
    ]
    assert len(set(delimiters)) == 2
    values = read_workflow_outputs(output)
    assert values["failed"] == "true"
    assert values["zlib_blocked"] == "true"
    assert values["report"] == "line one\nPROBE_EOF\nline two"


@pytest.mark.parametrize(
    "domain", ["other.invalid", "sample.invalid\nextra", "sample.invalid\rextra"]
)
def test_validate_workflow_outputs_require_configured_domain(
    check_upstream, monkeypatch, tmp_path, domain
):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(check_upstream, "ZLIB_DOMAIN", domain)
    check_upstream.emit_github_output("report", failed=False, zlib_blocked="false")
    values = read_workflow_outputs(output)
    assert values["zlib_blocked"] == "unknown"
    assert "zlib_domain" not in values


def test_treat_upstream_text_as_data_in_report_rows(check_upstream, monkeypatch):
    monkeypatch.setattr(check_upstream, "LIBGEN_MIRROR_CANDIDATES", ("li",))
    monkeypatch.setattr(check_upstream, "LIBGEN_PROBE_DELAY_SECONDS", 0)

    def respond(request):
        if request.url.path == "/get.php":
            return httpx.Response(307, headers={"location": "/ads.php"})
        return httpx.Response(
            200, text='<a href="get.php?md5=fixture&key=part%0Anext%0Dlast">GET</a>'
        )

    async def probe():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            return await check_upstream.probe_libgen_download(client)

    result = asyncio.run(probe())
    report = check_upstream.render([result])
    assert "key part next last bounced" in report
    assert not any(
        line.startswith("next") or line.startswith("last")
        for line in report.splitlines()
    )


def test_status_json_recursion_is_treated_as_missing_state(check_upstream, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RecursionError("deep status data")

    monkeypatch.setattr(check_upstream.json, "loads", unavailable)
    result = check_upstream.ProbeResult("libgen:search", False, "offline")
    summary = check_upstream.summarize(
        [result],
        previous_body="<!-- upstream-source-status: {} -->",
        today="2026-11-05",
    )
    assert summary["results"][0]["first_seen"] == "2026-11-05"


def test_annas_warning_has_run_annotation(
    check_upstream, monkeypatch, tmp_path, capsys
):
    async def probes():
        return [
            check_upstream.ProbeResult(
                "annas-archive:search", False, "HTTP 403", blocked=True
            )
        ]

    monkeypatch.delenv("UPSTREAM_RELIED_SOURCES", raising=False)
    monkeypatch.setattr(check_upstream, "run_probes", probes)
    monkeypatch.setattr(sys, "argv", ["check_upstream.py", "--github-output"])
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    assert check_upstream.main() == 0
    assert "::warning::Anna's Archive" in capsys.readouterr().out


@pytest.mark.parametrize("github_output", [False, True])
@pytest.mark.parametrize("json_stdout", [False, True])
def test_neutralise_runner_commands_in_report_values(
    check_upstream, monkeypatch, tmp_path, capsys, github_output, json_stdout
):
    value = "note ##[warning]fixture"

    async def probes():
        return [
            check_upstream.ProbeResult(
                "libgen:search", False, value, extra={"domains": [value]}
            )
        ]

    monkeypatch.setattr(check_upstream, "run_probes", probes)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_upstream.py"]
        + (["--github-output"] if github_output else [])
        + (["--json"] if json_stdout else []),
    )
    assert check_upstream._single_line(value) == "note ## [warning]fixture"
    assert check_upstream.main() == 1
    captured = capsys.readouterr()
    printed = captured.out
    assert "##[" not in captured.err
    assert "##[" not in printed
    if github_output:
        assert printed == ""
        assert "##[" not in read_workflow_outputs(output)["report"]


@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
def test_status_json_parser_limits_retain_default_dates(
    check_upstream, monkeypatch, error_type
):
    def unavailable(*args, **kwargs):
        raise error_type("unsupported status value")

    monkeypatch.setattr(check_upstream.json, "loads", unavailable)
    result = check_upstream.ProbeResult("libgen:search", False, "offline")
    summary = check_upstream.summarize(
        [result],
        previous_body="<!-- upstream-source-status: {} -->",
        today="2026-11-05",
    )
    assert summary["results"][0]["first_seen"] == "2026-11-05"


def test_status_json_integer_limit_retains_default_dates(check_upstream):
    if not hasattr(sys, "set_int_max_str_digits"):
        pytest.skip("interpreter has no integer conversion limit")
    original = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        body = "<!-- upstream-source-status: " + "1" * 5000 + " -->"
        result = check_upstream.ProbeResult("libgen:search", False, "offline")
        summary = check_upstream.summarize(
            [result], previous_body=body, today="2026-11-05"
        )
        assert summary["results"][0]["first_seen"] == "2026-11-05"
    finally:
        sys.set_int_max_str_digits(original)


@pytest.mark.parametrize("state", [False, True, None, 0])
def test_workflow_output_requires_state_string(
    check_upstream, monkeypatch, tmp_path, state
):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with pytest.raises(TypeError, match="state string"):
        check_upstream.emit_github_output("report", failed=False, zlib_blocked=state)
    assert not output.exists()


@pytest.mark.parametrize("state", ["False", "false\nnext", "unexpected"])
def test_workflow_output_requires_canonical_state(
    check_upstream, monkeypatch, tmp_path, state
):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with pytest.raises(ValueError, match="state"):
        check_upstream.emit_github_output("report", failed=False, zlib_blocked=state)
    assert not output.exists()


def test_ci_outputs_default_before_probes_start(check_upstream, monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["check_upstream.py", "--github-output"])

    async def unfinished():
        assert read_workflow_outputs(output) == {
            "zlib_blocked": "unknown",
            "zlib_domain": "",
        }
        raise LookupError("unfinished probe")

    monkeypatch.setattr(check_upstream, "run_probes", unfinished)
    with pytest.raises(LookupError, match="unfinished probe"):
        check_upstream.main()
    assert read_workflow_outputs(output) == {
        "zlib_blocked": "unknown",
        "zlib_domain": "",
    }


@pytest.mark.parametrize("stage", ["ads", "get"])
def test_probe_reply_decoding_errors_are_reported(
    check_upstream, libgen_download, stage
):
    def respond(request):
        if request.url.path == "/ads.php" and stage != "ads":
            return httpx.Response(
                200, text='<a href="get.php?md5=fixture&key=fixture">GET</a>'
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream; charset=undefined"},
            content=b"fixture",
        )

    result = libgen_download(respond, mirrors=("li",))
    assert result.ok is False
    assert result.blocked is False


@pytest.mark.parametrize(
    "target",
    [
        "probe_zlibrary_eapi",
        "probe_annas",
        "probe_libgen_canaries",
        "probe_libgen_download",
    ],
)
def test_concurrent_probe_errors_are_reported(check_upstream, monkeypatch, target):
    async def zlib(_client):
        return [
            check_upstream.ProbeResult("zlibrary:eapi/info/domains", True, "ok"),
            check_upstream.ProbeResult("zlibrary:eapi/book/search", True, "ok"),
        ]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def canaries(_client):
        return (
            check_upstream.ProbeResult("libgen:search", True, "ok"),
            check_upstream.ProbeResult("libgen:article-search", True, "ok"),
        )

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    async def invalid(_client):
        raise LookupError("reply unavailable")

    for name, func in (
        ("probe_zlibrary_eapi", zlib),
        ("probe_annas", annas),
        ("probe_libgen_canaries", canaries),
        ("probe_libgen_download", download),
    ):
        monkeypatch.setattr(check_upstream, name, invalid if name == target else func)
    results = asyncio.run(check_upstream.run_probes())
    failures = [row for row in results if not row.ok]
    assert len(failures) == 1
    assert "LookupError" in failures[0].detail


def test_concurrent_probe_reports_invalid_rows(check_upstream, monkeypatch):
    from lib.sources.libgen import LibgenAdapter

    async def bad_rows(self, *args, **kwargs):
        return [object()]

    async def zlib(_client):
        return [
            check_upstream.ProbeResult("zlibrary:eapi/info/domains", True, "ok"),
            check_upstream.ProbeResult("zlibrary:eapi/book/search", True, "ok"),
        ]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    monkeypatch.setattr(LibgenAdapter, "search", bad_rows)
    monkeypatch.setattr(check_upstream, "probe_zlibrary_eapi", zlib)
    monkeypatch.setattr(check_upstream, "probe_annas", annas)
    monkeypatch.setattr(check_upstream, "probe_libgen_download", download)
    results = asyncio.run(check_upstream.run_probes())
    failures = [row for row in results if not row.ok]
    assert len(failures) == 1
    assert failures[0].source == "libgen"
    assert "AttributeError" in failures[0].detail


@pytest.mark.parametrize("value", ["::error::fixture", "  ::warning::fixture"])
def test_text_report_values_remain_data(check_upstream, value):
    report = check_upstream.render(
        [check_upstream.ProbeResult("libgen:search", False, value)]
    )
    assert not any(line.lstrip().startswith("::") for line in report.splitlines())


@pytest.mark.parametrize("stalled", ["probe_zlibrary_eapi", "probe_annas"])
def test_probe_deadline_preserves_finished_results(
    check_upstream, monkeypatch, stalled
):
    cancelled = []

    async def pending(_client):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(stalled)

    async def zlib(_client):
        return [
            check_upstream.ProbeResult("zlibrary:eapi/info/domains", True, "ok"),
            check_upstream.ProbeResult("zlibrary:eapi/book/search", True, "ok"),
        ]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def canaries(_client):
        raise LookupError("fixture reply")

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    for name, function in [
        ("probe_zlibrary_eapi", zlib),
        ("probe_annas", annas),
        ("probe_libgen_canaries", canaries),
        ("probe_libgen_download", download),
    ]:
        monkeypatch.setattr(
            check_upstream, name, pending if name == stalled else function
        )
    monkeypatch.setattr(check_upstream, "PROBE_DEADLINE_SECONDS", 0.01, raising=False)

    async def execute():
        return await asyncio.wait_for(check_upstream.run_probes(), timeout=1)

    results = asyncio.run(execute())
    assert cancelled == [stalled]
    failures = [row for row in results if not row.ok]
    assert len(failures) == (3 if stalled == "probe_zlibrary_eapi" else 2)
    assert any("deadline" in row.detail for row in failures)
    assert any("LookupError" in row.detail for row in failures)
    assert any(row.name == "libgen:download" and row.ok for row in results)


def test_probe_deadline_budget(check_upstream):
    assert check_upstream.PROBE_DEADLINE_SECONDS == 600


@pytest.mark.parametrize(
    "stalled,names",
    [
        (
            "probe_zlibrary_eapi",
            ("zlibrary:eapi/info/domains", "zlibrary:eapi/book/search"),
        ),
        ("probe_libgen_canaries", ("libgen:search", "libgen:article-search")),
    ],
)
def test_probe_deadline_preserves_row_dates(
    check_upstream, monkeypatch, stalled, names
):
    dates = {names[0]: "2026-10-21", names[1]: "2026-10-24"}
    previous = "<!-- upstream-source-status: " + json.dumps(dates) + " -->"
    partial = []

    async def pending(_client):
        partial.append(check_upstream.ProbeResult(names[0], True, "finished subprobe"))
        await asyncio.Event().wait()

    async def zlib(_client):
        return [
            check_upstream.ProbeResult("zlibrary:eapi/info/domains", True, "ok"),
            check_upstream.ProbeResult("zlibrary:eapi/book/search", True, "ok"),
        ]

    async def annas(_client):
        return check_upstream.ProbeResult("annas-archive:search", True, "ok")

    async def canaries(_client):
        return [
            check_upstream.ProbeResult("libgen:search", True, "ok"),
            check_upstream.ProbeResult("libgen:article-search", True, "ok"),
        ]

    async def download(_client):
        return check_upstream.ProbeResult("libgen:download", True, "ok")

    for name, function in [
        ("probe_zlibrary_eapi", zlib),
        ("probe_annas", annas),
        ("probe_libgen_canaries", canaries),
        ("probe_libgen_download", download),
    ]:
        monkeypatch.setattr(
            check_upstream, name, pending if name == stalled else function
        )
    monkeypatch.setattr(check_upstream, "PROBE_DEADLINE_SECONDS", 0.01)

    async def execute():
        return await asyncio.wait_for(check_upstream.run_probes(), timeout=1)

    results = asyncio.run(execute())
    assert partial[0].ok is True
    failures = [row for row in results if not row.ok]
    assert {row.name for row in failures} == set(names)
    assert all("deadline" in row.detail for row in failures)
    summary = check_upstream.summarize(
        results, previous_body=previous, today="2026-11-05"
    )
    assert summary["failed"] is True
    if stalled == "probe_zlibrary_eapi":
        assert summary["zlib_blocked"] == "unknown"
    report = check_upstream.render(results, previous_body=previous, today="2026-11-05")
    state = json.loads(
        report.rsplit("<!-- upstream-source-status: ", 1)[1].split(" -->", 1)[0]
    )
    assert state == dates
    later = check_upstream.summarize(
        [check_upstream.ProbeResult(name, False, "still failing") for name in names],
        previous_body=report,
        today="2026-11-06",
    )
    assert {row["name"]: row["first_seen"] for row in later["results"]} == dates


@pytest.mark.parametrize(
    "failure,expected,statuses",
    [
        (httpx.ReadError("session timeout"), "ReadError", []),
        (httpx.RemoteProtocolError("DNS fake payload"), "RemoteProtocolError", []),
        (httpx.ReadTimeout("canary"), "ReadTimeout", []),
        (ValueError("timeout DNS TLS"), "ValueError", []),
        (
            httpx.HTTPStatusError(
                "canary",
                request=httpx.Request("GET", "https://offline.invalid"),
                response=httpx.Response(503),
            ),
            "HTTPStatusError",
            [503],
        ),
    ],
)
def test_probe_records_typed_failures_without_interpreting_text(
    check_upstream, failure, expected, statuses
):
    async def run():
        def handler(request):
            raise failure

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_upstream.probe_zlibrary_eapi(client)

    rows = check_upstream.summarize(asyncio.run(run()))["results"]
    assert len(rows) == 2
    for row in rows:
        assert expected in row["error_types"]
        assert row["http_statuses"] == statuses


@pytest.mark.parametrize(
    "resolver_text",
    [
        "[Errno -2] Name or service not known",
        "[Errno -3] Temporary failure in name resolution",
    ],
)
def test_probe_records_resolver_cause_not_connecterror_text(
    check_upstream, resolver_text
):
    import socket

    failure = httpx.ConnectError(resolver_text)
    failure.__cause__ = socket.gaierror(-2, resolver_text)

    async def run():
        def handler(request):
            raise failure

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_upstream.probe_annas(client)

    row = check_upstream.summarize([asyncio.run(run())])["results"][0]
    assert row["error_types"] == ["ConnectError", "gaierror"]


def test_download_probe_retains_structured_server_failure(
    libgen_download, check_upstream
):
    def handler(request):
        if request.url.path == "/ads.php":
            return httpx.Response(
                200, text='<a href="get.php?md5=abc&key=canary">GET</a>'
            )
        return httpx.Response(503, text="session timeout")

    row = check_upstream.summarize([libgen_download(handler)])["results"][0]
    assert row["error_types"] == ["HTTPStatusError"]
    assert row["http_statuses"] == [503]
