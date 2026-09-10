"""Validate all ground truth JSON files conform to v3 schema.

This test ensures schema consistency across all ground truth files.
Any new ground truth file added must conform to schema_v3.json.
"""

import json
import pytest
from pathlib import Path
from jsonschema import validate, ValidationError

pytestmark = pytest.mark.unit

GROUND_TRUTH_DIR = Path("test_files/ground_truth")
SCHEMA_PATH = GROUND_TRUTH_DIR / "schema_v3.json"

# Files that are NOT ground truth test cases (utility/schema files)
EXCLUDED_FILES = {
    "schema_v3.json",
    "body_text_baseline.json",
    "correction_matrix.json",
}


def get_ground_truth_files():
    """List all ground truth JSON files that should conform to v3 schema."""
    return sorted(
        [f for f in GROUND_TRUTH_DIR.glob("*.json") if f.name not in EXCLUDED_FILES]
    )


@pytest.fixture(scope="module")
def v3_schema():
    """Load the v3 schema once for all tests in this module."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


class TestGroundTruthSchemaConformance:
    """Every ground truth JSON file must validate against schema_v3.json."""

    @pytest.mark.parametrize(
        "gt_file",
        get_ground_truth_files(),
        ids=lambda f: f.stem,
    )
    def test_conforms_to_v3_schema(self, v3_schema, gt_file):
        """Validate a single ground truth file against v3 schema."""
        with open(gt_file, encoding="utf-8") as f:
            data = json.load(f)
        try:
            validate(instance=data, schema=v3_schema)
        except ValidationError as e:
            pytest.fail(
                f"{gt_file.name} failed v3 schema validation:\n"
                f"  Path: {' -> '.join(str(p) for p in e.absolute_path)}\n"
                f"  Error: {e.message}"
            )

    @pytest.mark.parametrize(
        "gt_file",
        get_ground_truth_files(),
        ids=lambda f: f.stem,
    )
    def test_referenced_pdf_exists(self, gt_file):
        """Every ground truth file must reference a PDF that exists on disk."""
        with open(gt_file, encoding="utf-8") as f:
            data = json.load(f)
        pdf_path = Path(data["pdf_file"])
        assert pdf_path.exists(), (
            f"{gt_file.name} references PDF '{data['pdf_file']}' which does not exist"
        )

    def test_no_old_schema_files(self):
        """Old schema versions must not exist (migration complete)."""
        assert not (GROUND_TRUTH_DIR / "schema.json").exists(), (
            "schema.json (v1) should be deleted"
        )
        assert not (GROUND_TRUTH_DIR / "schema_v2.json").exists(), (
            "schema_v2.json should be deleted"
        )

    def test_v3_schema_exists(self):
        """The canonical v3 schema must exist."""
        assert SCHEMA_PATH.exists(), "schema_v3.json must exist"
