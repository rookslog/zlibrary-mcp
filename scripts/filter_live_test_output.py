#!/usr/bin/env python3
"""Replace configured and login-session values in output prepared for upload."""

import ast
import base64
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, quote_plus
import xml.etree.ElementTree as ET


FAILURE = "Could not prepare test output for upload."
# Session fields returned by login or written as cookie/header dictionaries.
SESSION_FIELD = re.compile(
    r"""["']?(remix_userkey|remix_userid|user_key|session_key|access_token|refresh_token|token)["']?\s*[:=]\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""",
    re.IGNORECASE,
)
USER_ID = re.compile(
    r"""["']user["']\s*:\s*\{\s*["']id["']\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\d+)"""
)


def encoded_values(value):
    forms = {
        value,
        html.escape(value),
        html.escape(value, quote=False),
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        repr(value)[1:-1],
        ascii(value)[1:-1],
        quote(value, safe=""),
        quote_plus(value),
        base64.b64encode(value.encode("utf-8")).decode("ascii"),
    }
    # A representation can itself be escaped by an XML/HTML writer.
    return (
        forms
        | {html.escape(form) for form in forms}
        | {html.escape(form, quote=False) for form in forms}
    )


def session_values(text):
    for decoded in (text, html.unescape(text)):
        for pattern in (SESSION_FIELD, USER_ID):
            for match in pattern.finditer(decoded):
                literal = match.group(match.lastindex)
                try:
                    try:
                        value = json.loads(literal)
                    except ValueError:
                        value = ast.literal_eval(literal)
                except (ValueError, SyntaxError):
                    raise ValueError("invalid session output") from None
                if str(value):
                    yield str(value)


def read_xml(path):
    try:
        return ET.parse(path)
    except FileNotFoundError:
        return None
    except (ET.ParseError, LookupError, UnicodeError, ValueError):
        path.unlink(missing_ok=True)
        raise ValueError("invalid test output") from None


def xml_content(tree):
    for element in tree.iter():
        yield element.tag
        yield from element.attrib
        yield from element.attrib.values()
    yield "".join(tree.getroot().itertext())


def filter_output() -> int:
    def fail():
        print(FAILURE)
        return 1

    configured = [
        os.environ.get(name, "") for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD")
    ]
    log = Path("live.log")
    xml = Path("live.xml")
    try:
        try:
            data = log.read_bytes()
        except FileNotFoundError:
            had_log = False
            data = b""
        else:
            had_log = True
        tree = read_xml(xml)
        if (had_log or tree is not None) and not all(configured):
            return fail()
        values = {value for value in configured if value}
        values.update(session_values(data.decode("utf-8")))
        if tree is not None:
            for text in xml_content(tree):
                values.update(session_values(text))
        patterns = sorted(
            {form for value in values for form in encoded_values(value)},
            key=len,
            reverse=True,
        )

        def redact(text):
            for pattern in patterns:
                text = text.replace(pattern, "[filtered]")
            return text

        if had_log:
            log.write_bytes(redact(data.decode("utf-8")).encode("utf-8"))
        if tree is not None:
            for element in tree.iter():
                if element.text is not None:
                    element.text = redact(element.text)
                if element.tail is not None:
                    element.tail = redact(element.tail)
                for name, value in element.attrib.items():
                    element.set(name, redact(value))
            xml.write_bytes(
                ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
            )
        try:
            written = log.read_bytes()
        except FileNotFoundError:
            if had_log:
                return fail()
        else:
            if not had_log or any(
                pattern.encode("utf-8") in written for pattern in patterns
            ):
                return fail()
        written_tree = read_xml(xml)
        if (written_tree is None) != (tree is None):
            return fail()
        if written_tree is not None and any(
            pattern in text
            for text in xml_content(written_tree)
            for pattern in patterns
        ):
            return fail()
    except (OSError, ET.ParseError, LookupError, UnicodeError, ValueError):
        return fail()
    return 0


if __name__ == "__main__":
    raise SystemExit(filter_output())
