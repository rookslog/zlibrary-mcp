"""Regression coverage for the core/RAG/scholar dependency boundaries."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import pytest


pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check_optional_imports.py"


def _dependency_name(requirement: str) -> str:
    """Return the normalized distribution name from a simple requirement."""
    return (
        requirement.split("[", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .split("=", 1)[0]
        .strip()
        .lower()
        .replace("_", "-")
    )


def test_pyproject_keeps_heavy_dependencies_out_of_core_and_in_named_extras():
    """Moving a heavy package back to core must fail the packaging boundary."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    project = config["project"]

    core = {_dependency_name(value) for value in project["dependencies"]}
    extras = {
        name: {_dependency_name(value) for value in values}
        for name, values in project["optional-dependencies"].items()
    }

    rag = {"pymupdf", "ebooklib", "nltk"}
    scholar_only = {
        "opencv-python-headless",
        "ocrmypdf",
        "pytesseract",
        "pdf2image",
        "pillow",
    }

    assert not core.intersection(rag | scholar_only)
    assert rag <= extras["rag"]
    assert rag | scholar_only <= extras["scholar"]
    assert "ocr" not in extras
    assert config["tool"]["uv"]["package"] is False


def test_import_checker_reports_module_scope_heavy_imports(tmp_path):
    """An unguarded heavy import must produce a path-and-line diagnostic."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "guarded.py").write_text(
        "try:\n    import fitz\nexcept ImportError:\n    fitz = None\n"
    )
    (lib_dir / "unguarded.py").write_text("from PIL import Image\n")

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(lib_dir)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unguarded.py:1" in result.stderr
    assert "PIL" in result.stderr
    assert "lib/guarded.py:" not in result.stderr


def test_repository_has_no_unguarded_module_scope_heavy_imports():
    """The repository checker must pass on the complete lib import graph."""
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_checker_core_smoke_completes_offline_search_and_checks_rag_error():
    """The CI smoke entry point must exercise startup, search, and install guidance."""
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--core-smoke"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Core-only bridge smoke OK" in result.stdout


@pytest.mark.asyncio
async def test_core_smoke_does_not_create_a_background_executor(monkeypatch):
    """The standalone core check must terminate without executor shutdown work."""
    from scripts.check_optional_imports import _run_core_smoke

    async def unexpected_to_thread(*args, **kwargs):
        raise AssertionError("core smoke started a background executor")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_to_thread)

    await _run_core_smoke(REPO_ROOT)


def test_resolution_package_does_not_eagerly_import_heavy_implementations():
    """Importing resolution models must not pull analyzer or renderer into startup."""
    script = textwrap.dedent(
        """
        import sys
        import lib.rag.resolution

        assert "lib.rag.resolution.analyzer" not in sys.modules
        assert "lib.rag.resolution.renderer" not in sys.modules
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _nltk_tokenizer_data_present() -> bool:
    """Whether this environment has NLTK's punkt data, without downloading it."""
    try:
        from nltk.tokenize import sent_tokenize
    except ImportError:
        return False
    try:
        sent_tokenize("Tokenizer readiness probe.")
    except LookupError:
        return False
    return True


def test_footnote_detection_falls_back_without_nltk_data_or_network(tmp_path):
    """RAG footnote continuation must remain deterministic with empty NLTK data."""
    script = textwrap.dedent(
        """
        import os
        import nltk

        nltk.data.path[:] = [os.environ["NLTK_DATA"]]

        def unexpected_download(*args, **kwargs):
            raise AssertionError("footnote import attempted an NLTK data download")

        nltk.download = unexpected_download

        import lib.footnote_continuation as footnotes

        assert footnotes._nltk_ready is False
        assert footnotes.is_footnote_incomplete("This note continues") == (
            True,
            0.75,
            "fallback_incomplete",
        )
        assert footnotes.is_footnote_incomplete("This note is complete.") == (
            False,
            0.85,
            "fallback_complete",
        )
        assert footnotes.is_footnote_incomplete("This concept refers to") == (
            True,
            0.90,
            "incomplete_phrase",
        )
        """
    )
    env = os.environ.copy()
    env["NLTK_DATA"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    not _nltk_tokenizer_data_present(),
    reason=(
        "NLTK punkt data absent. This test asserts the NLTK path is preferred "
        "WHEN the data exists; asserting the data exists would make a CI "
        "provisioning choice look like a footnote-detection defect."
    ),
)
def test_footnote_detection_uses_nltk_when_tokenizer_data_is_available():
    """The higher-quality sentence tokenizer remains active when its data exists."""
    script = textwrap.dedent(
        """
        import lib.footnote_continuation as footnotes

        assert footnotes._nltk_ready is True
        assert footnotes.is_footnote_incomplete("See Dr. Smith") == (
            True,
            0.80,
            "nltk_incomplete",
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_footnote_detection_does_not_hide_unrelated_nltk_errors(monkeypatch):
    """Only missing tokenizer data may select the deterministic fallback."""
    import lib.footnote_continuation as footnotes

    def unexpected_tokenizer_error(text):
        raise RuntimeError("unexpected tokenizer failure")

    footnotes.is_footnote_incomplete.cache_clear()
    monkeypatch.setattr(footnotes, "_nltk_ready", True)
    monkeypatch.setattr(footnotes, "_sent_tokenize", unexpected_tokenizer_error)

    with pytest.raises(RuntimeError, match="unexpected tokenizer failure"):
        footnotes.is_footnote_incomplete("A footnote with enough words.")


def test_core_only_bridge_starts_and_completes_offline_search():
    """Core bridge dispatch must work when every RAG/scholar import is unavailable."""
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import json
        from pathlib import Path
        import sys

        HEAVY = {
            "fitz", "PIL", "cv2", "numpy", "nltk", "ebooklib",
            "ocrmypdf", "pytesseract", "pdf2image",
        }

        class BlockHeavy(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".", 1)[0] in HEAVY:
                    raise ModuleNotFoundError(
                        f"blocked optional dependency: {fullname}", name=fullname
                    )
                return None

        sys.meta_path.insert(0, BlockHeavy())
        sys.path.insert(0, str(Path.cwd() / "lib"))

        import python_bridge as bridge

        class OfflineRouter:
            async def search(self, query, source="auto", **kwargs):
                assert query == "offline boundary probe"
                return []

            async def close(self):
                return None

        bridge._source_router = OfflineRouter()
        sys.argv = [
            "python_bridge.py",
            "search_multi_source",
            json.dumps(
                {
                    "query": "offline boundary probe",
                    "source": "libgen",
                    "count": 1,
                }
            ),
        ]
        asyncio.run(bridge.main())
        """
    )
    env = os.environ.copy()
    for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD", "ANNAS_SECRET_KEY"):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload == {"books": [], "sources_used": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "availability_flag"),
    [(".pdf", "PYMUPDF_AVAILABLE"), (".epub", "EBOOKLIB_AVAILABLE")],
)
async def test_rag_operation_without_rag_extra_names_install_command(
    tmp_path, monkeypatch, suffix, availability_flag
):
    """Missing RAG dependencies must name the exact uv extra, not leak ImportError."""
    import lib.rag_processing as facade
    from lib.rag.orchestrator import process_document

    document = tmp_path / f"missing-extra{suffix}"
    document.write_bytes(b"")
    monkeypatch.setattr(facade, availability_flag, False)

    with pytest.raises(RuntimeError, match=r"uv sync --no-dev --extra rag"):
        await process_document(str(document))


@pytest.mark.slow
@pytest.mark.ground_truth
def test_rag_tier_processes_real_pdf_without_scholar_dependencies(
    lfs_fixture, tmp_path
):
    """The RAG tier must preserve real-PDF extraction with offline NLTK fallback."""
    pdf_path = REPO_ROOT / "test_files" / "sample.pdf"
    lfs_fixture(pdf_path)

    script = textwrap.dedent(
        """
        import importlib.abc
        import json
        import os
        from pathlib import Path
        import sys
        import time

        import nltk

        nltk.data.path[:] = [os.environ["NLTK_DATA"]]

        def unexpected_download(*args, **kwargs):
            raise AssertionError("real-PDF processing attempted an NLTK data download")

        nltk.download = unexpected_download

        SCHOLAR_ONLY = {
            "PIL", "cv2", "ocrmypdf", "pdf2image", "pytesseract"
        }

        class BlockScholar(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".", 1)[0] in SCHOLAR_ONLY:
                    raise ModuleNotFoundError(
                        f"blocked scholar-only dependency: {fullname}", name=fullname
                    )
                return None

        sys.meta_path.insert(0, BlockScholar())

        import lib.footnote_continuation as footnotes
        from lib.rag.orchestrator_pdf import process_pdf

        assert footnotes._nltk_ready is False

        repo_root = Path.cwd()
        baseline = json.loads(
            (repo_root / "test_files/ground_truth/body_text_baseline.json").read_text()
        )["baselines"]["sample.pdf"]
        budget = json.loads(
            (repo_root / "test_files/performance_budgets.json").read_text()
        )["quality_gates"]["pre_commit"]["max_single_test_time_seconds"]

        started = time.perf_counter()
        text = process_pdf(
            repo_root / "test_files/sample.pdf",
            output_format="markdown",
            enable_quality_pipeline=False,
        )
        elapsed = time.perf_counter() - started

        assert len(text) >= baseline["body_text_length"] * 0.95
        assert all(line in text for line in baseline["sample_lines"])
        assert elapsed <= budget, f"real-PDF extraction took {elapsed:.2f}s > {budget}s"
        """
    )

    nltk_data = tmp_path / "nltk_data"
    nltk_data.mkdir()
    env = os.environ.copy()
    env["NLTK_DATA"] = str(nltk_data)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
