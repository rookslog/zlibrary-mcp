"""Remaining #292 regressions: local files, synthetic providers, bounded timers."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import acquisition as acq
from lib import python_bridge as bridge
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import AllSourcesFailedError, ProviderUnreachableError
from lib.sources.net import WalkDeadline

BOOK = {"source": "libgen", "md5": "a" * 32, "extension": "pdf"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_ms", [2, 5])
async def test_stalled_chain_keeps_mirror_failures_through_cleanup(
    tmp_path, monkeypatch, cleanup_ms
):
    """Catch the outer watchdog replacing attributed errors during stream close."""
    monkeypatch.setenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", "40000")
    now = [0.0]
    monkeypatch.setattr(
        acq, "WalkDeadline", lambda budget: WalkDeadline(budget, clock=lambda: now[0])
    )
    lock = acq.destination_lock

    @asynccontextmanager
    async def delayed_lock(*args):
        async with lock(*args):
            now[0] = 29.0  # Leave a useful but small real transfer allocation.
            yield

    cleaned = []

    async def candidates(*args, **kwargs):
        for host in ("libgen.li", "libgen.vg"):
            yield SimpleNamespace(
                source="libgen", url=f"https://{host}/book", mirror=host
            )

    async def router(**kwargs):
        return SimpleNamespace(
            config=SourceConfig(), iter_download_candidates=candidates
        )

    async def download(url, *args, **kwargs):
        if "libgen.li" in url:
            raise ProviderUnreachableError(
                "libgen", "libgen.li", reason="connect_timeout"
            )
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(cleanup_ms / 1000)
            cleaned.append(True)

    async def transfer(book, directory, **kwargs):
        return await bridge._fetch_from_source(book, directory)

    monkeypatch.setattr(acq, "destination_lock", delayed_lock)
    monkeypatch.setattr(bridge, "get_source_router", router)
    monkeypatch.setattr(bridge, "_download_url_to_file", download)
    with pytest.raises(AllSourcesFailedError) as error:
        await asyncio.wait_for(
            acq.acquire(BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer),
            8.0,
        )
    assert [(f.host, f.reason) for f in error.value.failures] == [
        ("libgen.li", "connect_timeout"),
        ("libgen.vg", "read_timeout"),
    ]
    assert cleaned == [True]
    assert not (tmp_path / "chosen.pdf").exists()
    async with acq.destination_lock(tmp_path / "chosen.pdf", 0):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_adoption_commits_before_best_effort_stage_cleanup(
    tmp_path, monkeypatch, persistent
):
    """Catch adoption cleanup replacing an identical artifact's committed success."""
    stage, target = tmp_path / ".source-fixture.pdf", tmp_path / "chosen.pdf"
    payload = b"%PDF-1.7\nadopted artifact"
    unlink = Path.unlink
    attempts = []
    inode = []

    def refuse_cleanup(path, *args, **kwargs):
        if path == stage and target.exists():
            attempts.append(acq._acquisition_scope.get()["published"])
            if persistent or len(attempts) == 1:
                raise PermissionError("synthetic adoption cleanup refusal")
        return unlink(path, *args, **kwargs)

    async def transfer(book, directory, *, filename, publication_hook):
        stage.write_bytes(payload)
        publication_hook(stage, target)
        target.write_bytes(payload)  # An identical artifact appears after checkpoint.
        inode.append(target.stat().st_ino)
        reused = bridge._publish_or_reuse(stage, target)
        return {"file_path": str(target), "publication_reused": reused}

    monkeypatch.setattr(Path, "unlink", refuse_cleanup)
    result = await acq.acquire(
        BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer
    )
    assert result["reused"] is True
    assert attempts == [True]
    assert target.stat().st_ino == inode[0]
    assert target.read_bytes() == payload
    assert json.loads(Path(result["record"]).read_text())["state"] == "complete"

    async def forbidden(*args, **kwargs):
        pytest.fail("adoption receipt replayed provider work")

    hit = await acq.acquire(
        BOOK, str(tmp_path), filename="chosen.pdf", transfer=forbidden
    )
    assert hit["transfer_skipped"] is True
    assert hit["path"] == str(target)


@pytest.mark.parametrize("download", [False, True])
def test_short_source_owner_names_configuration_and_retry_document(
    monkeypatch, download
):
    """Catch environment errors presented as caller argument corrections."""
    name = "PYTHON_BRIDGE_LONG_TIMEOUT" if download else "PYTHON_BRIDGE_TIMEOUT"
    monkeypatch.setenv(name, "9999")
    with pytest.raises(ValueError) as error:
        get_source_config(for_download=download)
    assert "configuration error" in str(error.value).lower()
    assert name in str(error.value)
    assert "docs/RETRY_CONFIGURATION.md" in str(error.value)
    assert error.value.to_dict()["reason"] == "configuration_error"
    assert error.value.to_dict()["request_issued"] is False
    assert error.value.to_dict()["variable"] == name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bad", 30.0),
        ("", 30.0),
        ("0", 30.0),
        ("1.5", 30.0),
        ("2147483648", 30.0),
        ("90000", 90.0),
        ("\ufeff90000\u2029", 90.0),
    ],
)
def test_direct_python_effective_owner_fallback_matches_node(
    monkeypatch, raw, expected
):
    """Node falls back to its ordinary owner for invalid CallOptions.timeoutMs."""
    monkeypatch.setenv("PYTHON_BRIDGE_TIMEOUT", "30000")
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "120000")
    monkeypatch.setenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", raw)
    assert acq._long_bridge_budget() == expected
    monkeypatch.delenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS")
    assert acq._long_bridge_budget() == 120.0
