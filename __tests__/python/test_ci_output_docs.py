"""Keep the documented workflow-check boundary explicit."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["SECURITY.md", "CONTRIBUTING.md"])
def test_workflow_policy_documents_artifact_scope(name):
    text = (ROOT / name).read_text()
    assert (
        "The check covers direct `actions/upload-artifact` steps in workflow files only. "
        "It does not inspect composite actions, other actions, shell commands, caches, "
        "job summaries or job outputs."
    ) in text
    assert (
        "Reusable-workflow callers using secrets require a named job exception." in text
    )
