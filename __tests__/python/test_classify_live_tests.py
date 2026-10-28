"""Offline status/reporting contracts for the scheduled credentialed suite."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "classify_live_tests.py"
RECORDED = ET.parse(ROOT / "__tests__/python/fixtures/live-junit/pytest-junit.xml")


def recorded_problem(name):
    case = next(c for c in RECORDED.iter("testcase") if c.get("name") == name)
    problem = next(p for p in case if p.tag in ("failure", "error"))
    return problem.tag, problem.get("message")


@pytest.fixture(scope="module")
def classifier():
    spec = importlib.util.spec_from_file_location("classify_live_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_report(tmp_path, problems=(), *, skipped=False):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    case = ET.SubElement(suite, "testcase", name="test_contract")
    for tag, message in problems:
        ET.SubElement(case, tag, message=message).text = "traceback body"
    if skipped:
        ET.SubElement(case, "skipped")
    report = tmp_path / "live.xml"
    ET.ElementTree(root).write(report, encoding="unicode")
    return report


WALL = recorded_problem("test_wall_call[z-library.ec]")
CONNECT_SETUP = recorded_problem("test_connect_setup[ConnectError]")


@pytest.mark.parametrize(
    "problem",
    [
        WALL,
        CONNECT_SETUP,
        recorded_problem("test_wall_setup"),
        recorded_problem("test_connect_message_setup"),
        ("failure", "AssertionError: expected books to be a list"),
        ("failure", "KeyError: 'books'"),
        ("failure", "json.decoder.JSONDecodeError: Expecting value"),
        ("failure", "ValueError: parser regression"),
        recorded_problem("test_connect_call"),
        recorded_problem("test_connect_teardown"),
        ("error", 'failed on setup with "KeyError: books"'),
        ("error", "collection failure: ImportError"),
        ("failure", "AssertionError: expected DiamWallError but parser regressed"),
        ("failure", "AssertionError: httpx.ConnectError appeared in a response"),
        ("failure", "ExceptionGroup: DiamWallError and KeyError"),
    ],
)
def test_live_problems_are_failures(classifier, tmp_path, problem):
    result = classifier.classify(1, write_report(tmp_path, [problem]))
    assert result["status"] == "failed"
    assert result["blocked"] is False


def test_cause_in_traceback_does_not_hide_terminal_parser_error(classifier, tmp_path):
    report = write_report(tmp_path, [("failure", "KeyError: 'books'")])
    tree = ET.parse(report)
    tree.find(
        ".//failure"
    ).text = "zlibrary.eapi.DiamWallError: earlier cause\nKeyError: books"
    tree.write(report)
    assert classifier.classify(1, report)["status"] == "failed"


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5, -9, 127])
def test_non_test_failure_exit_codes_cannot_be_hidden(classifier, tmp_path, exit_code):
    assert (
        classifier.classify(exit_code, write_report(tmp_path, [WALL]))["status"]
        == "failed"
    )


@pytest.mark.parametrize("content", [None, "", "<incomplete", "<testsuites/>"])
def test_missing_or_invalid_evidence_fails_closed(classifier, tmp_path, content):
    report = tmp_path / "invalid.xml"
    if content is not None:
        report.write_text(content)
    assert classifier.classify(1, report)["status"] == "failed"


def test_passing_suite(classifier, tmp_path):
    assert classifier.classify(0, write_report(tmp_path))["status"] == "passed"


def test_all_skipped_suite_claims_no_authenticated_coverage(classifier, tmp_path):
    result = classifier.classify(0, write_report(tmp_path, skipped=True))
    assert result["blocked"] is False
    assert result["status"] == "skipped"
    assert "0 passed; 1 skipped" in result["detail"]
    assert "no authenticated coverage" in result["detail"].lower()


def test_partly_skipped_suite_reports_pass_and_skip_counts(classifier, tmp_path):
    report = write_report(tmp_path, skipped=True)
    tree = ET.parse(report)
    suite = tree.find("testsuite")
    ET.SubElement(suite, "testcase", name="test_pass_one")
    ET.SubElement(suite, "testcase", name="test_pass_two")
    tree.write(report)
    result = classifier.classify(0, report)
    assert result["status"] == "passed"
    assert result["blocked"] is False
    assert "2 passed; 1 skipped" in result["detail"]


@pytest.mark.parametrize("exit_code", [1, 2, 3, 4, 5, -9, 127])
def test_skips_do_not_hide_nonzero_exit_codes(classifier, tmp_path, exit_code):
    assert (
        classifier.classify(exit_code, write_report(tmp_path, skipped=True))["status"]
        == "failed"
    )


def test_cli_emits_status_block_flag_and_summary(tmp_path):
    report = write_report(tmp_path, [CONNECT_SETUP])
    output = tmp_path / "github-output"
    summary = tmp_path / "summary"
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--exit-code", "1", "--junitxml", str(report)],
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "status=failed\n" in output.read_text()
    assert "blocked=false\n" in output.read_text()
    assert "Live suite: failed; 1 failure(s); pytest exit 1." in summary.read_text()


@pytest.mark.parametrize("skipped", [True, False])
def test_cli_warns_only_for_skipped_authenticated_coverage(tmp_path, skipped):
    report = write_report(tmp_path, skipped=skipped)
    output = tmp_path / "github-output"
    summary = tmp_path / "summary"
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--exit-code", "0", "--junitxml", str(report)],
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    status = "skipped" if skipped else "passed"
    assert output.read_text() == f"status={status}\nblocked=false\n"
    assert f"Live suite: {status};" in summary.read_text()
    warnings = [
        line for line in run.stdout.splitlines() if line.startswith("::warning::")
    ]
    if skipped:
        assert len(warnings) == 1
        assert "live suite produced no authenticated coverage" in warnings[0].lower()
    else:
        assert warnings == []


def test_actual_pytest_reports_are_classified_offline(classifier, tmp_path):
    """Exercise pytest's real setup/call messages, not only hand-written XML."""
    source = tmp_path / "test_synthetic.py"
    source.write_text(
        "import pytest\nimport httpx\nfrom zlibrary.eapi import decode_eapi_json\n"
        "@pytest.fixture\ndef refused():\n    raise httpx.ConnectError('offline')\n"
        "def test_setup(refused):\n    pass\n"
        "def test_wall():\n    decode_eapi_json(httpx.Response(517, text='DiamWall'), 'z-library.ec')\n"
        "def test_drift():\n    assert 'books' in {'success': 1}\n"
    )
    report = tmp_path / "actual.xml"
    for selection in ["not drift", ""]:
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                "/dev/null",
                str(source),
                "-k",
                selection,
                f"--junitxml={report}",
                "-q",
            ],
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
        )
        assert run.returncode == 1, run.stdout + run.stderr
        result = classifier.classify(run.returncode, report)
        assert result["status"] == "failed"
        assert result["blocked"] is False


@pytest.fixture(scope="module")
def workflow():
    run = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import fs from 'node:fs'; import YAML from 'yaml'; "
            "console.log(JSON.stringify(YAML.parse(fs.readFileSync(process.argv[1], 'utf8'))));",
            str(ROOT / ".github/workflows/upstream-check.yml"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(run.stdout)


@pytest.mark.parametrize(
    "problems,skipped,exit_code,expected",
    [
        ([WALL], False, 1, "failed"),
        ([WALL, ("failure", "KeyError: books")], False, 1, "failed"),
        ([], True, 0, "skipped"),
        ([], False, 0, "passed"),
    ],
)
def test_workflow_live_step_classifies_and_retains_evidence(
    workflow, tmp_path, problems, skipped, exit_code, expected
):
    report = write_report(tmp_path, problems, skipped=skipped)
    report = report.rename(tmp_path / "recorded.xml")
    output = tmp_path / "github-output"
    fake_uv = tmp_path / "uv"
    # Replace only the provider-facing pytest invocation. Run the production
    # classifier and the exact workflow shell, with the recorded pytest result.
    fake_uv.write_text(
        f"#!{sys.executable}\nimport os, sys, shutil\nfrom pathlib import Path\n"
        "if sys.argv[1:3] == ['run', 'pytest']:\n"
        "    if '--collect-only' in sys.argv:\n        sys.exit(0)\n"
        "    shutil.copyfile(os.environ['SYNTHETIC_REPORT'], 'live.xml')\n"
        f"    print('offline pytest log')\n    sys.exit({exit_code})\n"
        "assert sys.argv[1:3] == ['run', 'python']\n"
        f"os.execv(sys.executable, [sys.executable, str(Path({str(ROOT)!r}) / sys.argv[3]), *sys.argv[4:]])\n"
    )
    fake_uv.chmod(0o755)
    step = next(
        s for s in workflow["jobs"]["live_tests"]["steps"] if s.get("id") == "live"
    )
    contexts = {
        "secrets": {
            "ZLIBRARY_EMAIL": "offline@example.test",
            "ZLIBRARY_PASSWORD": "offline",
        },
        "needs": {
            "reachability": {
                "outputs": {"zlib_blocked": "false", "zlib_domain": "z-library.ec"}
            }
        },
    }
    step_env = {}
    for key, expression in step["env"].items():
        value = contexts
        for part in expression.removeprefix("${{ ").removesuffix(" }}").split("."):
            value = value[part]
        step_env[key] = value
    assert step_env["ZLIBRARY_EAPI_DOMAIN"] == "z-library.ec"
    assert step_env["ZLIB_PROBE_DOMAIN"] == "z-library.ec"
    run = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            **step_env,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "SYNTHETIC_REPORT": str(report),
        },
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"status={expected}\n" in output.read_text()
    assert "blocked=false\n" in output.read_text()
    assert "offline pytest log" in (tmp_path / "live.log").read_text()
    if expected == "skipped":
        assert "no authenticated coverage" in run.stdout.lower()
        assert "0 passed; 1 skipped" in (tmp_path / "summary").read_text()


@pytest.mark.parametrize(
    "probe_blocked,failed", [(True, False), (True, True), (False, True)]
)
def test_workflow_issue_gate_and_body(workflow, probe_blocked, failed):
    # Execute the real report condition and github-script body against a local
    # GitHub API double: no credentials, requests, comments or issue writes.
    needs = {
        "reachability": {
            "result": "success",
            "outputs": {
                "failed": str(failed and probe_blocked).lower(),
                "zlib_blocked": str(probe_blocked).lower(),
                "zlib_domain": "library.example.test",
            },
        },
        "live_tests": {
            "result": "failure" if failed and not probe_blocked else "success",
            "outputs": {
                "status": "blocked" if probe_blocked else "failed",
                "blocked": str(probe_blocked).lower(),
            },
        },
    }
    report = workflow["jobs"]["report"]
    step = report["steps"][0]
    # Resolve the actual env expressions for the contexts used by this job.
    env = {}
    for key, value in step["env"].items():
        if value.startswith("${{ needs."):
            parts = value.removeprefix("${{ ").removesuffix(" }}").split(".")
            resolved = {"needs": needs}
            for part in parts:
                resolved = resolved.get(part, "") if isinstance(resolved, dict) else ""
            env[key] = resolved
    runner = """
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const enabled = new Function('needs', 'github', 'always', 'return ' + data.condition)(data.needs, {event_name:'schedule'}, () => true);
let body = '';
const github = {paginate: async () => [], rest: {issues: {
  listForRepo: async () => ({data: []}),
  create: async value => { body = value.body; },
}}};
const context = {serverUrl: 'https://github.example', repo: {owner:'example', repo:'offline'}, runId: 1};
Object.assign(process.env, data.env);
(async () => {
  if (enabled) await new (Object.getPrototypeOf(async function(){}).constructor)('github', 'context', data.script)(github, context);
  console.log(JSON.stringify({enabled, body}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    run = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps(
            {
                "condition": report["if"],
                "script": step["with"]["script"],
                "needs": needs,
                "env": env,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(run.stdout)
    # The report job also runs on recovery to refresh existing issue dates;
    # a healthy run with no existing issue must still create nothing.
    assert result["enabled"] is True
    assert bool(result["body"]) is failed
    if failed:
        line = (
            "Z-Library reachability probe reported an anti-bot wall on "
            "library.example.test; live suite skipped."
        )
        assert (line in result["body"]) is probe_blocked
        assert "corroborated" not in result["body"]
        assert "runner's IP" not in result["body"]


@pytest.mark.parametrize(
    "probe_case,block_value,expected",
    [
        ("empty-outputs", None, "unknown"),
        ("failed-probe", None, "unknown"),
        ("reported", "", "unknown"),
        ("reported", "maybe", "unknown"),
        ("reported", "False", "unknown"),
        ("reported", "true", "blocked"),
        ("reported", "false", "ran"),
    ],
)
def test_live_job_never_invokes_integration_without_explicit_unblocked(
    workflow, tmp_path, probe_case, block_value, expected
):
    """Run the job's actual shell/conditions; only uv's child calls are fake."""
    outputs = {}
    probe_result = "failure" if probe_case == "failed-probe" else "success"
    if probe_case == "failed-probe":
        # Real failed probe: missing prior issue fails before run_probes, with no
        # network call and no zlib_blocked output to authorize a login.
        probe_output = tmp_path / "probe-output"
        probe = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/check_upstream.py"),
                "--github-output",
                "--previous-issue",
                str(tmp_path / "missing.md"),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "GITHUB_OUTPUT": str(probe_output)},
        )
        assert probe.returncode != 0
        assert "FileNotFoundError" in probe.stderr
        assert not probe_output.exists()
    elif probe_case == "reported":
        outputs = {"zlib_blocked": block_value, "zlib_domain": "offline.example.test"}

    needs = {"reachability": {"result": probe_result, "outputs": outputs}}
    steps = {"live": {"outputs": {}}}
    calls = tmp_path / "uv-calls"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    fake_uv.chmod(0o755)
    live_output = tmp_path / "live-output"

    def enabled(condition):
        result = subprocess.run(
            [
                "node",
                "-e",
                "const fs=require('fs');const d=JSON.parse(fs.readFileSync(0,'utf8'));"
                # GitHub compares expression strings without case sensitivity.
                "const lower=v=>typeof v==='string'?v.toLowerCase():"
                "v&&typeof v==='object'?Object.fromEntries(Object.entries(v).map(([k,x])=>[k,lower(x)])):v;"
                "console.log(new Function('needs','steps','return '+d.condition)(lower(d.needs),lower(d.steps)));",
            ],
            input=json.dumps({"condition": condition, "needs": needs, "steps": steps}),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() == "true"

    for step in workflow["jobs"]["live_tests"]["steps"]:
        if "run" not in step or not enabled(step.get("if", "true")):
            continue
        step_env = {}
        contexts = {
            "needs": needs,
            "secrets": {
                "ZLIBRARY_EMAIL": "dummy@example.test",
                "ZLIBRARY_PASSWORD": "dummy",
            },
        }
        for key, expression in step.get("env", {}).items():
            value = contexts
            for part in expression.removeprefix("${{ ").removesuffix(" }}").split("."):
                value = value.get(part, "") if isinstance(value, dict) else ""
            step_env[key] = value
        run = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                **step_env,
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(live_output),
            },
        )
        assert run.returncode == 0, run.stdout + run.stderr
        if step.get("id") == "live" and live_output.exists():
            steps["live"]["outputs"] = dict(
                line.split("=", 1) for line in live_output.read_text().splitlines()
            )
    invocations = (
        [json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else []
    )
    integration = [
        args
        for args in invocations
        if args[:4] == ["run", "pytest", "-m", "integration"]
    ]
    if expected == "ran":
        assert len(integration) == 2  # collection plus execution, both stubbed
    else:
        assert integration == [], invocations
        assert steps["live"]["outputs"]["status"] == expected
        if expected == "blocked":
            assert steps["live"]["outputs"]["blocked"] == "true"
        else:
            assert steps["live"]["outputs"].get("blocked") != "true"
