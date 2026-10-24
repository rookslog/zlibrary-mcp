"""Opt-in visible Anna browser search; session/usage layer ported from bfef94b.

Only Playwright startup imports the optional dependency. Search shares the
operator's dedicated profile, durable request budget and OS lock. Download
resolution is intentionally deferred to a separate slice.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import signal
from pathlib import Path
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .annas_usage import (
    CrossProcessLock,
    DailyUsage,
    UsageCounterUnavailableError,
    UsageWaitExceedsDeadlineError,
)
from .config import SourceConfig
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

    async def acquire(self, deadline: Optional[WalkDeadline] = None) -> Optional[int]:
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
            )
        except UsageWaitExceedsDeadlineError as exc:
            retry_at = datetime.fromtimestamp(
                math.ceil(exc.retry_at), timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            raise ProviderResponseError(
                PROVIDER,
                "",
                f"browser pacing/backoff requires {exc.wait_seconds:.1f}s; "
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
        if wait > 0:
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
    return bool(
        soup.select_one("form.js-search-form[role='search']")
    ) and not _challenge_page("", soup=soup)


def needs_human_check(page_html: str) -> bool:
    """Recognize the saved manual DDoS-Guard check without interacting with it."""
    soup = BeautifulSoup(page_html, "html.parser")
    if _settled_search_page(soup):
        return False
    return bool(
        soup.select_one("#ddg-captcha, #ddg-iframe")
        or "complete the manual check" in soup.get_text(" ", strip=True).lower()
    )


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
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.config.annas_browser_profile_dir,
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as configuration
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

    async def close(self, *, page=None, deadline=None) -> None:
        """Bound page/context/driver cleanup, forcing owned processes on failure."""
        budget = 2.0 if deadline is None else min(2.0, max(0.0, deadline.remaining()))
        cleanup = WalkDeadline(budget)
        driver = self._driver_process()
        pinned = self._pin_owned_processes(driver)
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        operations = (
            ([page.close] if page is not None else [])
            + ([context.close] if context is not None else [])
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
        page = None
        session_locked = False
        process_locked = False

        async def operation():
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
            await self._limiter.acquire(deadline)
            context = await self._ensure_context()
            page = await context.new_page()
            return await self._visit(page, f"{self.base_url}/search?q={quote(query)}")

        try:
            # The operation timer cancels navigation/lock waits, not cleanup.
            # Cleanup spends only its reserved share of the same owner clock.
            return await bounded_await(
                operation(),
                deadline.remaining(),
                provider=PROVIDER,
                host=self.host,
                operation="browser search",
            )
        finally:
            try:
                if process_locked:
                    try:
                        await self.close(page=page, deadline=cleanup_deadline)
                    finally:
                        self._process_lock.release()
            finally:
                if session_locked:
                    self._lock.release()

    def _browser_check(self, detail: str) -> ProviderResponseError:
        self._limiter.penalise(self.config.annas_browser_backoff_seconds)
        return ProviderResponseError(
            PROVIDER,
            self.host,
            detail + " No check was clicked. Backing off before another attempt.",
            reason="browser_check",
        )

    async def _visit(self, page, url: str) -> str:
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
        while True:
            try:
                page_html = await page.content()
            except asyncio.CancelledError:
                raise
            except Exception:
                # An automatic check can navigate while content is read.
                # Retry the DOM read only; never reload or click the check.
                page_html = None
            if page_html is not None:
                soup = BeautifulSoup(page_html, "html.parser")
                if _settled_search_page(soup):
                    # Settled content wins over incidental manual-check prose
                    # in a result, and over an initial challenge-hop status.
                    return page_html
                if needs_human_check(page_html):
                    raise self._browser_check(
                        "Anna's asks for a person in the visible Chrome window."
                    )
                if (
                    not _challenge_page(page_html)
                    and status is not None
                    and status >= 400
                ):
                    if status in (403, 429, 503):
                        raise self._browser_check(
                            f"Anna's refused browser navigation (HTTP {status})."
                        )
                    raise ProviderResponseError(
                        PROVIDER,
                        self.host,
                        f"HTTP {status} from browser search",
                        reason="http_error",
                    )
            left = expires - loop.time()
            if left <= 0:
                if page_html is not None and _challenge_page(page_html):
                    raise self._browser_check(
                        "The browser check did not clear before its settle timeout."
                    )
                raise ProviderResponseError(
                    PROVIDER,
                    self.host,
                    "No settled Anna search page before its settle timeout.",
                    reason="protocol_error",
                )
            await asyncio.sleep(min(0.25, left))
