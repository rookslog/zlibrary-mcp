"""Shared classification for bytes returned by document download endpoints.

Production transfers and the credential-free drift probe ask the same narrow
question: did the endpoint return file bytes, or an HTML error surface?  Keep
that policy here so a health check cannot report healthy for bytes production
will deterministically refuse (or reject a supported XML document that
production accepts).
"""

from __future__ import annotations

import re
from typing import Optional

_HTML_OPENING_RE = re.compile(
    rb"^(?:<!doctype\s+html|<html(?:\s|>)|<head(?:\s|>)|<body(?:\s|>)|<script(?:\s|>)|<!--)"
)


def signature_probe(signature: bytes) -> bytes:
    """Strip a UTF-8 BOM and leading whitespace for content classification."""
    return signature.removeprefix(b"\xef\xbb\xbf").lstrip()


def looks_like_html(signature: bytes) -> bool:
    """Recognize common leading HTML shapes independent of response headers."""
    return bool(_HTML_OPENING_RE.match(signature_probe(signature).lower()))


def payload_rejection(sample: bytes, content_type: str) -> Optional[str]:
    """Why a transfer must refuse this response, or ``None`` if it may proceed."""
    if not sample:
        return "an empty body"
    if "html" in (content_type or "").lower():
        return f"content-type {content_type!r}"
    if looks_like_html(sample):
        return "an HTML body rather than file bytes"
    return None
