"""Offline producer contracts for the scheduled, versioned reader artifact."""

import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/source_health_report.py"
EXPECTED = {
    "zlibrary": ["zlibrary:eapi/info/domains", "zlibrary:eapi/book/search"],
    "annas-archive": ["annas-archive:search"],
    "libgen": ["libgen:search", "libgen:article-search", "libgen:download"],
}


@pytest.fixture
def inputs(tmp_path):
    probe = {
        "checked_at": "2026-11-06",
        "relied_sources": ["zlibrary", "libgen"],
        "results": [
            {
                "name": name,
                "source": source,
                "status": "passed",
                "ok": True,
                "blocked": False,
                "relied_on": source != "annas-archive",
                "reason": "synthetic success",
                "first_seen": None,
            }
            for source, names in EXPECTED.items()
            for name in names
        ],
    }
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for cls, name in [
        ("TestRealAuthentication", "test_authentication_succeeds"),
        ("TestRealBasicSearch", "test_basic_search_returns_results"),
        ("TestDownloadOperations", "test_download_small_book"),
    ]:
        ET.SubElement(
            suite,
            "testcase",
            classname="integration.test_real_zlibrary." + cls,
            name=name,
        )
    env = {
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": "schedule",
        "REACHABILITY_RESULT": "success",
        "PROBE_OUTCOME": "success",
        "LIVE_RESULT": "success",
        "LIVE_STATUS": "passed",
        "LIVE_OUTCOME": "success",
        "LIVE_STARTED": "true",
        "LIVE_PYTEST_EXIT_CODE": "0",
        "UPSTREAM_RELIED_SOURCES": "zlibrary,libgen",
    }
    return {"path": tmp_path, "probe": probe, "xml": root, "env": env}


def produce(data, *, raw_probe=None, raw_xml=None, raw_counts=None):
    path = data["path"]
    if data["probe"] is not None:
        (path / "probe.json").write_text(
            raw_probe if raw_probe is not None else json.dumps(data["probe"])
        )
    if data["xml"] is not None:
        (path / "live.xml").write_text(
            raw_xml
            if raw_xml is not None
            else ET.tostring(data["xml"], encoding="unicode")
        )
    counts = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/live_test_counts.py"),
            "--junitxml",
            str(path / "live.xml"),
            "--output",
            str(path / "live-counts.json"),
        ],
        env=data["env"],
        capture_output=True,
        text=True,
    )
    assert counts.returncode == 0
    if raw_counts is not None:
        (path / "live-counts.json").write_text(raw_counts)
    run = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--probe-report",
            str(path / "probe.json"),
            "--live-counts",
            str(path / "live-counts.json"),
            "--output",
            str(path / "health.json"),
            "--generated-at",
            "2026-11-06T12:34:56Z",
        ],
        env=data["env"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, (
        "Producer must persist explicit non-pass evidence: " + run.stderr
    )
    return json.loads((path / "health.json").read_text())


def no_live(data, status="no_account"):
    data["env"].update(LIVE_STATUS=status, LIVE_STARTED="", LIVE_PYTEST_EXIT_CODE="")
    data["xml"] = None


def test_report_binds_run_attempt_sha_utc_and_all_expected_sources(inputs):
    report = produce(inputs)
    assert report["schema_version"] == 1
    assert report["generated_at"] == "2026-11-06T12:34:56Z"
    assert report["run"] == {
        "id": "123456",
        "attempt": 2,
        "head_sha": "a" * 40,
        "event": "schedule",
    }
    assert report["expected_sources"] == list(EXPECTED)
    assert list(report["sources"]) == list(EXPECTED)
    for source, expected in EXPECTED.items():
        row = report["sources"][source]
        assert row["reachability"]["expected_checks"] == expected
        assert row["reachability"]["missing_checks"] == []
        assert row["reachability"]["status"] == "passed"
        assert [c["id"] for c in row["reachability"]["checks"]] == expected
        assert row["alert_policy"] == (
            "warning_only" if source == "annas-archive" else "relied_on"
        )
    auth = report["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "passed" and auth["ran"] is True
    assert auth["scope"] == "zlibrary:python-integration"
    assert auth["counts"] == {"total": 3, "passed": 3, "failed": 0, "skipped": 0}
    assert auth["required_check"] == "zlibrary:authentication-succeeds"
    assert auth["coverage_complete"] is True


@pytest.mark.parametrize("source", ["annas-archive", "libgen"])
def test_sources_without_authenticated_suite_are_explicitly_not_run(inputs, source):
    auth = produce(inputs)["sources"][source]["authenticated"]
    assert auth["status"] == "not_run" and auth["ran"] is False
    assert auth["reason"] == "no_authenticated_suite_configured"
    assert auth["coverage_complete"] is False and auth["counts"] is None


def test_no_account_is_never_inferred_healthy_from_workflow_success(inputs):
    no_live(inputs)
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "no_account" and auth["ran"] is False
    assert auth["reason"] == "no_ci_account_configured"
    assert auth["coverage_complete"] is False


@pytest.mark.parametrize(
    "job,outcome,status,reason",
    [
        ("failure", "skipped", "", "live_step_not_started"),
        ("skipped", "skipped", "", "live_job_skipped"),
        ("cancelled", "cancelled", "", "live_job_cancelled"),
        ("success", "success", "unknown", "probe_state_unknown"),
        ("success", "success", "blocked", "probe_refused"),
    ],
)
def test_missing_execution_stays_not_run_or_blocked(
    inputs, job, outcome, status, reason
):
    no_live(inputs, status)
    inputs["env"].update(LIVE_RESULT=job, LIVE_OUTCOME=outcome)
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == ("blocked" if status == "blocked" else "not_run")
    assert auth["reason"] == reason
    assert auth["ran"] is (None if job == "cancelled" else False)
    assert auth["coverage_complete"] is False


@pytest.mark.parametrize(
    "job,outcome,state,reason,ran",
    [
        ("failure", "failure", "failed", "live_collection_error", False),
        ("cancelled", "failure", "failed", "live_collection_error", False),
        ("skipped", "failure", "failed", "live_collection_error", False),
        ("success", "failure", "failed", "live_collection_error", False),
        ("failure", "", "failed", "execution_not_recorded", None),
        ("success", "success", "failed", "execution_not_recorded", None),
        ("cancelled", "success", "failed", "execution_not_recorded", None),
        ("skipped", "success", "failed", "execution_not_recorded", None),
        ("failure", "cancelled", "failed", "execution_not_recorded", None),
        ("", "", "failed", "execution_not_recorded", None),
        ("failure", "skipped", "not_run", "live_step_not_started", False),
        ("skipped", "", "not_run", "live_job_skipped", False),
        ("cancelled", "", "not_run", "live_job_cancelled", None),
    ],
)
def test_unmarked_live_step_uses_outcome_before_inferring_non_execution(
    inputs, job, outcome, state, reason, ran
):
    no_live(inputs, "")
    inputs["env"].update(LIVE_RESULT=job, LIVE_OUTCOME=outcome)
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert (auth["status"], auth["reason"], auth["ran"]) == (state, reason, ran)
    assert auth["counts"] is None and auth["coverage_complete"] is False


@pytest.mark.parametrize("outcome", ["skipped", "", "success"])
@pytest.mark.parametrize("job", ["failure", "cancelled", "skipped"])
def test_unmarked_live_step_with_junit_cannot_claim_non_execution(inputs, outcome, job):
    inputs["env"].update(
        LIVE_STARTED="", LIVE_STATUS="", LIVE_OUTCOME=outcome, LIVE_RESULT=job
    )
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert (auth["status"], auth["reason"], auth["ran"]) == (
        "failed",
        "execution_not_recorded",
        None,
    )
    assert auth["counts"]["total"] == 3 and auth["coverage_complete"] is False


def test_preliminary_collection_failure_in_actual_workflow_is_reported_failed(inputs):
    workflow = json.loads(
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
    step = next(
        step
        for step in workflow["jobs"]["live_tests"]["steps"]
        if step.get("id") == "live"
    )
    path = inputs["path"]
    policy = path / "zlibrary/src/zlibrary/eapi.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("DEFAULT_EAPI_DOMAINS = ['z-library.ec']\n")
    bin_path = path / "bin"
    bin_path.mkdir()
    (bin_path / "python3").symlink_to(sys.executable)
    uv = bin_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:] == ['run', 'pytest', '-m', 'integration', '--collect-only', '--no-cov', '-q']\n"
        "Path('collection-called').touch()\nraise SystemExit(2)\n"
    )
    uv.chmod(0o755)
    output = path / "github-output"
    run = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=path,
        env={
            "PATH": str(bin_path),
            "GITHUB_WORKSPACE": str(path),
            "GITHUB_OUTPUT": str(output),
            "ZLIB_PROBE_DOMAIN": "z-library.ec",
            "ZLIB_BLOCKED": "false",
            "ZLIBRARY_EMAIL": "synthetic@example.test",
            "ZLIBRARY_PASSWORD": "synthetic-password",
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == 2, run.stderr
    assert (path / "collection-called").exists()
    assert not output.exists() and not (path / "live.xml").exists()
    no_live(inputs, "")
    inputs["env"].update(LIVE_RESULT="failure", LIVE_OUTCOME="failure")
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert (auth["status"], auth["reason"], auth["ran"]) == (
        "failed",
        "live_collection_error",
        False,
    )
    assert auth["counts"] is None and auth["coverage_complete"] is False


@pytest.mark.parametrize("missing", ["missing", "malformed", "empty", "doctype"])
def test_started_suite_with_missing_or_invalid_required_evidence_fails(inputs, missing):
    raw = None
    if missing == "missing":
        inputs["xml"] = None
    elif missing == "malformed":
        raw = "<broken"
    elif missing == "empty":
        raw = "<testsuites/>"
    else:
        raw = '<!DOCTYPE x [<!ENTITY secret "private.invalid">]><testsuites/>'
    auth = produce(inputs, raw_xml=raw)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["coverage_complete"] is False
    assert auth["reason"] in {
        "missing_live_evidence",
        "invalid_live_evidence",
        "empty_live_evidence",
    }
    assert auth["ran"] is None


@pytest.mark.parametrize(
    "status,exit_code", [("failed", "1"), ("passed", "1"), ("failed", "0")]
)
def test_failed_or_conflicting_execution_never_becomes_passed(
    inputs, status, exit_code
):
    inputs["env"].update(
        LIVE_STATUS=status, LIVE_PYTEST_EXIT_CODE=exit_code, LIVE_RESULT="failure"
    )
    ET.SubElement(
        inputs["xml"].find(".//testcase"),
        "failure",
        message="private.invalid/token=secret",
    )
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["ran"] is True
    assert auth["counts"]["failed"] == 1


@pytest.mark.parametrize(
    "kind", ["all_skipped", "anchor_skipped", "anchor_missing", "partial"]
)
def test_skips_missing_anchor_and_partial_coverage_remain_visible(inputs, kind):
    cases = inputs["xml"].findall(".//testcase")
    if kind == "all_skipped":
        for case in cases:
            ET.SubElement(case, "skipped")
        inputs["env"]["LIVE_STATUS"] = "skipped"
    elif kind == "anchor_skipped":
        ET.SubElement(cases[0], "skipped")
    elif kind == "anchor_missing":
        inputs["xml"].find("testsuite").remove(cases[0])
    else:
        ET.SubElement(cases[1], "skipped")
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["coverage_complete"] is False
    assert auth["ran"] is True
    assert (
        auth["status"]
        == {
            "all_skipped": "not_run",
            "anchor_skipped": "not_run",
            "anchor_missing": "failed",
            "partial": "passed",
        }[kind]
    )
    assert (
        auth["reason"]
        == {
            "all_skipped": "all_live_tests_skipped",
            "anchor_skipped": "authentication_check_skipped",
            "anchor_missing": "required_authentication_check_missing",
            "partial": "live_suite_partially_covered",
        }[kind]
    )


@pytest.mark.parametrize("status", ["no_account", "blocked", "unknown", "passed"])
def test_inconsistent_execution_flags_cannot_claim_a_skip_or_pass(inputs, status):
    inputs["env"].update(LIVE_STATUS=status)
    if status == "passed":
        inputs["env"]["LIVE_STARTED"] = ""
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["coverage_complete"] is False
    if status == "passed":
        assert auth["ran"] is None


@pytest.mark.parametrize("job", ["failure", "cancelled", "skipped", "success"])
def test_missing_probe_input_never_becomes_healthy(inputs, job):
    inputs["probe"] = None
    inputs["env"].update(REACHABILITY_RESULT=job, PROBE_OUTCOME=job)
    report = produce(inputs)
    for source, expected in EXPECTED.items():
        reach = report["sources"][source]["reachability"]
        assert reach["status"] == (
            "failed" if job in ("success", "failure") else "not_run"
        )
        assert reach["missing_checks"] == expected
        assert all(check["status"] != "passed" for check in reach["checks"])


@pytest.mark.parametrize("job", ["cancelled", "skipped", "failure", ""])
@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_completed_probe_with_missing_report_cannot_be_relabelled_unstarted(
    inputs, job, outcome
):
    inputs["probe"] = None
    inputs["env"].update(REACHABILITY_RESULT=job, PROBE_OUTCOME=outcome)
    report = produce(inputs)
    assert report["probe_contract_complete"] is False
    for source in report["sources"].values():
        reach = source["reachability"]
        assert (reach["status"], reach["reason"], reach["evidence_complete"]) == (
            "failed",
            "missing_probe_report",
            False,
        )
        assert all(check["status"] == "failed" for check in reach["checks"])


@pytest.mark.parametrize("job", ["cancelled", "skipped", ""])
def test_failed_probe_with_report_cannot_be_relabelled_unstarted(inputs, job):
    inputs["probe"]["results"][0].update(ok=False, status="changed")
    inputs["env"].update(REACHABILITY_RESULT=job, PROBE_OUTCOME="failure")
    reach = produce(inputs)["sources"]["zlibrary"]["reachability"]
    assert reach["status"] == "failed" and reach["evidence_complete"] is False


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_row",
        "duplicate",
        "contradictory",
        "unknown",
        "bad_json",
        "duplicate_key",
    ],
)
def test_missing_invalid_and_unknown_probe_checks_are_not_recoveries(
    inputs, corruption
):
    raw = None
    rows = inputs["probe"]["results"]
    if corruption == "missing_row":
        rows.pop(0)
    elif corruption == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif corruption == "contradictory":
        rows[0]["ok"] = False
    elif corruption == "unknown":
        rows[0]["name"] = "private.invalid/secret"
    elif corruption == "bad_json":
        raw = "{broken"
    else:
        raw = '{"results": [], "results": []}'
    reach = produce(inputs, raw_probe=raw)["sources"]["zlibrary"]["reachability"]
    assert reach["status"] == "failed"
    assert reach["reason"]


@pytest.mark.parametrize(
    "status,blocked,reason",
    [("blocked", True, "probe_refused"), ("changed", False, "probe_contract_failed")],
)
def test_probe_failures_and_warning_only_sources_are_retained(
    inputs, status, blocked, reason
):
    row = next(r for r in inputs["probe"]["results"] if r["source"] == "annas-archive")
    row.update(
        status=status,
        ok=False,
        blocked=blocked,
        reason="synthetic refusal",
        first_seen="2026-11-05",
    )
    report = produce(inputs)
    reach = report["sources"]["annas-archive"]["reachability"]
    assert reach["status"] == ("blocked" if blocked else "failed")
    assert reach["checks"][0]["reason"] == reason
    assert reach["checks"][0]["first_seen"] == "2026-11-05"
    assert report["sources"]["annas-archive"]["alert_policy"] == "warning_only"
    assert report["sources"]["libgen"]["reachability"]["status"] == "passed"


def test_report_is_closed_and_does_not_copy_provider_hosts_messages_or_case_ids(inputs):
    secret = "private-host.invalid/token=CANARY-SECRET"
    inputs["probe"]["results"][0].update(
        reason=secret, detail=secret, host=secret, credentials=secret
    )
    inputs["xml"].findall(".//testcase")[1].set("name", secret)
    inputs["env"]["UNRELATED_ACCOUNT_VALUE"] = secret
    report = produce(inputs)
    encoded = json.dumps(report)
    assert (
        secret not in encoded
        and "CANARY-SECRET" not in encoded
        and ".invalid" not in encoded
    )
    assert not re.search(r"https?://", encoded)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ATTEMPT", "0"),
        ("GITHUB_SHA", "bad"),
        ("GITHUB_EVENT_NAME", "unexpected"),
    ],
)
def test_invalid_run_provenance_is_rejected_without_echoing_values(inputs, key, value):
    inputs["env"][key] = value
    inputs["probe"] = None
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(inputs["path"] / "health.json")],
        env=inputs["env"],
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert not (inputs["path"] / "health.json").exists()
    assert "invalid run metadata" in run.stderr


@pytest.mark.parametrize("name", [[], {}, None, True, 7])
def test_malformed_probe_identifiers_persist_a_nonhealthy_report(inputs, name):
    inputs["probe"]["results"][0]["name"] = name
    reach = produce(inputs)["sources"]["zlibrary"]["reachability"]
    assert reach["status"] == "failed"
    assert "zlibrary:eapi/info/domains" in reach["missing_checks"]


@pytest.mark.parametrize("policy", ["", " , ", "private-policy.invalid"])
def test_empty_policy_matches_probe_defaults_and_invalid_policy_is_explicit(
    inputs, policy
):
    inputs["env"]["UPSTREAM_RELIED_SOURCES"] = policy
    report = produce(inputs)
    assert "private-policy.invalid" not in json.dumps(report)
    if "private" in policy:
        assert report["policy_valid"] is False
        assert all(
            row["reachability"]["status"] == "failed"
            for row in report["sources"].values()
        )
        assert all(
            row["alert_policy"] == "unknown" for row in report["sources"].values()
        )
    else:
        assert report["policy_valid"] is True
        assert report["sources"]["zlibrary"]["alert_policy"] == "relied_on"
        assert report["sources"]["zlibrary"]["reachability"]["status"] == "passed"


@pytest.mark.parametrize("which", ["probe", "live"])
def test_oversized_inputs_are_rejected_without_copying_them(inputs, which):
    if which == "probe":
        report = produce(
            inputs, raw_probe=json.dumps(inputs["probe"]) + " " * (5 * 1024 * 1024)
        )
        assert report["sources"]["libgen"]["reachability"]["status"] == "failed"
    else:
        raw = (
            ET.tostring(inputs["xml"], encoding="unicode")
            + "<!--"
            + "x" * (5 * 1024 * 1024)
            + "-->"
        )
        assert (
            produce(inputs, raw_xml=raw)["sources"]["zlibrary"]["authenticated"][
                "status"
            ]
            == "failed"
        )


def test_standalone_collection_error_cannot_hide_behind_passing_cases(inputs):
    ET.SubElement(inputs["xml"], "error", message="private.invalid")
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["reason"] == "live_collection_error"


def test_failed_case_is_not_double_counted_when_it_also_has_skip_metadata(inputs):
    case = inputs["xml"].find(".//testcase")
    ET.SubElement(case, "failure")
    ET.SubElement(case, "skipped")
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["counts"] == {"total": 3, "passed": 2, "failed": 1, "skipped": 0}
    assert auth["status"] == "failed"


@pytest.mark.parametrize(
    "message,reason",
    [
        ("TimeoutError private.invalid", "probe_timeout"),
        ("TLS refused private.invalid", "probe_tls_failure"),
        ("DNS error private.invalid", "probe_dns_failure"),
        ("ConnectError private.invalid", "probe_connection_failed"),
    ],
)
def test_probe_reason_categories_are_retained_without_raw_messages(
    inputs, message, reason
):
    row = inputs["probe"]["results"][0]
    row.update(ok=False, status="changed", reason=message)
    report = produce(inputs)
    assert (
        report["sources"]["zlibrary"]["reachability"]["checks"][0]["reason"] == reason
    )
    assert "private.invalid" not in json.dumps(report)


def test_report_records_expected_attempt_input_names_and_job_outcomes(inputs):
    report = produce(inputs)
    assert report["inputs"] == {
        "probe_artifact": "upstream-source-health-probe-2",
        "live_artifact": "upstream-source-health-live-2",
    }
    assert report["jobs"] == {
        "reachability": "success",
        "reachability_step": "success",
        "live_tests": "success",
        "live_step": "success",
    }


def test_actual_workflow_producer_shell_writes_even_when_both_inputs_are_absent(inputs):
    import shutil

    workflow = json.loads(
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
    step = next(
        step
        for step in workflow["jobs"]["source_health"]["steps"]
        if step.get("id") == "health"
    )
    path = inputs["path"]
    (path / "scripts").mkdir()
    shutil.copyfile(SCRIPT, path / "scripts/source_health_report.py")
    (path / "bin").mkdir()
    (path / "bin/python3").symlink_to(sys.executable)
    metadata = {
        **inputs["env"],
        "PATH": str(path / "bin"),
        "REACHABILITY_RESULT": "failure",
        "PROBE_OUTCOME": "skipped",
        "LIVE_RESULT": "failure",
        "LIVE_OUTCOME": "skipped",
        "LIVE_STARTED": "",
        "LIVE_STATUS": "",
        "LIVE_PYTEST_EXIT_CODE": "",
    }
    run = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=path,
        env=metadata,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads((path / "source-health.v1.json").read_text())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", report["generated_at"])
    assert all(
        row["reachability"]["status"] == "not_run" for row in report["sources"].values()
    )
    assert report["sources"]["zlibrary"]["authenticated"]["status"] == "not_run"


def test_known_probe_setup_failure_is_not_run_while_missing_execution_evidence_is_failed(
    inputs,
):
    inputs["probe"] = None
    inputs["env"].update(REACHABILITY_RESULT="failure", PROBE_OUTCOME="skipped")
    report = produce(inputs)
    assert all(
        row["reachability"]["status"] == "not_run" for row in report["sources"].values()
    )
    assert all(
        row["reachability"]["reason"] == "probe_step_not_started"
        for row in report["sources"].values()
    )


def test_additional_unrecognized_probe_check_does_not_leave_its_source_healthy(inputs):
    inputs["probe"]["results"].append(
        {
            "name": "libgen:unexpected",
            "source": "libgen",
            "status": "changed",
            "ok": False,
            "blocked": False,
            "relied_on": True,
        }
    )
    report = produce(inputs)
    assert report["sources"]["libgen"]["reachability"]["status"] == "failed"
    assert (
        report["sources"]["libgen"]["reachability"]["reason"]
        == "unexpected_probe_check"
    )
    assert report["unexpected_probe_checks"] == 1
    assert report["probe_contract_complete"] is False


@pytest.mark.parametrize(
    "key,value", [("tests", "9"), ("skipped", "1"), ("failures", "1")]
)
def test_inconsistent_junit_summary_is_invalid_evidence(inputs, key, value):
    inputs["xml"].find("testsuite").set(key, value)
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["reason"] == "invalid_live_evidence"


@pytest.mark.parametrize("job", ["", "unrecognized"])
def test_unknown_probe_job_outcome_cannot_present_a_healthy_source(inputs, job):
    inputs["env"]["REACHABILITY_RESULT"] = job
    report = produce(inputs)
    assert report["jobs"]["reachability"] == "unknown"
    assert all(
        row["reachability"]["status"] == "not_run" for row in report["sources"].values()
    )


def test_missing_execution_marker_and_lost_junit_are_unknown_not_a_claim_of_no_run(
    inputs,
):
    inputs["env"]["LIVE_STARTED"] = ""
    inputs["xml"] = None
    auth = produce(inputs)["sources"]["zlibrary"]["authenticated"]
    assert auth["status"] == "failed" and auth["ran"] is None
    assert auth["counts"] is None and auth["coverage_complete"] is False
