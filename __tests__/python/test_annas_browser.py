"""Tests for the browser-resident Anna's route (#143) and its limits (#144).

Playwright is mocked throughout, per the repo invariant that unit tests mock
every third-party call. That is a deliberate limitation with a stated boundary:
these tests pin the *logic* — link selection, wall classification, rate
limiting, error typing — and say nothing about whether a real browser clears a
real challenge. #142 measured that separately, and only a live run can.

The rate-limiter tests use tiny intervals so the suite stays fast. The
production defaults are deliberately slow (20s spacing, 30 requests/day) and
`test_defaults_are_slow_on_purpose` guards them, because a future change that
"tunes" them for test speed would silently remove the politeness this route's
scope is conditional on.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.sources.annas_browser import (  # noqa: E402
    AnnasBrowserSession,
    BrowserUnavailableError,
    ChallengeNotClearedError,
    DailyLimitReachedError,
    ProviderRateLimitedError,
    _classify_page,
    _RateLimiter,
)
from lib.sources.config import SourceConfig  # noqa: E402
from lib.sources.errors import ProviderResponseError  # noqa: E402

pytestmark = pytest.mark.unit

MD5 = "a" * 32


def _config(**overrides) -> SourceConfig:
    base = dict(
        annas_base_url="https://annas-archive.gl",
        annas_browser_enabled=True,
        annas_browser_min_interval=0.0,
        annas_browser_settle_seconds=0.0,
        annas_browser_daily_limit=100,
        annas_browser_max_servers=3,
    )
    base.update(overrides)
    return SourceConfig(**base)


class _FakePage:
    """A page that returns queued bodies, one per `goto`."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.visited = []
        self.closed = False

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        if not self._bodies:
            raise AssertionError(f"unexpected extra navigation to {url}")
        self._current = self._bodies.pop(0)
        if isinstance(self._current, Exception):
            raise self._current
        return None

    async def content(self):
        return self._current

    async def close(self):
        self.closed = True


def _session(bodies, **overrides) -> AnnasBrowserSession:
    session = AnnasBrowserSession(_config(**overrides))
    page = _FakePage(bodies)

    class _Ctx:
        async def new_page(self):
            return page

    session._context = _Ctx()
    session._page = page  # for assertions
    return session


BOOK_PAGE = f"""
<html><body>
  <a href="/slow_download/{MD5}/0/0">Slow Partner Server #1</a>
  <a href="/slow_download/{MD5}/0/1">Slow Partner Server #2</a>
  <a href="/slow_download/{"b" * 32}/0/0">a different book</a>
</body></html>
"""

PARTNER_PAGE = """
<html><body>
  <a href="/faq">Anna's FAQ</a>
  <a href="https://cdn3.example.net/d/9f2/Plotinus-Enneads.pdf?sig=abc">Download now</a>
</body></html>
"""


class TestLinkSelection:
    """The browser's whole job is picking the right link off Anna's own pages."""

    def test_only_this_book_s_partner_links_are_taken(self):
        paths = AnnasBrowserSession._slow_download_paths(BOOK_PAGE, MD5)

        assert paths == [f"/slow_download/{MD5}/0/0", f"/slow_download/{MD5}/0/1"], (
            "another book's link on the same page must not be followed — it "
            "would spend rate budget and download the wrong file"
        )

    def test_annas_own_ordering_is_preserved(self):
        page = (
            f'<a href="/slow_download/{MD5}/0/5">e</a>'
            f'<a href="/slow_download/{MD5}/0/2">b</a>'
        )

        assert AnnasBrowserSession._slow_download_paths(page, MD5) == [
            f"/slow_download/{MD5}/0/5",
            f"/slow_download/{MD5}/0/2",
        ], (
            "Anna's lists the servers it expects to work first; sorting invents knowledge"
        )

    def test_duplicate_links_are_visited_once(self):
        page = f'<a href="/slow_download/{MD5}/0/0">x</a>' * 3

        assert len(AnnasBrowserSession._slow_download_paths(page, MD5)) == 1

    def test_the_payload_link_must_be_off_site(self):
        """Same-host candidates are other Anna's pages, not the file."""
        page = (
            '<a href="https://annas-archive.gl/md5/deadbeef.pdf">not the file</a>'
            '<a href="https://cdn9.example.org/x/book.epub">the file</a>'
        )

        url = AnnasBrowserSession._direct_file_url(page, "annas-archive.gl")

        assert url == "https://cdn9.example.org/x/book.epub"

    def test_no_payload_link_returns_none_rather_than_a_page_url(self):
        page = '<a href="https://annas-archive.gl/faq">faq</a><a href="/x">y</a>'

        assert AnnasBrowserSession._direct_file_url(page, "annas-archive.gl") is None


class TestWallClassification:
    """A wall and a missing book need opposite responses, so they need names."""

    @pytest.mark.parametrize(
        "body",
        [
            "<h1>Checking your browser before accessing</h1>",
            "<p>DDoS-Guard</p>",
            "<div>Please enable JavaScript and cookies to continue</div>",
        ],
    )
    def test_challenge_bodies_are_named(self, body):
        assert _classify_page(body) == "challenge"

    def test_annas_own_limit_is_distinguished_from_the_challenge(self):
        assert _classify_page("You have downloaded too many files today") == "exhausted"

    def test_an_ordinary_page_is_not_a_wall(self):
        assert _classify_page(BOOK_PAGE) is None


class TestRateLimiter:
    """#144 is structural here, not a documented convention."""

    def test_defaults_are_slow_on_purpose(self):
        """Guard the production numbers against being 'tuned' for test speed.

        The scope reversal that put this route in scope is conditional on these
        limits shipping with it. A change that quietly widened them would make
        the politeness claim rhetorical, which is the one outcome the doctrine
        entry exists to prevent.
        """
        config = SourceConfig()

        assert config.annas_browser_min_interval >= 20.0
        assert config.annas_browser_daily_limit <= 30
        assert config.annas_browser_backoff_seconds >= 300.0
        assert config.annas_browser_enabled is False, "must stay opt-in"

    @pytest.mark.asyncio
    async def test_requests_are_spaced(self):
        limiter = _RateLimiter(min_interval=0.05, daily_limit=10)
        loop = asyncio.get_running_loop()

        await limiter.acquire()
        start = loop.time()
        await limiter.acquire()

        assert loop.time() - start >= 0.04

    @pytest.mark.asyncio
    async def test_the_daily_ceiling_refuses_rather_than_sleeping(self):
        limiter = _RateLimiter(min_interval=0.0, daily_limit=2)

        await limiter.acquire()
        await limiter.acquire()

        with pytest.raises(DailyLimitReachedError) as excinfo:
            await limiter.acquire()

        assert excinfo.value.reason == "quota_exhausted"
        assert "our limit, not Anna's" in str(excinfo.value) or "not Anna" in str(
            excinfo.value
        ), "the message must not read as a provider failure"

    @pytest.mark.asyncio
    async def test_backing_off_moves_the_floor_not_just_the_clock(self):
        limiter = _RateLimiter(min_interval=0.0, daily_limit=10)
        await limiter.acquire()

        limiter.penalise(60.0)

        assert limiter._last_request > asyncio.get_running_loop().time() - 1, (
            "penalise must push the next allowed request into the future; a "
            "no-op here means retrying straight back into the wall"
        )

    def test_remaining_is_reported_so_the_caller_can_see_the_budget(self):
        limiter = _RateLimiter(min_interval=0.0, daily_limit=4)

        assert limiter.remaining_today == 4


class TestResolveDownloadUrl:
    @pytest.mark.asyncio
    async def test_the_happy_path_returns_a_url_and_the_remaining_budget(self):
        session = _session([BOOK_PAGE, PARTNER_PAGE])

        url, remaining = await session.resolve_download_url(MD5)

        assert url.startswith("https://cdn3.example.net/")
        assert remaining == 98, "two navigations spent, out of a 100 ceiling"
        assert session._page.visited == [
            f"https://annas-archive.gl/md5/{MD5}",
            f"https://annas-archive.gl/slow_download/{MD5}/0/0",
        ]

    @pytest.mark.asyncio
    async def test_the_browser_never_fetches_the_file(self):
        """The transfer belongs to httpx, which already verifies content md5."""
        session = _session([BOOK_PAGE, PARTNER_PAGE])

        url, _ = await session.resolve_download_url(MD5)

        assert url not in session._page.visited, (
            "resolving must stop at the URL; fetching it here would bypass the "
            "content-md5, throughput and atomic-staging machinery in "
            "_download_url_to_file and reimplement all three worse"
        )

    @pytest.mark.asyncio
    async def test_a_dead_partner_server_falls_through_to_the_next(self):
        dead = '<html><body><a href="/faq">nothing here</a></body></html>'
        session = _session([BOOK_PAGE, dead, PARTNER_PAGE])

        url, _ = await session.resolve_download_url(MD5)

        assert url.startswith("https://cdn3.example.net/")
        assert len(session._page.visited) == 3

    @pytest.mark.asyncio
    async def test_a_challenge_stops_immediately_rather_than_trying_more_servers(self):
        """Retrying into the wall is the behaviour the limiter exists to stop."""
        challenge = "<html><body>Checking your browser before accessing</body></html>"
        session = _session([BOOK_PAGE, challenge, PARTNER_PAGE])

        with pytest.raises(ChallengeNotClearedError) as excinfo:
            await session.resolve_download_url(MD5)

        assert len(session._page.visited) == 2, (
            "a challenge must abort the walk, not cost another request per "
            "remaining partner server"
        )
        assert "20 minutes" in str(excinfo.value), (
            "the message must tell the operator what to do — clearance lapses "
            "and is re-established by hand"
        )

    @pytest.mark.asyncio
    async def test_a_challenge_backs_the_limiter_off(self):
        challenge = "<html><body>DDoS-Guard</body></html>"
        session = _session([BOOK_PAGE, challenge], annas_browser_backoff_seconds=120.0)
        before = session._limiter._last_request

        with pytest.raises(ChallengeNotClearedError):
            await session.resolve_download_url(MD5)

        assert session._limiter._last_request > before + 60

    @pytest.mark.asyncio
    async def test_annas_own_limit_is_reported_as_quota_not_as_a_challenge(self):
        limited = "<html><body>You have downloaded too many files</body></html>"
        session = _session([BOOK_PAGE, limited, PARTNER_PAGE])

        with pytest.raises(ProviderRateLimitedError) as excinfo:
            await session.resolve_download_url(MD5)

        assert excinfo.value.reason == "quota_exhausted"
        assert not isinstance(excinfo.value, ChallengeNotClearedError)
        assert len(session._page.visited) == 2, (
            "Anna's own limit applies to the whole session, so the remaining "
            "partner servers cannot succeed — trying them would sleep out a "
            "full backoff each time and spend the day's budget proving it"
        )

    @pytest.mark.asyncio
    async def test_a_missing_book_is_not_found_not_a_wall(self):
        """The page loaded and no challenge fired, so retrying is pointless."""
        empty = "<html><body><h1>Not found</h1></body></html>"
        session = _session([empty])

        with pytest.raises(ProviderResponseError) as excinfo:
            await session.resolve_download_url(MD5)

        assert excinfo.value.reason == "not_found"

    @pytest.mark.asyncio
    async def test_the_walk_is_bounded_by_max_servers(self):
        many = "".join(f'<a href="/slow_download/{MD5}/0/{n}">s</a>' for n in range(8))
        dead = "<html><body>no link</body></html>"
        session = _session(
            [f"<html><body>{many}</body></html>", dead, dead],
            annas_browser_max_servers=2,
        )

        with pytest.raises(ProviderResponseError):
            await session.resolve_download_url(MD5)

        assert len(session._page.visited) == 3, (
            "one book page plus max_servers partner pages; walking all eight "
            "would spend the day's budget on a single failing book"
        )

    @pytest.mark.asyncio
    async def test_the_page_is_closed_even_when_the_walk_fails(self):
        session = _session(["<html><body>Not found</body></html>"])

        with pytest.raises(ProviderResponseError):
            await session.resolve_download_url(MD5)

        assert session._page.closed is True

    @pytest.mark.asyncio
    async def test_concurrent_callers_are_serialised(self):
        """One request in flight per session, structurally (#144).

        The repo has already paid for fanning lanes out against a
        single-session provider; this is that lesson as a lock rather than as a
        convention someone has to remember.
        """
        session = _session([BOOK_PAGE, PARTNER_PAGE])
        assert session._lock is not None

        await session._lock.acquire()
        task = asyncio.create_task(session.resolve_download_url(MD5))
        await asyncio.sleep(0)

        assert not task.done(), "a second caller must wait, not proceed in parallel"

        session._lock.release()
        url, _ = await task
        assert url.startswith("https://cdn3.example.net/")


class TestMissingPlaywright:
    @pytest.mark.asyncio
    async def test_absent_playwright_is_a_configuration_error(self, monkeypatch):
        """Permanent until the operator acts, so not evidence Anna's is down."""
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        session = AnnasBrowserSession(_config())

        with pytest.raises(BrowserUnavailableError) as excinfo:
            await session._ensure_context()

        assert excinfo.value.reason == "configuration_error"
        message = str(excinfo.value)
        assert "annas-browser" in message, "must name the extra that installs it"
        assert "LibGen" in message, (
            "a credential-free LibGen user must be told this does not affect them"
        )
