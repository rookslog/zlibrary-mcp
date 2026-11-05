"""Whole-acquisition clock: no phase may reset the long bridge allowance."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from lib import acquisition as acq

BOOK = {"source": "libgen", "md5": "a" * 32, "extension": "pdf", "title": "Fixture"}


async def publish(book, directory, *, filename, publication_hook):
    stage, target = Path(directory) / ".stage", Path(directory) / filename
    stage.write_bytes(b"synthetic bytes")
    publication_hook(stage, target)
    stage.rename(target)
    return {"file_path": str(target)}


async def release_after(lease, delay):
    await asyncio.sleep(delay)
    lease.close()


@pytest.mark.asyncio
async def test_late_lease_refuses_transfer_before_node_deadline(tmp_path, monkeypatch):
    """Catch granting the transfer a fresh budget after a late holder fails."""
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "1500")
    target = tmp_path / "chosen.pdf"
    lease = acq.DestinationLease(target, 2).__enter__()
    release = asyncio.create_task(release_after(lease, 1.05))
    issued = []

    async def transfer(*args, **kwargs):
        issued.append(True)
        return await publish(*args, **kwargs)

    try:
        with pytest.raises(acq.AcquisitionError) as error:
            await asyncio.wait_for(
                acq.acquire(
                    BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer
                ),
                1.5,
            )
        assert error.value.to_dict() == {
            "reason": "walk_budget_exhausted",
            "request_issued": False,
        }
        assert issued == []
        assert not target.exists()
    finally:
        await release
        lease.close()


@pytest.mark.asyncio
async def test_early_lease_transfer_gets_remainder_not_fresh_budget(
    tmp_path, monkeypatch
):
    """Catch transfer surviving its remaining slice and reaching Node's kill timer."""
    from lib.sources.net import WalkDeadline

    now = [0.0]
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "40000")
    monkeypatch.setattr(
        acq, "WalkDeadline", lambda budget: WalkDeadline(budget, clock=lambda: now[0])
    )
    target = tmp_path / "chosen.pdf"
    lease = acq.DestinationLease(target, 2).__enter__()
    sleep = acq.asyncio.sleep

    async def release_on_wait(delay):
        now[0] = 20.0  # deterministic lease spend; >1s remains for admission
        lease.close()
        await sleep(0)

    monkeypatch.setattr(acq.asyncio, "sleep", release_on_wait)
    cancelled = []
    wait_for = acq.asyncio.wait_for
    allocations = []

    async def spend_allocation(task, timeout):
        allocations.append(timeout)
        # The allocation is observed at the real cancellation boundary. Drive
        # expiry without waiting for wall time or altering its assertion.
        return await wait_for(task, 0)

    monkeypatch.setattr(acq.asyncio, "wait_for", spend_allocation)

    async def transfer(*args, **kwargs):
        try:
            await asyncio.Event().wait()
            return await publish(*args, **kwargs)
        finally:
            cancelled.append(True)

    try:
        with pytest.raises(acq.AcquisitionError) as error:
            # Use the captured real timer: the production wait_for is patched.
            # A missing production deadline must fail instead of waiting forever.
            await wait_for(
                acq.acquire(
                    BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer
                ),
                5.0,
            )
        assert allocations == [pytest.approx(10.724125)]
        assert error.value.reason == "walk_budget_exhausted"
        assert error.value.issued is True
        assert cancelled == [True]
        assert not target.exists()
        async with acq.destination_lock(target, 0):
            pass
    finally:
        lease.close()


@pytest.mark.asyncio
async def test_deadline_expiring_after_publication_finishes_receipt_and_reuses(
    tmp_path, monkeypatch
):
    """No deadline refusal may strand a linked artifact or replace success with failure."""
    import hashlib
    from lib import python_bridge as bridge
    from lib.sources.net import WalkDeadline

    now = [0.0]
    monkeypatch.setattr(
        acq, "WalkDeadline", lambda budget: WalkDeadline(budget, clock=lambda: now[0])
    )
    link = bridge.os.link
    writes = []
    replace = acq.os.replace
    payload = b"%PDF-1.7\npost-publication finalization"

    def commit(source, target):
        link(source, target)
        now[0] += 10000.0  # Expire the shared clock at the successful link.

    def receipt_replace(source, target):
        replace(source, target)
        writes.append(Path(target))
        if (
            Path(target).name == "chosen.pdf.acq.json"
            and (tmp_path / "chosen.pdf").exists()
        ):
            now[0] += 10000.0  # Also exercise the post-replace durable-write path.

    async def fetch(*args):
        stage = tmp_path / ".source-fixture.pdf"
        stage.write_bytes(payload)
        return bridge._DownloadedPath(str(stage), "signature")

    monkeypatch.setattr(bridge.os, "link", commit)
    monkeypatch.setattr(acq.os, "replace", receipt_replace)
    monkeypatch.setattr(bridge, "_fetch_from_source", fetch)

    async def transfer(*args, **kwargs):
        result = await bridge.download_book(*args, **kwargs)
        assert (
            bridge._file_md5(Path(result["file_path"]))
            == hashlib.md5(payload).hexdigest()
        )
        return result

    first = await acq.acquire(
        {**BOOK, "extension": "epub"},
        str(tmp_path),
        filename="chosen.epub",
        transfer=transfer,
    )
    target = tmp_path / "chosen.pdf"
    canonical = json.loads((tmp_path / "chosen.pdf.acq.json").read_text())
    alias = json.loads((tmp_path / "chosen.epub.acq.json").read_text())
    assert first["path"] == str(target)
    assert canonical == alias
    assert canonical["state"] == "complete"
    assert canonical["sha256"] == hashlib.sha256(payload).hexdigest()
    assert target.read_bytes() == payload
    assert (tmp_path / "chosen.pdf.acq.json") in writes

    async def forbidden(*args, **kwargs):
        pytest.fail("second call replayed a completed acquisition")

    hit = await acq.acquire(
        {**BOOK, "extension": "epub"},
        str(tmp_path),
        filename="chosen.epub",
        transfer=forbidden,
    )
    assert hit["transfer_skipped"] is True
    assert hit["path"] == first["path"]


@pytest.mark.asyncio
async def test_postpublication_await_is_not_cancelled_by_transfer_deadline(
    tmp_path, monkeypatch
):
    """The pre-commit asyncio watchdog must not cancel committed finalization."""
    from lib import python_bridge as bridge

    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "2000")

    async def transfer(book, directory, *, filename, publication_hook):
        stage, target = tmp_path / ".stage", tmp_path / filename
        stage.write_bytes(b"synthetic bytes")
        publication_hook(stage, target)
        bridge._publish_or_reuse(stage, target)
        await asyncio.sleep(1.8)  # beyond the cooperative transfer slice
        return {"file_path": str(target)}

    first = await acq.acquire(
        BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer
    )
    assert first["transfer_skipped"] is False
    assert json.loads(Path(first["record"]).read_text())["state"] == "complete"


@pytest.mark.asyncio
async def test_early_release_can_finish_and_reuse_receipt(tmp_path, monkeypatch):
    """A remaining budget still admits useful work and preserves idempotence."""
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "3000")
    target = tmp_path / "chosen.pdf"
    lease = acq.DestinationLease(target, 3).__enter__()
    release = asyncio.create_task(release_after(lease, 0.1))
    try:
        first = await acq.acquire(
            BOOK, str(tmp_path), filename="chosen.pdf", transfer=publish
        )
    finally:
        await release
        lease.close()

    async def forbidden(*args, **kwargs):
        pytest.fail("verified receipt replayed a provider transfer")

    hit = await acq.acquire(
        BOOK, str(tmp_path), filename="chosen.pdf", transfer=forbidden
    )
    assert first["transfer_skipped"] is False
    assert hit["transfer_skipped"] is True
    assert hit["path"] == first["path"]


@pytest.mark.asyncio
async def test_provider_timeout_is_not_relabelled_as_local_budget(tmp_path):
    """The budget wrapper must preserve existing provider/auth typed failures."""
    original = asyncio.TimeoutError("provider-owned timeout")

    async def transfer(*args, **kwargs):
        raise original

    with pytest.raises(asyncio.TimeoutError) as error:
        await acq.acquire(BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer)
    assert error.value is original


@pytest.mark.asyncio
async def test_spent_checkpoint_cannot_start_publication(tmp_path, monkeypatch):
    """A synchronous pause cannot evade the timer and publish after its deadline."""
    from lib import python_bridge as bridge

    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "2000")
    target, stage = tmp_path / "chosen.pdf", tmp_path / ".source-stage"

    async def transfer(book, directory, *, filename, publication_hook):
        stage.write_bytes(b"synthetic bytes")
        publication_hook(stage, target)
        time.sleep(1.8)
        bridge._publish_or_reuse(stage, target)
        return {"file_path": str(target)}

    try:
        with pytest.raises(acq.AcquisitionError) as error:
            await acq.acquire(
                BOOK, str(tmp_path), filename="chosen.pdf", transfer=transfer
            )
        assert error.value.reason == "walk_budget_exhausted"
        assert error.value.issued is True
        assert not target.exists()
        assert (
            json.loads((tmp_path / "chosen.pdf.acq.json").read_text())["state"]
            == "intent"
        )
    finally:
        stage.unlink(missing_ok=True)
