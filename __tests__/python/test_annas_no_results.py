"""Published-source empty marker; all HTTP stays inside MockTransport."""

from pathlib import Path

import httpx
import pytest

from lib.sources.errors import SourceError
from test_annas_member import KEY, FakeSite

pytest_plugins = ["test_annas_member"]

SYNTHETIC = (
    Path(__file__).parent / "fixtures/annas-search/no-results-synthetic.html"
).read_text()
MARKER = '<div><span class="font-bold">No files found.</span></div>'
FORM = '<form class="js-search-form" role="search">{}</form>'


def responder(page):
    login = FakeSite()

    async def site(request):
        if request.method == "POST":
            return await login(request)
        return httpx.Response(200, text=page)

    return site


@pytest.mark.parametrize("key", ["", KEY], ids=["anonymous", "member"])
async def test_published_synthetic_no_files_page_is_empty_success(make_adapter, key):
    adapter = make_adapter(responder(SYNTHETIC), key=key)
    assert await adapter.search("nothing") == []
    assert adapter.member_status()["member_session"] == ("active" if key else "none")


@pytest.mark.parametrize("key", ["", KEY], ids=["anonymous", "member"])
@pytest.mark.parametrize(
    "page",
    [
        FORM.format(f'<a href="/help">{MARKER}</a>'),
        FORM.format(f'<div class="js-aarecord-list-outer">{MARKER}</div>'),
        FORM.format("") + MARKER,
        '<form class="js-search-form">' + MARKER + "</form>",
        FORM.format(MARKER.replace("No files found.", "No files found")),
        FORM.format(
            MARKER.replace("No files found.", "No files found. Check filters.")
        ),
        FORM.format(MARKER.replace("No files found.", "no files found.")),
        FORM.format(MARKER.replace("<span class=", "<span hidden class=")),
    ],
    ids=[
        "link",
        "result-list",
        "outside-form",
        "missing-search-role",
        "wrong-punctuation",
        "extra-text",
        "wrong-case",
        "hidden-marker",
    ],
)
async def test_marker_outside_empty_state_context_is_unrecognised(
    make_adapter, key, page
):
    with pytest.raises(SourceError, match="Unrecognised page"):
        await make_adapter(responder(page), key=key).search("nothing")


@pytest.mark.parametrize("key", ["", KEY], ids=["anonymous", "member"])
@pytest.mark.parametrize(
    "opening,closing",
    [
        ("<section hidden>", "</section>"),
        ('<section aria-hidden="true">', "</section>"),
        ('<section class="hidden">', "</section>"),
        ('<section class="invisible">', "</section>"),
        ('<section style="display: none !important">', "</section>"),
        ('<section style="visibility: hidden">', "</section>"),
        ("<template>", "</template>"),
    ],
    ids=[
        "hidden-attribute",
        "aria-hidden",
        "hidden-class",
        "invisible-class",
        "display-none",
        "visibility-hidden",
        "template",
    ],
)
async def test_hidden_marker_is_not_empty_success(make_adapter, key, opening, closing):
    page = FORM.format(opening + MARKER + closing)
    with pytest.raises(SourceError, match="Unrecognised page"):
        await make_adapter(responder(page), key=key).search("nothing")


@pytest.mark.parametrize("key", ["", KEY], ids=["anonymous", "member"])
async def test_result_row_wins_over_no_files_marker(make_adapter, key):
    page = FORM.format(MARKER + '<a href="/md5/abc">A book</a>')
    rows = await make_adapter(responder(page), key=key).search("books")
    assert [row.title for row in rows] == ["A book"]


@pytest.mark.parametrize("key", ["", KEY], ids=["anonymous", "member"])
async def test_challenge_with_no_files_marker_is_still_error(make_adapter, key):
    page = "<title>DDoS-Guard</title>" + FORM.format(MARKER)
    with pytest.raises(SourceError, match="Browser verification"):
        await make_adapter(responder(page), key=key).search("nothing")
