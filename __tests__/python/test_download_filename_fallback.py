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

    async def download_file(self, book_id, book_hash, output_dir):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / self.filename
        path.write_bytes(b"%PDF-1.4 unit test stand-in")
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
        ],
    )
    def test_is_preservable(self, filename, expected):
        assert is_preservable_server_filename(filename) is expected


class TestDownloadBookFilenameFallback:
    async def test_sparse_metadata_keeps_server_filename(self, tmp_path):
        python_bridge._eapi_client = FakeEAPIDownloader(SERVER_NAME)
        sparse = {
            "id": 127803575,
            "hash": "da1982",
            "title": "Mixed legal systems",
            "extension": "pdf",
        }

        result = await download_book(sparse, str(tmp_path))

        assert Path(result["file_path"]).name == SERVER_NAME
        assert (tmp_path / SERVER_NAME).exists()
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

        assert Path(result["file_path"]).name == "name_v1__.pdf"
