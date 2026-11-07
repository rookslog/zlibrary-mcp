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
REPLACEMENT = "[filtered]"
SESSION_NAMES = (
    "remix_userkey|remix_userid|user_key|session_key|access_token|refresh_token|token"
)
SESSION_FIELD = re.compile(
    rf"""(?<![A-Za-z0-9])['\"]?(?:{SESSION_NAMES})['\"]?\s*[:=](?!\s*=)\s*""",
    re.IGNORECASE,
)
USER_OBJECT = re.compile(r"""['\"]user['\"]\s*:\s*\{""", re.IGNORECASE)
USER_ID = re.compile(r"""(?<![\w])['\"]id['\"]\s*:\s*""", re.IGNORECASE)
ESCAPED_FIELD = re.compile(
    rf"""\\+['\"](?:{SESSION_NAMES}|user|id)\\+['\"]""", re.IGNORECASE
)
QUOTED_VALUE = re.compile(r"""(?:[bB])?(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
BARE_VALUE = re.compile(r"[^\s;&,{}\[\]<>\"']+")


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
    for render in (repr, ascii):
        body = "".join(render(char)[1:-1] for char in value)
        forms.update((body, body.replace("'", "\\'"), body.replace('"', '\\"')))
    # A representation can itself be escaped by an XML/HTML writer.
    return (
        forms
        | {html.escape(form) for form in forms}
        | {html.escape(form, quote=False) for form in forms}
    )


def discovery_texts(text):
    # HTML encoders and assertion representations can wrap a structured value.
    # Decode only quote-wrapped named fields, preserving escapes inside ordinary
    # values. Bound the layers and refuse anything still ambiguous.
    seen = set()
    for source in (text, html.unescape(text)):
        for _ in range(4):
            if source not in seen:
                seen.add(source)
                yield source
            if not ESCAPED_FIELD.search(source):
                break
            source = re.sub(r"""\\(['"])""", r"\1", source)
        else:
            raise ValueError("ambiguous session output")


def field_value(text, start):
    remaining = text[start:]
    if remaining.startswith("="):
        raise ValueError("invalid session value")
    quoted = QUOTED_VALUE.match(remaining)
    if quoted:
        literal = quoted.group()
        try:
            try:
                value = json.loads(literal)
            except ValueError:
                value = ast.literal_eval(literal)
        except (ValueError, SyntaxError):
            raise ValueError("invalid session output") from None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        end = quoted.end()
    elif remaining.startswith(REPLACEMENT):
        value = REPLACEMENT
        end = len(REPLACEMENT)
    else:
        plain = BARE_VALUE.match(remaining)
        if plain is None:
            raise ValueError("unrecognized session output")
        value = plain.group()
        end = plain.end()
    # Delimiters must end the value, not hide an unparsed suffix. Quotes may
    # close an outer assertion string; query/cookie separators need another
    # assignment (or the end of the message).
    tail = remaining[end:]
    if tail[:1] in ("'", '"'):
        tail = tail[1:]
        if tail and tail[0] not in " \t\r\n;&,}]/>":
            raise ValueError("ambiguous session suffix")
    if tail[:1] in ("&", ";"):
        following = tail[1:].lstrip()
        if following and not re.match(r"[^\s;&=]+=", following):
            raise ValueError("ambiguous session suffix")
    # Do not accept a replacement prefix followed by a residual suffix.
    if not value or (
        end < len(remaining) and remaining[end] not in " \t\r\n;&,}]/>\"'"
    ):
        raise ValueError("ambiguous session output")
    return str(value)


def user_objects(text):
    for match in USER_OBJECT.finditer(text):
        start = match.end()
        depth, quote, escaped = 1, None, False
        end = start
        for end in range(start, len(text)):
            char = text[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
        yield text[start : end + 1]


def session_values(text):
    for decoded in discovery_texts(text):
        for match in SESSION_FIELD.finditer(decoded):
            value = field_value(decoded, match.end())
            if value != REPLACEMENT:
                yield value
        for body in user_objects(decoded):
            for match in USER_ID.finditer(body):
                value = field_value(body, match.end())
                if value != REPLACEMENT:
                    yield value


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


def captured_values(stamp):
    pending, complete, values = set(), set(), set()
    for line in stamp.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("invalid session capture")
        attempt = record.get("attempt")
        status = record.get("status")
        if not isinstance(attempt, str) or not attempt:
            raise ValueError("invalid session capture")
        if status == "pending" and set(record) == {"attempt", "status"}:
            if attempt in pending or attempt in complete:
                raise ValueError("duplicate session capture")
            pending.add(attempt)
        elif status == "complete" and set(record) == {"attempt", "status", "values"}:
            captured = record["values"]
            if (
                attempt not in pending
                or not isinstance(captured, list)
                or len(captured) != 2
                or not all(isinstance(value, str) for value in captured)
            ):
                raise ValueError("invalid session capture")
            pending.remove(attempt)
            complete.add(attempt)
            values.update(value for value in captured if value)
        else:
            raise ValueError("invalid session capture")
    if pending:
        raise ValueError("incomplete session capture")
    return values


def prepare_output(stamp) -> int:
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
        if stamp is not None and (had_log or tree is not None or stamp.exists()):
            values.update(captured_values(stamp))
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
            if (
                not had_log
                or any(pattern.encode("utf-8") in written for pattern in patterns)
                or any(session_values(written.decode("utf-8")))
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
        if written_tree is not None and any(
            value
            for text in xml_content(written_tree)
            for value in session_values(text)
        ):
            return fail()
    except (OSError, ET.ParseError, LookupError, UnicodeError, ValueError):
        return fail()
    return 0


def filter_output() -> int:
    stamp = None
    result = 1
    try:
        if path := os.environ.get("ZLIBRARY_TEST_SESSION_VALUES"):
            candidate = Path(path).absolute()
            workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
            if candidate.resolve().is_relative_to(workspace):
                raise ValueError("invalid session capture path")
            stamp = candidate
        result = prepare_output(stamp)
    except (OSError, ValueError):
        print(FAILURE)
    finally:
        if stamp is not None:
            try:
                stamp.unlink(missing_ok=True)
            except OSError:
                print(FAILURE)
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(filter_output())
