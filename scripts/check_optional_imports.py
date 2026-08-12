#!/usr/bin/env python3
"""Reject eager module-scope imports of optional RAG/scholar dependencies."""

from __future__ import annotations

import argparse
import asyncio
import ast
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
        if (
            self._function_depth == 0
            and self._guarded == 0
            and root in HEAVY_MODULES
        ):
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
    """Exercise the bridge without providers and check missing-RAG guidance."""
    sys.path.insert(0, str(repo_root / "lib"))
    import python_bridge
    import lib.rag_processing as facade
    from lib.rag.orchestrator import process_document

    class OfflineRouter:
        async def search(self, query, source="auto", **kwargs):
            if query != "offline boundary probe":
                raise AssertionError(f"unexpected smoke query: {query!r}")
            return []

        async def close(self):
            return None

    python_bridge._source_router = OfflineRouter()
    result = await python_bridge.search_multi_source(
        query="offline boundary probe", source="libgen", count=1
    )
    if result != {"books": [], "sources_used": []}:
        raise AssertionError(f"unexpected offline search result: {result!r}")

    original_pymupdf = facade.PYMUPDF_AVAILABLE
    original_ebooklib = facade.EBOOKLIB_AVAILABLE
    try:
        facade.PYMUPDF_AVAILABLE = False
        facade.EBOOKLIB_AVAILABLE = False
        with tempfile.TemporaryDirectory(prefix="zlibrary-core-smoke-") as temp_dir:
            for suffix in (".pdf", ".epub"):
                document = Path(temp_dir) / f"missing-extra{suffix}"
                document.write_bytes(b"")
                try:
                    await process_document(str(document))
                except RuntimeError as error:
                    if "uv sync --extra rag" not in str(error):
                        raise AssertionError(
                            f"missing actionable rag install hint for {suffix}: {error}"
                        ) from error
                else:
                    raise AssertionError(
                        f"{suffix} processing unexpectedly succeeded without the rag extra"
                    )
    finally:
        facade.PYMUPDF_AVAILABLE = original_pymupdf
        facade.EBOOKLIB_AVAILABLE = original_ebooklib


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
