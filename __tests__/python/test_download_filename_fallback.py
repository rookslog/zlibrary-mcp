# Tests for the download filename fallback: when the caller's book_details is
# too sparse for a meaningful unified filename, the server-provided filename
# is kept instead of collapsing to UnknownAuthor_UntitledBook_<id>.

import os
import sys
from pathlib import Path

import pytest

# Add lib directory to sys.path explicitly (mirrors test_python_bridge.py)
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
)

import python_bridge  # noqa: E402
from filename_utils import (  # noqa: E402
    is_degraded_unified_filename,
    is_preservable_server_filename,
    sanitize_preserved_filename,
)
from python_bridge import download_book  # noqa: E402

SERVER_NAME = (
    "Mixed legal systems in comparative perspective "
    "(Zimmermann, Reinhard etc.) (z-library.sk, 1lib.sk, z-lib.sk).pdf"
)

STAGING_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


class FakeEAPIDownloader:
    """Pretend to be the EAPI client: drop a named file into output_dir."""

    def __init__(self, filename):
        self.filename = filename

    async def download_file(self, book_id, book_hash, output_dir, host_observer=None):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / self.filename
        path.write_bytes(b"%PDF-1.4 unit test stand-in")
        if host_observer is not None:
            host_observer("synthetic-transfer-host")
        return str(path)


@pytest.fixture(autouse=True)
def _reset_bridge_client():
    saved = python_bridge._eapi_client
    yield
    python_bridge._eapi_client = saved


class TestFilenameHelpers:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("UnknownAuthor_MixedLegalSystems_1.pdf", True),
            ("UnknownAuthor_UntitledBook_1.pdf", True),
            ("GeorgeOrwell_UnknownAuthor_1.pdf", True),
            ("GeorgeOrwell_1984_1949_en_12345.epub", False),
            ("plainname.pdf", False),
        ],
    )
    def test_is_degraded(self, filename, expected):
        assert is_degraded_unified_filename(filename) is expected

    def test_sanitize_keeps_wording_and_extension(self):
        assert (
            sanitize_preserved_filename(SERVER_NAME)
            == "Mixed legal systems in comparative perspective "
            "(Zimmermann, Reinhard etc.) (z-library.sk, 1lib.sk, z-lib.sk).pdf"
        )

    def test_sanitize_replaces_path_and_control_characters(self):
        # Path separators are neutralized first (basename on the rewritten
        # name), then reserved punctuation and control characters.
        sanitized = sanitize_preserved_filename('a/b\\c:d*e?f"g<h>i|j\x00k.pdf')
        assert sanitized == "c_d_e_f_g_h_i_j_k.pdf"

    def test_sanitize_truncates_long_names_preserving_extension(self):
        long_stem = "x" * 500
        sanitized = sanitize_preserved_filename(f"{long_stem}.pdf")
        assert len(sanitized) == 200
        assert sanitized.endswith(".pdf")

    def test_sanitize_empty_and_dot_names_yield_nothing(self):
        assert sanitize_preserved_filename("") == ""
        assert sanitize_preserved_filename("   .. ") == ""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            (SERVER_NAME, True),
            (".source-abc123.part", False),  # mkstemp staging temporary
            (f"{STAGING_MD5}.download", False),  # content-hash staging artifact
            (STAGING_MD5, False),
            ("", False),
            ("12345.bin", False),
            ("real title.BIN", False),
            ("real title", False),
        ],
    )
    def test_is_preservable(self, filename, expected):
        assert is_preservable_server_filename(filename) is expected

    @pytest.mark.parametrize(
        "filename", ["Title.pdf.", "Title.pdf ."], ids=["dot", "space-dot"]
    )
    def test_trailing_dots_and_spaces_preserve_supported_extension(self, filename):
        assert is_preservable_server_filename(filename) is True
        assert sanitize_preserved_filename(filename) == "Title.pdf"

    @pytest.mark.parametrize(
        "filename",
        [
            ".Title.pdf.",
            ".source-abc123.part.",
            f"{STAGING_MD5}.download.",
            f"{STAGING_MD5}.pdf.",
            "download.pdf.",
            "book123.pdf .",
            "Title.bin.",
        ],
        ids=[
            "hidden", "staging", "hash-staging", "hash", "generic", "generic-id",
            "unsupported",
        ],
    )
    def test_trailing_cleanup_does_not_admit_rejected_names(self, filename):
        assert is_preservable_server_filename(filename) is False

    def test_all_digit_title_remains_generic(self):
        assert is_preservable_server_filename("1984.epub") is False

    @pytest.mark.parametrize(
        "filename,max_total_length",
        [
            ("Title.pdf", 4),
            ("Title.pdf", 3),
            ("x." + "a" * 199, 200),
            ("x." + "é" * 100, 200),
        ],
        ids=[
            "ending-fills-budget", "ending-exceeds-budget", "long-unsupported",
            "utf8-unsupported",
        ],
    )
    def test_unusable_ending_returns_empty_fallback(self, filename, max_total_length):
        assert sanitize_preserved_filename(filename, max_total_length) == ""


class TestDownloadBookFilenameFallback:
    @pytest.mark.parametrize(
        "metadata_extension", ["pdf", None], ids=["declared", "missing"]
    )
    @pytest.mark.parametrize(
        "server_name", ["Title.pdf.", "Title.pdf ."], ids=["dot", "space-dot"]
    )
    async def test_trailing_server_name_publishes_preserved_wording(
        self, tmp_path, server_name, metadata_extension
    ):
        python_bridge._eapi_client = FakeEAPIDownloader(server_name)
        book_details = {"id": 10, "hash": "h"}
        if metadata_extension is not None:
            book_details["extension"] = metadata_extension

        result = await download_book(book_details, str(tmp_path))

        path = Path(result["file_path"])
        assert path == tmp_path / "Title_10.pdf"
        assert path.read_bytes() == b"%PDF-1.4 unit test stand-in"
        assert list(tmp_path.iterdir()) == [path]

    async def test_sparse_metadata_keeps_server_filename(self, tmp_path):
        python_bridge._eapi_client = FakeEAPIDownloader(SERVER_NAME)
        sparse = {
            "id": 127803575,
            "hash": "da1982",
            "title": "Mixed legal systems",
            "extension": "pdf",
        }

        result = await download_book(sparse, str(tmp_path))

        assert Path(result["file_path"]).name == SERVER_NAME[:-4] + "_127803575.pdf"
        assert Path(result["file_path"]).exists()
        assert result["provenance"]["host"] == "synthetic-transfer-host"
        assert not any(
            path.name.startswith("UnknownAuthor") for path in tmp_path.iterdir()
        )

    async def test_staging_artifact_name_falls_back_to_unified(self, tmp_path):
        python_bridge._eapi_client = FakeEAPIDownloader(f"{STAGING_MD5}.download")
        sparse = {"id": 1, "hash": "h", "extension": "pdf"}

        result = await download_book(sparse, str(tmp_path))

        assert Path(result["file_path"]).name == "UnknownAuthor_UntitledBook_1.pdf"
        assert not (tmp_path / f"{STAGING_MD5}.download").exists()

    async def test_full_metadata_still_gets_unified_filename(self, tmp_path):
        python_bridge._eapi_client = FakeEAPIDownloader(SERVER_NAME)
        complete = {
            "id": 2,
            "hash": "h",
            "author": "George Orwell",
            "title": "1984",
            "extension": "pdf",
        }

        result = await download_book(complete, str(tmp_path))

        # Author surname first, per the unified filename convention.
        assert Path(result["file_path"]).name == "OrwellGeorge_1984_2.pdf"

    async def test_sanitize_applies_to_unsafe_server_names(self, tmp_path):
        # Path separators cannot occur in an on-disk download path (the real
        # client could not have written such a file), so this exercises the
        # remaining reserved characters.
        unsafe = "name:v1*?.pdf"
        python_bridge._eapi_client = FakeEAPIDownloader(unsafe)
        sparse = {"id": 3, "hash": "h", "extension": "pdf"}

        result = await download_book(sparse, str(tmp_path))

        assert Path(result["file_path"]).name == "name_v1___3.pdf"


@pytest.mark.parametrize("server_name", ["12345.bin", "12345", "Title.BIN"])
async def test_unknown_server_extension_keeps_generated_pdf_name(tmp_path, server_name):
    python_bridge._eapi_client = FakeEAPIDownloader(server_name)
    result = await download_book(
        {"id": 4, "hash": "h", "extension": "pdf"}, str(tmp_path)
    )
    assert Path(result["file_path"]).name == "UnknownAuthor_UntitledBook_4.pdf"


@pytest.mark.parametrize("extension", ["PDF", ".pdf", " pdf "])
async def test_declared_normalized_extension_wins_over_server_suffix(
    tmp_path, extension
):
    python_bridge._eapi_client = FakeEAPIDownloader("A real title.epub")
    result = await download_book(
        {"id": 5, "hash": "h", "extension": extension}, str(tmp_path)
    )
    assert Path(result["file_path"]).name == "A real title_5.pdf"


def test_bidi_and_unicode_format_characters_are_removed():
    assert sanitize_preserved_filename("Report\u202e\u200d.pdf") == "Report.pdf"


@pytest.mark.parametrize(
    "reserved",
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    ],
)
def test_windows_device_names_are_neutralized(reserved):
    assert sanitize_preserved_filename(f"{reserved}.pdf") == f"_{reserved}.pdf"
    assert (
        sanitize_preserved_filename(f"{reserved.lower()}.notes.pdf")
        == f"_{reserved.lower()}.notes.pdf"
    )


async def test_long_server_name_retains_id_and_normalized_extension(tmp_path):
    python_bridge._eapi_client = FakeEAPIDownloader("x" * 240 + ".epub")
    result = await download_book(
        {"id": 6, "hash": "h", "extension": "pdf"}, str(tmp_path)
    )
    name = Path(result["file_path"]).name
    assert len(name) <= 200
    assert name.endswith("_6.pdf")


async def test_sparse_id_without_declared_extension_keeps_valid_server_extension(
    tmp_path,
):
    python_bridge._eapi_client = FakeEAPIDownloader("A real server title.epub")
    result = await download_book({"id": 7}, str(tmp_path))
    assert Path(result["file_path"]).name == "A real server title_7.epub"
    assert Path(result["file_path"]).exists()


async def test_same_server_name_with_existing_id_does_not_move_previous_book(tmp_path):
    python_bridge._eapi_client = FakeEAPIDownloader("Shared server title_8.pdf")
    first = await download_book({"id": 8, "extension": "pdf"}, str(tmp_path))
    first_path = Path(first["file_path"])
    first_content = first_path.read_bytes()
    second = await download_book({"id": 9, "extension": "pdf"}, str(tmp_path))
    assert first_path.is_file()
    assert first_path.read_bytes() == first_content
    assert first_path != Path(second["file_path"])
    assert first_path.name == "Shared server title_8_8.pdf"
    assert Path(second["file_path"]).name == "Shared server title_8_9.pdf"


async def test_raw_server_name_cannot_overwrite_previous_published_book(tmp_path):
    class Downloader:
        async def download_file(
            self, book_id, book_hash, output_dir, host_observer=None
        ):
            path = Path(output_dir) / ("Foo.pdf" if book_id == 1 else "Foo_1.pdf")
            path.write_bytes(f"book {book_id}".encode())
            if host_observer is not None:
                host_observer("synthetic-transfer-host")
            return str(path)

    python_bridge._eapi_client = Downloader()
    first = await download_book({"id": 1, "extension": "pdf"}, str(tmp_path))
    second = await download_book({"id": 2, "extension": "pdf"}, str(tmp_path))
    assert Path(first["file_path"]).read_bytes() == b"book 1"
    assert Path(second["file_path"]).read_bytes() == b"book 2"
    assert first["provenance"]["host"] == "synthetic-transfer-host"
    assert second["provenance"]["host"] == "synthetic-transfer-host"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "Foo_1.pdf",
        "Foo_1_2.pdf",
    ]


@pytest.mark.parametrize("word", ["中国", "Платон"])
async def test_non_ascii_metadata_placeholder_keeps_unicode_server_wording(
    tmp_path, word
):
    python_bridge._eapi_client = FakeEAPIDownloader(word + ".pdf")
    result = await download_book(
        {"id": 42, "author": word, "title": word, "extension": "pdf"}, str(tmp_path)
    )
    assert Path(result["file_path"]).name == word + "_42.pdf"


@pytest.mark.parametrize("stem", ["文" * 81, "📚" * 60])
async def test_multibyte_server_name_reserves_identity_in_filesystem_units(
    tmp_path, stem
):
    python_bridge._eapi_client = FakeEAPIDownloader(stem + ".pdf")
    result = await download_book({"id": 123456789, "extension": "pdf"}, str(tmp_path))
    path = Path(result["file_path"])
    assert path.is_file()
    assert path.name.endswith("_123456789.pdf")
    assert len(path.name.encode("utf-8")) <= 200
    assert len(path.name.encode("utf-16-le")) // 2 <= 200


async def test_failed_eapi_download_cleans_private_staging(tmp_path):
    class Downloader:
        async def download_file(
            self, book_id, book_hash, output_dir, host_observer=None
        ):
            (Path(output_dir) / "partial.pdf").write_bytes(b"partial")
            raise OSError("simulated transfer failure")

    python_bridge._eapi_client = Downloader()
    with pytest.raises(OSError, match="simulated transfer failure"):
        await download_book({"id": 1, "extension": "pdf"}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "server_name", ["book456.epub", "456.epub", "download456.epub"]
)
async def test_generic_id_response_name_does_not_discard_known_title(
    tmp_path, server_name
):
    python_bridge._eapi_client = FakeEAPIDownloader(server_name)
    result = await download_book(
        {"id": 456, "title": "A Meaningful Title", "extension": "epub"}, str(tmp_path)
    )
    assert Path(result["file_path"]).name == "UnknownAuthor_AMeaningfulTitle_456.epub"


@pytest.mark.parametrize("server_name", ["download.epub", "book.pdf", "x." + "a" * 199])
async def test_bare_generic_or_unsupported_long_suffix_preserves_known_title(
    tmp_path, server_name
):
    python_bridge._eapi_client = FakeEAPIDownloader(server_name)
    result = await download_book(
        {"id": 456, "title": "A Meaningful Title", "extension": "pdf"}, str(tmp_path)
    )
    path = Path(result["file_path"])
    assert path.name == "UnknownAuthor_AMeaningfulTitle_456.pdf"
    assert path.read_bytes() == b"%PDF-1.4 unit test stand-in"
