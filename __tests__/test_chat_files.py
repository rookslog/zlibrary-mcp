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


def test_map_estimates_labels_sections_and_safe_windows(helper, book):
    with fitz.open(book / "nested.pdf") as src:
        builder = helper.PdfBuilder(src, book / "cache", "fixture")
        result = helper.pdf_map(src, 1100000)
        assert result["pages"] == 12
        assert result["page_labels"]["present"] is True
        # One page of overlap: each section ends on the next one's start page.
        assert [n["pdf_pages"] for n in result["sections"]] == ["1-3", "3-11", "11-12"]
        assert [n["pdf_pages"] for n in result["sections"][1]["children"]] == [
            "3-7",
            "7-11",
        ]
        assert result["sections"][1]["estimated_bytes"] > 1100000
        assert result["text_layer"] == 1
        assert result["safe_window_estimated"] is True
        assert result["size_estimate_margin"] == 0.05
        assert "subsetting" not in result
        # No false fits: every estimated fit is under the cap by a real build.
        for node in helper.walk_sections(result["sections"]):
            assert "bytes" not in node
            if node["fits"]:
                start, end = helper.parse_pages(node["pdf_pages"], 12)
                assert len(builder.build(start, end)[0]) <= 1100000
        next_page = 1
        for part in result["suggested_parts"]:
            start, end = helper.parse_pages(part["pdf_pages"], 12)
            assert start == next_page
            assert (
                len(builder.build(start, end)[0]) <= part["estimated_bytes"] <= 1100000
            )
            next_page = end + 1
        assert next_page == 13
        n = result["safe_window_pages"]
        assert n >= 1
        assert all(
            len(builder.build(i, i + n - 1)[0]) <= 1100000 for i in range(1, 14 - n)
        )
        # Largest by the estimate: one more page makes some window too big.
        assert helper.SizeEstimate(src).worst_window(n + 1) > 1100000


def test_estimate_covers_every_window_with_the_margin(helper, book):
    with fitz.open(book / "nested.pdf") as src:
        estimate = helper.SizeEstimate(src)
        for n in (1, 3, 12):
            sizes = [estimate.size(i, i + n - 1) for i in range(1, 14 - n)]
            assert estimate.worst_window(n) == max(sizes)


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
            m = helper.pdf_map(src, 1100000)
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


def test_sections_overlap_by_one_page_and_stay_inside_their_parent(helper):
    tree = helper.outline_tree(
        [[1, "One", 1], [2, "A", 2], [2, "B", 5], [2, "C", 5], [1, "Two", 8]], 12
    )
    one, two = tree
    assert (one["pdf_pages"], two["pdf_pages"]) == ("1-8", "8-12")
    # Same start page: the section is that one page. The last child ends with
    # its parent, so it too reaches the next section's start page.
    assert [c["pdf_pages"] for c in one["children"]] == ["2-5", "5", "5-8"]


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
        result = helper.pdf_map(src, 100)
        assert all(p["estimated_bytes"] <= 100 for p in result["suggested_parts"])
        assert result["safe_window_pages"] == 0
        assert len(result["unshareable_pages"]) == 5
        assert all(p["use"] == "get_page_image" for p in result["unshareable_pages"])


def _fd_open(fd):
    import os

    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def run_helper(command, pdf, cache, **args):
    import json
    import os
    import subprocess

    with open(pdf, "rb") as source:
        # The helper reads the book from fd 3, as the server lends it.
        saved = os.dup(3) if _fd_open(3) else None
        os.dup2(source.fileno(), 3)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(ROOT / "lib/chat_files.py"), command],
                input=json.dumps(
                    {"cache": str(cache), "key": "k", "max_bytes": 7750000, **args}
                ).encode(),
                capture_output=True,
                pass_fds=(3,),
                timeout=120,
            )
        finally:
            if saved is None:
                os.close(3)
            else:
                os.dup2(saved, 3)
                os.close(saved)
    assert proc.returncode == 0
    return proc.stdout.decode(), proc.stderr.decode()


def test_malformed_pdf_maps_and_excerpts_with_clean_stdout(book):
    cache = book / "cache"
    cache.mkdir()
    out, err = run_helper("map", book / "malformed.pdf", cache)
    import json

    assert out.count("\n") == 1 and "MuPDF" not in out
    result = json.loads(out)
    assert result["pages"] == 3
    assert result["repairs"] > 0
    out, _ = run_helper("excerpt", book / "malformed.pdf", cache, pages="1-3")
    piece = json.loads(out)
    assert piece["pdf_pages"] == "1-3"
    with fitz.open(cache / piece["piece"]) as excerpt:
        assert [p.get_text().strip() for p in excerpt] == [
            "PRIVATE 0",
            "PRIVATE 1",
            "PRIVATE 2",
        ]
    # Control: a well-formed file reports no repairs.
    out, _ = run_helper("map", book / "plain.pdf", cache)
    assert "repairs" not in json.loads(out)


def test_estimate_counts_serialisation_overhead(helper, book):
    # plain.pdf is all small objects, so per-object overhead dominates its size;
    # without it the estimate falls below the exact build.
    with fitz.open(book / "plain.pdf") as src:
        estimate = helper.SizeEstimate(src)
        builder = helper.PdfBuilder(src, book / "cache", "overhead")
        for start in range(1, 6):
            for end in range(start, 6):
                assert estimate.size(start, end) >= builder.size(start, end)
