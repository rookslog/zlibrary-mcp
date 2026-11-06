#!/usr/bin/env python3
"""Keep test output out of uploaded files."""

import os
from pathlib import Path
import xml.etree.ElementTree as ET


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
        yield element.text or ""
        yield element.tail or ""
    yield "".join(tree.getroot().itertext())


def filter_output() -> int:
    values = sorted(
        {
            value
            for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD")
            if (value := os.environ.get(name))
        },
        key=len,
        reverse=True,
    )

    def redact(text):
        for value in values:
            text = text.replace(value, "[filtered]")
        return text

    log = Path("live.log")
    xml = Path("live.xml")
    try:
        try:
            data = log.read_bytes()
        except FileNotFoundError:
            had_log = False
        else:
            had_log = True
            for value in values:
                data = data.replace(value.encode("utf-8"), b"[filtered]")
            log.write_bytes(data)
        tree = read_xml(xml)
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
                return 1
        else:
            if not had_log or any(value.encode("utf-8") in written for value in values):
                return 1
        written_tree = read_xml(xml)
        if (written_tree is None) != (tree is None):
            return 1
        if written_tree is not None and any(
            value in text for text in xml_content(written_tree) for value in values
        ):
            return 1
    except (OSError, ET.ParseError, LookupError, UnicodeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(filter_output())
