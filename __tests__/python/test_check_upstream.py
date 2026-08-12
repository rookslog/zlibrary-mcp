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


def test_annas_parking_page_is_reported_as_parked(check_upstream):
    """A parked domain returns HTTP 200 with no /md5/ links; the report must say
    'parked', not just 'no links found', so the operator knows the domain is gone."""
    assert any("trellian" in m for m in check_upstream.PARKING_MARKERS)
    # The markers list is what probe_annas scans the body with; keep them
    # lowercase because the body is lowercased before matching.
    assert all(m == m.lower() for m in check_upstream.PARKING_MARKERS)
