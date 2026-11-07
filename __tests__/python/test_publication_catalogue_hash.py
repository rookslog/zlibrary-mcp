"""Catalogue claims cannot decide whether two local artifacts are the same."""

import hashlib

import pytest

from lib import python_bridge as bridge


@pytest.mark.parametrize("identical", [True, False])
def test_collision_compares_staged_bytes_even_with_catalogue_hash(tmp_path, identical):
    destination = tmp_path / "chosen.pdf"
    stage = tmp_path / ".staged.pdf"
    destination.write_bytes(b"%PDF-existing")
    stage.write_bytes(b"%PDF-existing" if identical else b"%PDF-differnt")
    assert stage.stat().st_size == destination.stat().st_size
    catalogue = (
        "0" * 32 if identical else hashlib.md5(destination.read_bytes()).hexdigest()
    )
    if identical:
        assert bridge._publish_or_reuse(stage, destination, catalogue) is True
    else:
        with pytest.raises(FileExistsError):
            bridge._publish_or_reuse(stage, destination, catalogue)
    assert destination.read_bytes() == b"%PDF-existing"
    assert not stage.exists()
