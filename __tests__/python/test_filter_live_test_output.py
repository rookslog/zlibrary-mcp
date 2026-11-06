"""Keep test output out of uploaded files using synthetic values only."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def workflow():
    return json.loads(
        subprocess.check_output(
            [
                "node",
                "--input-type=module",
                "-e",
                "import fs from 'node:fs';import {parse} from 'yaml';process.stdout.write(JSON.stringify(parse(fs.readFileSync('.github/workflows/upstream-check.yml','utf8'))));",
            ],
            cwd=ROOT,
            text=True,
        )
    )


@pytest.mark.parametrize("password", ["CANARY-OUTPUT-123", 'CANARY<&"-OUTPUT-456'])
def test_real_live_step_keeps_canaries_out_of_uploaded_files(
    workflow, tmp_path, password
):
    email = "canary-output@example.test"
    frame = "CANARY-FRAME-CONTEXT"
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = integration\n")
    (tmp_path / "test_canary.py").write_text(
        "import os, pytest\n"
        "@pytest.fixture\ndef credentials():\n"
        "    return {'email': os.environ['ZLIBRARY_EMAIL'], 'password': os.environ['ZLIBRARY_PASSWORD']}\n"
        f"@pytest.fixture\ndef context():\n    return {frame!r}\n"
        "@pytest.mark.integration\ndef test_canary(credentials, context):\n"
        "    raise AssertionError(credentials['email'] + ' ' + credentials['password'])\n"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ["classify_live_tests.py", "filter_live_test_output.py"]:
        source = ROOT / "scripts" / name
        if source.exists():
            shutil.copyfile(source, scripts / name)
    policy = tmp_path / "zlibrary/src/zlibrary/eapi.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("DEFAULT_EAPI_DOMAINS = ['z-library.ec']\n")
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    (bin_path / "python3").symlink_to(sys.executable)
    (bin_path / "tee").symlink_to(shutil.which("tee"))
    uv = bin_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "assert sys.argv[1] == 'run'\n"
        "if sys.argv[2] == 'pytest':\n"
        "    os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[3:]])\n"
        "assert sys.argv[2] == 'python'\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[3:]])\n"
    )
    uv.chmod(0o755)
    env = {
        "PATH": str(bin_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "ZLIB_PROBE_DOMAIN": "z-library.ec",
        "ZLIB_BLOCKED": "false",
        "ZLIBRARY_EMAIL": email,
        "ZLIBRARY_PASSWORD": password,
    }
    steps = workflow["jobs"]["live_tests"]["steps"]
    live = next(s for s in steps if s.get("id") == "live")
    run = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", live["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, "The real classifier must preserve a failed test result"
    assert "status=failed" in (tmp_path / "outputs").read_text()
    prepare = next((s for s in steps if s.get("id") == "filter_output"), None)
    if prepare is not None:
        run = subprocess.run(
            ["/bin/bash", "-e", "-o", "pipefail", "-c", prepare["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0
        assert all(
            value not in run.stdout + run.stderr for value in [email, password, frame]
        )
    for name in ["live.log", "live.xml"]:
        data = (tmp_path / name).read_text()
        assert all(value not in data for value in [email, password, frame])
    root = ET.parse(tmp_path / "live.xml")
    assert len(list(root.iter("testcase"))) == 1
    assert len(list(root.iter("failure"))) == 1


def test_upload_requires_successful_output_filtering(workflow):
    steps = workflow["jobs"]["live_tests"]["steps"]
    prepare = next((s for s in steps if s.get("id") == "filter_output"), None)
    assert prepare is not None
    assert prepare["if"] == "always()"
    assert prepare["env"] == {
        "ZLIBRARY_EMAIL": "${{ secrets.ZLIBRARY_CI_EMAIL }}",
        "ZLIBRARY_PASSWORD": "${{ secrets.ZLIBRARY_CI_PASSWORD }}",
    }
    assert prepare["run"].strip() == "python3 scripts/filter_live_test_output.py"
    uploads = [
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert len(uploads) == 1
    assert steps.index(prepare) < steps.index(uploads[0])
    assert uploads[0]["if"] == "always() && steps.filter_output.outcome == 'success'"
    assert uploads[0]["with"]["path"].split() == ["live.log", "live.xml"]


@pytest.mark.parametrize("case", ["missing", "unreadable", "replacement"])
def test_filter_skips_absent_files_and_fails_closed(workflow, tmp_path, case):
    if case == "unreadable":
        (tmp_path / "live.log").mkdir()
    elif case == "replacement":
        (tmp_path / "live.log").write_text("[filtered]")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_EMAIL": "", "ZLIBRARY_PASSWORD": "[filtered]"},
        capture_output=True,
        text=True,
    )
    assert run.returncode == (0 if case == "missing" else 1)
    assert "[filtered]" not in run.stdout + run.stderr
    if case == "missing":
        assert not (tmp_path / "live.log").exists()
        assert not (tmp_path / "live.xml").exists()
