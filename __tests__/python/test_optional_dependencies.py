"""Regression coverage for the core/RAG/scholar dependency boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib

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

    rag = {"pymupdf", "ebooklib"}
    scholar_only = {
        "nltk",
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
        result = asyncio.run(
            bridge.search_multi_source(
                query="offline boundary probe", source="libgen", count=1
            )
        )
        print(json.dumps(result, sort_keys=True))
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
    assert result.stdout.strip() == '{"books": [], "sources_used": []}'


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

    with pytest.raises(RuntimeError, match=r"uv sync --extra rag"):
        await process_document(str(document))
