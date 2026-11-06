#!/usr/bin/env python3
"""Build the scheduled, metadata-only source-health v1 artifact offline."""

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

EXPECTED_CHECKS = {
    "zlibrary": ["zlibrary:eapi/info/domains", "zlibrary:eapi/book/search"],
    "annas-archive": ["annas-archive:search"],
    "libgen": ["libgen:search", "libgen:article-search", "libgen:download"],
}
MAX_INPUT_BYTES = 5 * 1024 * 1024


def read_input(path):
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return None, "invalid"
        return data, None
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def probe_input(path):
    data, problem = read_input(path)
    if problem:
        return None, problem
    try:
        value = json.loads(data, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise ValueError("invalid probe envelope")
        if any(not isinstance(row, dict) for row in value["results"]):
            raise ValueError("invalid probe row")
        return value["results"], None
    except (ValueError, UnicodeError, RecursionError):
        return None, "invalid"


def probe_reason(row):
    # Never copy upstream messages: they can contain URLs, keys or account data.
    text = str(row.get("reason", "")).lower()
    if row["status"] == "passed":
        return "probe_passed"
    if row["status"] == "blocked":
        return "probe_refused"
    for needles, reason in [
        (("timeout", "timed out", "deadline"), "probe_timeout"),
        (("ssl", "tls"), "probe_tls_failure"),
        (("dns", "gaierror", "could not resolve"), "probe_dns_failure"),
        (("connecterror", "connection refused"), "probe_connection_failed"),
    ]:
        if any(needle in text for needle in needles):
            return reason
    return "probe_contract_failed"


def reachability(source, rows, problem, job, outcome, relied, today):
    expected = EXPECTED_CHECKS[source]
    checks, missing = [], []
    for name in expected:
        matches = [row for row in rows or [] if row.get("name") == name]
        state, reason, first_seen = "failed", "invalid_probe_report", None
        if not matches:
            missing.append(name)
        if problem == "missing":
            unstarted = job in ("cancelled", "skipped") or outcome == "skipped"
            state = "not_run" if unstarted else "failed"
            reason = (
                "probe_step_not_started"
                if outcome == "skipped"
                else "reachability_not_completed"
                if unstarted
                else "missing_probe_report"
            )
        elif problem:
            pass
        elif job in ("cancelled", "skipped", "unknown"):
            state, reason = "not_run", "reachability_not_completed"
        elif outcome not in ("success", "failure"):
            reason = "probe_execution_not_recorded"
        elif not matches:
            reason = "missing_probe_check"
        elif len(matches) != 1:
            reason = "duplicate_probe_check"
        else:
            row = matches[0]
            status = row.get("status")
            valid = (
                row.get("source") == source
                and type(row.get("relied_on")) is bool
                and row["relied_on"] == (source in relied)
                and (
                    (
                        status == "passed"
                        and row.get("ok") is True
                        and row.get("blocked") is False
                    )
                    or (
                        status == "blocked"
                        and row.get("ok") is False
                        and row.get("blocked") is True
                    )
                    or (
                        status == "changed"
                        and row.get("ok") is False
                        and row.get("blocked") is False
                    )
                )
            )
            if valid:
                state = "failed" if status == "changed" else status
                reason = probe_reason(row)
                try:
                    value = row.get("first_seen")
                    if (
                        isinstance(value, str)
                        and date.fromisoformat(value).isoformat() == value
                        and value <= today
                    ):
                        first_seen = value
                except ValueError:
                    pass
        checks.append(
            {"id": name, "status": state, "reason": reason, "first_seen": first_seen}
        )
    worst = next(
        (
            c
            for state in ("failed", "not_run", "blocked")
            for c in checks
            if c["status"] == state
        ),
        checks[0],
    )
    unexpected = any(
        row.get("source") == source
        and (not isinstance(row.get("name"), str) or row.get("name") not in expected)
        for row in rows or []
    )
    complete = (
        not problem
        and not missing
        and not unexpected
        and job in ("success", "failure")
        and outcome in ("success", "failure")
        and not any(
            check["reason"]
            in {
                "invalid_probe_report",
                "duplicate_probe_check",
                "probe_execution_not_recorded",
            }
            for check in checks
        )
    )
    return {
        "status": "failed" if unexpected else worst["status"],
        "reason": "unexpected_probe_check" if unexpected else worst["reason"],
        "evidence_complete": bool(complete),
        "expected_checks": expected,
        "missing_checks": missing,
        "checks": checks,
    }


def live_input(path):
    data, problem = read_input(path)
    if problem:
        return None, problem
    try:
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise ValueError("unsupported XML declaration")
        root = ET.fromstring(data)
        if root.tag not in ("testsuites", "testsuite"):
            raise ValueError("invalid JUnit envelope")
        cases = list(root.iter("testcase"))
        if not cases:
            return None, "empty"
        failed = sum(any(p.tag in ("error", "failure") for p in case) for case in cases)
        skipped = sum(
            case.find("skipped") is not None
            and not any(p.tag in ("error", "failure") for p in case)
            for case in cases
        )
        passed = sum(
            not any(p.tag in ("error", "failure", "skipped") for p in case)
            for case in cases
        )
        problems = [p for p in root.iter() if p.tag in ("error", "failure")]
        case_problems = {
            p for case in cases for p in case.iter() if p.tag in ("error", "failure")
        }
        outside_case_problems = any(p not in case_problems for p in problems)
        if not problems:
            for suite in root.iter():
                if suite.tag not in ("testsuites", "testsuite"):
                    continue
                children = list(suite.iter("testcase"))
                observed = {
                    "tests": len(children),
                    "skipped": sum(c.find("skipped") is not None for c in children),
                    "errors": 0,
                    "failures": 0,
                }
                if any(
                    key in suite.attrib
                    and (
                        not suite.attrib[key].isascii()
                        or not suite.attrib[key].isdigit()
                        or int(suite.attrib[key]) != count
                    )
                    for key, count in observed.items()
                ):
                    raise ValueError("inconsistent JUnit counters")
        anchor = [
            case
            for case in cases
            if case.get("classname", "").split(".")[-1] == "TestRealAuthentication"
            and case.get("name", "").split("[")[0] == "test_authentication_succeeds"
        ]
        anchor_passed = any(
            not any(p.tag in ("error", "failure", "skipped") for p in case)
            for case in anchor
        )
        return {
            "counts": {
                "total": len(cases),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            },
            "problems": bool(problems),
            "outside_case_problems": outside_case_problems,
            "anchor_present": bool(anchor),
            "anchor_passed": anchor_passed,
        }, None
    except (ET.ParseError, ValueError, RecursionError):
        return None, "invalid"


def authenticated(evidence, problem, status, job, outcome, started, exit_code):
    marked = started == "true"
    ran = True if marked and evidence else None if marked else False
    if not marked and (evidence or status in ("passed", "failed", "skipped")):
        ran = None
    if job == "cancelled" and outcome != "skipped" and not marked:
        ran = None
    counts = evidence["counts"] if evidence else None
    result = {
        "status": "failed",
        "reason": "invalid_live_status",
        "ran": ran,
        "scope": "zlibrary:python-integration",
        "counts": counts,
        "required_check": "zlibrary:authentication-succeeds",
        "coverage_complete": False,
    }

    def finish(state, reason, complete=False):
        return {
            **result,
            "status": state,
            "reason": reason,
            "coverage_complete": complete,
        }

    if job == "cancelled":
        return finish("not_run", "live_job_cancelled")
    if job == "skipped":
        return finish("not_run", "live_job_skipped")
    if status in ("no_account", "blocked", "unknown"):
        if marked or problem != "missing" or outcome != "success":
            return finish("failed", "contradictory_live_evidence")
        return finish(
            *{
                "no_account": ("no_account", "no_ci_account_configured"),
                "blocked": ("blocked", "probe_refused"),
                "unknown": ("not_run", "probe_state_unknown"),
            }[status]
        )
    if not marked:
        if status in ("passed", "failed", "skipped"):
            return finish("failed", "execution_not_recorded")
        return finish("not_run", "live_step_not_started")
    if problem:
        return finish(
            "failed",
            {
                "missing": "missing_live_evidence",
                "invalid": "invalid_live_evidence",
                "empty": "empty_live_evidence",
            }[problem],
        )
    if status not in ("passed", "failed", "skipped"):
        return finish("failed", "invalid_live_status")
    if evidence["outside_case_problems"]:
        return finish("failed", "live_collection_error")
    if exit_code != "0" or evidence["problems"] or status == "failed":
        return finish("failed", "live_suite_failed")
    if job != "success" or outcome != "success":
        return finish("failed", "live_job_incomplete")
    if counts["skipped"] == counts["total"]:
        return finish("not_run", "all_live_tests_skipped")
    if not evidence["anchor_present"]:
        return finish("failed", "required_authentication_check_missing")
    if not evidence["anchor_passed"]:
        return finish("not_run", "authentication_check_skipped")
    if status != "passed":
        return finish("failed", "contradictory_live_evidence")
    return finish(
        "passed",
        "live_suite_partially_covered" if counts["skipped"] else "live_suite_passed",
        not counts["skipped"],
    )


def build_report(probe, live, metadata, generated_at):
    try:
        run_id, attempt, sha, event = [
            metadata.get(key, "")
            for key in (
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_SHA",
                "GITHUB_EVENT_NAME",
            )
        ]
        if (
            not re.fullmatch(r"[1-9][0-9]*", run_id)
            or not re.fullmatch(r"[1-9][0-9]*", attempt)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or event not in ("schedule", "workflow_dispatch")
        ):
            raise ValueError
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        stamp = (
            stamp.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, TypeError):
        raise ValueError("invalid run metadata") from None
    policy = metadata.get("UPSTREAM_RELIED_SOURCES", "zlibrary,libgen")
    relied = {part.strip() for part in policy.split(",") if part.strip()} or {
        "zlibrary",
        "libgen",
    }
    policy_valid = relied <= EXPECTED_CHECKS.keys()
    job_states = {"success", "failure", "cancelled", "skipped"}
    jobs = {
        name: metadata.get(key) if metadata.get(key) in job_states else "unknown"
        for name, key in (
            ("reachability", "REACHABILITY_RESULT"),
            ("reachability_step", "PROBE_OUTCOME"),
            ("live_tests", "LIVE_RESULT"),
            ("live_step", "LIVE_OUTCOME"),
        )
    }
    rows, probe_problem = probe_input(probe)
    evidence, live_problem = live_input(live)
    auth = authenticated(
        evidence,
        live_problem,
        metadata.get("LIVE_STATUS", ""),
        jobs["live_tests"],
        jobs["live_step"],
        metadata.get("LIVE_STARTED", ""),
        metadata.get("LIVE_PYTEST_EXIT_CODE", ""),
    )
    no_suite = {
        "status": "not_run",
        "reason": "no_authenticated_suite_configured",
        "ran": False,
        "scope": None,
        "counts": None,
        "required_check": None,
        "coverage_complete": False,
    }
    sources = {
        source: {
            "alert_policy": "relied_on" if source in relied else "warning_only",
            "reachability": reachability(
                source,
                rows,
                probe_problem,
                jobs["reachability"],
                jobs["reachability_step"],
                relied,
                stamp[:10],
            ),
            "authenticated": auth if source == "zlibrary" else no_suite.copy(),
        }
        for source in EXPECTED_CHECKS
    }
    if not policy_valid:
        for source in sources.values():
            source["alert_policy"] = "unknown"
            source["reachability"].update(
                status="failed", reason="invalid_alert_policy", evidence_complete=False
            )
            for check in source["reachability"]["checks"]:
                check.update(status="failed", reason="invalid_alert_policy")
    expected = {name for names in EXPECTED_CHECKS.values() for name in names}
    return {
        "schema_version": 1,
        "generated_at": stamp,
        "run": {"id": run_id, "attempt": int(attempt), "head_sha": sha, "event": event},
        "expected_sources": list(EXPECTED_CHECKS),
        "policy_valid": policy_valid,
        "jobs": jobs,
        "inputs": {
            "probe_artifact": f"upstream-source-health-probe-{attempt}",
            "live_artifact": f"upstream-source-health-live-{attempt}",
        },
        "sources": sources,
        "probe_contract_complete": all(
            row["reachability"]["evidence_complete"] for row in sources.values()
        )
        and not any(
            not isinstance(row.get("name"), str) or row.get("name") not in expected
            for row in rows or []
        ),
        "unexpected_probe_checks": sum(
            not isinstance(row.get("name"), str) or row.get("name") not in expected
            for row in rows or []
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-report",
        type=Path,
        default=Path("health-input/probe/upstream-report.json"),
    )
    parser.add_argument(
        "--live-junit", type=Path, default=Path("health-input/live/live.xml")
    )
    parser.add_argument("--output", type=Path, default=Path("source-health.v1.json"))
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC producer time; injectable for offline fixtures",
    )
    args = parser.parse_args()
    keys = (
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_EVENT_NAME",
        "REACHABILITY_RESULT",
        "PROBE_OUTCOME",
        "LIVE_RESULT",
        "LIVE_STATUS",
        "LIVE_OUTCOME",
        "LIVE_STARTED",
        "LIVE_PYTEST_EXIT_CODE",
        "UPSTREAM_RELIED_SOURCES",
    )
    metadata = {key: os.environ[key] for key in keys if key in os.environ}
    try:
        report = build_report(
            args.probe_report,
            args.live_junit,
            metadata,
            args.generated_at or datetime.now(timezone.utc).isoformat(),
        )
    except ValueError:
        print("invalid run metadata", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
