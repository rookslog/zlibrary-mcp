"""Opt-in visible Anna browser search; session/usage layer ported from bfef94b.

Only Playwright startup imports the optional dependency. Search and download
URL resolution share the dedicated profile, durable request budget and OS lock.
File bytes remain on the ordinary HTTP transfer path.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import signal
import time

import httpx
from pathlib import Path
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString

from ..filename_utils import SAFE_DOCUMENT_EXTENSIONS
from .annas_display import VirtualDisplay
from .annas_usage import (
    CrossProcessLock,
    DailyUsage,
    UsageCounterUnavailableError,
    UsageWaitExceedsDeadlineError,
)
from .config import ANNAS_TRUSTED_HOSTS, SourceConfig
from .errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .net import WalkDeadline, bounded_await

logger = logging.getLogger(__name__)
PROVIDER = "annas"


class BrowserUnavailableError(ProviderConfigurationError):
    """Playwright, or a browser it can drive, is not installed.

    A configuration failure rather than an outage: it is permanent until the
    operator changes something, so it must not count as evidence that Anna's is
    unhealthy. `reason` stays `configuration_error` for that reason.
    """


class DailyLimitReachedError(ProviderResponseError):
    """This session's own per-day ceiling is spent.

    Ours, not Anna's. Hitting it is the politeness layer working, so it says so
    plainly rather than presenting as a provider failure.
    """

    reason = "quota_exhausted"


class _PacingWaitError(ProviderResponseError):
    """Keep the closed refusal reason, describing our wait rather than a wall."""

    reason = "browser_check"

    def _build_message(self) -> str:
        return f"{self.provider}: {self.detail}"


class _DownloadTimeoutError(ProviderTimeoutError):
    """Keep the existing timeout reason without its search-only gloss."""

    def _build_message(self) -> str:
        return f"{self.provider}: {self.detail}"


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

    @property
    def remaining_today(self) -> int:
        return self.usage.remaining(self.daily_limit)

    async def acquire(
        self,
        deadline: Optional[WalkDeadline] = None,
        *,
        operation: str = "browser",
        reserve: bool = True,
    ) -> Optional[int]:
        """Take a slot from the day's budget, then wait out the interval.

        Returns the budget remaining afterwards. The budget check comes first:
        sleeping twenty seconds only to then refuse would be the wrong order to
        find out.
        """
        try:
            allowed, remaining, wait = self.usage.spend(
                self.daily_limit,
                self.min_interval,
                max_wait=None if deadline is None else deadline.remaining(),
                reserve=reserve,
            )
        except UsageWaitExceedsDeadlineError as exc:
            retry_at = datetime.fromtimestamp(
                math.ceil(exc.retry_at), timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            raise _PacingWaitError(
                PROVIDER,
                "",
                f"{operation} pacing/backoff requires {exc.wait_seconds:.1f}s; "
                f"retry at {retry_at}. No browser request or daily slot was reserved.",
                reason="browser_check",
            ) from None
        except UsageCounterUnavailableError as exc:
            # A configuration_error, not an outage: permanent until the
            # operator changes something, so it must not count as evidence
            # Anna's is unhealthy, and it must not be retried.
            raise ProviderConfigurationError(PROVIDER, "", str(exc)) from None
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
        # The wait was computed and the next slot reserved inside the lock, so
        # sleeping it here cannot race another process into the same slot.
        if reserve and wait > 0:
            await asyncio.sleep(wait)
        return remaining

    def penalise(self, seconds: float) -> None:
        """Push the earliest allowed request out, after a refusal.

        Called on a challenge or a 429/403. Backing off is not the same as
        waiting: it moves the *floor*, so the next request is late even if the
        caller asks immediately — and the floor is shared state, because a
        backoff that died with its process was true of one call and false of
        the next one an operator made.
        """
        self.usage.penalise(seconds)


# Content types that say nothing about the body; a relay may also omit it.
_GENERIC_TYPES = ("", "text/plain", "application/octet-stream", "binary/octet-stream")
# A challenge page is small; result pages are not inspected past this size.
_CHALLENGE_MAX_BYTES = 256 * 1024
_MD5_RE = re.compile(r"[a-f0-9]{32}")
_SLOW_LINK_RE = re.compile(r"/slow_download/([a-f0-9]{32})/0/\d+")
_DOWNLOAD_LABEL_RE = re.compile(r"\b(?:download|save|get\s+file)\b", re.I)
_PARTNER_REFUSAL_CONFIRM_SECONDS = 3.0


def _slow_download_paths(page_html: str, md5: str) -> list[str]:
    """Port bfef94b's ordered/deduplicated partners, using actual local anchors."""
    soup = BeautifulSoup(page_html, "html.parser")
    paths = []
    for anchor in soup.select("main a[href]"):
        path = anchor["href"]
        match = _SLOW_LINK_RE.fullmatch(path)
        if match and match.group(1) == md5 and path not in paths:
            paths.append(path)
    return paths


def _hidden_link(anchor) -> bool:
    for node in (anchor, *anchor.parents):
        if (
            node.has_attr("hidden")
            or node.has_attr("disabled")
            or node.get("aria-hidden") == "true"
            or node.get("aria-disabled") == "true"
            or "hidden" in node.get("class", [])
            or re.search(
                r"display\s*:\s*none|visibility\s*:\s*hidden",
                node.get("style", ""),
                re.I,
            )
        ):
            return True
    return False


def _partner_block(soup):
    """The immediate sibling of the partner heading, excluding the book card."""
    marker = next(
        (
            p
            for p in soup.select("main p")
            if re.fullmatch(r"Slow Partner Server #\d+", p.get_text(" ", strip=True))
        ),
        None,
    )
    return marker.find_next_sibling() if marker is not None else None


def _direct_file_url(page_html, base_host: str) -> Optional[str]:
    """A visible selected file link wins over incidental countdown evidence."""
    soup = (
        BeautifulSoup(page_html, "html.parser")
        if isinstance(page_html, str)
        else page_html
    )
    block = _partner_block(soup)
    block_links = (
        []
        if block is None
        else ([block] if block.name == "a" else block.select("a[href]"))
    )
    ranked, opaque = [], []
    for anchor in soup.select("main a[href]"):
        if _hidden_link(anchor):
            continue
        in_block = any(anchor is candidate for candidate in block_links)
        label = anchor.get_text(" ", strip=True)
        download_now = bool(re.search(r"\bdownload\s+now\b", label, re.I))
        if not in_block and not download_now:
            continue
        href = anchor["href"].strip()  # BeautifulSoup decodes &amp; in signed URLs.
        try:
            parsed = urlsplit(href)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (
                parsed.scheme not in ("http", "https")
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or host in ANNAS_TRUSTED_HOSTS
                or host == base_host
                or host == "localhost"
                or host.endswith((".localhost", ".local"))
            ):
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                pass  # Hostname, resolved only by the existing HTTP transfer.
            parsed.port  # Reject malformed/out-of-range ports without requesting.
        except ValueError:
            continue
        suffix = Path(unquote(parsed.path)).suffix.lstrip(".").lower()
        if download_now:
            ranked.append((0 if in_block else 1, href))
        elif in_block and (
            suffix in SAFE_DOCUMENT_EXTENSIONS or _DOWNLOAD_LABEL_RE.search(label)
        ):
            ranked.append((2, href))
        elif in_block:
            opaque.append(href)
    return (
        min(ranked, key=lambda item: item[0])[1]
        if ranked
        else (opaque[0] if len(opaque) == 1 else None)
    )


def _partner_section_text(soup) -> str:
    """Whole partner section, including later siblings, with book cards omitted.

    A semantic section is authoritative. Otherwise walk forward from the
    partner heading (even out of its wrapper), stopping at a peer/higher
    heading. While the heading has not appeared, use main without its cards.
    """
    main = soup.select_one("main")
    if main is None:
        return ""
    headings = ("h1", "h2", "h3", "h4", "h5", "h6")
    marker = next(
        (
            node
            for node in main.find_all(("p", *headings))
            if re.fullmatch(r"Slow Partner Server #\d+", node.get_text(" ", strip=True))
        ),
        None,
    )
    section = marker.find_parent("section") if marker is not None else None
    if section is not None and main not in section.parents:
        section = None
    root = section if section is not None else main
    cards = list(root.select("article"))
    for anchor in root.select("a.custom-a[href^='/md5/']"):
        card = anchor.parent
        # An unwrapped card link must not turn all of main into a book card.
        if card is root or (marker is not None and marker in card.descendants):
            card = anchor
        cards.append(card)
    excluded = {id(node) for card in cards for node in (card, *card.descendants)}
    nodes = (
        marker.next_elements
        if marker is not None and section is None
        else root.descendants
    )
    level = int(marker.name[1]) if marker is not None and marker.name in headings else 6
    text = []
    for node in nodes:
        if root not in node.parents:
            break
        if id(node) in excluded:
            continue
        if (
            marker is not None
            and section is None
            and getattr(node, "name", None) in headings
        ):
            if int(node.name[1]) <= level:
                break
        if (
            isinstance(node, NavigableString)
            and not isinstance(node, Comment)
            and node.parent.name not in ("script", "style")
            and not _hidden_link(node.parent)
        ):
            text.append(str(node))
    return " ".join(text)


def _partner_countdown(soup) -> bool:
    """Wait evidence after ready-link selection, with refusal taking precedence.

    Explicit markers remain usable while JavaScript fills in a number. Free
    text belongs only to the partner section, never the book card.
    """
    text = _partner_section_text(soup)
    if re.search(
        r"\b(?:too\s+many\s+requests|rate[\s-]*limit(?:ed|ing)?|"
        r"downloads?\s+refused|access\s+denied|try\s+again\s+later)\b",
        text,
        re.I,
    ):
        return False
    for node in soup.select(
        "main .js-download-countdown, main .js-countdown, main #countdown, "
        "main [data-countdown], main .js-partner-countdown"
    ):
        if not _hidden_link(node):
            return True
    return bool(
        re.search(
            r"\b(?:wait(?:\s+for)?|(?:ready|available|will\s+start)\s+in)\s+"
            r"(?:\d+(?:\.\d+)?\s*)?(?:seconds?|minutes?)\b",
            text,
            re.I,
        )
    )


def _looks_like_html(response) -> bool:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if "html" in kind:
        return True
    if kind not in _GENERIC_TYPES:
        return False  # e.g. application/json from the fast-download API
    head = response.content[:1024].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


def _challenge_page(text: str, *, soup=None) -> bool:
    """Structure of a DDoS-Guard challenge, not words in a result.

    No result rows (`/md5/` links), a title or heading that names DDoS-Guard
    or says "Checking your browser", and not an Anna's Archive page title
    (which echoes the query). A result titled "Checking Your Browser" stays a
    result (review of 6ad5cfb, F3).
    """
    if soup is None:
        soup = BeautifulSoup(text, "html.parser")
    if soup.select_one("a[href^='/md5/']"):
        return False
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    # Recognize the site's title branding, not its domain embedded in a
    # challenge title (REVIEW-2 N1). Empty search titles can echo the query.
    if re.search(r"(?:^|\s[-–—|]\s)anna['’]s archive$", title):
        return False
    headings = [title] + [
        h.get_text(" ", strip=True).lower() for h in soup.find_all(["h1", "h2"])
    ]
    return "ddos-guard" in title or any(
        h.startswith("checking your browser") for h in headings
    )


def is_browser_check(response) -> bool:
    """Whether DDoS-Guard answered instead of Anna's Archive.

    Require an HTML or HTML-looking body shaped like the challenge page,
    including missing/generic content types. A DDoS-Guard server header is
    not evidence of a check: it can also relay an ordinary origin error.
    """
    if len(response.content) > _CHALLENGE_MAX_BYTES or not _looks_like_html(response):
        return False
    return _challenge_page(response.text)


def _settled_search_page(soup) -> bool:
    """A form can arrive before its streamed result cards and partial matches.

    Require the trailing footer and every row announced by the result headings.
    Unknown/localized markup refuses conservatively rather than claiming empty.
    Lazy cover images and incidental "loading" prose are not list state.
    """
    form = soup.select_one("form.js-search-form[role='search']")
    if form is None or _challenge_page("", soup=soup) or not form.find_next("footer"):
        return False
    lists = form.select(".js-aarecord-list-outer")
    number = r"[\d,]+"
    range_re = re.compile(rf"Results ({number})-({number}) \(.+ total\)")
    partial_re = re.compile(rf"({number})\+? partial matches")
    ranges = [
        match
        for node in form.select("div")
        if (match := range_re.fullmatch(node.get_text(" ", strip=True)))
    ]
    no_matches = any(
        node.get_text(" ", strip=True) == "No files found."
        and not _hidden_link(node)
        and node.find_parent("a") is None
        and node.find_parent(class_="js-aarecord-list-outer") is None
        for node in form.select("div > span.font-bold")
    )
    if len(ranges) > 1 or (not ranges and not no_matches):
        return False
    expected = 0
    if ranges:
        first, last = (int(value.replace(",", "")) for value in ranges[0].groups())
        expected = last - first + 1
        if expected <= 0 or not lists:
            return False
    for index, node in enumerate(lists):
        partial = next(
            (
                match
                for heading in node.select("div")
                if (match := partial_re.fullmatch(heading.get_text(" ", strip=True)))
            ),
            None,
        )
        count = len(
            {
                link["href"]
                for link in node.select("a[href^='/md5/']")
                if link.get_text(strip=True)
            }
        )
        want = int(partial[1].replace(",", "")) if partial else expected
        if (index > 0 and partial is None) or count != want:
            return False
    return True


def needs_human_check(page_html: str) -> bool:
    """Recognize the saved manual DDoS-Guard check without interacting with it."""
    soup = BeautifulSoup(page_html, "html.parser")
    if soup.select_one("#ddg-captcha, #ddg-iframe"):
        return True
    # Preserve the challenge detector's form rule even while results stream.
    if soup.select_one("form.js-search-form[role='search']") and not _challenge_page(
        "", soup=soup
    ):
        return False
    return "complete the manual check" in soup.get_text(" ", strip=True).lower()


class FetchHTML(str):
    """HTML with an internal `method` hook (`browser` or `session`).

    String compatibility preserves existing callers and public reply shapes.
    """

    def __new__(cls, html: str, method: str):
        value = super().__new__(cls, html)
        value.method = method
        return value


@dataclass(repr=False)
class _EarnedSession:
    cookies: list
    user_agent: str
    expires: float


class AnnasBrowserSession:
    """Serialized browser owner; Chrome closes before releasing the OS lock."""

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
                ),
                # A synchronous five-second lock wait would block the event
                # loop past a short owner deadline. Counter contention refuses
                # without spending a navigation; it never removes the limit.
                lock_timeout=0,
            ),
        )
        self._playwright = None
        self._context = None
        self._browser = None
        self._display = None
        self._earned_session = None
        self.last_fetch_method = None
        # The in-process lock keeps coroutines in order; this one keeps
        # *processes* in order, which is the level that actually matters here
        # since every MCP operation gets its own bridge process.
        self._process_lock = CrossProcessLock(
            os.path.join(config.annas_browser_profile_dir, "..", "annas-browser.lock")
        )

    async def _ensure_context(self):
        """Start visible Chrome lazily on the dedicated Anna profile."""
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
                "  uv run --no-sync playwright install chrome\n"
                "(`uv run` is required for the second line: uv sync puts the "
                "console script in .venv/bin without putting it on PATH; "
                "`--no-sync` preserves whichever dependency tier is already "
                "installed.) "
                "Anna's keyed fast_download and every LibGen route are "
                "unaffected and need none of this.",
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            options = dict(
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            if self.config.annas_browser_display == "virtual":
                self._display = VirtualDisplay(self.config.annas_xvfb_path)
                options["env"] = {**os.environ, "DISPLAY": await self._display.start()}
            if self.config.annas_session_reuse:
                # Incognito context: earned clearance must never enter the
                # existing persistent profile or a storage_state file.
                self._browser = await self._playwright.chromium.launch(**options)
                self._context = await self._browser.new_context()
            else:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        self.config.annas_browser_profile_dir, **options
                    )
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as configuration
            await self.close()
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

    def _driver_process(self):
        # Playwright 1.64.1 PipeTransport owns this asyncio child process.
        # Optional/internal access: missing support must not block graceful close.
        impl = getattr(self._playwright, "_impl_obj", None)
        connection = getattr(impl, "_connection", None)
        transport = getattr(connection, "_transport", None)
        return getattr(transport, "_proc", None)

    @staticmethod
    def _pin_owned_processes(driver):
        """Pin only this driver's live descendant identities on Linux."""
        if (
            driver is None
            or driver.returncode is not None
            or not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"))
        ):
            return []
        pinned = []
        todo = [(driver.pid, os.getpid(), None)]
        seen = set()
        while todo and len(seen) < 256:
            pid, parent_pid, parent_fd = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            fd = None
            try:
                if parent_fd is not None:
                    import select

                    if select.select([parent_fd], [], [], 0)[0]:
                        continue  # Parent exited; never follow a reused PID.
                fd = os.pidfd_open(pid)
                proc = Path(f"/proc/{pid}")
                parent = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
                if parent != parent_pid:
                    os.close(fd)
                    continue
                pinned.append(fd)
                children = (proc / "task" / str(pid) / "children").read_text().split()
                todo.extend((int(child), pid, fd) for child in children)
            except (OSError, ValueError):
                if fd is not None and fd not in pinned:
                    os.close(fd)
        return pinned

    @staticmethod
    def _force_kill_owned(pinned, driver):
        # pidfds keep identity even if a graceful close exited and PID was reused.
        for fd in reversed(pinned):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning(
                    "Could not terminate owned Anna browser process: %s", exc
                )
        if not pinned and driver is not None and driver.returncode is None:
            # Non-Linux / missing pidfd support: terminate the owned child driver.
            # Descendant termination on those platforms is not qualified here.
            try:
                driver.kill()
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning("Could not terminate owned Anna driver: %s", exc)

    async def _bounded_close(self, operation, seconds):
        if seconds <= 0:
            return False
        task = None
        try:
            task = asyncio.create_task(operation())
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if task not in done:
                return False
            task.result()
            return True
        except Exception as exc:
            logger.warning("Anna browser cleanup did not finish: %s", exc)
            return False
        finally:
            if task is not None and not task.done():
                task.cancel()
                # Do not wait indefinitely for cancellation acknowledgement.
                task.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )

    async def close(self, *, page=None, deadline=None, keep_session=False) -> None:
        """Bound page/context/driver cleanup, forcing owned processes on failure."""
        if not keep_session:
            self._earned_session = None
        budget = 2.0 if deadline is None else min(2.0, max(0.0, deadline.remaining()))
        cleanup = WalkDeadline(budget)
        driver = self._driver_process()
        pinned = self._pin_owned_processes(driver)
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        browser, self._browser = self._browser, None
        display, self._display = self._display, None
        operations = (
            ([page.close] if page is not None else [])
            + ([context.close] if context is not None else [])
            + ([browser.close] if browser is not None else [])
            + ([playwright.stop] if playwright is not None else [])
        )
        force = True
        try:
            complete = True
            for index, operation in enumerate(operations):
                complete = (
                    await self._bounded_close(
                        operation, cleanup.remaining() / (len(operations) - index)
                    )
                    and complete
                )
            force = not complete
        finally:
            if force:
                self._force_kill_owned(pinned, driver)
            for fd in pinned:
                os.close(fd)
            if display is not None:
                await display.close()

    def _served(self, html, method):
        self.last_fetch_method = method
        logger.info("Anna page fetch method=%s", method)
        return FetchHTML(html, method)

    async def _reuse_search(self, url, deadline):
        earned = self._earned_session
        if earned is None:
            return None
        if time.monotonic() >= earned.expires:
            self._earned_session = None
            return None
        # Both this request and any browser fallback reserve their own page
        # slot under the same process lock and pacing clock.
        await self._limiter.acquire(deadline, operation="session search")

        async def same_origin(request):
            if (request.url.scheme, request.url.host, request.url.port) != (
                httpx.URL(self.base_url).scheme,
                httpx.URL(self.base_url).host,
                httpx.URL(self.base_url).port,
            ):
                raise ValueError("Anna session redirect left its origin")

        jar = httpx.Cookies()
        for cookie in earned.cookies:
            jar.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
            )
        try:
            budget = min(self.config.total_timeout, deadline.remaining() / 2)
            async with httpx.AsyncClient(
                cookies=jar,
                headers={"User-Agent": earned.user_agent},
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(
                    self.config.read_timeout, connect=self.config.connect_timeout
                ),
                event_hooks={"request": [same_origin]},
                trust_env=False,
            ) as client:
                response = await bounded_await(
                    client.get(url),
                    budget,
                    provider=PROVIDER,
                    host=self.host,
                    operation="session search",
                )
            if (
                response.status_code == 200
                and not needs_human_check(response.text)
                and _settled_search_page(BeautifulSoup(response.text, "html.parser"))
            ):
                return self._served(response.text, "session")
        except asyncio.CancelledError:
            self._earned_session = None
            raise
        except Exception:
            # Never log client exceptions: they may contain private headers.
            pass
        self._earned_session = None
        return None

    async def search_html(
        self, query: str, deadline: Optional[WalkDeadline] = None
    ) -> str:
        """Read settled search HTML within the caller's remaining walk clock."""
        budget = self.config.annas_browser_search_timeout
        if deadline is not None:
            budget = min(budget, max(0.0, deadline.remaining()))
        local_deadline = WalkDeadline(budget)
        operation_budget = max(0.0, budget - min(2.0, budget / 4))
        operation_deadline = WalkDeadline(operation_budget)
        return await self._search_html(
            query, deadline=operation_deadline, cleanup_deadline=local_deadline
        )

    async def _search_html(
        self, query: str, *, deadline: WalkDeadline, cleanup_deadline: WalkDeadline
    ) -> str:
        url = f"{self.base_url}/search?q={quote(query)}"

        async def search(page, operation_deadline):
            html = await self._visit(page, url)
            if self.config.annas_session_reuse:
                try:
                    cookies = await self._context.cookies([self.base_url])
                    user_agent = await page.evaluate("navigator.userAgent")
                    self._earned_session = _EarnedSession(
                        cookies,
                        user_agent,
                        time.monotonic() + self.config.annas_session_ttl,
                    )
                except Exception:
                    self._earned_session = None
            return self._served(html, "browser")

        async def reuse(operation_deadline):
            if self.config.annas_session_reuse:
                return await self._reuse_search(url, operation_deadline)
            return None

        return await self._run_page(search, deadline, cleanup_deadline, reuse=reuse)

    async def _run_page(
        self,
        visit,
        deadline,
        cleanup_deadline,
        *,
        operation="browser search",
        reuse=None,
    ):
        """Reserve the first visit and own its page/driver/locks through cleanup."""
        page = None
        session_locked = False
        process_locked = False

        async def run_operation():
            nonlocal page, session_locked, process_locked
            await self._lock.acquire()
            session_locked = True
            try:
                while not self._process_lock.acquire(0, strict=True):
                    await asyncio.sleep(0.05)
            except UsageCounterUnavailableError as exc:
                raise ProviderConfigurationError(PROVIDER, self.host, str(exc)) from exc
            process_locked = True
            try:
                with tempfile.TemporaryFile(dir=self.config.annas_browser_profile_dir):
                    pass
            except OSError as exc:
                raise ProviderConfigurationError(
                    PROVIDER, self.host, f"unusable Anna's browser profile: {exc}"
                ) from exc
            # Refuse known quota/backoff/counter failures before starting Chrome,
            # without spending a slot that a failed launch cannot use.
            await self._limiter.acquire(deadline, operation=operation, reserve=False)
            if reuse is not None:
                reused = await reuse(deadline)
                if reused is not None:
                    return reused
            context = await self._ensure_context()
            self._context = context
            page = await context.new_page()
            # Another profile can share this counter: recheck and reserve
            # atomically only when this context and page are ready to visit.
            await self._limiter.acquire(deadline, operation=operation)
            return await visit(page, deadline)

        try:
            # The operation timer cancels navigation/lock waits, not cleanup.
            # Cleanup spends only its reserved share of the same owner clock.
            return await bounded_await(
                run_operation(),
                deadline.remaining(),
                provider=PROVIDER,
                host=self.host,
                operation=operation,
            )
        except ProviderTimeoutError as exc:
            self._earned_session = None
            if operation == "browser download":
                raise _DownloadTimeoutError(
                    exc.provider, exc.host, exc.detail, reason=exc.reason
                ) from None
            raise
        except BaseException:
            self._earned_session = None
            raise
        finally:
            try:
                if process_locked:
                    try:
                        await self.close(
                            page=page, deadline=cleanup_deadline, keep_session=True
                        )
                    finally:
                        self._process_lock.release()
            finally:
                if session_locked:
                    self._lock.release()

    async def iter_download_urls(
        self, md5: str, deadline: Optional[WalkDeadline] = None
    ) -> AsyncIterator[str]:
        """Read detail once, then partners in site order; close before every yield."""
        md5 = md5.strip().lower()
        if not _MD5_RE.fullmatch(md5):
            raise ValueError("Anna's browser downloads require a 32-hex MD5")
        resolution = WalkDeadline(
            self.config.download_timeout,
            remaining_limit=(
                None
                if deadline is None
                else lambda left: min(left, deadline.remaining())
            ),
        )
        pending = None
        last_failure = None

        async def resolve(page, operation_deadline):
            nonlocal pending, last_failure
            needs_slot = False  # _run_page reserved the first visit.
            if pending is None:
                book = await self._visit(
                    page, f"{self.base_url}/md5/{md5}", kind="detail"
                )
                pending = _slow_download_paths(book, md5)[
                    : self.config.annas_browser_max_servers
                ]
                if not pending:
                    raise ProviderResponseError(
                        PROVIDER,
                        self.host,
                        f"no slow-partner link for md5 {md5}",
                        reason="not_found",
                    )
                needs_slot = True
            while pending:
                path = pending.pop(0)
                if needs_slot:
                    await self._limiter.acquire(
                        operation_deadline, operation="browser download"
                    )
                needs_slot = True
                try:
                    partner = await self._visit(
                        page, f"{self.base_url}{path}", kind="partner"
                    )
                    return _direct_file_url(partner, self.host)
                except (ProviderResponseError, ProviderTimeoutError) as exc:
                    if exc.reason in ("browser_check", "quota_exhausted"):
                        raise
                    last_failure = exc
            if last_failure is not None:
                raise last_failure
            return None

        while pending is None or pending:
            budget = resolution.remaining()
            cleanup_deadline = WalkDeadline(budget)
            operation_deadline = WalkDeadline(max(0.0, budget - min(2.0, budget / 4)))
            url = await self._run_page(
                resolve,
                operation_deadline,
                cleanup_deadline,
                operation="browser download",
            )
            if url is not None:
                with resolution.suspended():
                    yield url

    def _browser_check(self, detail: str) -> ProviderResponseError:
        self._limiter.penalise(self.config.annas_browser_backoff_seconds)
        return ProviderResponseError(
            PROVIDER,
            self.host,
            detail + " No check was clicked. Backing off before another attempt.",
            reason="browser_check",
        )

    async def _visit(self, page, url: str, *, kind: str = "search") -> str:
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.annas_browser_nav_timeout * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ProviderTimeoutError(
                PROVIDER,
                self.host,
                f"browser navigation did not complete: {exc}",
                reason="read_timeout",
            ) from exc
        status = getattr(response, "status", None)
        loop = asyncio.get_running_loop()
        expires = loop.time() + self.config.annas_browser_settle_seconds
        refusal_since = None
        while True:
            try:
                page_html = await page.content()
            except asyncio.CancelledError:
                raise
            except Exception:
                # An automatic check can navigate while content is read.
                # Retry the DOM read only; never reload or click the check.
                page_html = None
            if page_html is None:
                refusal_since = None
            if page_html is not None:
                soup = BeautifulSoup(page_html, "html.parser")
                challenge = _challenge_page("", soup=soup)
                pending_search = (
                    kind == "search"
                    and bool(soup.select_one("form.js-search-form[role='search']"))
                    and not challenge
                )
                ready = (
                    _settled_search_page(soup)
                    if kind == "search"
                    else (
                        bool(soup.select_one("main [id^='cover_aarecord_id__md5:']"))
                        if kind == "detail"
                        else bool(_direct_file_url(soup, self.host))
                    )
                )
                if (
                    ready
                    and not soup.select_one("#ddg-captcha, #ddg-iframe")
                    and not challenge
                ):
                    # Settled content wins over incidental manual-check prose
                    # in a result, and over an initial challenge-hop status.
                    return page_html
                if needs_human_check(page_html):
                    raise self._browser_check(
                        "Anna's asks for a person in the visible Chrome window."
                    )
                pending_partner = kind == "partner" and _partner_countdown(soup)
                confirming_refusal = False
                if kind == "partner" and status == 403:
                    if pending_partner or challenge:
                        refusal_since = None
                    elif refusal_since is None:
                        # A 403 hop can precede JS countdown content. The first
                        # no-countdown read can never confirm a refusal alone.
                        refusal_since = loop.time()
                        confirming_refusal = True
                    else:
                        confirming_refusal = (
                            loop.time() - refusal_since
                            < _PARTNER_REFUSAL_CONFIRM_SECONDS
                        )
                if (
                    not challenge
                    and status is not None
                    and status >= 400
                    # The initial check hop can precede an unfinished search.
                    and not pending_search
                    # A challenge hop can answer 403 before the genuine
                    # partner countdown loads. Wait for its visible file link.
                    and not (status == 403 and (pending_partner or confirming_refusal))
                ):
                    if status in (403, 429, 503):
                        raise self._browser_check(
                            f"Anna's refused browser navigation (HTTP {status})."
                        )
                    raise ProviderResponseError(
                        PROVIDER,
                        self.host,
                        f"HTTP {status} from browser {kind}",
                        reason="http_error",
                    )
            left = expires - loop.time()
            if left <= 0:
                if page_html is not None and _challenge_page(page_html):
                    raise self._browser_check(
                        "The browser check did not clear before its settle timeout."
                    )
                if page_html is not None and pending_search:
                    raise ProviderTimeoutError(
                        PROVIDER,
                        self.host,
                        "Anna search result list did not finish before its settle timeout.",
                        reason="read_timeout",
                    )
                raise ProviderResponseError(
                    PROVIDER,
                    self.host,
                    f"No settled Anna {kind} page before its settle timeout.",
                    reason="protocol_error",
                )
            await asyncio.sleep(min(0.25, left))
