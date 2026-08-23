"""Anna's Archive download-link resolution from a browser-resident session.

Anna's free download route sits behind a DDoS-Guard challenge. The route this
module implements is the one an operator ruling brought into scope (#142, #147):
a **real browser on the operator's machine** holds the clearance, and this code
drives that browser to read the links Anna's puts on its own pages. Nothing is
exported to another client, so #84's finding — DDoS-Guard binds the challenge
cookie to the issuing IP inside `__ddg9_`, making a transplanted cookie exactly
as useless as no cookie — does not apply here.

Two facts from the #142/#143 spike shape everything below.

**The browser resolves links; it does not move the file.** The signed URL that
Anna's partner-server page hands out is fetched successfully *outside* the
browser (measured: HTTP 206, `bytes 0-4095/4762590`, body starting `%PDF-1.5`).
So the browser's job ends at a URL, and the transfer stays on the existing httpx
path in `python_bridge._download_url_to_file`, which already does content-md5
verification, throughput bounding and atomic staging. Routing bytes through
Playwright would have meant reimplementing all three, worse.

**Headful is mandatory.** A headless launch fails while holding clearance that
worked headful minutes earlier from the same profile. This is not a tuning
parameter, so `launch_headless` exists only to be refused loudly rather than to
be set.

The politeness layer (#144) is not a separate module because it must not be
separable: every navigation goes through `_RateLimiter`, one request is in
flight at a time, and a challenge or refusal backs off rather than retrying
into the wall. Anna's states its reason for the control plainly — *"browser
verification for our slow downloads, because otherwise bots and scrapers will
abuse them"* — and the limiter is what makes this path's claim to be on the
right side of that true rather than rhetorical.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from .annas_usage import CrossProcessLock, DailyUsage
from .config import SourceConfig
from .errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)

PROVIDER = "annas"

# The partner-server links Anna's renders on a book page. Index 0 is the
# "slow partner server" set; the trailing integer selects which one.
_SLOW_LINK_RE = re.compile(r"/slow_download/([a-f0-9]{32})/(\d+)/(\d+)")

# A direct file URL, as opposed to another Anna's page. The partner page's
# payload link points off-site at a CDN node, which is what makes it fetchable
# outside the browser at all.
# A mirror of `filename_utils.SAFE_DOCUMENT_EXTENSIONS`, used only when that
# module cannot be imported. Guarded by a test that compares the two.
_FALLBACK_EXTENSIONS = frozenset(
    {
        "azw",
        "azw3",
        "cbr",
        "cbz",
        "djv",
        "djvu",
        "doc",
        "docx",
        "epub",
        "fb2",
        "lit",
        "mobi",
        "odt",
        "pdf",
        "rtf",
        "txt",
    }
)


def _payload_extension_pattern() -> "re.Pattern[str]":
    """Build the file-link pattern from the project's own extension vocabulary.

    A hand-written allowlist here silently failed every Anna's result in a
    format it happened to omit — `rtf`, `lit`, `djv`, `azw`, `doc`, `docx` and
    `odt` were all missing, and each produced a protocol error on a partner
    page that had a perfectly good link on it (Codex on #150). Deriving it from
    `filename_utils.SAFE_DOCUMENT_EXTENSIONS` means a format added there cannot
    be forgotten here.
    """
    extensions = None
    for module in ("filename_utils", "lib.filename_utils"):
        try:
            extensions = __import__(
                module, fromlist=["SAFE_DOCUMENT_EXTENSIONS"]
            ).SAFE_DOCUMENT_EXTENSIONS
            break
        except Exception:  # noqa: BLE001 - fall through to the mirror
            continue
    if extensions is None:
        # A partial environment must not make this module unimportable at all.
        # `_FALLBACK_EXTENSIONS` mirrors the same set and
        # `test_the_fallback_mirrors_the_source_of_truth` fails if the two ever
        # diverge, so the mirror cannot rot the way the hand-written allowlist
        # this replaced did.
        logger.debug("filename_utils unavailable; using the mirrored extension set")
        extensions = _FALLBACK_EXTENSIONS
    # Longest first, so `.azw3` cannot be shadowed by `.azw`.
    alternatives = "|".join(
        re.escape(ext) for ext in sorted(extensions, key=len, reverse=True)
    )
    return re.compile(rf"\.({alternatives})\b", re.I)


_FILE_EXTENSION_RE = _payload_extension_pattern()

# Any Anna's book link. Used as positive evidence that a page is genuinely
# Anna's rather than an interstitial standing in front of it.
_BOOK_LINK_RE = re.compile(r"/md5/[a-f0-9]{32}")

# Everything that is not rendered prose. Classification runs on visible text
# only, and that is not a nicety — a live run on 2026-08-23 rejected a
# perfectly good 295KB book page because every Anna's page carries the literal
# JavaScript comment `// "text/css" for DDOS-GUARD caching.`, three times on a
# book page. Matching raw HTML meant the wall detector fired on 100% of
# successful requests, and no amount of mocked-page unit testing was going to
# show that.
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Phrases that mean "the wall answered", not "the book is missing". Verified
# absent from the visible text of known-good book and partner-server pages
# captured live, which is the bar a marker has to clear before it goes in here:
# a false positive costs every request, a false negative costs one.
_CHALLENGE_MARKERS = (
    "checking your browser",
    "verifying you are human",
    "enable javascript and cookies",
    "just a moment",
    "ddos-guard protection",
)

# Anna's own refusal. Deliberately specific: "please wait" and "rate limit"
# were here and are not, because a normal slow-download page legitimately asks
# the reader to wait, and reading a countdown as an exhausted quota would abort
# a request that was about to succeed.
_EXHAUSTED_MARKERS = (
    "you have downloaded too many",
    "too many downloads",
    "download limit reached",
    "daily download limit",
)


class BrowserUnavailableError(ProviderConfigurationError):
    """Playwright, or a browser it can drive, is not installed.

    A configuration failure rather than an outage: it is permanent until the
    operator changes something, so it must not count as evidence that Anna's is
    unhealthy. `reason` stays `configuration_error` for that reason.
    """


class ChallengeNotClearedError(ProviderResponseError):
    """The wall answered instead of the page.

    Distinct from an ordinary response error because the correct response is
    the opposite one: back off and tell the operator to re-establish clearance
    in the browser, rather than failing over to another host or retrying.
    """


class ProviderRateLimitedError(ProviderResponseError):
    """Anna's declined further downloads for now — its limit, not ours.

    A wall like the challenge, and handled like one: the walk stops rather than
    trying the next partner server. Typed because the caller's move differs
    from an ordinary response error, and because catching it by message would
    put a string match on the abort path.
    """

    reason = "quota_exhausted"


class BrowserBusyError(ProviderResponseError):
    """Another process already holds the browser.

    Non-retryable by reason, because the generic retry loop would start a
    fourth and fifth process queueing for the same browser rather than waiting.
    """

    reason = "configuration_error"


class DailyLimitReachedError(ProviderResponseError):
    """This session's own per-day ceiling is spent.

    Ours, not Anna's. Hitting it is the politeness layer working, so it says so
    plainly rather than presenting as a provider failure.
    """

    reason = "quota_exhausted"


@dataclass
class _RateLimiter:
    """Minimum spacing between requests, plus a ceiling on how many there are.

    Deliberately crude. A token bucket would let a burst through after an idle
    period, and a burst is the exact shape of traffic this exists to prevent —
    the point is not to average out politely, it is to never be fast.

    Spacing and backoff are in-process, which is correct: they govern one
    session's own pacing. The **daily ceiling is not**, and cannot be. Every MCP
    operation starts a fresh `python_bridge.py`, so a counter living in this
    object resets to zero on every download and the advertised ceiling would
    never be reached (Codex on #150). It is delegated to `DailyUsage`, which
    keeps the count in a locked file beside the browser profile.
    """

    min_interval: float
    daily_limit: int
    usage: "DailyUsage"
    _last_request: float = 0.0

    @property
    def remaining_today(self) -> int:
        return self.usage.remaining(self.daily_limit)

    async def acquire(self) -> Optional[int]:
        """Take a slot from the day's budget, then wait out the interval.

        Returns the budget remaining afterwards. The budget check comes first:
        sleeping twenty seconds only to then refuse would be the wrong order to
        find out.
        """
        allowed, remaining = self.usage.spend(self.daily_limit)
        if not allowed:
            raise DailyLimitReachedError(
                PROVIDER,
                "",
                f"this session's own daily ceiling of {self.daily_limit} "
                f"requests is spent. This is our limit, not Anna's — the "
                f"browser route is deliberately bounded (#144). It resets 24 "
                f"hours after the first request of the current window. Raise "
                f"ANNAS_BROWSER_DAILY_LIMIT only with a reason.",
            )
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()
        return remaining

    def penalise(self, seconds: float) -> None:
        """Push the next allowed request out, after a refusal.

        Called on a challenge or a 429/403. Backing off is not the same as
        waiting: it moves the *floor*, so the next request is late even if the
        caller asks immediately.
        """
        self._last_request = time.monotonic() + max(0.0, seconds - self.min_interval)


def visible_text(html: str) -> str:
    """The prose a reader would see, with scripts, styles and markup removed.

    Public because it is the thing the wall detector is actually about: match
    raw HTML and Anna's own source comments become evidence of a wall.
    """
    text = _SCRIPT_RE.sub(" ", html)
    text = _STYLE_RE.sub(" ", text)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _classify_page(html: str) -> Optional[str]:
    """Name the wall in a page, or None if it looks like a real page.

    Two guards, because a false positive here is far more expensive than a
    false negative: it makes every request fail, and it fails them with a
    message telling the operator to go solve a challenge that is not there.

    1. Only visible text is searched (see `visible_text`).
    2. A page carrying this site's own navigation links is a real page whatever
       else it says. A challenge interstitial has no book links in it, so their
       presence is positive evidence that cannot coexist with a wall.
    """
    if _SLOW_LINK_RE.search(html) or _BOOK_LINK_RE.search(html):
        return None
    low = visible_text(html).lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in low:
            return "challenge"
    for marker in _EXHAUSTED_MARKERS:
        if marker in low:
            return "exhausted"
    return None


class AnnasBrowserSession:
    """A serialised, rate-limited handle on one browser-resident Anna's session.

    One instance owns one browser profile. Every public method takes the same
    lock, so there is exactly one request in flight per session no matter how
    many callers there are — the repo has already learned what fanning lanes out
    against a single-session provider costs (#144), and this is the structural
    version of that lesson rather than a documented convention.
    """

    def __init__(self, config: SourceConfig):
        self.config = config
        self.base_url = config.annas_base_url.rstrip("/")
        self.host = (urlsplit(self.base_url).hostname or "").lower()
        self._lock = asyncio.Lock()
        self._limiter = _RateLimiter(
            min_interval=config.annas_browser_min_interval,
            daily_limit=config.annas_browser_daily_limit,
            # Beside the profile rather than inside it, deliberately: the
            # budget belongs to the operator, not to a browser profile. Two
            # profiles under one directory share one day's allowance, which is
            # the honest reading of "30 requests per day" — a per-profile
            # counter would let anyone multiply the ceiling by making a second
            # profile, which is the same hole as the per-process counter this
            # replaced.
            usage=DailyUsage(
                os.path.join(
                    config.annas_browser_profile_dir, "..", "annas-browser-usage.json"
                )
            ),
        )
        self._playwright = None
        self._context = None
        # Budget as it stood when the current resolution began. Reporting the
        # budget *after* spending would tell the router `downloads_left == 0`
        # for a resolution that legitimately used the last slots — and the
        # router discards such a result and raises QuotaExhaustedError, so the
        # final fully-budgeted download would fail after paying for itself
        # (Codex on #150).
        self._remaining_today = config.annas_browser_daily_limit
        self._remaining_at_start: Optional[int] = None
        # The in-process lock keeps coroutines in order; this one keeps
        # *processes* in order, which is the level that actually matters here
        # since every MCP operation gets its own bridge process.
        self._process_lock = CrossProcessLock(
            os.path.join(config.annas_browser_profile_dir, "..", "annas-browser.lock")
        )

    # -- lifecycle ---------------------------------------------------------

    async def _ensure_context(self):
        """Start the browser once, headful, on the operator's own profile."""
        if self._context is not None:
            return self._context

        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise BrowserUnavailableError(
                PROVIDER,
                self.host,
                "playwright is not installed. The browser-resident Anna's route "
                "needs it:\n"
                "  uv sync --extra annas-browser\n"
                "  uv run --extra annas-browser playwright install chrome\n"
                "(`uv run` is required for the second line: uv sync puts the "
                "console script in .venv/bin without putting it on PATH.) "
                "Anna's keyed fast_download and every LibGen route are "
                "unaffected and need none of this.",
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.config.annas_browser_profile_dir,
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as configuration
            await self._shutdown_playwright()
            raise BrowserUnavailableError(
                PROVIDER,
                self.host,
                f"could not start a browser on profile "
                f"{self.config.annas_browser_profile_dir!r}: {exc}. This route "
                f"requires a real display — #142 measured headless failing "
                f"while holding clearance that worked headful from the same "
                f"profile minutes earlier.",
            ) from exc
        return self._context

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
        await self._shutdown_playwright()

    # -- navigation --------------------------------------------------------

    async def _visit(self, page, url: str) -> str:
        """Fetch one page politely and return its HTML, or name the wall.

        The settle wait is not a guess dressed as a constant: the challenge hop
        answers 403 *while succeeding*, so a status check immediately after
        `goto` reports failure on a run that is about to work. #142 lost a
        probe to exactly that reading before the wait was long enough. Cold
        solves measured ~15s and re-solves ~35s, so the budget is configurable
        and defaults above the slower of the two.
        """
        self._remaining_today = await self._limiter.acquire()
        if self._remaining_at_start is None and self._remaining_today is not None:
            # +1 because acquire() has already taken this resolution's first
            # slot. The number is a true statement: this many were available
            # when the download started.
            self._remaining_at_start = self._remaining_today + 1
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.annas_browser_nav_timeout * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise ProviderTimeoutError(
                PROVIDER,
                self.host,
                f"navigation to {url} did not complete: {exc}",
                reason="read_timeout",
            ) from exc

        await asyncio.sleep(self.config.annas_browser_settle_seconds)
        html = await page.content()

        wall = _classify_page(html)
        if wall == "challenge":
            self._limiter.penalise(self.config.annas_browser_backoff_seconds)
            raise ChallengeNotClearedError(
                PROVIDER,
                self.host,
                "the DDoS-Guard challenge answered instead of the page. "
                "Clearance lapses after roughly 20 minutes (#142); solve it "
                "once in the visible browser window and retry. Backing off "
                f"{self.config.annas_browser_backoff_seconds:.0f}s rather than "
                "retrying into the wall.",
                # NOT http_error: that reads as retryable, and the Node retry
                # layer would spawn three fresh bridge processes whose limiters
                # have each forgotten this backoff — walking straight back into
                # the wall on the retry delays alone (Codex on #150). A person
                # clears this wall, so the person decides when to try again.
                reason="challenge_required",
            )
        if wall == "exhausted":
            self._limiter.penalise(self.config.annas_browser_backoff_seconds)
            raise ProviderRateLimitedError(
                PROVIDER,
                self.host,
                "Anna's declined further downloads for now (its own limit, not "
                "ours). Backing off rather than retrying.",
                reason="quota_exhausted",
            )
        return html

    # -- the actual job ----------------------------------------------------

    @staticmethod
    def _slow_download_paths(html: str, md5: str) -> List[str]:
        """Partner-server links on a book page, in the order Anna's lists them.

        Order is preserved rather than sorted: Anna's puts the servers it
        expects to work first, and second-guessing that would be inventing
        knowledge we do not have.
        """
        seen = []
        for match in _SLOW_LINK_RE.finditer(html):
            if match.group(1).lower() != md5.lower():
                continue
            path = match.group(0)
            if path not in seen:
                seen.append(path)
        return seen

    @staticmethod
    def _direct_file_url(page_html: str, base_host: str) -> Optional[str]:
        """The off-site payload link on a partner-server page.

        Constrained to off-site hosts on purpose: every same-host candidate is
        another Anna's page, and following one would walk the flow in a circle
        while spending rate budget on each lap.
        """
        for match in re.finditer(r'href="([^"]+)"', page_html):
            # `page.content()` serialises attribute separators as entities, so
            # a signed URL with several query parameters arrives carrying
            # `&amp;`. Sent to httpx unchanged that becomes `amp;Signature`,
            # which invalidates the signature and 403s a perfectly good link
            # (Codex on #150). The live URL that verified this route happened
            # to be path-signed and had no `&` in it at all, which is exactly
            # how a bug like this survives a green end-to-end run.
            href = html.unescape(match.group(1))
            if not href.startswith("http"):
                continue
            if not _FILE_EXTENSION_RE.search(href):
                continue
            host = (urlsplit(href).hostname or "").lower()
            if host and host != base_host:
                return href
        return None

    async def resolve_download_url(self, md5: str) -> Tuple[str, int]:
        """First working partner URL, for callers that want a single answer."""
        async for url, remaining in self.iter_download_urls(md5):
            return url, remaining
        raise ProviderResponseError(
            PROVIDER,
            self.host,
            f"no partner server yielded a payload link for md5 {md5}",
            reason="protocol_error",
        )

    async def iter_download_urls(self, md5: str):
        """Yield one payload URL per working partner server, in Anna's order.

        A stream rather than a single answer because the transfer happens
        *after* this returns: a syntactically valid URL whose CDN is dead,
        expired, or serving the wrong bytes is only discovered downstream, and
        a single return ends the candidate stream before the remaining partner
        servers are tried (Codex on #150). `ANNAS_BROWSER_MAX_SERVERS`
        advertises that failover; this is what makes it real.

        The transfer stays on the ordinary httpx path either way — the browser
        never sees the bytes.
        """
        async with self._lock:
            # A full walk is a couple of navigations plus settle time, so the
            # wait is generous: queueing behind another download is correct
            # behaviour, not a fault. `to_thread` keeps the blocking wait off
            # the event loop.
            wait = self.config.annas_browser_nav_timeout * 3
            if not await asyncio.to_thread(self._process_lock.acquire, wait):
                raise BrowserBusyError(
                    PROVIDER,
                    self.host,
                    f"another process has held the Anna's browser for more "
                    f"than {wait:.0f}s. One download at a time is deliberate "
                    f"(#144) and Chrome will not share a profile anyway. Retry "
                    f"once the other download finishes.",
                )
            # The release below must cover launch too: a browser that fails to
            # start would otherwise leave the lock held until it went stale,
            # blocking every later download for ten minutes.
            try:
                context = await self._ensure_context()
                page = await context.new_page()
            except BaseException:
                self._process_lock.release()
                raise
            try:
                book_html = await self._visit(page, f"{self.base_url}/md5/{md5}")
                candidates = self._slow_download_paths(book_html, md5)
                if not candidates:
                    raise ProviderResponseError(
                        PROVIDER,
                        self.host,
                        f"no partner-server link for md5 {md5} on its book "
                        f"page. The page loaded and the challenge did not "
                        f"fire, so this is a missing edition or a layout "
                        f"change — not a wall, and not a reason to retry.",
                        # Non-retryable on the Node side too: without that, the
                        # message saying "not a reason to retry" was followed by
                        # three retries, each spending another daily slot and
                        # settle delay on a page that will not change, and each
                        # counting toward the shared bridge circuit breaker
                        # (Codex on #150).
                        reason="not_found",
                    )

                attempts: List[str] = []
                yielded = 0
                for path in candidates[: self.config.annas_browser_max_servers]:
                    try:
                        partner_html = await self._visit(page, f"{self.base_url}{path}")
                    except (
                        ChallengeNotClearedError,
                        ProviderRateLimitedError,
                        DailyLimitReachedError,
                    ):
                        # A wall answered. Every remaining partner server sits
                        # behind the same wall, so trying them cannot succeed —
                        # it can only spend the day's budget proving it, after
                        # sleeping out the backoff each time (#144: back off on
                        # a challenge or refusal rather than retrying into it).
                        raise
                    except Exception as exc:  # noqa: BLE001 - try the next server
                        attempts.append(f"{path}: {type(exc).__name__}")
                        continue

                    url = self._direct_file_url(partner_html, self.host)
                    if url:
                        yielded += 1
                        yield url, self._remaining_at_start
                        continue
                    attempts.append(f"{path}: no payload link")

                if not yielded:
                    raise ProviderResponseError(
                        PROVIDER,
                        self.host,
                        "no partner server yielded a payload link — "
                        + " | ".join(attempts),
                        reason="protocol_error",
                    )
            finally:
                self._remaining_at_start = None
                await page.close()
                self._process_lock.release()
