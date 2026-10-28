"""Offline #132 port-1 regressions for mixed book/article parsing and identity."""

import inspect
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from libgen_api_enhanced.book import Book
from libgen_api_enhanced.search_request import SearchRequest
import pytest

from lib.auth_diagnostics import DiagnosticLogFilter
from lib.sources.config import SourceConfig
from lib.sources.libgen import LibgenAdapter

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "test_files/libgen/mixed_book_article_results.html"
)
ARTICLE_MD5 = "e3f85ebc6faa062bceb95b349f369672"
BOOK_MD5 = "3b516fa296c861195bc94fad9bddda9e"


def parse_row(title_markup, *, cover='<td><img src="" /></td>'):
    table = BeautifulSoup(
        f"""<table><tr>{cover}<td>{title_markup}</td>
        <td>Author</td><td>Publisher</td><td>2026</td><td>English</td>
        <td>1</td><td>1 MB</td><td>pdf</td>
        <td><a href="/ads.php?md5={ARTICLE_MD5}">1</a></td>
        </tr></table>""",
        "html.parser",
    ).table
    return list(SearchRequest("article", mirror="https://libgen.li").get_books(table))


@pytest.fixture
def parsed_rows():
    table = BeautifulSoup(FIXTURE.read_text(), "html.parser").find(
        "table", id="tablelibgen"
    )
    request = SearchRequest("inner citadel")
    request.strip_i_tag_from_soup(table)
    return list(request.get_books(table))


def test_article_columns_and_md5_are_aligned(parsed_rows):
    """Src-based cover detection shifts the article and loses its download identity."""
    article = parsed_rows[1]
    assert (
        article.md5,
        article.author,
        article.publisher,
        article.year,
        article.language,
        article.pages,
        article.size,
        article.extension,
        article.cover_url,
    ) == (
        ARTICLE_MD5,
        "Rutherford, R. B.",
        "",
        "2001 October",
        "English",
        "0",
        "73 kB",
        "pdf",
        None,
    )
    assert article.mirrors[0] == (
        "https://libgen.li/ads.php?md5=e3f85ebc6faa062bceb95b349f369672"
        "&downloadname=10.1093/cr/51.2.298"
    )


def test_article_identity_uses_direct_visible_edition_link(parsed_rows):
    """Journal, issue and DOI links must not replace or contaminate the edition."""
    article = parsed_rows[1]
    assert (article.id, article.title) == (
        "76430726",
        "The Inner Citadel P. Hadot: The Inner Citadel: the Meditations of "
        "Marcus Aurelius . Pp. x + 351. Cambridge, MA and London: Harvard "
        "University Press, 1998. Cased, $27.95. ISBN: 0-674-46171-1.",
    )


@pytest.mark.asyncio
async def test_mixed_search_retains_books_and_articles_with_internal_type():
    """Exercise real search, UA shim and conversion with fixture HTTP."""
    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    response = MagicMock(status_code=200, text=FIXTURE.read_text())

    async def inline_worker(func, *_args, **_kwargs):
        # Thread/deadline behavior has its own suite. Keep this parser
        # regression deterministic by executing its worker on the test thread.
        return func()

    with (
        patch("requests.get", return_value=response),
        patch("lib.sources.libgen.run_bounded", side_effect=inline_worker),
    ):
        results = await adapter.search("inner citadel")

    assert [row.md5 for row in results] == [
        BOOK_MD5,
        ARTICLE_MD5,
        "0aaced84265e767256fa026c21e6ab24",
        "1bf9dd21f01e3b17c82a44417eee7f58",
    ]
    assert [row.extra["content_type"] for row in results] == [
        "book",
        "article",
        "article",
        "book",
    ]
    book = results[0]
    assert (book.author, book.extension, book.size, book.year) == (
        "Pierre Hadot, Michael Chase",
        "pdf",
        "5 MB",
        "2001",
    )
    assert book.extra == {
        "id": "137175221",
        "language": "English",
        "pages": "0 / 368",
        "content_type": "book",
    }
    assert all(row.download_url == "" for row in results)


def test_row_types_come_from_badges(parsed_rows):
    assert [getattr(row, "content_type", "") for row in parsed_rows] == [
        "book",
        "article",
        "article",
        "book",
    ]


def test_parsed_rows_match_production_italic_preprocessing(parsed_rows):
    # The retained last book has an italic "Revised" suffix. Production
    # removes it before parsing, so fixture-only parsing must do the same.
    assert parsed_rows[-1].title == (
        "The Inner Citadel: The Meditations of Marcus Aurelius"
    )


def test_installed_book_constructor_matches_parser_positional_contract():
    assert str(inspect.signature(Book)) == (
        "(id, title, author, publisher, year, language, pages, size, extension, "
        "md5, mirrors, cover_url, date_added, date_last_modified)"
    ), (
        "Installed libgen-api-enhanced Book signature changed; review _get_books positional arguments"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("no_edition,invalid_md5", [(1, 0), (1, 1), (4, 0), (0, 0)])
async def test_search_drop_diagnostic_counts_parser_and_md5_rows(
    no_edition, invalid_md5, caplog, monkeypatch
):
    # As a library consumer, bypass bridge-only filters on zlibrary.sources
    # and pytest's reused caplog handler. monkeypatch restores both after each case.
    logger = logging.getLogger("zlibrary.sources")
    monkeypatch.setattr(
        logger,
        "filters",
        [f for f in logger.filters if not isinstance(f, DiagnosticLogFilter)],
    )
    monkeypatch.setattr(
        caplog.handler,
        "filters",
        [f for f in caplog.handler.filters if not isinstance(f, DiagnosticLogFilter)],
    )

    async def inline_worker(func, *_args, **_kwargs):
        return func()

    monkeypatch.setattr("lib.sources.libgen.run_bounded", inline_worker)
    soup = BeautifulSoup(FIXTURE.read_text(), "html.parser")
    rows = soup.select("#tablelibgen tbody tr")
    for row in rows[:no_edition]:
        row.find_all("td")[1].clear()
    for row in rows[no_edition : no_edition + invalid_md5]:
        for anchor in row.find_all("td")[-1].find_all("a", href=True):
            anchor["href"] = anchor["href"].replace(BOOK_MD5, "invalid")
            anchor["href"] = anchor["href"].replace(ARTICLE_MD5, "invalid")

    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "MIN_REQUEST_INTERVAL", 0)
    with (
        patch("requests.get", return_value=MagicMock(status_code=200, text=str(soup))),
        caplog.at_level(logging.DEBUG, logger="zlibrary.sources"),
    ):
        results = await adapter.search("inner citadel")

    assert len(results) == 4 - no_edition - invalid_md5
    diagnostics = [
        r.getMessage() for r in caplog.records if "LibGen search: dropped" in r.message
    ]
    total = no_edition + invalid_md5
    if total:
        assert len(diagnostics) == 1
        assert f"dropped {total} row(s)" in diagnostics[0]
        assert f"{no_edition} without a direct edition link" in diagnostics[0]
        assert f"{invalid_md5} without a valid 32-hex md5" in diagnostics[0]
    else:
        assert diagnostics == []

    # A later clean attempt must not inherit the previous worker's count.
    caplog.clear()
    with (
        patch(
            "requests.get",
            return_value=MagicMock(status_code=200, text=FIXTURE.read_text()),
        ),
        caplog.at_level(logging.DEBUG, logger="zlibrary.sources"),
    ):
        assert len(await adapter.search("inner citadel")) == 4
    assert not any("LibGen search: dropped" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "href",
    [
        "edition.php?id=101",
        "/edition.php?id=102",
        "https://libgen.li/edition.php?id=103",
    ],
)
def test_visible_edition_url_forms_skip_blank_and_nested_metadata(href):
    rows = parse_row(
        '<span><a href="edition.php?id=999">Issue metadata</a></span>'
        '<a href="edition.php?id=888"><span></span></a>'
        f'<a href="{href}"><span>Visible <strong>Article</strong></span></a>'
    )
    assert len(rows) == 1
    assert rows[0].id == href[-3:]
    assert rows[0].title == "Visible Article"


@pytest.mark.parametrize(
    "href",
    [
        "/notedition.php?id=123",
        "/fooedition.php?id=123",
        "edition.php",
        "edition.php?id=",
        "edition.php?id=not-a-number",
        "edition.php?not_id=123",
    ],
)
def test_non_edition_or_unusable_id_is_not_a_row_identity(href):
    # Nine columns is the supported no-cover layout. Upstream otherwise emits
    # a downloadable-looking record even when no edition identity is present.
    assert parse_row(f'<a href="{href}">Article</a>', cover="") == []


def test_no_cover_layout_still_selects_the_edition_over_series():
    rows = parse_row(
        '<b><a href="series.php?id=999">Journal</a></b>'
        '<a href="edition.php?id=123"><span>Actual</span> article</a>',
        cover="",
    )
    assert len(rows) == 1
    assert (rows[0].id, rows[0].title, rows[0].md5) == (
        "123",
        "Actual article",
        ARTICLE_MD5,
    )


def test_blank_or_nested_only_edition_cannot_supply_identity():
    assert (
        parse_row(
            '<a href="edition.php?id=111"><span></span></a>'
            '<span><a href="edition.php?id=222">Metadata</a></span>',
            cover="",
        )
        == []
    )


@pytest.mark.parametrize(
    "markup,expected",
    [
        ("L’Étranger : étude — naïve &amp; café", "L’Étranger : étude — naïve & café"),
        (
            "<em>中文</em> 日本語 — Русский Ελληνικά العربية हिन्दी",
            "中文 日本語 — Русский Ελληνικά العربية हिन्दी",
        ),
        ("Cafe\u0301 <strong>notes</strong>\x00\x1b\u200b", "Cafe\u0301 notes"),
        ("  A\n B\tC  ", "A B C"),
    ],
)
def test_title_preserves_unicode_and_punctuation_without_markup_or_controls(
    markup, expected
):
    # The former ASCII-only filter destroys accents, scripts and punctuation.
    rows = parse_row(f'<a href="edition.php?id=123">{markup}</a>')
    assert len(rows) == 1
    assert rows[0].title == expected


@pytest.mark.parametrize(
    "title",
    [
        "می\u200cخواهم",
        "क्\u200dष",
        "👩\u200d👩\u200d👧",
    ],
)
def test_title_retains_joiners_and_non_joiners(title):
    rows = parse_row(f'<a href="edition.php?id=123">{title}</a>')
    assert rows[0].title == title


@pytest.mark.parametrize(
    "formatting",
    [
        "\u00ad",  # Soft hyphen.
        "\u061c",  # Arabic letter mark.
        "\u200b",  # Zero-width space.
        "\u200e",
        "\u200f",  # Directional marks.
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",  # Bidi embedding/overrides.
        "\u2060",  # Word joiner.
        "\u2061",
        "\u2062",
        "\u2063",
        "\u2064",  # Invisible math operators.
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",  # Bidi isolates.
        "\u206a",
        "\u206b",
        "\u206c",
        "\u206d",
        "\u206e",
        "\u206f",  # Deprecated bidi controls.
        "\ufeff",  # Byte order mark / zero-width no-break space.
    ],
)
def test_title_removes_named_invisible_formatting(formatting):
    rows = parse_row(f'<a href="edition.php?id=123">A{formatting}B</a>')
    assert rows[0].title == "AB"


@pytest.mark.parametrize("title", ["A \x1b B", "A \u200b B", " A \ufeff  B "])
def test_title_collapses_spaces_after_removing_characters(title):
    rows = parse_row(f'<a href="edition.php?id=123">{title}</a>')
    assert rows[0].title == "A B"
