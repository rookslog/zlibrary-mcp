"""Saved pages distinguish results, no matches and parse failures."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import pytest

from lib.sources.libgen import LibgenAdapter, _unparseable_search_page
from lib.sources.config import SourceConfig

FIXTURES = Path(__file__).resolve().parents[2] / "test_files/libgen"


def test_saved_no_match_page_is_a_valid_empty_result():
    page = SimpleNamespace(
        status_code=200, text=(FIXTURES / "li-empty.html").read_text()
    )
    assert _unparseable_search_page(page) is None


def test_positive_page_contains_a_results_table():
    page = SimpleNamespace(
        status_code=200, text=(FIXTURES / "li-positive.html").read_text()
    )
    assert _unparseable_search_page(page) is None


@pytest.mark.parametrize(
    "html",
    [
        "<style>#tablelibgen td { color: red; }</style>",
        '<!-- <table id="tablelibgen"></table> -->',
        "<script>const sample = '<table id=\"tablelibgen\"></table>';</script>",
        '<div id="tablelibgen"></div>',
        '<table id="tablelibgen1"></table>',
    ],
)
def test_non_table_mentions_do_not_mask_a_parse_failure(html):
    assert "no results table" in (
        _unparseable_search_page(SimpleNamespace(text=html)) or ""
    )


def test_actual_empty_results_table_is_accepted():
    assert (
        _unparseable_search_page(
            SimpleNamespace(text="<TABLE ID='tablelibgen'></TABLE>")
        )
        is None
    )


@pytest.mark.asyncio
async def test_saved_no_match_page_stops_after_one_get(monkeypatch):
    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "MIN_REQUEST_INTERVAL", 0)
    calls = []

    def saved_page(url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = (FIXTURES / "li-empty.html").read_bytes()
        response.encoding = "utf-8"
        return response

    async def inline_worker(func, *_args, **_kwargs):
        return func()

    with (
        patch("requests.get", side_effect=saved_page),
        patch("lib.sources.libgen.run_bounded", side_effect=inline_worker),
    ):
        results = await adapter.search("absent title")

    assert results == []
    assert len(calls) == 1


@pytest.mark.parametrize(
    "markup",
    [
        '<style>.nav-tabs:after { content: "Files 0"; }</style>',
        """<script>const markup = '<ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link active" href="/index.php?curtab=f">Files <span class="badge">0</span></a></li></ul>';</script>""",
        '<!-- <ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link active" href="/index.php?curtab=f">Files <span class="badge">0</span></a></li></ul> -->',
        '<ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link active" href="/index.php?curtab=e">Editions <span class="badge">0</span></a></li></ul>',
        '<ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link active" href="/index.php?curtab=f">Files <span class="badge">10</span></a></li></ul>',
        '<ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link" href="/index.php?curtab=f">Files <span class="badge">0</span></a></li></ul>',
    ],
)
def test_only_the_active_zero_files_badge_signals_no_matches(markup):
    assert "no results table" in (
        _unparseable_search_page(SimpleNamespace(text=markup)) or ""
    )


@pytest.mark.parametrize("container", ["template", "noscript"])
@pytest.mark.parametrize(
    "signal",
    [
        '<table id="tablelibgen"></table>',
        '<ul class="nav nav-tabs"><li class="nav-item"><a class="nav-link active" href="/index.php?curtab=f">Files <span class="badge">0</span></a></li></ul>',
    ],
)
def test_inert_content_does_not_signal_results_or_no_matches(container, signal):
    page = SimpleNamespace(text=f"<{container}>{signal}</{container}>")
    assert "no results table" in (_unparseable_search_page(page) or "")


@pytest.mark.parametrize("container", ["template", "noscript"])
def test_visible_table_remains_valid_beside_inert_content(container):
    page = SimpleNamespace(
        text=(
            f'<{container}><table id="tablelibgen"></table></{container}>'
            '<table id="tablelibgen"></table>'
        )
    )
    assert _unparseable_search_page(page) is None
