#!/usr/bin/env python3
"""Probe the third-party surfaces this server depends on.

Two audiences:

* CI — `--github-output` emits a `failed` flag and a `report` block consumed by
  `.github/workflows/upstream-check.yml`, which files an issue on drift.
* Users — `npm run doctor` runs this to answer "is it me or is it them?" before
  filing a bug. Every capability here rides on undocumented endpoints that
  rotate domains without notice, so "the server is broken" and "the upstream
  moved" look identical from a client.

No credentials are required: the probe checks reachability and response shape,
not authenticated behaviour. Authenticated coverage lives in the
`integration`-marked pytest suite.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

# Import the runtime defaults directly from lib/sources/config.py so the probe
# cannot drift from what the server actually contacts (it previously hardcoded
# copies that went stale).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zlibrary" / "src"))
from lib.sources.config import get_source_config  # noqa: E402
from lib.sources.libgen import FALLBACK_MIRRORS as LIBGEN_FALLBACK_MIRRORS  # noqa: E402
from lib.sources.libgen import _nginx_stub as _libgen_nginx_stub  # noqa: E402
from lib.sources.libgen import get_user_agent as libgen_user_agent  # noqa: E402
from zlibrary.eapi import DEFAULT_EAPI_DOMAINS, WALLED_STATUS_CODES  # noqa: E402

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_source_config = get_source_config()

# Mirrors the runtime resolution in lib/python_bridge.py: an explicit
# ZLIBRARY_EAPI_DOMAIN wins; otherwise the runtime probes DEFAULT_EAPI_DOMAINS
# in order and the doctor checks the first candidate (importing the list so
# the probe cannot drift from what the server actually contacts).
ZLIB_DOMAIN = os.environ.get("ZLIBRARY_EAPI_DOMAIN", DEFAULT_EAPI_DOMAINS[0])
# get_source_config() already applies the ANNAS_BASE_URL / LIBGEN_MIRROR
# environment overrides, same as the runtime adapters.
ANNAS_BASE_URL = _source_config.annas_base_url
# LibgenSearch(mirror=suffix) builds https://libgen.{suffix}/ (see
# lib/sources/libgen.py and libgen_api_enhanced) — mirror that construction so
# the probe checks the host the runtime actually contacts.
LIBGEN_BASE_URL = f"https://libgen.{_source_config.libgen_mirror}"

# Mirror suffixes the download probe walks, configured mirror first. `li`, `vg`
# and `la` were the three resolving suffixes on 2026-08-10 (issue #80); `rs`,
# `gs`, `is` and `st` had no DNS. Per-mirror results are meaningful because
# mirrors hand off to *different* CDN nodes that fail independently — on
# 2026-08-10 `li` -> cdn4.booksdl.lc failed TLS while `vg`/`la` -> cdn3 served
# real bytes.
LIBGEN_MIRROR_CANDIDATES: tuple[str, ...] = tuple(
    dict.fromkeys([_source_config.libgen_mirror, "li", "vg", "la"])
)

# A small, stable PDF used to exercise the download path end to end. Verified
# to resolve on li/vg/la on 2026-08-10.
LIBGEN_PROBE_MD5 = "73b76499ab3f33cd09d0cdbefc75ff54"
# Only the first 2 KiB is fetched — enough for the magic bytes, small enough
# that the probe is not a download.
LIBGEN_PROBE_RANGE_BYTES = 2048
# Minimum spacing between requests the download probe issues, in seconds.
LIBGEN_PROBE_DELAY_SECONDS = 2.0

# `ads.php` renders exactly one anchor whose href carries the CDN `key`:
#   <a href="get.php?md5=...&key=GST1V9KIA7FWM2JQ"><h2>GET</h2></a>
# Anchor *text* is not matched: it is markup (<h2>GET</h2>) and cosmetic, while
# the key-bearing href is the thing the download actually needs.
_LIBGEN_GET_HREF_RE = re.compile(
    r"""<a\b[^>]*\bhref=["']([^"']*\bget\.php\?[^"']*\bkey=[^"']*)["']""",
    re.IGNORECASE,
)

# Markers of domain-parking/traffic-monetization pages. A lapsed mirror that a
# squatter re-registered (e.g. annas-archive.li -> Trellian/Above.com in
# 2026-03) still returns HTTP 200, so "no /md5/ links" alone under-reports what
# happened.
PARKING_MARKERS = (
    "above.com",
    "abovedomains",
    "trellian",
    "tr_uuid=",
    "fingerprintjs",
)


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str
    required: bool = True
    # A network-level refusal (anti-bot wall, datacenter-IP block). Not drift:
    # the upstream contract may be perfectly intact for clients the wall lets
    # through, so a blocked probe must not trigger the drift issue.
    blocked: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        if self.ok:
            return "OK"
        if self.blocked:
            return "BLOCK"
        return "FAIL" if self.required else "WARN"


# The openings `_download_url_to_file` treats as an HTML error page rather than
# file bytes. `<html`/`<!doctype` alone missed a fragment beginning `<head>`,
# `<body>`, `<script>` or a comment — all of which production rejects and a
# narrower probe reported as a healthy transfer (Codex on #150).
_HTML_OPENINGS = (
    b"<!doctype",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<!--",
    b"<?xml",
)


def _payload_rejection(sample: bytes, content_type: str) -> Optional[str]:
    """Why production would refuse these bytes, or None if it would accept them.

    Mirrors the guards in `python_bridge.py::_download_url_to_file`: an empty
    body, an HTML content type, or an HTML-looking opening are all rejected
    there, so a probe that accepted them would report the browser-to-httpx
    handoff healthy on exactly the responses that fail in production.
    """
    if not sample:
        return "an empty body"
    if "html" in (content_type or "").lower():
        return f"content-type {content_type!r}"
    head = sample[:512].lstrip().lower()
    if any(head.startswith(opening) for opening in _HTML_OPENINGS):
        return "an HTML body rather than file bytes"
    return None


def _looks_like_html(sample: bytes) -> bool:
    """Kept for the tests that assert the opening set directly."""
    return _payload_rejection(sample, "") is not None


def _annas_block_detail(resp: httpx.Response) -> Optional[str]:
    """A block report for Anna's, naming Anna's.

    `_block_detail` hardcodes the Z-Library domain and tells the operator to
    set `ZLIBRARY_EAPI_DOMAIN`. Reusing it here reported a Z-Library host and
    an irrelevant remedy for an Anna's wall — actively misleading in the exact
    anti-bot scenario this probe exists to distinguish (Codex on #150).
    """
    from lib.sources.annas_browser import _REFUSAL_STATUSES  # noqa: PLC0415

    body_is_diamwall = "diamwall" in resp.text.lower()
    # Anna's refusal statuses, not Z-Library's wall statuses. Reusing
    # WALLED_STATUS_CODES (307/403/513/517) missed a bare 429 or 503, so
    # throttling fell through to the missing-link branch and was reported as
    # DOM drift — sending a maintainer to look for a layout change while Anna's
    # was simply saying slow down (Codex on #150). `_REFUSAL_STATUSES` is what
    # the production route already uses for exactly this question.
    throttled = resp.status_code in _REFUSAL_STATUSES
    if (
        not body_is_diamwall
        and not throttled
        and resp.status_code not in WALLED_STATUS_CODES
    ):
        return None
    wall = (
        "DiamWall anti-bot wall"
        if body_is_diamwall
        else "throttled or refused"
        if throttled
        else "network-level block"
    )
    host = urlsplit(ANNAS_BASE_URL).hostname or "annas-archive"
    return (
        f"{wall} (HTTP {resp.status_code}) — {host} refuses this network's "
        f"clients. From a datacenter or CI address this is expected IP "
        f"blocking, not upstream drift. The browser-resident route (#143) "
        f"clears Anna's wall with a real browser; this probe deliberately does "
        f"not, so it cannot verify the DOM shape from here"
    )


def _block_detail(resp: httpx.Response) -> Optional[str]:
    """Detect a network-level block in a response, returning a report line.

    Covers the DiamWall anti-bot wall (307 self-redirect setting a `__diamwall`
    cookie, then 513/517 "Access Denied" pages built from cdn.diamwall.com
    assets — ISSUE-API-002) and plain IP-reputation blocking (bare 403, which
    is what GitHub-hosted runners get from every Z-Library domain).

    A probe from a blocked network cannot distinguish "this IP is refused"
    from "the wall is up for everyone" — only a probe from a residential
    network (a user running `npm run doctor`) can. So the detail says both.
    """
    body_is_diamwall = "diamwall" in resp.text.lower()
    if not body_is_diamwall and resp.status_code not in WALLED_STATUS_CODES:
        return None
    wall = "DiamWall anti-bot wall" if body_is_diamwall else "network-level block"
    return (
        f"{wall} (HTTP {resp.status_code}) — {ZLIB_DOMAIN} refuses this "
        "network's clients. From a datacenter/CI address this is expected IP "
        "blocking, not upstream drift. From a residential network, "
        "export ZLIBRARY_EAPI_DOMAIN=<working-domain> (e.g. z-library.ec) "
        "or unset it to use the fallback list"
    )


async def probe_zlibrary_eapi(client: httpx.AsyncClient) -> list[ProbeResult]:
    """Check the EAPI domain-discovery endpoint and the search endpoint's shape."""
    results: list[ProbeResult] = []
    base = f"https://{ZLIB_DOMAIN}"

    try:
        resp = await client.get(f"{base}/eapi/info/domains")
        walled = _block_detail(resp)
        if walled:
            results.append(
                ProbeResult(
                    name="zlibrary:eapi/info/domains",
                    ok=False,
                    detail=walled,
                    blocked=True,
                )
            )
        else:
            resp.raise_for_status()
            payload = resp.json()
            domains = payload.get("domains") or []
            results.append(
                ProbeResult(
                    name="zlibrary:eapi/info/domains",
                    ok=bool(domains),
                    detail=(
                        f"{len(domains)} domain(s) advertised: {', '.join(map(str, domains[:3]))}"
                        if domains
                        else "endpoint reachable but advertised no domains "
                        "(contract change — domain discovery drives every later call)"
                    ),
                    extra={"domains": domains[:5]},
                )
            )
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable signal
        results.append(
            ProbeResult(
                name="zlibrary:eapi/info/domains",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    try:
        # An unauthenticated search still exercises the JSON contract: a Cloudflare
        # interstitial or an HTML error page fails to parse, which is the signal.
        resp = await client.post(
            f"{base}/eapi/book/search",
            data={"message": "philosophy", "limit": "1", "page": "1"},
        )
        walled = _block_detail(resp)
        if walled:
            results.append(
                ProbeResult(
                    name="zlibrary:eapi/book/search",
                    ok=False,
                    detail=walled,
                    blocked=True,
                )
            )
            return results
        resp.raise_for_status()
        payload = resp.json()
        has_shape = isinstance(payload, dict) and (
            "books" in payload or "success" in payload
        )
        results.append(
            ProbeResult(
                name="zlibrary:eapi/book/search",
                ok=has_shape,
                detail=(
                    f"JSON response with keys: {sorted(payload)[:6]}"
                    if has_shape
                    else f"unexpected response shape: {str(payload)[:200]}"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            ProbeResult(
                name="zlibrary:eapi/book/search",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    return results


async def probe_annas(client: httpx.AsyncClient) -> ProbeResult:
    """Check that Anna's HTML adapter can extract a search-result title."""
    try:
        resp = await client.get(f"{ANNAS_BASE_URL}/search", params={"q": "philosophy"})
        resp.raise_for_status()
        body = resp.text
        soup = BeautifulSoup(body, "html.parser")
        # The runtime adapter skips the empty cover anchor and uses the text-bearing
        # /md5/ anchor as the result title. Probe that extracted field directly.
        titles = [
            link.get_text(strip=True)
            for link in soup.select("a[href^='/md5/']")
            if link.get_text(strip=True)
        ]
        if titles:
            detail = f"search extracted title: {titles[0]!r}"
        else:
            lower_body = body.lower()
            parked = any(marker in lower_body for marker in PARKING_MARKERS)
            detail = (
                "domain appears PARKED (squatter/traffic-monetization page) — "
                "the configured base URL no longer belongs to Anna's Archive; "
                "update ANNAS_BASE_URL / lib/sources/config.py"
                if parked
                else "reachable but no non-empty title extracted "
                "(layout change or block page — the HTML adapter will return nothing)"
            )
        return ProbeResult(
            name="annas-archive:search",
            ok=bool(titles),
            detail=detail,
            # Anna's is optional: the router falls back to LibGen without a key.
            required=False,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name="annas-archive:search",
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            required=False,
        )


# A book that has been on Anna's for years and is not going anywhere. The probe
# needs a page that reliably exists; a missing edition would read as DOM drift.
ANNAS_DOM_CANARY_MD5 = "e2055de39f1c745d606301917fe66344"


async def probe_annas_download_dom(client: httpx.AsyncClient) -> ProbeResult:
    """Check the two DOM shapes the browser download route (#143) depends on.

    `probe_annas` only exercises `/search`. Anna's could change the book page or
    the partner-server page and every browser download would fail while the
    doctor still reported the adapter healthy (Codex on #150) — the precise
    reachability-vs-capability gap this script exists to close.

    This probe deliberately runs over plain httpx, not the browser. It cannot
    clear the challenge and does not try: from a walled network it reports BLOCK
    and says outright that the shape is unverified, which is honest and
    actionable. From a network Anna's serves, it checks the thing that actually
    matters — that partner-server links are still on the book page in the shape
    the extractor looks for.
    """
    from lib.sources.annas_browser import (  # noqa: PLC0415
        AnnasBrowserSession,
        _classify_page,
    )
    from lib.sources.annas_usage import (  # noqa: PLC0415
        CrossProcessLock,
        DailyUsage,
        UsageCounterUnavailableError,
    )
    from lib.sources.config import (  # noqa: PLC0415
        DEFAULT_BROWSER_USER_AGENT,
        get_source_config,
    )

    # This probe is real traffic to Anna's free route, so it is metered by the
    # same counter the production path uses. Uncounted health checks would let
    # repeated `npm run doctor` runs bypass the 20-second spacing and the
    # 30-request ceiling that are the condition of this route's scope — the
    # politeness layer would cover the feature and not the tool that exercises
    # it (Codex on #150).
    browser_config = get_source_config()
    profile = browser_config.annas_browser_profile_dir
    usage = DailyUsage(os.path.join(profile, "..", "annas-browser-usage.json"))
    # The same lock the browser route takes. Without it the probe could issue
    # requests while a download already had one in flight, so `npm run doctor`
    # would break the one-request-at-a-time guarantee it exists to report on
    # (Codex on #150). A short wait: a health check should yield to a real
    # download rather than queue behind it for twenty-five minutes.
    browser_lock = CrossProcessLock(os.path.join(profile, "..", "annas-browser.lock"))
    if not await asyncio.to_thread(browser_lock.acquire, 5.0):
        return ProbeResult(
            name="annas-archive:download-dom",
            ok=False,
            detail=(
                "not probed: a browser download holds the Anna's session. The "
                "health check yields rather than competing with real traffic."
            ),
            required=False,
        )

    async def walk() -> ProbeResult:
        async def metered() -> Optional[ProbeResult]:
            """Take a slot, or return the row explaining why the probe did not run."""
            try:
                allowed, _remaining, wait = usage.spend(
                    browser_config.annas_browser_daily_limit,
                    browser_config.annas_browser_min_interval,
                )
            except UsageCounterUnavailableError as exc:
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=f"not probed: {exc}",
                    required=False,
                )
            if not allowed:
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        "not probed: today's Anna's browser-route budget is spent. "
                        "The health check is metered by the same counter as the "
                        "route it checks (#144), so it yields to real downloads "
                        "rather than competing with them."
                    ),
                    required=False,
                )
            if wait > 0:
                await asyncio.sleep(wait)
            return None

        skipped = await metered()
        if skipped is not None:
            return skipped

        url = f"{ANNAS_BASE_URL}/md5/{ANNAS_DOM_CANARY_MD5}"
        try:
            resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name="annas-archive:download-dom",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                required=False,
            )

        walled = _annas_block_detail(resp)
        body = resp.text
        # The production classifier, not a copy of some of its markers: a partial
        # copy reported a 200 challenge page as DOM drift, sending the maintainer
        # to look for a layout change that is not there (Codex on #150).
        #
        # Both of its verdicts, not just one: it also returns "exhausted" for
        # Anna's own download-limit text on an ordinary HTTP 200, and dropping
        # that meant throttling read as DOM drift and skipped the backoff.
        challenged = _classify_page(body) in ("challenge", "exhausted")
        if walled or challenged:
            # Production penalises on a wall; a probe that does not would let the
            # next doctor run or download hit Anna's again after only the spacing
            # interval, and the advertised five-minute backoff would apply to the
            # feature but not to the tool exercising it (Codex on #150).
            usage.penalise(browser_config.annas_browser_backoff_seconds)
            return ProbeResult(
                name="annas-archive:download-dom",
                ok=False,
                blocked=True,
                detail=(
                    "browser verification answered instead of the book page, so the "
                    "partner-link DOM shape is UNVERIFIED from this network — not "
                    "evidence that it drifted. The browser route (#143) clears this "
                    "wall with a real browser; this probe deliberately does not. "
                    + (walled or "challenge interstitial served")
                ),
                required=False,
            )

        partner_links = AnnasBrowserSession._slow_download_paths(
            body, ANNAS_DOM_CANARY_MD5
        )
        if partner_links:
            # Stopping here would leave the second half of the flow unmonitored: a
            # partner-page redesign breaks every download while this row stays
            # green (Codex on #150). One extra request, against the first server
            # Anna's lists, through the same extractor production uses.
            skipped = await metered()
            if skipped is not None:
                return skipped

            try:
                partner = await client.get(f"{ANNAS_BASE_URL}{partner_links[0]}")
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        f"book page intact ({len(partner_links)} partner links) but "
                        f"{partner_links[0]} could not be fetched: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    required=False,
                )

            partner_body = partner.text
            partner_walled = (
                _annas_block_detail(partner)
                or _classify_page(partner_body) == "challenge"
            )
            if partner_walled:
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    blocked=True,
                    detail=(
                        f"book page intact ({len(partner_links)} partner links) but "
                        f"the partner page answered with browser verification, so "
                        f"payload-link extraction is UNVERIFIED from this network — "
                        f"not evidence that it drifted"
                    ),
                    required=False,
                )

            annas_host = (urlsplit(ANNAS_BASE_URL).hostname or "").lower()
            payload = AnnasBrowserSession._direct_file_url(partner_body, annas_host)
            if not payload:
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        f"book page intact ({len(partner_links)} partner links) but "
                        f"{partner_links[0]} yielded NO off-site payload link "
                        f"(HTTP {partner.status_code}, {len(partner_body)} bytes) — "
                        f"partner-page drift, and every browser download fails"
                    ),
                    required=False,
                )

            # Extraction is not the end of the flow — the browser hands the URL to
            # httpx, and that handoff is the part this route depends on. A signed
            # URL that starts requiring browser cookies or a referrer would still
            # extract cleanly while every production transfer failed (Codex on
            # #150). Four kilobytes, from the CDN rather than from Anna's, so it
            # costs Anna's nothing and is not metered.
            payload_host = urlsplit(payload).hostname or "?"
            try:
                # Streamed and stopped after the first chunk. A plain `get()`
                # buffers the WHOLE file when a CDN ignores `Range`, so a
                # "four-kilobyte probe" could pull a 30MB book on every doctor
                # run (Codex on #150).
                #
                # With production's User-Agent, too: the shared doctor client
                # identifies as `zlibrary-mcp-upstream-check`, so a CDN that
                # admits the browser UA and blocks that one would have this
                # probe report the handoff broken while
                # `_download_url_to_file` succeeded.
                async with client.stream(
                    "GET",
                    payload,
                    headers={
                        "Range": "bytes=0-4095",
                        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
                    },
                ) as response:
                    probe_status = response.status_code
                    probe_content_type = response.headers.get("content-type", "")
                    sample = b""
                    async for chunk in response.aiter_bytes(4096):
                        sample = chunk
                        break
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        f"payload link extracted from {partner_links[0]} but "
                        f"{payload_host} could not be reached: "
                        f"{type(exc).__name__}: {exc} — the browser-to-httpx "
                        f"handoff is what every download depends on"
                    ),
                    required=False,
                )

            if probe_status not in (200, 206):
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        f"payload link extracted but {payload_host} answered HTTP "
                        f"{probe_status} to a plain ranged GET — the URL no "
                        f"longer works outside the browser, so extraction being "
                        f"healthy says nothing about downloads"
                    ),
                    required=False,
                )

            # Status alone is not the production check. `_download_url_to_file`
            # rejects an HTML body served at HTTP 200 — a soft block, a login
            # page, an interstitial — and verifies content md5. A probe that
            # stopped at the status would report the handoff healthy on exactly
            # the response production refuses (Codex on #150).
            rejection = _payload_rejection(sample, probe_content_type)
            if rejection is not None:
                return ProbeResult(
                    name="annas-archive:download-dom",
                    ok=False,
                    detail=(
                        f"payload link extracted and {payload_host} answered "
                        f"HTTP {probe_status}, but production would refuse the "
                        f"response: {rejection}. Extraction is healthy and the "
                        f"transfer is not"
                    ),
                    required=False,
                )

            return ProbeResult(
                name="annas-archive:download-dom",
                ok=True,
                detail=(
                    f"{len(partner_links)} partner-server link(s) on the canary book "
                    f"page; {partner_links[0]} yielded a payload link on "
                    f"{payload_host}, which served HTTP {probe_status} and "
                    f"{len(sample)} bytes of non-HTML body to plain httpx"
                ),
                required=False,
            )
        return ProbeResult(
            name="annas-archive:download-dom",
            ok=False,
            detail=(
                f"book page loaded (HTTP {resp.status_code}, {len(body)} bytes) with "
                f"NO /slow_download/<md5>/<n>/<n> links — the browser download route "
                f"extracts those, so this is DOM drift and every #143 download will "
                f"fail while search keeps working"
            ),
            required=False,
        )

    try:
        return await walk()
    finally:
        # Every return path above is inside `walk()`, so the lock is released
        # once, here, whatever happens — including the several early returns
        # that report a wall or drift.
        browser_lock.release()


# Headroom over the adapter's own worst case. The canary's deadline exists to
# catch a hang the adapter failed to bound, so it must sit ABOVE every budget
# the adapter is entitled to spend — otherwise a slow-but-legitimate failover
# walk is cancelled and reported as LibGen drift (Codex on #133).
LIBGEN_PROBE_MARGIN = 30.0

# What the downloader actually needs: ads.php?md5= resolves nothing else. A
# column-shifted row can put an ISBN or a citation in this field, and a
# truthiness check would call that healthy (Codex on #133; the #132 shape).
MD5_PATTERN = re.compile(r"[0-9a-fA-F]{32}")


def libgen_probe_timeout(config, min_request_interval: float = 0.0) -> float:
    """Wall clock the production adapter may legitimately spend on one search.

    `LibgenAdapter.search` walks every mirror candidate, and each attempt can
    cost a two-phase preflight (DNS, then TCP — the budget is per phase), the
    rate-limit wait, and the full per-provider total budget. Deriving the
    deadline from the same config the adapter reads means an operator who
    raises `BOOK_SOURCE_TOTAL_TIMEOUT` does not thereby make the canary
    cancel searches production would complete.

    Args:
        config: SourceConfig the adapter will be constructed with
        min_request_interval: LibgenAdapter.MIN_REQUEST_INTERVAL, per attempt

    Returns:
        Seconds, worst-case mirror walk plus LIBGEN_PROBE_MARGIN
    """
    mirrors = len({config.libgen_mirror, *LIBGEN_FALLBACK_MIRRORS})
    per_mirror = float(config.total_timeout) + float(min_request_interval)
    if config.preflight_enabled:
        per_mirror += 2 * float(config.preflight_timeout)
    return mirrors * per_mirror + LIBGEN_PROBE_MARGIN


def _usable_row(result) -> bool:
    """Whether a parsed row is one the downloader could actually act on."""
    return bool((result.title or "").strip()) and bool(
        MD5_PATTERN.fullmatch(result.md5 or "")
    )


async def probe_libgen(client: httpx.AsyncClient) -> ProbeResult:
    """LibGen is the router's fallback source; mirrors rotate frequently.

    The probe goes through the PRODUCTION adapter, not a hand-rolled fetch.
    On 2026-08-17 (#124) the mirror served its UA-blocklist stub (default
    nginx page, HTTP 200, 639 bytes) to the search library's default UA
    while this script's own UA was admitted — so a transport-level probe
    reported OK across a fully broken production search path, and the
    stub's 639 bytes would have passed the old ``> 500`` byte threshold
    even with the right UA. ``ok`` therefore means "the adapter parsed at
    least one result for a canary query that cannot plausibly be empty",
    never a status code or byte count. ``client`` is unused by design: the
    adapter builds production's own HTTP stack.
    """
    from lib.sources.errors import AllSourcesFailedError  # noqa: PLC0415
    from lib.sources.libgen import (  # noqa: PLC0415
        LibgenAdapter,
        LibgenUserAgentBlocked,
    )

    canary = "Pride and Prejudice"
    config = get_source_config()
    try:
        # #106's budgets bound the adapter internally, but the canary carries
        # its own deadline too: a canary that can hang has the exact defect
        # it exists to detect (Codex on #128). The deadline is computed from
        # the adapter's own configured mirror-walk budget rather than fixed at
        # 90s, which was below the ~165s default worst case (Codex on #133).
        results = await asyncio.wait_for(
            LibgenAdapter(config).search(canary),
            timeout=libgen_probe_timeout(config, LibgenAdapter.MIN_REQUEST_INTERVAL),
        )
    except Exception as exc:  # noqa: BLE001
        # A UA block is not upstream drift and must not be summarised as one.
        # Without this the same refusal, on the same run, came back as WARN
        # here and BLOCK from the download probe — and the summary counted it
        # as an optional failure while the message said it was not drift
        # (Codex on #146). The adapter raises a distinct type for exactly this.
        # ALL mirrors, not any: a run where one mirror served the stub and
        # another timed out is a mixed result, and calling it BLOCK would drop
        # the genuine transport failure out of the optional-failure count and
        # describe drift as a wall (Codex on #146). This is the same
        # all-mirrors rule `probe_libgen_download` applies via `blocked_count`,
        # and the two probes have to agree or the doctor contradicts itself
        # again in the opposite direction.
        failures = list(getattr(exc, "failures", None) or [])
        blocked = isinstance(exc, LibgenUserAgentBlocked) or (
            isinstance(exc, AllSourcesFailedError)
            and bool(failures)
            and all(isinstance(f, LibgenUserAgentBlocked) for f in failures)
        )
        return ProbeResult(
            name="libgen:search",
            ok=False,
            blocked=blocked,
            detail=f"adapter search failed: {type(exc).__name__}: {exc}",
            required=False,
        )
    # A nonempty list of unusable rows is a parser regression, not a pass:
    # the adapter maps missing fields to empty strings, and a result without
    # a resolvable md5 can never be downloaded (Codex on #128; the article-row
    # column-shift in #132 is exactly this shape). Shape, not truthiness — a
    # shifted column can carry an ISBN or a citation here and still be truthy.
    usable = [r for r in results if _usable_row(r)]
    if usable:
        return ProbeResult(
            name="libgen:search",
            ok=True,
            detail=(
                f"{len(usable)} usable result(s) (32-hex md5 + title) of "
                f"{len(results)} parsed by the production adapter "
                f"for canary {canary!r}"
            ),
            required=False,
        )
    if results:
        return ProbeResult(
            name="libgen:search",
            ok=False,
            detail=(
                f"{len(results)} parsed result(s) but NONE usable "
                f"(a 32-hex md5 and a nonblank title) for canary "
                f"{canary!r} — row-markup drift"
            ),
            required=False,
        )
    return ProbeResult(
        name="libgen:search",
        ok=False,
        detail=(
            f"0 parsed results for canary {canary!r} — page had a results "
            "table but nothing parsed from it (row-markup drift?)"
        ),
        required=False,
    )


def _extract_libgen_key(
    page_url: str, body: str
) -> tuple[Optional[str], Optional[str]]:
    """Pull the CDN key out of an `ads.php` page.

    Returns `(get_url, key)`, or `(None, None)` when the page carries no
    key-bearing GET anchor — which is the DOM-drift signal.
    """
    match = _LIBGEN_GET_HREF_RE.search(body)
    if not match:
        return None, None
    href = html.unescape(match.group(1))
    get_url = urljoin(page_url, href)
    key_values = parse_qs(urlsplit(get_url).query).get("key") or []
    key = key_values[0] if key_values and key_values[0] else None
    if not key:
        return None, None
    return get_url, key


def _libgen_block_detail(mirror: str, resp: httpx.Response) -> Optional[str]:
    """Classify a network-level refusal from a LibGen mirror.

    Distinct from `_block_detail`, which is worded for the Z-Library DiamWall
    wall and names ZLIB_DOMAIN. A refusal is not drift: the resolve-and-fetch
    contract may be intact for clients the wall lets through.
    """
    # A blocklisted UA is refused with HTTP 200 and nginx's default page, so
    # the status-code check below never sees it. Left unclassified it reaches
    # the operator as "DOM drift" or "no results" — a parser bug or an outage,
    # neither of which is true — which is what #141 cost a sweep to discover.
    if _libgen_nginx_stub(resp.text):
        return (
            f"libgen.{mirror} served nginx's default stub for User-Agent "
            f"{libgen_user_agent()!r} — this UA is blocklisted. Not an outage "
            "and not upstream drift; set LIBGEN_USER_AGENT to a string the "
            "mirror admits (#141)"
        )
    if resp.status_code not in (403, 429, *WALLED_STATUS_CODES):
        return None
    return (
        f"libgen.{mirror} refused this network's clients (HTTP "
        f"{resp.status_code}) — from a datacenter/CI address this is expected "
        "IP blocking, not upstream drift"
    )


async def _probe_libgen_download_mirror(
    client: httpx.AsyncClient, mirror: str
) -> tuple[bool, str, bool]:
    """Exercise the full resolve-and-fetch path against one mirror.

    Returns `(ok, detail, blocked)`. Three hops can each fail independently
    (issue #80): the `ads.php` DOM scrape for the key, the key's TTL (< 2.5h,
    and an expired key silently 307s back to `/ads.php` rather than erroring),
    and the liveness of whichever `cdn*.booksdl.*` node the mirror hands off to.
    """
    base = f"https://libgen.{mirror}"
    ads_url = f"{base}/ads.php"
    try:
        # Present the production adapter's UA, not this script's: the mirror
        # UA-blocklists tool defaults (#124), so probing with a different
        # identity than production is exactly the blindness this probe exists
        # to avoid.
        resp = await client.get(
            ads_url,
            params={"md5": LIBGEN_PROBE_MD5},
            headers={"User-Agent": libgen_user_agent()},
        )
        blocked = _libgen_block_detail(mirror, resp)
        if blocked:
            return False, blocked, True
        resp.raise_for_status()
        get_url, key = _extract_libgen_key(str(resp.url), resp.text)
        if not get_url or not key:
            return (
                False,
                f"{mirror}: ads.php returned HTTP {resp.status_code} with no "
                "key-bearing GET link (DOM drift — the resolver scrapes this "
                "anchor for the CDN key)",
                False,
            )
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable signal
        return False, f"{mirror}: ads.php {type(exc).__name__}: {exc}", False

    # Rate limit: never hammer a mirror, even across the two hops.
    await asyncio.sleep(LIBGEN_PROBE_DELAY_SECONDS)

    try:
        # get.php 307s to a CDN host, so redirects must be followed. Only the
        # first 2 KiB is requested — enough to see the magic bytes.
        resp = await client.get(
            get_url,
            headers={
                "Range": f"bytes=0-{LIBGEN_PROBE_RANGE_BYTES - 1}",
                "User-Agent": libgen_user_agent(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        # A dead CDN node lands here (cdn4.booksdl.lc failed TLS on
        # 2026-08-10). That is a real capability failure, not a block.
        return False, f"{mirror}: get.php {type(exc).__name__}: {exc}", False

    blocked = _libgen_block_detail(mirror, resp)
    if blocked:
        return False, blocked, True

    final_url = str(resp.url)
    hops = [str(r.headers.get("location", "")) for r in resp.history]
    if "/ads.php" in urlsplit(final_url).path or any("/ads.php" in h for h in hops):
        return (
            False,
            f"{mirror}: key {key} bounced back to /ads.php — expired or "
            "rejected key (this returns HTTP 200 HTML, not an error, so it "
            "must be detected by the redirect target)",
            False,
        )

    cdn_host = urlsplit(final_url).netloc or f"libgen.{mirror}"
    if resp.status_code not in (200, 206):
        return (
            False,
            f"{mirror}: get.php -> {cdn_host} returned HTTP {resp.status_code}",
            False,
        )

    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" in content_type:
        return (
            False,
            f"{mirror}: get.php -> {cdn_host} served a page, not a file "
            f"(HTTP {resp.status_code}, content-type {content_type or 'unset'})",
            False,
        )

    body = resp.content
    if not body.startswith(b"%PDF"):
        return (
            False,
            f"{mirror}: get.php -> {cdn_host} returned HTTP {resp.status_code} "
            f"but the bytes are not a PDF (first 8: {body[:8]!r})",
            False,
        )

    return (
        True,
        f"{mirror}: ads.php key -> {cdn_host} HTTP {resp.status_code}, "
        f"{len(body)} bytes of PDF (content-type {content_type or 'unset'})",
        False,
    )


async def probe_libgen_download(client: httpx.AsyncClient) -> ProbeResult:
    """Check that LibGen can actually deliver bytes, not merely answer a search.

    `probe_libgen` only asserts that search returns HTTP 200. That stayed green
    on 2026-08-10 while every download through the default mirror failed at the
    CDN hop — the reachability-vs-capability gap of issue #81. This probe walks
    the mirrors in order and reports the first that hands over real PDF bytes.
    """
    attempts: list[str] = []
    blocked_count = 0
    for index, mirror in enumerate(LIBGEN_MIRROR_CANDIDATES):
        if index:
            await asyncio.sleep(LIBGEN_PROBE_DELAY_SECONDS)
        ok, detail, blocked = await _probe_libgen_download_mirror(client, mirror)
        if ok:
            skipped = len(LIBGEN_MIRROR_CANDIDATES) - index - 1
            suffix = f"; {skipped} further mirror(s) not tried" if skipped else ""
            return ProbeResult(
                name="libgen:download",
                ok=True,
                detail=detail + suffix,
                required=False,
            )
        blocked_count += 1 if blocked else 0
        attempts.append(detail)

    return ProbeResult(
        name="libgen:download",
        ok=False,
        detail="no mirror delivered a file — " + " | ".join(attempts),
        required=False,
        # Only a wholly blocked walk is a network-level refusal; if any mirror
        # answered and still could not deliver, that is a real capability
        # failure and must not be filed under BLOCK.
        blocked=blocked_count == len(LIBGEN_MIRROR_CANDIDATES),
        extra={"mirrors_tried": list(LIBGEN_MIRROR_CANDIDATES)},
    )


async def run_probes() -> list[ProbeResult]:
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "zlibrary-mcp-upstream-check"},
    ) as client:
        (
            zlib_results,
            annas,
            annas_download_dom,
            libgen,
            libgen_download,
        ) = await asyncio.gather(
            probe_zlibrary_eapi(client),
            probe_annas(client),
            probe_annas_download_dom(client),
            probe_libgen(client),
            probe_libgen_download(client),
        )
    return [
        *zlib_results,
        annas,
        annas_download_dom,
        libgen,
        libgen_download,
    ]


def actionable_failures(results: list[ProbeResult]) -> list[ProbeResult]:
    """Failures that indicate drift a maintainer can act on.

    Blocked probes are excluded: an IP-level refusal says nothing about the
    upstream contract, and counting it would keep the drift issue permanently
    open from CI's datacenter address.
    """
    return [r for r in results if r.required and not r.ok and not r.blocked]


def render(results: list[ProbeResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = [f"{r.symbol:<5} {r.name:<{width}}  {r.detail}" for r in results]
    required_failures = actionable_failures(results)
    optional_failures = [
        r for r in results if not r.required and not r.ok and not r.blocked
    ]
    blocked = [r for r in results if r.blocked]
    lines.append("")
    summary = (
        f"{sum(1 for r in results if r.ok)} passing, "
        f"{len(required_failures)} required failing, "
        f"{len(optional_failures)} optional failing"
    )
    if blocked:
        summary += f", {len(blocked)} blocked (network-level, not drift)"
    lines.append(summary)
    return "\n".join(lines)


def zlibrary_blocked(results: list[ProbeResult]) -> bool:
    """True when a **Z-Library** probe was walled, ignoring other sources.

    Scoped by the `zlibrary:` name prefix. This value feeds
    upstream-check.yml's decision to skip filing the Z-Library drift issue, so
    folding another source's block into it suppresses a genuine Z-Library
    report for an unrelated reason — and #141 made that reachable, since a
    LibGen UA block is now classified as `blocked` too (Codex on #146).
    """
    return any(r.blocked for r in results if r.name.startswith("zlibrary:"))


def emit_github_output(report: str, failed: bool, zlib_blocked: bool = False) -> None:
    path: Optional[str] = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"failed={'true' if failed else 'false'}\n")
        # The live-suite job gates on this: logging in through a wall would
        # both fail spuriously and burn the ~10/hour login rate limit.
        handle.write(f"zlib_blocked={'true' if zlib_blocked else 'false'}\n")
        # Heredoc form so multi-line reports survive intact.
        handle.write("report<<PROBE_EOF\n")
        handle.write(report + "\n")
        handle.write("PROBE_EOF\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="also write failed/report to $GITHUB_OUTPUT",
    )
    args = parser.parse_args()

    results = asyncio.run(run_probes())
    required_failed = bool(actionable_failures(results))
    zlib_blocked = zlibrary_blocked(results)

    if args.json:
        print(
            json.dumps(
                {
                    "failed": required_failed,
                    "zlib_blocked": zlib_blocked,
                    "results": [
                        {
                            "name": r.name,
                            "ok": r.ok,
                            "required": r.required,
                            "blocked": r.blocked,
                            "detail": r.detail,
                            **r.extra,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(render(results))

    if args.github_output:
        emit_github_output(render(results), required_failed, zlib_blocked)

    # Required-source failure is an actionable signal; optional-source failure is not.
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())
