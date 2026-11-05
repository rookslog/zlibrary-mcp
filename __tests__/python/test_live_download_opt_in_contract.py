"""Check the live download skip marker without importing or running live code."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("value", [None, "", "0", "true", "1"])
def test_live_download_requires_exact_opt_in(value):
    source = Path(__file__).parent / "integration" / "test_real_zlibrary.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_download_small_book"
    )
    # Compile only the method's decorators onto an inert body. Live fixtures,
    # imports, module credential gate and provider code cannot run here.
    method.body = [ast.Pass()]
    ast.fix_missing_locations(method)
    namespace = {
        "pytest": pytest,
        "os": SimpleNamespace(
            getenv=lambda name: value if name == "ZLIBRARY_LIVE_DOWNLOAD" else None
        ),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    markers = [
        marker
        for marker in getattr(namespace[method.name], "pytestmark", [])
        if marker.name == "skipif"
    ]
    assert len(markers) == 1, "Download must have its own quota opt-in skip marker"
    marker = markers[0]
    assert marker.args == (value != "1",)
    assert "real download" in marker.kwargs["reason"].lower()
    assert "quota" in marker.kwargs["reason"].lower()
