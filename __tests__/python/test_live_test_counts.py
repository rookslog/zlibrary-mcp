"""Counts evidence and uploads use synthetic local files only."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/live_test_counts.py"
VALID = b'<testsuite><testcase classname="integration.TestRealAuthentication" name="test_authentication_succeeds"/></testsuite>'


def summarize(tmp_path, data):
    report = tmp_path / "live.xml"
    output = tmp_path / "live-counts.json"
    report.write_bytes(data)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--junitxml",
            str(report),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text())


def test_authentication_anchor_requires_its_class(tmp_path):
    result = summarize(
        tmp_path, VALID.replace(b"TestRealAuthentication", b"TestOtherOperation")
    )
    assert result["problem"] is None
    assert result["evidence"]["counts"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert result["evidence"]["anchor_present"] is False
    assert result["evidence"]["anchor_passed"] is False


@pytest.mark.parametrize("extra,problem", [(0, None), (1, "invalid")])
def test_input_cap_applies_to_valid_xml_with_trailing_whitespace(
    tmp_path, extra, problem
):
    limit = 5 * 1024 * 1024
    result = summarize(tmp_path, VALID + b" " * (limit + extra - len(VALID)))
    assert result["problem"] == problem
    if problem:
        assert result["evidence"] is None
    else:
        assert result["evidence"]["anchor_passed"] is True


def test_failed_counts_write_suppresses_only_the_counts_upload(tmp_path):
    report = tmp_path / "live.xml"
    report.write_bytes(VALID)
    output = tmp_path / "live-counts.json"
    output.mkdir()
    planted = output / "canary.txt"
    planted.write_text("SYNTHETIC-PREEXISTING-OUTPUT")
    generated = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--junitxml",
            str(report),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 1
    assert planted.read_text() == "SYNTHETIC-PREEXISTING-OUTPUT"
    run = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import fs from 'node:fs'; import {parse} from 'yaml';
const steps = parse(fs.readFileSync('.github/workflows/upstream-check.yml','utf8')).jobs.live_tests.steps;
const uploads = steps.filter(s => s.uses?.startsWith('actions/upload-artifact@'));
const result = Object.fromEntries(uploads.map(s => [s.with.path, new Function('steps','always','return ' + s.if)(
  {filter_output:{outcome:'success'},live_counts:{outcome:'failure',conclusion:'success'}}, () => true)]));
process.stdout.write(JSON.stringify(result));
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    decisions = json.loads(run.stdout)
    assert decisions["live-counts.json"] is False
    assert decisions["live.log\nlive.xml\n"] is True
