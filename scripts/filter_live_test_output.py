#!/usr/bin/env python3
"""Keep test output out of uploaded files."""

import html
import os
from pathlib import Path
import sys


def filter_output() -> int:
    values = {
        value
        for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD")
        if (value := os.environ.get(name))
    }
    try:
        patterns = {
            variant.encode("utf-8")
            for value in values
            for variant in (value, html.escape(value), html.escape(value, quote=False))
        }
        for name in ("live.log", "live.xml"):
            path = Path(name)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            for pattern in sorted(patterns, key=len, reverse=True):
                data = data.replace(pattern, b"[filtered]")
            if any(pattern in data for pattern in patterns):
                raise ValueError("output filtering incomplete")
            path.write_bytes(data)
    except (OSError, UnicodeError, ValueError):
        print("Could not prepare test output for upload.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(filter_output())
