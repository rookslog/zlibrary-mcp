"""Validate receipts actually persisted by offline acquisition against v1."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from lib.acquisition import acquire

SCHEMA = Path(__file__).resolve().parents[2] / "docs/schemas/acq-record-v1.schema.json"
PAYLOAD = b"%PDF-1.7\noffline receipt contract"
REQUEST = {
    "ref": "r-offline",
    "query": "edition",
    "item_id": "item-1",
    "why_chosen": "translator",
}


@pytest.fixture
def validator():
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture
def initial_record(tmp_path):
    return {
        "record_version": 1,
        "state": "intent",
        "path": str(tmp_path / "edition.pdf"),
        "bytes": None,
        "sha256": None,
        "md5": None,
        "reused": False,
        "transfer_skipped": False,
        "downloaded_at": "2026-10-22T05:00:00Z",
        "source": "zlibrary",
        "provenance": {"route": "zlibrary", "mirror": None, "host": None},
        "title": "Edition",
        "author": None,
        "year": None,
        "language": None,
        "publisher": None,
        "pages": None,
        "isbn": None,
        "series": None,
        "edition": None,
        "translator": None,
        "format": "pdf",
        "identifiers": {"id": "42", "hash": "catalog-hash"},
        "request": {"ref": None, "query": None, "item_id": None, "why_chosen": None},
    }


@pytest.mark.parametrize(
    "timestamp, valid",
    [
        ("2026-10-22T05:00:00Z", True),
        ("2026-10-22T05:00:00.123456Z", True),
        ("2026-10-22T05:00:00+00:00", False),
        ("2026-10-22T05:00:00", False),
        ("yesterday", False),
        ("yesterdayZ", False),
        ("2026-10-22T05:00:00Z\n", False),
    ],
)
def test_downloaded_at_requires_utc_z_timestamp(
    validator, initial_record, timestamp, valid
):
    assert validator.is_valid({**initial_record, "downloaded_at": timestamp}) is valid


@pytest.mark.parametrize(
    "path, valid",
    [
        ("/library/edition.pdf", True),
        (r"C:\library\edition.pdf", True),
        ("C:/library/edition.pdf", True),
        (r"\\server\share\edition.pdf", True),
        ("x.pdf", False),
        ("library/x.pdf", False),
        (r"C:x.pdf", False),
        (r"\library\x.pdf", False),
    ],
)
def test_path_requires_absolute_server_path(validator, initial_record, path, valid):
    assert validator.is_valid({**initial_record, "path": path}) is valid


@pytest.mark.parametrize("state", ["intent", "complete"])
@pytest.mark.parametrize(
    "digest, valid",
    [
        ("a" * 64, True),
        ("0123456789abcdef" * 4, True),
        ("A" * 64, False),
        ("g" * 64, False),
        ("a" * 63, False),
        ("a" * 65, False),
        ("a" * 64 + "\n", False),
    ],
)
def test_sha256_requires_exact_lowercase_hex(
    validator, initial_record, state, digest, valid
):
    record = {**initial_record, "state": state, "bytes": 1, "sha256": digest}
    assert validator.is_valid(record) is valid


@pytest.mark.parametrize(
    "size, digest, valid",
    [
        (None, None, True),
        (0, "a" * 64, True),
        (1, None, False),
        (None, "a" * 64, False),
    ],
)
def test_intent_bytes_and_sha256_are_paired(
    validator, initial_record, size, digest, valid
):
    assert (
        validator.is_valid({**initial_record, "bytes": size, "sha256": digest}) is valid
    )


@pytest.mark.parametrize("state", ["intent", "complete"])
@pytest.mark.parametrize(
    "format_value, valid",
    [("pdf", True), (None, True), (5, False), ({"unexpected": True}, False)],
)
def test_post_checkpoint_format_is_string_or_null(
    validator, initial_record, state, format_value, valid
):
    record = {
        **initial_record,
        "state": state,
        "bytes": 1,
        "sha256": "a" * 64,
        "format": format_value,
    }
    assert validator.is_valid(record) is valid


@pytest.mark.parametrize("format_value", ["pdf", None, 5, {"catalogue": True}, ["pdf"]])
def test_initial_format_accepts_catalogue_json(validator, initial_record, format_value):
    validator.validate({**initial_record, "format": format_value})


def test_complete_requires_format_key(validator, initial_record):
    record = {**initial_record, "state": "complete", "bytes": 1, "sha256": "a" * 64}
    validator.validate(record)
    del record["format"]
    assert not validator.is_valid(record)


@pytest.mark.parametrize(
    "source, identifiers, valid",
    [
        ("zlibrary", {"id": "42", "hash": "catalog-hash"}, True),
        ("zlibrary", {"id": "42", "md5": "a" * 32}, False),
        ("zlibrary", {"hash": "catalog-hash"}, False),
        ("libgen", {"md5": "a" * 32}, True),
        ("libgen", {"id": "42", "hash": "catalog-hash"}, False),
        ("annas", {"md5": "a" * 32}, True),
        ("annas", {"id": "42", "hash": "catalog-hash"}, False),
    ],
)
def test_identifiers_follow_source_rule(
    validator, initial_record, source, identifiers, valid
):
    assert (
        validator.is_valid(
            {**initial_record, "source": source, "identifiers": identifiers}
        )
        is valid
    )


@pytest.mark.parametrize("key", ["record_version", "title"])
def test_required_keys_cannot_be_omitted(validator, initial_record, key):
    validator.validate(initial_record)
    del initial_record[key]
    assert not validator.is_valid(initial_record)


@pytest.mark.parametrize("key", ["request", "provenance", "identifiers"])
def test_unknown_nested_keys_are_accepted(validator, initial_record, key):
    initial_record[key]["future"] = {"ignored": True}
    validator.validate(initial_record)


@pytest.mark.parametrize("source", ["zlibrary", "libgen", "annas_archive"])
@pytest.mark.asyncio
async def test_real_intent_checkpoint_complete_and_reuse_validate(
    tmp_path, monkeypatch, validator, source
):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(tmp_path))
    book = {
        "source": source,
        "id": "42",
        "hash": "catalog-hash",
        "md5": "A" * 32,
        "title": "Edition",
        "extension": "epub",
        "year": 2026,
        "pages": 123,
        "isbn": ["9780000000000"],
        "author": "Author",
        "translator": "Translator",
    }
    initial_path = tmp_path / "chosen.epub.acq.json"
    canonical_path = tmp_path / "chosen.pdf.acq.json"
    observed = []

    def read_valid(path):
        record = json.loads(path.read_text())
        validator.validate(record)
        assert record["downloaded_at"].endswith("Z")
        timestamp = datetime.fromisoformat(
            record["downloaded_at"].replace("Z", "+00:00")
        )
        assert timestamp.tzinfo == timezone.utc
        # Keep the contract's field inventory synchronized with the real writer.
        assert set(record) <= set(validator.schema["properties"])
        observed.append(record)
        return record

    async def transfer(book, directory, *, filename, publication_hook):
        initial = read_valid(initial_path)
        assert initial["state"] == "intent"
        assert initial["bytes"] is None and initial["sha256"] is None
        assert initial["request"] == REQUEST
        stage = Path(directory) / ".offline-stage"
        stage.write_bytes(PAYLOAD)
        target = Path(directory) / "chosen.pdf"
        publication_hook(stage, target)
        assert not target.exists()
        alias, canonical = read_valid(initial_path), read_valid(canonical_path)
        assert alias == canonical
        assert canonical["state"] == "intent"
        assert canonical["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
        assert canonical["bytes"] == len(PAYLOAD)
        assert canonical["format"] == "pdf"
        stage.replace(target)
        return {
            "file_path": str(target),
            "provenance": {
                "route": "offline",
                "mirror": None,
                "host": "fixture.invalid",
            },
        }

    result = await acquire(
        book, str(tmp_path), filename="chosen.epub", request=REQUEST, transfer=transfer
    )
    complete = read_valid(Path(result["record"]))
    assert read_valid(initial_path) == complete
    assert complete["state"] == "complete"
    assert complete["year"] == 2026 and complete["isbn"] == ["9780000000000"]
    assert complete["publisher"] is None
    assert complete["source"] == ("annas" if source == "annas_archive" else source)
    assert complete["identifiers"] == (
        {"id": "42", "hash": "catalog-hash"}
        if source == "zlibrary"
        else {"md5": "a" * 32}
    )
    raw = Path(result["record"]).read_bytes()
    forbidden = AsyncMock(
        side_effect=AssertionError("receipt reuse must remain offline")
    )
    reused = await acquire(
        book, str(tmp_path), filename="chosen.epub", transfer=forbidden
    )
    forbidden.assert_not_awaited()
    assert reused["transfer_skipped"] is True
    assert complete["transfer_skipped"] is False
    assert Path(reused["record"]).read_bytes() == raw
    assert [r["state"] for r in observed] == [
        "intent",
        "intent",
        "intent",
        "complete",
        "complete",
    ]
    validator.validate({**complete, "future_additive_key": {"ignored": True}})
    assert not validator.is_valid({**complete, "record_version": 2})
    assert not validator.is_valid({**complete, "sha256": None, "bytes": None})


@pytest.mark.asyncio
async def test_published_hashed_intent_recovers_to_valid_complete(
    tmp_path, monkeypatch, validator
):
    monkeypatch.setenv("ZLIBRARY_OUTPUT_ROOT", str(tmp_path))
    book = {
        "source": "zlibrary",
        "id": "42",
        "hash": "catalog-hash",
        "title": "Recovery",
        "extension": "pdf",
    }

    async def interrupted(book, directory, *, filename, publication_hook):
        stage = Path(directory) / ".offline-stage"
        target = Path(directory) / "recovery.pdf"
        stage.write_bytes(PAYLOAD)
        publication_hook(stage, target)
        stage.replace(target)
        raise RuntimeError("interrupted after publication")

    with pytest.raises(RuntimeError, match="interrupted after publication"):
        await acquire(
            book, str(tmp_path), filename="recovery.pdf", transfer=interrupted
        )
    sidecar = tmp_path / "recovery.pdf.acq.json"
    intent = json.loads(sidecar.read_text())
    validator.validate(intent)
    assert intent["state"] == "intent" and intent["sha256"] is not None
    forbidden = AsyncMock(side_effect=AssertionError("recovery must remain offline"))
    recovered = await acquire(
        book, str(tmp_path), filename="recovery.pdf", transfer=forbidden
    )
    forbidden.assert_not_awaited()
    complete = json.loads(sidecar.read_text())
    validator.validate(complete)
    assert complete["state"] == "complete"
    assert complete["transfer_skipped"] is True and complete["reused"] is False
    assert recovered["transfer_skipped"] is True
    assert complete["downloaded_at"] == intent["downloaded_at"]
