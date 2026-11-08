#!/usr/bin/env python3
"""Write counts-only evidence from local live-test output."""

import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

MAX_INPUT_BYTES = 5 * 1024 * 1024


class NoDeclarations(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("unsupported XML declaration")


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


def summarize(path):
    data, problem = read_input(path)
    if problem:
        return None, problem
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=NoDeclarations()))
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
    except (ET.ParseError, LookupError, UnicodeError, ValueError, RecursionError):
        return None, "invalid"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, default=Path("live.xml"))
    parser.add_argument("--output", type=Path, default=Path("live-counts.json"))
    args = parser.parse_args()
    evidence, problem = summarize(args.junitxml)
    try:
        args.output.write_text(
            json.dumps({"schema_version": 1, "evidence": evidence, "problem": problem})
            + "\n"
        )
    except OSError:
        print("Could not write live-test counts.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
