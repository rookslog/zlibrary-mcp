"""Offline regressions for the settled #204 output policy."""

import os
import json
import errno
import hashlib
from pathlib import Path, PureWindowsPath
import stat
import sys
from unittest.mock import AsyncMock

import pytest

from lib import acquisition, python_bridge as bridge
from test_interface_acquisition import BOOK, PAYLOAD, transfer


def test_default_root_is_data_directory_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("ZLIBRARY_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    expected = (
        tmp_path
        / ("local" if os.name == "nt" else "data")
        / "zlibrary-mcp"
        / "downloads"
    )
    for name in ("first", "second"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        assert acquisition.output_directory(None) == expected
    if os.name != "nt":
        monkeypatch.delenv("XDG_DATA_HOME")
        assert (
            acquisition.output_directory(None)
            == Path.home() / ".local/share/zlibrary-mcp/downloads"
        )


def test_relative_output_is_root_relative(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    assert acquisition.output_directory("nested") == root / "nested"
    assert acquisition.output_directory(str(root / "inside")) == root / "inside"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe", ["..", "a/../b", "~/books", "~someone/books", "outside", "symlink"]
)
@pytest.mark.parametrize("unrestricted", ["0", "1"])
async def test_refusals_precede_provider_contact(
    tmp_path, monkeypatch, unsafe, unrestricted
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("ZLIBRARY_UNRESTRICTED_OUTPUT", unrestricted)
    if unsafe in {"outside", "symlink"}:
        if unrestricted == "1":
            pytest.skip("unrestricted mode deliberately permits outside resolved paths")
        outside = tmp_path / "elsewhere"
        if unsafe == "symlink":
            (root / "link").symlink_to(outside, target_is_directory=True)
            unsafe = "link"
        else:
            unsafe = str(outside)
    provider = AsyncMock(side_effect=AssertionError("no provider contact"))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        await acquisition.acquire(BOOK, unsafe, transfer=provider)
    assert caught.value.to_dict()["reason"] == "output_refused"
    assert caught.value.to_dict()["request_issued"] is False
    provider.assert_not_awaited()


def test_unrestricted_lifts_only_containment(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("ZLIBRARY_UNRESTRICTED_OUTPUT", "1")
    assert (
        acquisition.output_directory(str(tmp_path / "outside")) == tmp_path / "outside"
    )
    assert acquisition.output_directory("nested") == root / "nested"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_directory", [True, False], ids=["new-levels", "existing-levels"]
)
@pytest.mark.parametrize("staged", [True, False], ids=["staged", "callback-copy"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_private_new_levels_artifact_and_receipt_preserve_existing_mode(
    tmp_path, monkeypatch, staged, new_directory
):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(tmp_path))
    tmp_path.chmod(0o755)
    directory = tmp_path / "new" / "nested"
    if not new_directory:
        directory.mkdir(parents=True)
        directory.parent.chmod(0o755)
        directory.chmod(0o755)

    async def copy_callback(book, directory, *, filename, publication_hook):
        stage = Path(directory) / ".stage"
        stage.write_bytes(PAYLOAD)
        target = Path(directory) / acquisition.destination_name(book, filename)
        publication_hook(stage, target)
        target.write_bytes(stage.read_bytes())
        return {"file_path": str(target)}

    previous = os.umask(0o022)
    try:
        result = await acquisition.acquire(
            BOOK, str(directory), transfer=transfer if staged else copy_callback
        )
    finally:
        os.umask(previous)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    for path in (directory.parent, directory):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if new_directory else 0o755)
    for path in (Path(result["path"]), Path(result["record"])):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_relative_environment_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", "relative")
    with pytest.raises(acquisition.AcquisitionError, match="ZLIBRARY_OUTPUT_ROOT"):
        acquisition.output_directory(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["zlibrary", "libgen"])
async def test_legacy_outside_root_refuses_before_provider(
    tmp_path, monkeypatch, source
):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(tmp_path / "root"))
    login = AsyncMock(side_effect=AssertionError("no authentication"))
    provider = AsyncMock(side_effect=AssertionError("no provider contact"))
    monkeypatch.setattr(bridge, "get_eapi_client", login)
    monkeypatch.setattr(bridge, "_fetch_from_source", provider)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        await bridge.download_book(
            {**BOOK, "source": source}, str(tmp_path / "outside")
        )
    assert caught.value.to_dict() == {
        "reason": "output_refused",
        "request_issued": False,
    }
    login.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_cli_outside_root_refuses_before_authentication(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python_bridge.py",
            "download_book",
            json.dumps(
                {
                    "book_details": BOOK,
                    "output_dir": str(tmp_path / "outside"),
                }
            ),
        ],
    )
    login = AsyncMock(side_effect=AssertionError("no authentication"))
    monkeypatch.setattr(bridge, "initialize_eapi_client", login)
    monkeypatch.setattr(bridge, "_eapi_client", None)
    monkeypatch.setattr(bridge, "_session_input", None)
    with pytest.raises(SystemExit) as stopped:
        await bridge.main()
    assert stopped.value.code == 1
    login.assert_not_awaited()
    streams = capsys.readouterr()
    assert streams.out == ""
    error = json.loads(streams.err.strip().splitlines()[-1])
    assert error["details"]["reason"] == "output_refused"
    assert error["details"]["request_issued"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, "new/nested"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_legacy_resolves_under_root_and_publishes_private_file(
    tmp_path, monkeypatch, output
):
    root = tmp_path / "root"
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(root))

    async def fetch(book, directory):
        stage = Path(directory) / ".source-test.pdf"
        stage.write_bytes(PAYLOAD)
        stage.chmod(0o644)
        return str(stage)

    monkeypatch.setattr(bridge, "_fetch_from_source", fetch)
    result = await bridge.download_book(
        {**BOOK, "source": "libgen"}, output, filename="book.pdf"
    )
    directory = root / (output or "")
    artifact = Path(result["file_path"])
    assert artifact == directory / "book.pdf"
    assert artifact.read_bytes() == PAYLOAD
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(p.stat().st_mode) == 0o700
        for p in (root, directory.parent, directory)
        if p != tmp_path
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_symlink_inserted_during_directory_creation_refuses(
    tmp_path, monkeypatch, legacy
):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(root))
    original = acquisition._private_directory

    def swap(directory):
        original(directory)
        if directory == root / "nested":
            directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(acquisition, "_private_directory", swap)
    provider = AsyncMock(side_effect=AssertionError("no provider contact"))
    monkeypatch.setattr(bridge, "_fetch_from_source", provider)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        if legacy:
            await bridge.download_book({**BOOK, "source": "libgen"}, "nested")
        else:
            await acquisition.acquire(BOOK, "nested", transfer=provider)
    assert caught.value.to_dict() == {
        "reason": "output_refused",
        "request_issued": False,
    }
    provider.assert_not_awaited()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_only", [False, True])
@pytest.mark.parametrize("corrected", [False, True])
@pytest.mark.parametrize("mode", [0o640, 0o440], ids=["shared", "read-only"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_verified_reuse_preserves_existing_modes(
    tmp_path, receipt_only, corrected, mode
):
    async def corrected_transfer(book, directory, *, filename, publication_hook):
        return await transfer(
            {**book, "extension": "epub"},
            directory,
            filename=filename,
            publication_hook=publication_hook,
        )

    provider = AsyncMock(side_effect=corrected_transfer if corrected else transfer)
    first = await acquisition.acquire(BOOK, str(tmp_path), transfer=provider)
    paths = [Path(first["path"]), *tmp_path.glob("*.acq.json")]
    before = {p: p.read_bytes() for p in paths}
    for path in paths:
        path.chmod(mode)
    metadata = {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths
    }
    result = await acquisition.acquire(
        BOOK, str(tmp_path), receipt_only=receipt_only, transfer=provider
    )
    assert result["transfer_skipped"] is True
    assert provider.await_count == 1
    assert {p: p.read_bytes() for p in paths} == before
    assert all(stat.S_IMODE(p.stat().st_mode) == mode for p in paths)
    assert {
        p: (p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths
    } == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o640, 0o440], ids=["shared", "read-only"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_legacy_reuse_preserves_existing_mode(tmp_path, monkeypatch, mode):
    async def fetch(book, directory):
        stage = Path(directory) / ".source-test.pdf"
        stage.write_bytes(PAYLOAD)
        return str(stage)

    monkeypatch.setattr(bridge, "_fetch_from_source", fetch)
    book = {**BOOK, "source": "libgen"}
    first = await bridge.download_book(book, str(tmp_path), filename="book.pdf")
    artifact = Path(first["file_path"])
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    artifact.chmod(mode)
    metadata = (
        artifact.stat().st_ino,
        artifact.stat().st_mtime_ns,
        artifact.stat().st_ctime_ns,
    )
    again = await bridge.download_book(
        book, str(tmp_path), filename="book.pdf", publication_hook=lambda *_: None
    )
    assert again["publication_reused"] is True
    assert artifact.read_bytes() == PAYLOAD
    assert stat.S_IMODE(artifact.stat().st_mode) == mode
    assert (
        artifact.stat().st_ino,
        artifact.stat().st_mtime_ns,
        artifact.stat().st_ctime_ns,
    ) == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deny_chmod", [False, True], ids=["shared", "foreign-owned-eperm"]
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_format_corrected_publication_reuse_preserves_mode(
    tmp_path, monkeypatch, deny_chmod
):
    payload = b"PK\x03\x04synthetic epub bytes"

    async def fetch(book, directory):
        stage = Path(directory) / ".source-fixture.epub"
        stage.write_bytes(payload)
        return bridge._DownloadedPath(str(stage), "signature")

    monkeypatch.setattr(bridge, "_fetch_from_source", fetch)
    book = {
        **BOOK,
        "source": "libgen",
        "md5": hashlib.md5(payload).hexdigest(),
        "extension": "epub",
    }
    provider = AsyncMock(side_effect=bridge.download_book)
    first = await acquisition.acquire(
        book, str(tmp_path), filename="book.epub", transfer=provider
    )
    artifact = Path(first["path"])
    artifact.chmod(0o640)
    metadata = (
        artifact.stat().st_ino,
        artifact.stat().st_mtime_ns,
        artifact.stat().st_ctime_ns,
    )
    canonical_receipt = Path(first["record"]).read_bytes()
    chmod = Path.chmod

    def refuse_artifact_chmod(path, *args, **kwargs):
        if path == artifact:
            raise PermissionError(
                errno.EPERM, "synthetic foreign-owned artifact", str(path)
            )
        return chmod(path, *args, **kwargs)

    if deny_chmod:
        monkeypatch.setattr(Path, "chmod", refuse_artifact_chmod)
    corrected = await acquisition.acquire(
        {**book, "extension": "pdf"},
        str(tmp_path),
        filename="book.pdf",
        transfer=provider,
    )
    assert corrected["path"] == str(artifact)
    assert corrected["reused"] is True
    assert corrected["transfer_skipped"] is False
    assert provider.await_count == 2
    assert artifact.read_bytes() == payload
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o640
    assert (
        artifact.stat().st_ino,
        artifact.stat().st_mtime_ns,
        artifact.stat().st_ctime_ns,
    ) == metadata
    assert Path(first["record"]).read_bytes() == canonical_receipt
    assert (tmp_path / "book.pdf.acq.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrestricted", ["0", "1"])
async def test_nul_output_refuses_before_provider(tmp_path, monkeypatch, unrestricted):
    monkeypatch.setenv("ZLIBRARY_UNRESTRICTED_OUTPUT", unrestricted)
    provider = AsyncMock(side_effect=AssertionError("no provider contact"))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        await acquisition.acquire(BOOK, "bad\x00path", transfer=provider)
    assert caught.value.to_dict() == {
        "reason": "output_refused",
        "request_issued": False,
    }
    provider.assert_not_awaited()


class LexicalWindowsPath(PureWindowsPath):
    """Reading-level path resolution: no Windows filesystem access on POSIX."""

    def resolve(self):
        return self

    def expanduser(self):
        return self


@pytest.mark.parametrize("value", [r"\foo", "D:foo"])
def test_windows_configured_root_must_have_drive_and_root(monkeypatch, value):
    monkeypatch.setattr(acquisition, "_WINDOWS", True)
    monkeypatch.setattr(acquisition, "Path", LexicalWindowsPath)
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", value)
    with pytest.raises(acquisition.AcquisitionError, match="ZLIBRARY_OUTPUT_ROOT"):
        acquisition.output_directory(None)


@pytest.mark.parametrize("unrestricted", ["0", "1"])
@pytest.mark.parametrize("value", [r"\foo", "D:foo"])
def test_windows_partially_anchored_output_refuses(monkeypatch, unrestricted, value):
    monkeypatch.setattr(acquisition, "_WINDOWS", True)
    monkeypatch.setattr(acquisition, "Path", LexicalWindowsPath)
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", r"C:\private")
    monkeypatch.setenv("ZLIBRARY_UNRESTRICTED_OUTPUT", unrestricted)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.output_directory(value)
    assert caught.value.to_dict() == {
        "reason": "output_refused",
        "request_issued": False,
    }


def test_windows_relative_and_fully_qualified_outputs(monkeypatch):
    monkeypatch.setattr(acquisition, "_WINDOWS", True)
    monkeypatch.setattr(acquisition, "Path", LexicalWindowsPath)
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", r"C:\private")
    assert acquisition.output_directory("nested") == PureWindowsPath(
        r"C:\private\nested"
    )
    assert acquisition.output_directory(r"C:\private\inside") == PureWindowsPath(
        r"C:\private\inside"
    )
    monkeypatch.setenv("ZLIBRARY_UNRESTRICTED_OUTPUT", "1")
    assert acquisition.output_directory(r"D:\outside") == PureWindowsPath(r"D:\outside")
