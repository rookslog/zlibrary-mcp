"""Offline catalogue digests survive EAPI normalization; hashes stay opaque."""

import pytest
from zlibrary.eapi import normalize_eapi_book

pytestmark = pytest.mark.unit


def test_catalogue_digests_pass_through_without_reinterpreting_selection_hash():
    raw = {"id": 12, "hash": "opaque-selection", "md5": "A" * 32, "sha256": "b" * 64}
    result = normalize_eapi_book(raw)
    assert result["md5"] == "A" * 32
    assert result["sha256"] == "b" * 64
    assert result["hash"] == result["book_hash"] == "opaque-selection"
    assert "md5" not in normalize_eapi_book({"id": 13, "hash": "opaque-only"})
