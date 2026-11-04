#!/usr/bin/env python3
"""Check that acquisition stays independent of parked text-processing dependencies."""

from __future__ import annotations

import argparse
import asyncio
import ast
import os
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile


HEAVY_MODULES = {
    "fitz",
    "PIL",
    "cv2",
    "numpy",
    "nltk",
    "ebooklib",
    "ocrmypdf",
    "pytesseract",
    "pdf2image",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    module: str


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    exception = handler.type
    if isinstance(exception, ast.Name):
        return exception.id == "ImportError"
    if isinstance(exception, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id == "ImportError"
            for item in exception.elts
        )
    return False


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self._guarded = 0
        self._function_depth = 0

    def _record(self, node: ast.Import | ast.ImportFrom, module: str) -> None:
        root = module.split(".", 1)[0]
        if self._function_depth == 0 and self._guarded == 0 and root in HEAVY_MODULES:
            self.violations.append(Violation(self.path, node.lineno, root))

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._record(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module:
            self._record(node, node.module)

    def _visit_function(self, node: ast.AST) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        catches_import_error = any(_catches_import_error(h) for h in node.handlers)
        if catches_import_error:
            self._guarded += 1
        for statement in node.body:
            self.visit(statement)
        if catches_import_error:
            self._guarded -= 1

        for handler in node.handlers:
            self.visit(handler)
        for statement in node.orelse:
            self.visit(statement)
        for statement in node.finalbody:
            self.visit(statement)


def find_violations(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ImportVisitor(path)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


async def _run_core_smoke(repo_root: Path) -> None:
    """Exercise real bridge dispatch with fake providers and an import trap."""
    import importlib.abc

    parked = {
        "rag",
        "rag_processing",
        "rag_data_models",
        "footnote_continuation",
        "footnote_corruption_model",
        "formatting_group_merger",
        "garbled_text_detection",
        "marginalia_extraction",
        "metadata_generator",
        "metadata_verification",
        "note_classification",
        "quality_verification",
        "strikethrough_detection",
    }

    class ImportTrap(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            parts = fullname.split(".")
            if (
                parts[0] in HEAVY_MODULES
                or parts[0] in parked
                or (parts[0] == "lib" and len(parts) > 1 and parts[1] in parked)
            ):
                raise AssertionError(f"acquisition imported parked module: {fullname}")

    trap = ImportTrap()
    sys.meta_path.insert(0, trap)
    sys.path.insert(0, str(repo_root / "lib"))
    try:
        import python_bridge

        class OfflineRouter:
            async def search(self, query, source="auto", **kwargs):
                assert query == "offline boundary probe"
                return []

        class OfflineEapi:
            async def search(self, **kwargs):
                return {"books": []}

            async def get_book_info(self, book_id, book_hash):
                return {
                    "book": {
                        "id": book_id,
                        "title": "Offline",
                        "description": "Offline description",
                        "hash": book_hash,
                    }
                }

            async def download_file(self, book_id, book_hash, output_dir):
                document = Path(output_dir) / "offline.txt"
                document.write_text("offline acquisition bytes", encoding="utf-8")
                return str(document)

        original_router, original_eapi = (
            python_bridge._source_router,
            python_bridge._eapi_client,
        )
        python_bridge._source_router = OfflineRouter()
        python_bridge._eapi_client = OfflineEapi()
        try:
            dispatch = python_bridge._dispatch_bridge_function
            result = await dispatch(
                "search_multi_source",
                {"query": "offline boundary probe", "source": "libgen"},
            )
            assert result == {"books": [], "sources_used": []}
            assert (await dispatch("search", {"query": "offline"}))["books"] == []
            metadata = await dispatch(
                "get_book_metadata_complete", {"book_id": "1", "book_hash": "offline"}
            )
            assert metadata["description"] == "Offline description"
            with tempfile.TemporaryDirectory(prefix="zlibrary-core-smoke-") as temp_dir:
                # Downloads are contained to ZLIBRARY_OUTPUT_ROOT (#204); give the
                # smoke its own root instead of loosening the policy.
                previous_root = os.environ.get("ZLIBRARY_OUTPUT_ROOT")
                os.environ["ZLIBRARY_OUTPUT_ROOT"] = temp_dir
                try:
                    result = await dispatch(
                        "download_book",
                        {
                            "book_details": {
                                "id": "1",
                                "hash": "offline",
                                "title": "Offline",
                                "extension": "txt",
                            },
                            "output_dir": temp_dir,
                        },
                    )
                finally:
                    if previous_root is None:
                        os.environ.pop("ZLIBRARY_OUTPUT_ROOT", None)
                    else:
                        os.environ["ZLIBRARY_OUTPUT_ROOT"] = previous_root
                assert set(result) == {"file_path"}
                assert (
                    Path(result["file_path"]).read_text() == "offline acquisition bytes"
                )
        finally:
            python_bridge._source_router, python_bridge._eapi_client = (
                original_router,
                original_eapi,
            )
        assert not any(
            name.split(".")[0] in HEAVY_MODULES or name.startswith("lib.rag")
            for name in sys.modules
        )
    finally:
        sys.meta_path.remove(trap)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parents[1] / "lib"
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument(
        "--core-smoke",
        action="store_true",
        help="also run the deterministic provider-free bridge smoke",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    violations = find_violations(root)
    if violations:
        for violation in violations:
            try:
                display_path = violation.path.relative_to(root.parent)
            except ValueError:
                display_path = violation.path
            print(
                f"{display_path}:{violation.line}: unguarded optional import "
                f"{violation.module!r}",
                file=sys.stderr,
            )
        return 1

    print(f"Optional import boundary OK ({root})")
    if args.core_smoke:
        asyncio.run(_run_core_smoke(Path(__file__).resolve().parents[1]))
        print("Core-only bridge smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
