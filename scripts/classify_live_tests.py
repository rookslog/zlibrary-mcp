#!/usr/bin/env python3
"""Classify live pytest results as passed, skipped or failed.

The workflow skips the live suite when its reachability probe reports a wall;
any errors from a suite that actually ran remain actionable.

The workflow captures pytest's exit code and uses these outputs to fail the job
only for real failures. This command itself succeeds after emitting a classified
failure; malformed/missing evidence is a failure, never a block or pass.
"""

import argparse
import os
from pathlib import Path
import xml.etree.ElementTree as ET


class NoDeclarations(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("XML declarations are unsupported")


def classify(exit_code: int, report: Path) -> dict[str, object]:
    try:
        root = ET.parse(report, parser=ET.XMLParser(target=NoDeclarations())).getroot()
    except (OSError, ET.ParseError, LookupError, UnicodeError, ValueError):
        return {
            "status": "failed",
            "blocked": False,
            "detail": "Missing/invalid live JUnit report",
        }

    cases = list(root.iter("testcase"))
    problems = [p for p in root.iter() if p.tag in ("error", "failure")]
    skipped = sum(case.find("skipped") is not None for case in cases)
    passed = sum(
        not any(child.tag in ("skipped", "error", "failure") for child in case)
        for case in cases
    )
    # Interruptions, collection errors, usage errors and empty suites cannot be
    # explained away by a block seen elsewhere in a partial report.
    if exit_code == 0 and cases and not problems:
        status = "skipped" if skipped == len(cases) else "passed"
    else:
        status = "failed"
    detail = (
        f"Live suite: {status}; {len(problems)} failure(s); pytest exit {exit_code}."
        f" {passed} passed; {skipped} skipped."
    )
    if status == "skipped":
        detail += " No authenticated coverage: all testcases skipped."
    return {"status": status, "blocked": False, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--junitxml", type=Path, required=True)
    args = parser.parse_args()
    result = classify(args.exit_code, args.junitxml)
    print(result["detail"])
    if result["status"] == "skipped":
        print(
            "::warning::Live suite produced no authenticated coverage: all testcases skipped."
        )
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"status={result['status']}\n")
            handle.write(f"blocked={str(result['blocked']).lower()}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(str(result["detail"]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
