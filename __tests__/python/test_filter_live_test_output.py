"""Keep test output out of uploaded files using synthetic values only."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

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


@pytest.mark.parametrize(
    "password",
    ["CANARY-OUTPUT-123", 'CANARY<&"-OUTPUT-456', "CANARY-é雪<&\"'-OUTPUT-789"],
)
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
    for name in [
        "classify_live_tests.py",
        "filter_live_test_output.py",
        "live_test_counts.py",
    ]:
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
    decoded = "".join(root.getroot().itertext()) + "".join(
        value for element in root.iter() for value in element.attrib.values()
    )
    assert all(value not in decoded for value in [email, password, frame])
    counts_step = next((s for s in steps if s.get("id") == "live_counts"), None)
    assert counts_step is not None
    run = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", counts_step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    counts_text = (tmp_path / "live-counts.json").read_text()
    assert all(value not in counts_text for value in [email, password, frame])
    counts = json.loads(counts_text)
    assert set(counts) == {"schema_version", "problem", "evidence"}
    assert set(counts["evidence"]) == {
        "counts",
        "problems",
        "outside_case_problems",
        "anchor_present",
        "anchor_passed",
    }
    assert counts["evidence"]["counts"] == {
        "total": 1,
        "passed": 0,
        "failed": 1,
        "skipped": 0,
    }


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
    diagnostic = next(s for s in uploads if s["with"]["name"] == "live-integration-log")
    health = next(
        s
        for s in uploads
        if s["with"]["name"].startswith("upstream-source-health-live-")
    )
    counts = next(s for s in steps if s.get("id") == "live_counts")
    assert counts["if"] == "always()"
    assert (
        steps.index(prepare)
        < steps.index(counts)
        < min(steps.index(s) for s in uploads)
    )
    assert diagnostic["if"] == "always() && steps.filter_output.outcome == 'success'"
    assert diagnostic["with"]["path"].split() == ["live.log", "live.xml"]
    assert health["with"]["path"] == "live-counts.json"


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


@pytest.mark.parametrize(
    "value", ["CANARY<&\"'-OUTPUT-456", "CANARY-é雪<&\"'-OUTPUT-789"]
)
def test_parsed_attributes_text_cdata_and_tails_are_filtered(tmp_path, value):
    (tmp_path / "live.xml").write_text(
        f"<testsuites label={quoteattr(value)}><testcase>{escape(value)}"
        f"<system-out><![CDATA[{value}]]></system-out>{escape(value)}"
        "</testcase></testsuites>",
        encoding="utf-8",
    )
    (tmp_path / "live.log").write_text(value, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_EMAIL": "", "ZLIBRARY_PASSWORD": value},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert value not in run.stdout + run.stderr
    assert value not in (tmp_path / "live.log").read_text()
    tree = ET.parse(tmp_path / "live.xml")
    assert tree.getroot().attrib["label"] == "[filtered]"
    assert tree.find("testcase").text == "[filtered]"
    assert tree.find("testcase/system-out").text == "[filtered]"
    assert tree.find("testcase/system-out").tail == "[filtered]"
    assert value not in "".join(tree.getroot().itertext())


@pytest.mark.parametrize(
    "data",
    [
        b"<testsuites>broken",
        b"\xff",
        b'<?xml version="1.0" encoding="unknown-codec"?><testsuites/>',
    ],
)
def test_unparseable_xml_is_removed_and_upload_preparation_fails(tmp_path, data):
    (tmp_path / "live.xml").write_bytes(data)
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_PASSWORD": "CANARY-INVALID-XML"},
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert not (tmp_path / "live.xml").exists()
    assert run.stdout == run.stderr == ""


@pytest.mark.parametrize("name", ["live.log", "live.xml"])
def test_verification_rereads_written_files(monkeypatch, tmp_path, name):
    value = "CANARY-AFTER-WRITE-é雪<&\"'"
    spec = importlib.util.spec_from_file_location(
        "filter_output_under_test", ROOT / "scripts/filter_live_test_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", value)
    (tmp_path / "live.log").write_text(value, encoding="utf-8")
    (tmp_path / "live.xml").write_text("<testsuites/>")
    write = Path.write_bytes

    def changed_after_write(path, data):
        result = write(path, data)
        if path.name == name:
            if name == "live.log":
                write(path, data + value.encode("utf-8"))
            else:
                root = ET.fromstring(data)
                root.set("test-output", value)
                write(path, ET.tostring(root, encoding="utf-8"))
        return result

    monkeypatch.setattr(Path, "write_bytes", changed_after_write)
    assert module.filter_output() == 1


@pytest.mark.parametrize("name", ["live.log", "live.xml"])
def test_verification_rechecks_initially_absent_files(monkeypatch, tmp_path, name):
    value = "CANARY-APPEARED-AFTER-FIRST-READ"
    spec = importlib.util.spec_from_file_location(
        "filter_output_absent_under_test", ROOT / "scripts/filter_live_test_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", value)
    if name == "live.log":
        read = Path.read_bytes
        first = True

        def appearing_log(path):
            nonlocal first
            if path.name == "live.log" and first:
                first = False
                path.write_text(value)
                raise FileNotFoundError
            return read(path)

        monkeypatch.setattr(Path, "read_bytes", appearing_log)
    else:
        read = module.read_xml
        first = True

        def appearing_xml(path):
            nonlocal first
            result = read(path)
            if first:
                first = False
                path.write_text(f'<testsuites label="{value}"/>')
            return result

        monkeypatch.setattr(module, "read_xml", appearing_xml)
    assert module.filter_output() == 1
