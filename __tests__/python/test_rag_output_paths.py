"""Regression tests for deterministic, collision-resistant RAG output files."""

import hashlib
import os
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import lib.rag.orchestrator as rag_orchestrator
from filename_utils import create_unified_filename
from lib.rag.pipeline.models import DocumentOutput
from lib.rag.orchestrator_pdf import process_pdf_structured
from lib.rag.utils.atomic_write import atomic_write_text


@pytest.mark.asyncio
async def test_no_book_details_outputs_are_unique_stable_and_absolute(
    tmp_path, monkeypatch
):
    inputs = tmp_path / "inputs"
    (inputs / "a").mkdir(parents=True)
    (inputs / "b").mkdir()
    sources = {
        inputs / "Платон.txt": "Plato source",
        inputs / "Аристотель.txt": "Aristotle source",
        inputs / "a" / "notes.txt": "Notes from A",
        inputs / "b" / "notes.txt": "Notes from B",
    }
    for source, content in sources.items():
        source.write_text(content, encoding="utf-8")

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(rag_orchestrator, "PROCESSED_OUTPUT_DIR", Path("processed"))
    replacements = []
    original_replace = os.replace

    def record_replace(source, target):
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.parent == target_path.parent
        replacements.append(target_path)
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", record_replace)

    results = {}
    for source in sources:
        results[source] = await rag_orchestrator.process_document(str(source))

    paths = [Path(result["processed_file_path"]) for result in results.values()]
    assert all(path.is_absolute() for path in paths)
    assert len({path.name for path in paths}) == len(sources)
    for source, result in results.items():
        path = Path(result["processed_file_path"])
        slug = "notes" if source.stem == "notes" else "doc"
        digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
        assert path.name == f"{slug}-{digest}.txt.processed.txt"
    for source, content in sources.items():
        assert content in Path(results[source]["processed_file_path"]).read_text(
            encoding="utf-8"
        )

    repeated_source = inputs / "a" / "notes.txt"
    first_path = results[repeated_source]["processed_file_path"]
    repeated_source.write_text("Updated notes from A", encoding="utf-8")
    repeated = await rag_orchestrator.process_document(str(repeated_source))

    assert repeated["processed_file_path"] == first_path
    assert "Updated notes from A" in Path(first_path).read_text(encoding="utf-8")
    expected_targets = {
        Path(path)
        for result in [*results.values(), repeated]
        for path in (result["processed_file_path"], result["metadata_file_path"])
    }
    assert set(replacements) == expected_targets


@pytest.mark.asyncio
async def test_book_details_keep_existing_filename_shape(tmp_path, monkeypatch):
    output_dir = tmp_path / "processed"
    monkeypatch.setattr(rag_orchestrator, "PROCESSED_OUTPUT_DIR", output_dir)
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    details = {"author": "Jane Doe", "title": "A Latin Title", "id": "12345"}

    result = await rag_orchestrator.process_document(str(source), book_details=details)

    expected_name = (
        f"{create_unified_filename(details, extension=None)}.txt.processed.txt"
    )
    assert Path(result["processed_file_path"]).name == expected_name


@pytest.mark.integration
def test_real_pdf_output_files_use_sibling_atomic_replacements(tmp_path, monkeypatch):
    pdf_candidates = (
        PROJECT_ROOT / "test_files" / "heidegger_pages_22-23_primary_footnote_test.pdf",
        PROJECT_ROOT / "test_files" / "derrida_footnote_pages_120_125.pdf",
    )
    pdf = next((path for path in pdf_candidates if path.exists()), None)
    if pdf is None:
        pytest.skip("No real PDF fixture available")
    document = process_pdf_structured(pdf, output_format="markdown")
    replacements = []
    original_replace = os.replace

    def record_replace(source, target):
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.parent == target_path.parent
        assert source_path.exists()
        replacements.append(target_path)
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", record_replace)
    written = document.write_files(tmp_path / pdf.name)

    assert written["body"].read_text(encoding="utf-8") == document.body_text
    assert written["metadata"].is_file()
    assert set(replacements) == {path for path in written.values()}
    assert all(path.parent == tmp_path for path in replacements)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        path.name for path in written.values()
    )


def test_footnote_replace_failure_preserves_old_file_and_cleans_temp(
    tmp_path, monkeypatch
):
    body_path = tmp_path / "book.processed.markdown"
    footnote_path = tmp_path / "book.processed_footnotes.markdown"
    body_path.write_text("old body", encoding="utf-8")
    footnote_path.write_text("old footnotes", encoding="utf-8")
    document = DocumentOutput(body_text="new body", footnotes="new footnotes")
    original_replace = os.replace

    def fail_footnote_replace(source, target):
        if Path(target) == footnote_path:
            raise OSError("simulated replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_footnote_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        document.write_files(body_path)

    assert body_path.read_text(encoding="utf-8") == "new body"
    assert footnote_path.read_text(encoding="utf-8") == "old footnotes"
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [body_path.name, footnote_path.name]
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes are unavailable")
def test_atomic_writes_preserve_existing_mode_and_use_default_creation_mode(tmp_path):
    regular_path = tmp_path / "regular.txt"
    regular_path.write_text("regular", encoding="utf-8")

    existing_path = tmp_path / "existing.txt"
    existing_path.write_text("old", encoding="utf-8")
    os.chmod(existing_path, 0o640)
    atomic_write_text(existing_path, "new")

    new_path = tmp_path / "new.txt"
    atomic_write_text(new_path, "new")

    assert stat.S_IMODE(existing_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(new_path.stat().st_mode) == stat.S_IMODE(
        regular_path.stat().st_mode
    )


def test_concurrent_atomic_writes_leave_one_complete_last_write(tmp_path):
    destination = tmp_path / "shared.txt"
    contents = ("first" * 100_000, "second" * 100_000)
    start_together = threading.Barrier(len(contents))

    def write(content):
        start_together.wait()
        atomic_write_text(destination, content)

    with ThreadPoolExecutor(max_workers=len(contents)) as executor:
        futures = [executor.submit(write, content) for content in contents]
        for future in futures:
            future.result()

    assert destination.read_text(encoding="utf-8") in contents
    assert sorted(path.name for path in tmp_path.iterdir()) == [destination.name]
