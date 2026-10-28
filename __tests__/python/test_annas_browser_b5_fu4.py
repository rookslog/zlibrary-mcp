"""FU-4: real saved partial/complete lists, synthetic renderer-based empty DOM."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.annas_browser import (
    AnnasBrowserSession,
    _settled_search_page,
    needs_human_check,
)
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderTimeoutError
from lib.sources.net import WalkDeadline

PAGES = Path(__file__).parent / "fixtures" / "annas-browser" / "fu4"


def saved(name):
    return (PAGES / name).read_text()


def make_session(tmp_path, monkeypatch, documents, **settings):
    cfg = SourceConfig(
        annas_browser_enabled=True,
        annas_browser_profile_dir=str(tmp_path / "profile"),
        annas_browser_min_interval=0,
        annas_browser_settle_seconds=settings.pop("settle", 1),
        **settings,
    )

    class Page:
        url = ""

        async def goto(self, url, **kwargs):
            self.url = url
            return SimpleNamespace(status=403)  # Initial automatic-check hop.

        async def content(self):
            if len(documents) > 1:
                return documents.pop(0)
            return documents[0]

        async def close(self):
            pass

    page = Page()
    session = AnnasBrowserSession(cfg)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(session, "_ensure_context", AsyncMock(return_value=context))
    return session


def test_partial_list_is_not_settled():
    assert not _settled_search_page(
        BeautifulSoup(saved("blue-partial.html"), "html.parser")
    )


@pytest.mark.parametrize(
    "case", ["missing_rows", "missing_partial_rows", "missing_footer"]
)
def test_footer_and_all_announced_rows_are_required(case):
    soup = BeautifulSoup(saved("blue-complete.html"), "html.parser")
    if case == "missing_rows":
        soup.select(".js-aarecord-list-outer")[0].select("a")[-1].decompose()
    elif case == "missing_partial_rows":
        soup.select(".js-aarecord-list-outer")[1].select("a")[-1].decompose()
    else:
        soup.footer.decompose()  # Main list complete, later partial list may still arrive.
    assert not _settled_search_page(soup)


@pytest.mark.parametrize("budget", ["settle", "walk"])
async def test_partial_deadline_is_timeout_never_empty(tmp_path, monkeypatch, budget):
    session = make_session(
        tmp_path,
        monkeypatch,
        [saved("blue-partial.html")],
        settle=0 if budget == "settle" else 2,
    )
    with pytest.raises(ProviderTimeoutError) as caught:
        await session.search_html(
            "Blue and Brown Books",
            deadline=WalkDeadline(0.12 if budget == "walk" else 2),
        )
    assert caught.value.reason in ("read_timeout", "search_timeout")
    assert session._process_lock._fd is None


def test_complete_list_settles_and_parses_all_31_rows():
    page = saved("blue-complete.html")
    assert _settled_search_page(BeautifulSoup(page, "html.parser"))
    rows = AnnasArchiveAdapter(SourceConfig())._parse_search_html(page)
    assert len(rows) == len({r.md5 for r in rows}) == 31
    assert rows[0].md5 == "94802be2866858bea3f4d8e726a72ec2"
    assert rows[-1].md5 == "09e700debde86800e110c30ab5cee29c"


def test_renderer_no_files_notice_settles_empty():
    page = saved("no-results-synthetic.html")
    assert _settled_search_page(BeautifulSoup(page, "html.parser"))
    assert AnnasArchiveAdapter(SourceConfig())._parse_search_html(page) == []


@pytest.mark.parametrize("case", ["absent_notice", "hidden_notice", "book_title"])
def test_blank_or_incidental_no_files_text_is_not_empty(case):
    soup = BeautifulSoup(saved("no-results-synthetic.html"), "html.parser")
    notice = soup.select_one("span.font-bold")
    if case == "absent_notice":
        notice.decompose()
    elif case == "hidden_notice":
        notice.parent["hidden"] = ""
    else:
        notice.wrap(soup.new_tag("a", href="/md5/" + "a" * 32))
    assert not _settled_search_page(soup)


async def test_dom_partial_then_complete_returns_all_rows(tmp_path, monkeypatch):
    session = make_session(
        tmp_path, monkeypatch, [saved("blue-partial.html"), saved("blue-complete.html")]
    )
    page = await session.search_html("Blue and Brown Books", deadline=WalkDeadline(2))
    rows = AnnasArchiveAdapter(SourceConfig())._parse_search_html(page)
    assert len(rows) == 31
    assert session._process_lock._fd is None


def test_partial_result_manual_phrase_does_not_change_challenge_rules():
    page = saved("blue-partial.html").replace(
        "Preliminary studies", "complete the manual check"
    )
    assert not needs_human_check(page)
