"""Occupied-destination admission must precede intent, auth and transfer."""

import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lib import acquisition as acq
from lib import python_bridge as bridge

PAYLOAD = b"%PDF-1.7\nsynthetic admission artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["libgen", "zlibrary"])
@pytest.mark.parametrize("corrected", [False, True])
@pytest.mark.parametrize("state", ["modified", "foreign", "intact"])
async def test_occupied_destination_admission(
    tmp_path, monkeypatch, source, corrected, state
):
    book = {
        "source": source,
        "id": "1",
        "hash": "hash1",
        "md5": hashlib.md5(PAYLOAD).hexdigest(),
        "title": "Chosen",
        "extension": "epub" if corrected else "pdf",
    }
    filename = "chosen.epub" if corrected else "chosen.pdf"
    predicted, target = tmp_path / filename, tmp_path / "chosen.pdf"
    transfers, demands, writes = [], [], []

    async def transfer(book, directory, *, filename, publication_hook):
        transfers.append(True)
        if source == "zlibrary":
            demands.append(True)
        stage = tmp_path / ".stage"
        stage.write_bytes(PAYLOAD)
        try:
            publication_hook(stage, target)
            reused = bridge._publish_or_reuse(stage, target)
            return {"file_path": str(target), "publication_reused": reused}
        finally:
            stage.unlink(missing_ok=True)

    if state == "foreign":
        predicted.write_bytes(b"unrelated existing bytes")
    else:
        await acq.acquire(book, str(tmp_path), filename=filename, transfer=transfer)
        if state == "modified":
            target.write_bytes(b"modified existing bytes")
    receipts = {p: p.read_bytes() for p in tmp_path.glob("*.acq.json")}
    files = {p: p.read_bytes() for p in {predicted, target} if p.exists()}
    original_atomic = acq._atomic_json

    def watch_write(*args):
        writes.append(True)
        return original_atomic(*args)

    monkeypatch.setattr(acq, "_atomic_json", watch_write)
    transfers.clear()
    demands.clear()
    if state == "intact":
        reply = await acq.acquire(
            book, str(tmp_path), filename=filename, transfer=transfer
        )
        assert reply["transfer_skipped"] is True
        assert json.loads(Path(reply["record"]).read_text())["state"] == "complete"
    else:
        with pytest.raises(acq.AcquisitionError) as error:
            await acq.acquire(book, str(tmp_path), filename=filename, transfer=transfer)
        assert error.value.reason == "output_refused"
        assert error.value.issued is False
        assert len(str(error.value)) <= 199
        assert "no matching receipt" in str(error.value)
        assert "different bytes" not in str(error.value)
    assert transfers == demands == writes == []
    assert {p: p.read_bytes() for p in tmp_path.glob("*.acq.json")} == receipts
    assert all(p.read_bytes() == contents for p, contents in files.items())


def publication_transfer(directory, calls):
    async def transfer(book, output_dir, *, filename, publication_hook):
        calls.append(True)
        stage, target = directory / ".stage-receipt", directory / "chosen.pdf"
        stage.write_bytes(PAYLOAD)
        try:
            publication_hook(stage, target)
            reused = bridge._publish_or_reuse(stage, target)
            return {"file_path": str(target), "publication_reused": reused}
        finally:
            stage.unlink(missing_ok=True)

    return transfer


def admission_book(source, corrected):
    return {
        "source": source,
        "id": "1",
        "hash": "hash1",
        "md5": hashlib.md5(PAYLOAD).hexdigest(),
        "title": "Chosen",
        "extension": "epub" if corrected else "pdf",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["libgen", "zlibrary"])
async def test_format_corrected_receipt_hit_ignores_occupied_predicted_name(
    tmp_path, source
):
    calls = []
    transfer = publication_transfer(tmp_path, calls)
    book = admission_book(source, corrected=True)
    first = await acq.acquire(
        book, str(tmp_path), filename="chosen.epub", transfer=transfer
    )
    assert Path(first["path"]) == tmp_path / "chosen.pdf"
    predicted = tmp_path / "chosen.epub"
    predicted.write_bytes(b"unrelated file at predicted name")
    receipts = {p: p.read_bytes() for p in tmp_path.glob("*.acq.json")}
    calls.clear()

    repeated = await acq.acquire(
        book, str(tmp_path), filename="chosen.epub", transfer=transfer
    )

    assert repeated["transfer_skipped"] is True
    assert repeated["path"] == first["path"]
    assert calls == []
    assert predicted.read_bytes() == b"unrelated file at predicted name"
    assert Path(repeated["path"]).read_bytes() == PAYLOAD
    assert {p: p.read_bytes() for p in tmp_path.glob("*.acq.json")} == receipts


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["libgen", "zlibrary"])
@pytest.mark.parametrize("corrected", [False, True])
async def test_deleted_receipt_artifact_redownloads_once(tmp_path, source, corrected):
    calls = []
    transfer = publication_transfer(tmp_path, calls)
    book = admission_book(source, corrected)
    filename = "chosen.epub" if corrected else "chosen.pdf"
    first = await acq.acquire(book, str(tmp_path), filename=filename, transfer=transfer)
    assert len(calls) == 1
    Path(first["path"]).unlink()
    calls.clear()

    repeated = await acq.acquire(
        book, str(tmp_path), filename=filename, transfer=transfer
    )

    assert repeated["transfer_skipped"] is False
    assert calls == [True]
    assert Path(repeated["path"]).read_bytes() == PAYLOAD
    assert repeated["bytes"] == len(PAYLOAD)
    receipts = list(tmp_path.glob("*.acq.json"))
    assert Path(repeated["record"]) in receipts
    for path in receipts:
        record = json.loads(path.read_text())
        assert record["state"] == "complete"
        assert record["path"] == repeated["path"]
        assert record["bytes"] == len(PAYLOAD)
        assert record["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["libgen", "zlibrary"])
@pytest.mark.parametrize("state", ["identical", "different", "verified"])
async def test_corrected_target_requires_verified_same_identity_receipt(
    tmp_path, monkeypatch, source, state
):
    book = admission_book(source, corrected=True)
    target = tmp_path / "chosen.pdf"
    canonical = tmp_path / "chosen.pdf.acq.json"
    predicted = tmp_path / "chosen.epub"
    if state == "verified":
        await acq.acquire(
            {**book, "extension": "pdf"},
            str(tmp_path),
            filename="chosen.pdf",
            transfer=publication_transfer(tmp_path, []),
        )
    else:
        target.write_bytes(PAYLOAD if state == "identical" else b"foreign bytes")
    original = target.read_bytes()
    original_receipt = canonical.read_bytes() if canonical.exists() else None
    stages, publications = [], []

    def stage_file(directory, name):
        stage = Path(directory) / name
        stage.write_bytes(PAYLOAD)
        stages.append(stage)
        return bridge._DownloadedPath(str(stage), "signature")

    async def fetch_source(book, directory):
        return stage_file(directory, ".source-corrected.pdf")

    async def fetch_eapi(*, output_dir, **kwargs):
        return stage_file(output_dir, "received.pdf")

    source_fetch = AsyncMock(side_effect=fetch_source)
    eapi_fetch = AsyncMock(side_effect=fetch_eapi)
    monkeypatch.setattr(bridge, "_fetch_from_source", source_fetch)
    monkeypatch.setattr(
        bridge,
        "get_eapi_client",
        AsyncMock(return_value=SimpleNamespace(download_file=eapi_fetch)),
    )
    publish = bridge._publish_or_reuse

    def observe_publication(*args):
        publications.append(True)
        return publish(*args)

    monkeypatch.setattr(bridge, "_publish_or_reuse", observe_publication)
    if state == "verified":
        reply = await acq.acquire(
            book, str(tmp_path), filename="chosen.epub", transfer=bridge.download_book
        )
        assert reply["reused"] is True
        assert reply["transfer_skipped"] is False
        assert publications == [True]
        assert canonical.read_bytes() == original_receipt
        assert (tmp_path / "chosen.epub.acq.json").read_bytes() == original_receipt
    else:
        with pytest.raises(acq.AcquisitionError) as error:
            await acq.acquire(
                book,
                str(tmp_path),
                filename="chosen.epub",
                transfer=bridge.download_book,
            )
        assert error.value.to_dict() == {
            "reason": "output_refused",
            "request_issued": True,
            "destination_occupied": True,
        }
        assert len(str(error.value)) <= 199
        assert publications == []
        assert not canonical.exists()
        intent = json.loads((tmp_path / "chosen.epub.acq.json").read_text())
        assert intent["state"] == "intent"
        assert intent["path"] == str(predicted)
        assert intent["sha256"] is None
    assert target.read_bytes() == original
    assert not predicted.exists()
    assert source_fetch.await_count + eapi_fetch.await_count == 1
    assert len(stages) == 1
    assert all(not stage.exists() for stage in stages)
    assert not list(tmp_path.glob(".eapi-*"))


def test_destination_lease_default_matches_long_bridge_timeout(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.delenv("PYTHON_BRIDGE_CALL_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("PYTHON_BRIDGE_LONG_TIMEOUT", raising=False)
    runner_source = (root / "src/lib/python-runner.ts").read_text()
    bridge = re.search(
        r"positiveIntEnv\('PYTHON_BRIDGE_LONG_TIMEOUT',\s*(\d+)\)",
        runner_source,
    )
    assert bridge is not None
    assert acq._long_bridge_budget() * 1000 == int(bridge.group(1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, budget",
    [
        (None, 3900.0),
        ("2700000", 2700.0),
        (" 2700000 ", 2700.0),
        ("2147483647", 2147483.647),
        ("2400000ms", 3900.0),
        ("abc", 3900.0),
        ("", 3900.0),
        ("0", 3900.0),
        ("-5", 3900.0),
        ("1.5", 3900.0),
        ("1e6", 3900.0),
        ("2147483648", 3900.0),
        ("١٢٣", 3900.0),
        # Breaker gate A: JavaScript trim() strips BOM but not NEL or FS, and
        # digit strings past Python's int() limit must still fall back.
        pytest.param("﻿2700000", 2700.0, id="bom"),
        pytest.param("\u00852700000", 3900.0, id="nel"),
        pytest.param("\u001c2700000", 3900.0, id="fs"),
        pytest.param("9" * 5000, 3900.0, id="5000-nines"),
        pytest.param("0" * 5000 + "2700000", 2700.0, id="5000-zeros"),
    ],
)
async def test_long_bridge_timeout_is_parsed_as_node_parses_it(
    tmp_path, monkeypatch, raw, budget
):
    # Node's positiveIntEnv falls back on anything but a plain positive integer of
    # at most 2**31 - 1 ms; a malformed value must not refuse downloads here.
    if raw is None:
        monkeypatch.delenv("PYTHON_BRIDGE_LONG_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", raw)
    budgets = []
    lock = acq.destination_lock

    def watch_lock(destination, seconds):
        budgets.append(seconds)
        return lock(destination, seconds)

    monkeypatch.setattr(acq, "destination_lock", watch_lock)
    calls = []
    transfer = publication_transfer(tmp_path, calls)
    book = admission_book("libgen", corrected=False)
    first = await acq.acquire(
        book, str(tmp_path), filename="chosen.pdf", transfer=transfer
    )
    hit = await acq.acquire(
        book, str(tmp_path), filename="chosen.pdf", transfer=transfer
    )
    assert first["transfer_skipped"] is False
    assert hit["transfer_skipped"] is True
    assert calls == [True]
    assert budgets == [budget, budget]
