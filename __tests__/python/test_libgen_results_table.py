"""Retained round-3b pages distinguish CSS from a results table element."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.sources.libgen import _unparseable_search_page

FIXTURES = Path(__file__).resolve().parents[2] / "test_files/libgen"


def test_css_only_empty_page_is_not_a_results_table():
    page = SimpleNamespace(
        status_code=200, text=(FIXTURES / "li-empty.html").read_text()
    )
    assert "no results table" in (_unparseable_search_page(page) or "")


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
