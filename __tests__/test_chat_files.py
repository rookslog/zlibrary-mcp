"""Catch estimates masquerading as real sizes, lost labels, and unsafe subsetting."""

import importlib.util
import pathlib
import sys

import pytest

fitz = pytest.importorskip(
    "fitz", reason="run with the ZLIBRARY_PDF_PYTHON interpreter"
)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def book(tmp_path):
    load("chat_fixture", ROOT / "__tests__/fixtures/generate-chat-pdfs.py").generate(
        tmp_path
    )
    return tmp_path


@pytest.fixture
def helper():
    return load("chat_files", ROOT / "lib/chat_files.py")


def test_map_sizes_labels_sections_and_all_safe_windows(helper, book):
    with fitz.open(book / "nested.pdf") as src:
        builder = helper.PdfBuilder(src, book / "cache", "fixture")
        result = helper.pdf_map(src, builder, 1100000)
        assert result["pages"] == 12
        assert result["page_labels"]["present"] is True
        assert result["sections"][1]["pdf_pages"] == "3-10"
        assert result["sections"][1]["children"][1]["pdf_pages"] == "7-10"
        assert result["sections"][1]["bytes"] > 1100000
        assert result["text_layer"] == 1
        next_page = 1
        for part in result["suggested_parts"]:
            start, end = helper.parse_pages(part["pdf_pages"], 12)
            assert start == next_page
            data, _ = builder.build(start, end)
            assert len(data) == part["bytes"] <= 1100000
            next_page = end + 1
        assert next_page == 13
        n = result["safe_window_pages"]
        assert n >= 1
        assert all(
            len(builder.build(i, i + n - 1)[0]) <= 1100000 for i in range(1, 14 - n)
        )
        if n < 12:
            assert any(
                len(builder.build(i, i + n)[0]) > 1100000 for i in range(1, 13 - n)
            )


def test_excerpt_copies_every_label_and_never_changes_text(helper, book):
    with fitz.open(book / "nested.pdf") as src:
        builder = helper.PdfBuilder(src, book / "cache", "fixture")
        data, measurement = builder.build(2, 6)
        with fitz.open(stream=data, filetype="pdf") as piece:
            assert [p.get_label() for p in piece] == [
                src[i].get_label() for i in range(1, 6)
            ]
            assert [p.get_text() for p in piece] == [
                src[i].get_text() for i in range(1, 6)
            ]
        assert measurement["kept"] == (
            measurement["text_unchanged"]
            and measurement["subset_bytes"] < measurement["original_bytes"]
        )


def test_scans_and_unlabelled_maps(helper, book):
    for name, share in [("scan.pdf", 0), ("plain.pdf", 1)]:
        with fitz.open(book / name) as src:
            m = helper.pdf_map(
                src, helper.PdfBuilder(src, book / "cache", name), 1100000
            )
            assert m["sections"] == []
            assert m["page_labels"]["present"] is False
            assert "offset" in m["page_labels"]["note"]
            assert m["text_layer"] == share


@pytest.mark.parametrize(
    "value", ["0", "1-99", "3-2", "1,2", "-1", "1.5", "1-2-3", "1-"]
)
def test_invalid_ranges(helper, value):
    with pytest.raises(ValueError):
        helper.parse_pages(value, 12)


def test_outline_repairs_invalid_destinations_and_levels(helper, book):
    # Avoid relying on MuPDF's own repair of malformed destinations.
    tree = helper.outline_tree(
        [[1, "Bad", -1], [4, "Child", 5], [1, "Backwards", 3], [1, "Beyond", 999]], 12
    )
    assert tree[0]["_start"] == 1
    assert tree[0]["children"][0]["level"] == 2
    assert all(1 <= n["_start"] <= n["_end"] <= 12 for n in helper.walk_sections(tree))


def test_subset_rejected_when_extraction_changes(helper, book, monkeypatch):
    def destructive(self):
        self[0].insert_text((20, 45), "CHANGED")

    monkeypatch.setattr(fitz.Document, "subset_fonts", destructive)
    with fitz.open(book / "plain.pdf") as src:
        data, m = helper.PdfBuilder(src, book / "cache", "mutant").build(1, 2)
        assert m["text_unchanged"] is False
        assert m["kept"] is False
        with fitz.open(stream=data, filetype="pdf") as excerpt:
            assert "CHANGED" not in excerpt[0].get_text()


def test_oversized_single_pages_are_not_suggested_as_sendable_parts(helper, book):
    with fitz.open(book / "plain.pdf") as src:
        result = helper.pdf_map(
            src, helper.PdfBuilder(src, book / "cache", "too-small"), 100
        )
        assert all(p["bytes"] <= 100 for p in result["suggested_parts"])
        assert result["safe_window_pages"] == 0
        assert len(result["unshareable_pages"]) == 5
        assert all(p["use"] == "get_page_image" for p in result["unshareable_pages"])
