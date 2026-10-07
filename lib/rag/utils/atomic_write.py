"""Helpers for replacing RAG output files atomically."""

import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path


def _create_temporary_path(destination: Path) -> Path:
    for _ in range(100):
        temporary_path = destination.with_name(
            f".rag-output-{secrets.token_hex(16)}.tmp"
        )
        try:
            with temporary_path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        return temporary_path

    raise FileExistsError(f"Could not allocate a temporary file beside {destination}")


def atomic_write(path: Path, write_temporary: Callable[[Path], None]) -> Path:
    """Write a sibling temporary file, then atomically replace ``path``."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        temporary_path = _create_temporary_path(destination)
        write_temporary(temporary_path)
        try:
            destination_mode = stat.S_IMODE(destination.stat().st_mode) & 0o777
        except FileNotFoundError:
            pass
        else:
            os.chmod(temporary_path, destination_mode)

        os.replace(temporary_path, destination)
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    """Atomically replace a text file with ``content``."""

    def write_temporary(temporary_path: Path) -> None:
        temporary_path.write_text(content, encoding=encoding)

    return atomic_write(path, write_temporary)
