#!/usr/bin/env python3
"""Build the scheduled, metadata-only source-health v1 artifact offline."""

import argparse
from datetime import date, datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import sys


class Reason(str, Enum):
    """Closed reason vocabulary shared with the version-1 reader contract."""

    ALL_LIVE_TESTS_SKIPPED = "all_live_tests_skipped"
    AUTHENTICATION_CHECK_SKIPPED = "authentication_check_skipped"
    CONTRADICTORY_LIVE_EVIDENCE = "contradictory_live_evidence"
    CONTRADICTORY_PROBE_EVIDENCE = "contradictory_probe_evidence"
    DUPLICATE_PROBE_CHECK = "duplicate_probe_check"
    EARLIER_ATTEMPT_OUTPUTS = "earlier_attempt_outputs"
    EMPTY_LIVE_EVIDENCE = "empty_live_evidence"
    EXECUTION_NOT_RECORDED = "execution_not_recorded"
    INVALID_ALERT_POLICY = "invalid_alert_policy"
    INVALID_LIVE_EVIDENCE = "invalid_live_evidence"
    INVALID_LIVE_STATUS = "invalid_live_status"
    INVALID_PROBE_REPORT = "invalid_probe_report"
    LIVE_COLLECTION_ERROR = "live_collection_error"
    LIVE_JOB_CANCELLED = "live_job_cancelled"
    LIVE_JOB_INCOMPLETE = "live_job_incomplete"
    LIVE_JOB_SKIPPED = "live_job_skipped"
    LIVE_STEP_NOT_STARTED = "live_step_not_started"
    LIVE_SUITE_FAILED = "live_suite_failed"
    LIVE_SUITE_PARTIALLY_COVERED = "live_suite_partially_covered"
    LIVE_SUITE_PASSED = "live_suite_passed"
    MISSING_LIVE_EVIDENCE = "missing_live_evidence"
    MISSING_PROBE_CHECK = "missing_probe_check"
    MISSING_PROBE_REPORT = "missing_probe_report"
    NO_AUTHENTICATED_SUITE_CONFIGURED = "no_authenticated_suite_configured"
    NO_CI_ACCOUNT_CONFIGURED = "no_ci_account_configured"
    PROBE_CONNECTION_FAILED = "probe_connection_failed"
    PROBE_CONTRACT_FAILED = "probe_contract_failed"
    PROBE_DNS_FAILURE = "probe_dns_failure"
    PROBE_ERROR_UNKNOWN = "probe_error_unknown"
    PROBE_EXECUTION_NOT_RECORDED = "probe_execution_not_recorded"
    PROBE_HTTP_FAILURE = "probe_http_failure"
    PROBE_PASSED = "probe_passed"
    PROBE_REFUSED = "probe_refused"
    PROBE_SERVER_ERROR = "probe_server_error"
    PROBE_STATE_UNKNOWN = "probe_state_unknown"
    PROBE_STEP_NOT_STARTED = "probe_step_not_started"
    PROBE_TIMEOUT = "probe_timeout"
    PROBE_TLS_FAILURE = "probe_tls_failure"
    REACHABILITY_NOT_COMPLETED = "reachability_not_completed"
    REQUIRED_AUTHENTICATION_CHECK_MISSING = "required_authentication_check_missing"
    UNEXPECTED_PROBE_CHECK = "unexpected_probe_check"


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
    if row["status"] == "passed":
        return Reason.PROBE_PASSED
    if row["status"] == "blocked":
        return Reason.PROBE_REFUSED
    types, statuses = row.get("error_types", []), row.get("http_statuses", [])
    if (
        not isinstance(types, list)
        or any(not isinstance(t, str) for t in types)
        or not isinstance(statuses, list)
        or any(type(s) is not int or not 100 <= s <= 599 for s in statuses)
    ):
        return Reason.PROBE_ERROR_UNKNOWN
    types = set(types)
    for names, reason in [
        ({"gaierror"}, Reason.PROBE_DNS_FAILURE),
        ({"SSLError", "SSLCertVerificationError"}, Reason.PROBE_TLS_FAILURE),
        (
            {
                "TimeoutError",
                "TimeoutException",
                "ConnectTimeout",
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
            },
            Reason.PROBE_TIMEOUT,
        ),
        (
            {
                "ConnectError",
                "ReadError",
                "WriteError",
                "CloseError",
                "RemoteProtocolError",
                "LocalProtocolError",
                "NetworkError",
                "TransportError",
                "ProxyError",
            },
            Reason.PROBE_CONNECTION_FAILED,
        ),
    ]:
        if names & types:
            return reason
    if "HTTPStatusError" in types:
        if not statuses:
            return Reason.PROBE_ERROR_UNKNOWN
        return (
            Reason.PROBE_SERVER_ERROR
            if any(s >= 500 for s in statuses)
            else Reason.PROBE_HTTP_FAILURE
        )
    if types - {
        "ValueError",
        "KeyError",
        "TypeError",
        "AssertionError",
        "JSONDecodeError",
        "AllSourcesFailedError",
    }:
        return Reason.PROBE_ERROR_UNKNOWN
    return Reason.PROBE_CONTRACT_FAILED


def reachability(source, rows, problem, job, outcome, relied, today):
    expected = EXPECTED_CHECKS[source]
    checks, missing = [], []
    for name in expected:
        matches = [row for row in rows or [] if row.get("name") == name]
        state, reason, first_seen = "failed", Reason.INVALID_PROBE_REPORT, None
        if not matches:
            missing.append(name)
        if problem == "attempt":
            state, reason = "not_run", Reason.EARLIER_ATTEMPT_OUTPUTS
        elif problem == "contradictory":
            reason = Reason.CONTRADICTORY_PROBE_EVIDENCE
        elif problem == "missing":
            unstarted = outcome == "skipped" or (
                job in ("cancelled", "skipped")
                and outcome not in ("success", "failure")
            )
            state = "not_run" if unstarted else "failed"
            reason = (
                Reason.PROBE_STEP_NOT_STARTED
                if outcome == "skipped"
                else Reason.REACHABILITY_NOT_COMPLETED
                if unstarted
                else Reason.MISSING_PROBE_REPORT
            )
        elif problem:
            pass
        elif job in ("cancelled", "skipped", "unknown"):
            state = "failed" if outcome == "failure" else "not_run"
            reason = Reason.REACHABILITY_NOT_COMPLETED
        elif outcome not in ("success", "failure"):
            reason = Reason.PROBE_EXECUTION_NOT_RECORDED
        elif not matches:
            reason = Reason.MISSING_PROBE_CHECK
        elif len(matches) != 1:
            reason = Reason.DUPLICATE_PROBE_CHECK
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
                Reason.INVALID_PROBE_REPORT,
                Reason.DUPLICATE_PROBE_CHECK,
                Reason.PROBE_EXECUTION_NOT_RECORDED,
                Reason.PROBE_ERROR_UNKNOWN,
            }
            for check in checks
        )
    )
    return {
        "status": "failed" if unexpected else worst["status"],
        "reason": Reason.UNEXPECTED_PROBE_CHECK if unexpected else worst["reason"],
        "evidence_complete": bool(complete),
        "expected_checks": expected,
        "missing_checks": missing,
        "checks": checks,
    }


def live_input(path):
    data, problem = read_input(path)
    if problem:
        return None, "unavailable" if problem == "missing" else problem
    try:
        value = json.loads(data, object_pairs_hook=unique_object)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "evidence", "problem"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("invalid counts envelope")
        evidence, problem = value["evidence"], value["problem"]
        if problem in ("missing", "invalid", "empty") and evidence is None:
            return None, problem
        if (
            problem is not None
            or not isinstance(evidence, dict)
            or set(evidence)
            != {
                "counts",
                "problems",
                "outside_case_problems",
                "anchor_present",
                "anchor_passed",
            }
        ):
            raise ValueError("invalid counts evidence")
        counts = evidence["counts"]
        if not isinstance(counts, dict) or set(counts) != {
            "total",
            "passed",
            "failed",
            "skipped",
        }:
            raise ValueError("invalid counts")
        if (
            any(type(n) is not int or n < 0 for n in counts.values())
            or not counts["total"]
            or counts["total"]
            != counts["passed"] + counts["failed"] + counts["skipped"]
        ):
            raise ValueError("invalid counts")
        if any(
            type(evidence[k]) is not bool
            for k in (
                "problems",
                "outside_case_problems",
                "anchor_present",
                "anchor_passed",
            )
        ):
            raise ValueError("invalid flags")
        if evidence["problems"] != bool(
            counts["failed"] or evidence["outside_case_problems"]
        ):
            raise ValueError("inconsistent failure counts")
        if evidence["anchor_passed"] and (
            not evidence["anchor_present"] or not counts["passed"]
        ):
            raise ValueError("inconsistent authentication counts")
        if (
            evidence["anchor_present"]
            and not evidence["anchor_passed"]
            and not (counts["failed"] or counts["skipped"])
        ):
            raise ValueError("inconsistent authentication counts")
        return evidence, None
    except (ValueError, UnicodeError, RecursionError):
        return None, "invalid"


def authenticated(evidence, problem, status, job, outcome, started, exit_code):
    marked = started == "true"
    ran = True if marked and evidence else None
    if (
        not marked
        and not evidence
        and status not in ("passed", "failed", "skipped")
        and (
            outcome == "skipped"
            or (job == "skipped" and outcome not in ("success", "failure"))
            or (status in ("no_account", "blocked", "unknown") and outcome == "success")
        )
    ):
        ran = False
    counts = evidence["counts"] if evidence else None
    result = {
        "status": "failed",
        "reason": Reason.INVALID_LIVE_STATUS,
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

    if problem == "attempt":
        result.update(ran=None, counts=None)
        return finish("not_run", Reason.EARLIER_ATTEMPT_OUTPUTS)
    if problem == "unavailable":
        result.update(ran=None, counts=None)
        return finish("failed", Reason.MISSING_LIVE_EVIDENCE)
    if not marked and (evidence or status in ("passed", "failed", "skipped")):
        return finish("failed", Reason.EXECUTION_NOT_RECORDED)
    if not marked and not status and outcome == "failure" and problem == "missing":
        return finish("failed", Reason.LIVE_COLLECTION_ERROR)
    if job == "cancelled" and (marked or outcome not in ("success", "failure")):
        return finish("not_run", Reason.LIVE_JOB_CANCELLED)
    if job == "skipped" and (marked or outcome not in ("success", "failure")):
        return finish("not_run", Reason.LIVE_JOB_SKIPPED)
    if status in ("no_account", "blocked", "unknown"):
        if marked or problem != "missing" or outcome != "success":
            return finish("failed", Reason.CONTRADICTORY_LIVE_EVIDENCE)
        return finish(
            *{
                "no_account": ("no_account", Reason.NO_CI_ACCOUNT_CONFIGURED),
                "blocked": ("blocked", Reason.PROBE_REFUSED),
                "unknown": ("not_run", Reason.PROBE_STATE_UNKNOWN),
            }[status]
        )
    if not marked:
        if problem != "missing" or outcome != "skipped":
            result["ran"] = None
            return finish("failed", Reason.EXECUTION_NOT_RECORDED)
        return finish("not_run", Reason.LIVE_STEP_NOT_STARTED)
    if problem:
        return finish(
            "failed",
            {
                "missing": Reason.MISSING_LIVE_EVIDENCE,
                "invalid": Reason.INVALID_LIVE_EVIDENCE,
                "empty": Reason.EMPTY_LIVE_EVIDENCE,
            }[problem],
        )
    if status not in ("passed", "failed", "skipped"):
        return finish("failed", Reason.INVALID_LIVE_STATUS)
    if evidence["outside_case_problems"]:
        return finish("failed", Reason.LIVE_COLLECTION_ERROR)
    if exit_code != "0" or evidence["problems"] or status == "failed":
        return finish("failed", Reason.LIVE_SUITE_FAILED)
    if job != "success" or outcome != "success":
        return finish("failed", Reason.LIVE_JOB_INCOMPLETE)
    if counts["skipped"] == counts["total"]:
        return finish("not_run", Reason.ALL_LIVE_TESTS_SKIPPED)
    if not evidence["anchor_present"]:
        return finish("failed", Reason.REQUIRED_AUTHENTICATION_CHECK_MISSING)
    if not evidence["anchor_passed"]:
        return finish("not_run", Reason.AUTHENTICATION_CHECK_SKIPPED)
    if status != "passed":
        return finish("failed", Reason.CONTRADICTORY_LIVE_EVIDENCE)
    return finish(
        "passed",
        Reason.LIVE_SUITE_PARTIALLY_COVERED
        if counts["skipped"]
        else Reason.LIVE_SUITE_PASSED,
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
    job_attempts = {}
    for job, key, step in (
        ("reachability", "REACHABILITY_ATTEMPT", "reachability_step"),
        ("live_tests", "LIVE_ATTEMPT", "live_step"),
    ):
        value = metadata.get(key, "")
        job_attempts[job] = int(value) if re.fullmatch(r"[1-9][0-9]*", value) else None
        if job_attempts[job] != int(attempt):
            jobs[job] = jobs[step] = "unknown"
            if job == "reachability":
                rows, probe_problem = None, "attempt"
            else:
                evidence, live_problem = None, "attempt"
    # A failed probe exits for relied-on failures only. An otherwise valid report
    # with no such row contradicts that outcome; warning-only rows cannot explain it.
    if (
        not probe_problem
        and jobs["reachability_step"] == "failure"
        and not any(
            isinstance(row.get("source"), str)
            and row.get("source") in relied
            and row.get("name") in EXPECTED_CHECKS.get(row.get("source"), [])
            and row.get("relied_on") is True
            and row.get("ok") is False
            and (
                (row.get("status") == "blocked" and row.get("blocked") is True)
                or (row.get("status") == "changed" and row.get("blocked") is False)
            )
            for row in rows
        )
    ):
        probe_problem = "contradictory"
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
        "reason": Reason.NO_AUTHENTICATED_SUITE_CONFIGURED,
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
                status="failed",
                reason=Reason.INVALID_ALERT_POLICY,
                evidence_complete=False,
            )
            for check in source["reachability"]["checks"]:
                check.update(status="failed", reason=Reason.INVALID_ALERT_POLICY)
    expected = {name for names in EXPECTED_CHECKS.values() for name in names}
    return {
        "schema_version": 1,
        "generated_at": stamp,
        "run": {"id": run_id, "attempt": int(attempt), "head_sha": sha, "event": event},
        "expected_sources": list(EXPECTED_CHECKS),
        "policy_valid": policy_valid,
        "jobs": jobs,
        "job_attempts": job_attempts,
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
        "--live-counts", type=Path, default=Path("health-input/live/live-counts.json")
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
        "REACHABILITY_ATTEMPT",
        "PROBE_OUTCOME",
        "LIVE_RESULT",
        "LIVE_ATTEMPT",
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
            args.live_counts,
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
