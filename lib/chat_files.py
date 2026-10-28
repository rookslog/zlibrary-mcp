"""Offline PDF planning/excerpts/rendering and EPUB navigation. JSON stdin/stdout.

The caller lends a checked, open source descriptor as fd 3. Standalone tests use
PdfBuilder directly. No extracted page text leaves this process.
"""

import base64
from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import struct
import sys
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile
import zlib

VERSION = 1

# Bound metadata before ZipFile allocates the central directory, then bound each
# decompression and XML tree. Chapter bodies, images and external DTDs are unread.
EPUB_MAX_ARCHIVE = 128 * 1024 * 1024
EPUB_MAX_MEMBERS = 4096
EPUB_MAX_XML = 1024 * 1024
EPUB_MAX_DEPTH = 64
EPUB_MAX_SECTIONS = 4096
OPF = "{http://www.idpf.org/2007/opf}"
XHTML = "{http://www.w3.org/1999/xhtml}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"


EPUB_PARSE_ERRORS = (
    OSError,
    ValueError,
    LookupError,
    RuntimeError,
    EOFError,
    expat.ExpatError,
    zipfile.BadZipFile,
    zlib.error,
    NotImplementedError,
)


def epub_path(base, href):
    """Resolve a local archive URI, never a host path or external URL."""
    uri = urlsplit(href)
    name = unquote(uri.path, errors="strict")
    if (
        uri.scheme
        or uri.netloc
        or uri.query
        or not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\0" in name
    ):
        raise ValueError("invalid EPUB path")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), name))
    if name in (".", "..") or name.startswith("../"):
        raise ValueError("EPUB path escapes archive")
    return name


def epub_xml(data):
    # Expat handles UTF-8/16 declarations, so entity rejection cannot be bypassed
    # by a different XML encoding. External DTD declarations are allowed (common
    # in NCX); parameter entities are disabled and no resolver fetches them.
    parser = expat.ParserCreate(namespace_separator="}")
    builder = ET.TreeBuilder()
    depth, count = 0, 0

    def qualified(name):
        return "{" + name if "}" in name else name

    def start(name, attrs):
        nonlocal depth, count
        depth += 1
        count += 1
        if depth > EPUB_MAX_DEPTH or count > 20000:
            raise ValueError("EPUB XML exceeds limits")
        builder.start(qualified(name), {qualified(k): v for k, v in attrs.items()})

    def end(name):
        nonlocal depth
        builder.end(qualified(name))
        depth -= 1

    def reject(*_args):
        raise ValueError("EPUB entities are unsupported")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(data, True)
    return builder.close()


def epub_outline(parent, ncx=False, document_name="", targets=None):
    total = 0

    def collect(parent, depth=1):
        nonlocal total
        if depth > 32:
            raise ValueError("EPUB outline exceeds depth limit")
        result = []
        tag = NCX + "navPoint" if ncx else XHTML + "li"
        for item in parent.findall(tag):
            label = (
                item.find(NCX + "navLabel/" + NCX + "text")
                if ncx
                else next(
                    (n for n in item if n.tag in (XHTML + "a", XHTML + "span")), None
                )
            )
            title = (
                " ".join("".join(label.itertext()).split()) if label is not None else ""
            )
            total += 1
            if len(title) > 2000 or total > EPUB_MAX_SECTIONS:
                raise ValueError("invalid or excessive EPUB outline")
            child = item if ncx else item.find(XHTML + "ol")
            children = (
                collect(child, depth + 1) if child is not None and len(child) else []
            )
            link = item.find(NCX + "content") if ncx else label
            href = link.get("src" if ncx else "href") if link is not None else None
            valid = True
            if targets is not None and href is not None:
                try:
                    # A fragment-only URI names the navigation document itself.
                    target = (
                        epub_path(
                            document_name,
                            href.split("#", 1)[0] or posixpath.basename(document_name),
                        )
                        if href
                        else None
                    )
                    valid = target in targets
                except (ValueError, UnicodeError):
                    valid = False
            if (
                not title
                or not valid
                or (targets is not None and href is None and not children)
            ):
                result.extend(children)
            else:
                result.append({"title": title, "children": children})
        return result

    def number(nodes, prefix="", level=1):
        for index, node in enumerate(nodes, 1):
            identifier = prefix + str(index)
            node.update(id=identifier, level=level)
            number(node["children"], identifier + ".", level + 1)
        return nodes

    return number(collect(parent))


def epub_sections(stream):
    """Read file-provided navigation only; bad or over-limit input yields no tree."""
    try:
        stream.seek(0, 2)
        size = stream.tell()
        if not 22 <= size <= EPUB_MAX_ARCHIVE:
            return []
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            return []
        _, disk, directory_disk, disk_count, count, directory_size, _, comment = (
            struct.unpack_from("<4s4H2LH", tail, offset)
        )
        if (
            disk
            or directory_disk
            or disk_count != count
            or count > EPUB_MAX_MEMBERS
            or directory_size > 2 * 1024 * 1024
            or len(tail) - offset != 22 + comment
            or tail[max(0, offset - 20) : offset - 16] == b"PK\x06\x07"
        ):
            return []  # Also rejects ZIP64 and multi-volume archives.
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            names = {item.filename for item in members}
            if len(members) != count or len(names) != count:
                return []
            # Some older ZIP writers stored UTF-8 bytes without setting bit 11.
            # Retain ZipInfo for the read, so local-header validation still uses
            # the original decoded ZIP name. Refuse ambiguous aliases on lookup.
            lookup = {}
            for item in members:
                aliases = {item.filename}
                if not item.flag_bits & 0x800:
                    try:
                        aliases.add(item.filename.encode("cp437").decode("utf-8"))
                    except UnicodeError:
                        pass
                for alias in aliases:
                    lookup.setdefault(alias, []).append(item)

            def read_xml(name):
                matches = lookup.get(name, [])
                if len(matches) != 1:
                    raise ValueError("missing or ambiguous EPUB member")
                info = matches[0]
                member = info.filename
                if (
                    not member
                    or member.startswith("/")
                    or "\\" in member
                    or ":" in member
                    or "\0" in info.orig_filename
                    or ".." in member.split("/")
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ValueError("unsafe EPUB member")
                if (
                    info.file_size > EPUB_MAX_XML
                    or info.flag_bits & 1
                    or info.compress_type
                    not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    raise ValueError(
                        "EPUB XML exceeds limit or has unsupported compression/encryption"
                    )
                with archive.open(info) as source:
                    data = source.read(EPUB_MAX_XML + 1)
                if len(data) > EPUB_MAX_XML:
                    raise ValueError("EPUB XML exceeds limit")
                return epub_xml(data)

            container = read_xml("META-INF/container.xml")
            root = container.find(
                "{urn:oasis:names:tc:opendocument:xmlns:container}rootfiles/{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
            )
            if root is None:
                return []
            package_name = epub_path("", root.attrib["full-path"])
            package = read_xml(package_name)
            items = package.findall(OPF + "manifest/" + OPF + "item")
            targets = set()
            for item in items:
                try:
                    targets.add(epub_path(package_name, item.attrib["href"]))
                except (ValueError, LookupError):
                    continue
            navigation = next(
                (n for n in items if "nav" in n.get("properties", "").split()), None
            )
            if navigation is not None:
                try:
                    document_name = epub_path(package_name, navigation.attrib["href"])
                    document = read_xml(document_name)
                    toc = next(
                        (
                            n
                            for n in document.iter(XHTML + "nav")
                            if "toc"
                            in n.get("{http://www.idpf.org/2007/ops}type", "").split()
                        ),
                        None,
                    )
                    ordered = toc.find(XHTML + "ol") if toc is not None else None
                    if ordered is not None:
                        sections = epub_outline(
                            ordered, document_name=document_name, targets=targets
                        )
                        if sections:
                            return sections
                except EPUB_PARSE_ERRORS:
                    pass  # A broken nav must not suppress an independent NCX.
            spine = package.find(OPF + "spine")
            toc_id = spine.get("toc") if spine is not None else None
            navigation = next(
                (n for n in items if toc_id and n.get("id") == toc_id), None
            )
            if navigation is None:
                navigation = next(
                    (
                        n
                        for n in items
                        if n.get("media-type") == "application/x-dtbncx+xml"
                    ),
                    None,
                )
            if navigation is not None:
                document_name = epub_path(package_name, navigation.attrib["href"])
                document = read_xml(document_name)
                toc = document.find(NCX + "navMap")
                if toc is not None:
                    return epub_outline(
                        toc, ncx=True, document_name=document_name, targets=targets
                    )
    except EPUB_PARSE_ERRORS:
        pass
    return []


def parse_pages(value, count):
    if not isinstance(value, str) or not re.fullmatch(
        r"[1-9][0-9]*(?:-[1-9][0-9]*)?", value
    ):
        raise ValueError(
            "pages must be a PDF page number or inclusive range, such as 47-66"
        )
    numbers = list(map(int, value.split("-")))
    start, end = numbers[0], numbers[-1]
    if not 1 <= start <= end <= count:
        raise ValueError("pages are outside the PDF page range")
    return start, end


def page_range(start, end):
    return str(start) if start == end else f"{start}-{end}"


def printed_range(doc, start, end):
    if not doc.get_page_labels():
        return None
    first, last = doc[start - 1].get_label(), doc[end - 1].get_label()
    return first if start == end else f"{first}-{last}"


def copy_labels(src, dst, start, end):
    rules = src.get_page_labels()
    if not rules:
        return
    first = start - 1
    active = None
    result = []
    for rule in rules:
        if rule["startpage"] <= first:
            active = rule
        elif rule["startpage"] < end:
            result.append({**rule, "startpage": rule["startpage"] - first})
    if active is not None:
        rule = {**active, "startpage": 0}
        rule["firstpagenum"] = (
            active.get("firstpagenum", 1) + first - active["startpage"]
        )
        result.insert(0, rule)
    dst.set_page_labels(result)


class PdfBuilder:
    def __init__(self, document, cache, key):
        self.document = document
        self.cache = Path(cache)
        self.key = key
        self.sizes = {}
        self.measurements = {}
        self.recent = OrderedDict()

    def build(self, start, end):
        import fitz

        key = (start, end)
        if key in self.recent:
            self.recent.move_to_end(key)
            return self.recent[key]
        out = fitz.open()
        try:
            out.insert_pdf(self.document, from_page=start - 1, to_page=end - 1)
            copy_labels(self.document, out, start, end)
            original = out.tobytes(garbage=4, deflate=True, no_new_id=True)
            sample = sorted({0, len(out) // 2, len(out) - 1})
            before = [out[i].get_text() for i in sample]
            try:
                out.subset_fonts()
                unchanged = before == [out[i].get_text() for i in sample]
                subset = out.tobytes(garbage=4, deflate=True, no_new_id=True)
            except Exception:
                unchanged, subset = False, original
            kept = unchanged and len(subset) < len(original)
            data = subset if kept else original
            facts = {
                "pdf_pages": page_range(start, end),
                "original_bytes": len(original),
                "subset_bytes": len(subset),
                "text_unchanged": unchanged,
                "kept": kept,
            }
            self.sizes[key] = len(data)
            self.measurements[key] = facts
            self.recent[key] = (data, facts)
            while len(self.recent) > 3:
                self.recent.popitem(last=False)
            return data, facts
        finally:
            out.close()

    def size(self, start, end):
        key = (start, end)
        if key not in self.sizes:
            self.build(start, end)
        return self.sizes[key]

    def save(self, start, end):
        data, facts = self.build(start, end)
        # Hash binding makes a cached piece immutable across overlapping calls.
        digest = hashlib.sha256(data).hexdigest()
        target = self.cache / f"piece-{digest}.pdf"
        if not target.exists():
            atomic_write(target, data)
        return {
            "piece": target.name,
            "bytes": len(data),
            "sha256": digest,
            "subsetting": facts,
        }


def outline_tree(toc, count):
    roots, stack = [], []
    previous = 1
    for raw in toc:
        level, title, page = raw[:3]
        # Clamp bad destinations and enforce monotonicity; skipped levels become
        # direct children. Equal destinations (parent and first child) are valid.
        start = max(previous, min(count, max(1, int(page))))
        previous = start
        level = max(1, min(int(level), len(stack) + 1))
        while len(stack) >= level:
            stack.pop()
        siblings = stack[-1]["children"] if stack else roots
        identifier = f"{stack[-1]['id']}." if stack else ""
        node = {
            "id": identifier + str(len(siblings) + 1),
            "title": str(title),
            "level": level,
            "_start": start,
            "children": [],
        }
        siblings.append(node)
        stack.append(node)

    def finish(nodes, parent_end):
        # A section ends on the page where the next one starts: a chapter
        # often ends on that page, so one page of overlap keeps its last lines.
        for i, node in enumerate(nodes):
            end = nodes[i + 1]["_start"] if i + 1 < len(nodes) else parent_end
            node["_end"] = max(node["_start"], min(parent_end, end))
            node["pdf_pages"] = page_range(node["_start"], node["_end"])
            finish(node["children"], node["_end"])

    finish(roots, count)
    return roots


def walk_sections(nodes):
    for node in nodes:
        yield node
        yield from walk_sections(node["children"])


def fit_end(builder, start, end, budget, boundaries=()):
    # Interpolate between sizes, then close the integer gap. With a PdfBuilder
    # (sends) every size is a real build, which avoids repeated large
    # font-subsetting passes; with a SizeEstimate (the map) each is an estimate.
    first_size = builder.size(start, start)
    if first_size > budget:
        return start - 1
    last_size = builder.size(start, end)
    low, high = start, end
    low_size, high_size = first_size, last_size
    if last_size <= budget:
        return end
    else:
        while high - low > 1:
            fraction = (budget - low_size) / max(1, high_size - low_size)
            mid = max(low + 1, min(high - 1, low + int((high - low) * fraction)))
            size = builder.size(start, mid)
            if size <= budget:
                low, low_size = mid, size
            else:
                high, high_size = mid, size
    candidates = [
        b
        for b in boundaries
        if start <= b <= low and b - start + 1 >= 0.6 * (low - start + 1)
    ]
    if candidates:
        chosen = max(candidates)
        if builder.size(start, chosen) <= budget:
            return chosen
    return low


# Lane P's (b0) byte model plus serialisation overhead, checked on five books
# (2026-10-23) against exact builds: no false fits. It overestimates born-digital
# books (subsetting shrinks fonts), hence parts below the cap; the margin covers
# small underestimates. The map uses only this; sends weigh exactly.
MARGIN = 1.05
# Bytes each written object costs beyond its text and stream ("N 0 obj",
# "endobj", the xref entry; "stream"/"endstream" and /Length for streams).
# Least-squares fit over 20 written excerpts of 5 books: 38.3 B per object and
# 19.3 B per stream, residuals within 35 B per excerpt. Rounded up.
OBJECT_OVERHEAD = 39
STREAM_OVERHEAD = 20
# Page attributes a page may inherit from its page-tree ancestors (PDF 32000-1,
# 7.7.3.4). insert_pdf copies the inherited value into the excerpt's page, so
# the estimate must too. Rotate is a number and carries no objects.
INHERITED = ("Resources", "MediaBox", "CropBox")
REFERENCE = re.compile(rb"(\d+) 0 R")
BACK_LINKS = re.compile(
    r"/(Parent|P|Dest|D|Prev|Next|First|Last|StructParent|B)\s+\d+ 0 R"
)


def is_page(text):
    text = text.replace(" ", "")
    return text.startswith("<<") and "/Type/Page" in text and "/Type/Pages" not in text


class SizeEstimate:
    """Estimated excerpt bytes: the union of the objects each page drags along
    (its reference closure, not following back-links or other pages), weighed
    raw with their serialisation overhead, plus a fixed per-page cost, times
    MARGIN."""

    def __init__(self, document):
        self.document = document
        self.weights = {}
        self.references = {}
        self.ancestors = {}
        self.inline = 0
        self.pages = [self._closure(page.xref) for page in document]

    def _object(self, xref):
        try:
            return self.document.xref_object(xref, compressed=True)
        except Exception:
            return ""

    def _weight(self, xref):
        # Negative ids are inherited values written inline into one page.
        if xref not in self.weights:
            weight = len(self._object(xref)) + OBJECT_OVERHEAD
            try:
                if self.document.xref_is_stream(xref):
                    weight += STREAM_OVERHEAD
                    weight += len(self.document.xref_stream_raw(xref) or b"")
            except Exception:
                pass
            self.weights[xref] = weight
        return self.weights[xref]

    def _refs(self, xref):
        if xref not in self.references:
            text = self._object(xref)
            page = is_page(text)
            if page:
                text = re.sub(r"/Parent\s+\d+ 0 R", "", text)
            text = BACK_LINKS.sub("", text)
            self.references[xref] = (
                page,
                [int(m) for m in REFERENCE.findall(text.encode())],
            )
        return self.references[xref]

    def _key(self, xref, key):
        try:
            kind, value = self.document.xref_get_key(xref, key)
        except Exception:
            return None
        return None if kind == "null" else value

    def _inherited(self, page_xref, key):
        # The nearest ancestor's value, walking up /Parent only: siblings and
        # the rest of the page tree are never part of the closure.
        node, visited = page_xref, set()
        while node and node not in visited:
            visited.add(node)
            if (node, key) in self.ancestors:
                return self.ancestors[(node, key)]
            value = self._key(node, key) if node != page_xref else None
            if value is not None:
                break
            parent = self._key(node, "Parent")
            node = int(parent.split()[0]) if parent and parent.endswith(" R") else None
        else:
            value = None
        for seen_node in visited - {page_xref}:
            self.ancestors[(seen_node, key)] = value
        return value

    def _closure(self, page_xref):
        seen = {page_xref}
        stack = list(self._refs(page_xref)[1])
        for key in INHERITED:
            if self._key(page_xref, key) is not None:
                continue
            value = self._inherited(page_xref, key)
            if value is None:
                continue
            stack.extend(int(m) for m in REFERENCE.findall(value.encode()))
            if not value.endswith(" R"):
                # An inline value is written into this page's dictionary.
                self.inline -= 1
                self.weights[self.inline] = len(value)
                seen.add(self.inline)
        while stack:
            xref = stack.pop()
            if xref in seen or xref <= 0:
                continue
            page, refs = self._refs(xref)
            if page:
                continue  # a link target: insert_pdf does not copy other pages
            seen.add(xref)
            stack.extend(refs)
        return seen

    def _scaled(self, weight, pages):
        return math.ceil((weight + 300 * pages) * MARGIN)

    def size(self, start, end):
        union = set().union(*self.pages[start - 1 : end])
        return self._scaled(sum(self._weight(x) for x in union), end - start + 1)

    def worst_window(self, n):
        # Slide an n-page window over the book, keeping reference counts, so
        # every window is estimated, not a sample of them.
        counts, weight, worst = {}, 0, 0
        for i, objects in enumerate(self.pages):
            for x in objects:
                counts[x] = counts.get(x, 0) + 1
                if counts[x] == 1:
                    weight += self._weight(x)
            if i >= n:
                for x in self.pages[i - n]:
                    counts[x] -= 1
                    if not counts[x]:
                        weight -= self._weight(x)
            if i >= n - 1:
                worst = max(worst, self._scaled(weight, n))
        return worst


def safe_window(estimate, budget):
    # The largest n for which every n-page window is estimated to fit. The
    # worst window only grows with n, so a binary search is exact.
    low, high = 0, len(estimate.pages) + 1
    while high - low > 1:
        mid = (low + high) // 2
        if estimate.worst_window(mid) <= budget:
            low = mid
        else:
            high = mid
    return low


def pdf_map(doc, budget):
    # Every size here is an estimate (with MARGIN); share_book_file weighs exactly.
    count = len(doc)
    if count == 0:
        raise ValueError(
            "The request was valid; the PDF helper failed: PDF has no pages"
        )
    estimate = SizeEstimate(doc)
    tree = outline_tree(doc.get_toc(), count)
    for node in walk_sections(tree):
        start, end = node["_start"], node["_end"]
        label = printed_range(doc, start, end)
        if label is not None:
            node["printed_pages"] = label
        size = estimate.size(start, end)
        node.update(estimated_bytes=size, fits=size <= budget)
    boundaries = {n["_start"] - 1 for n in walk_sections(tree)}
    parts = []
    unshareable = []
    start = 1
    while start <= count:
        end = fit_end(estimate, start, count, budget, boundaries)
        if end < start:
            unshareable.append(
                {
                    "pdf_pages": str(start),
                    "estimated_bytes": estimate.size(start, start),
                    "fits": False,
                    "use": "get_page_image",
                }
            )
            start += 1
            continue
        part = {
            "pdf_pages": page_range(start, end),
            "estimated_bytes": estimate.size(start, end),
        }
        label = printed_range(doc, start, end)
        if label is not None:
            part["printed_pages"] = label
        section = next((n for n in walk_sections(tree) if n["_start"] == start), None)
        if section:
            part["starts_at_section"] = section["id"]
        parts.append(part)
        start = end + 1
    safe = safe_window(estimate, budget)
    rules = doc.get_page_labels()
    labels = {"present": bool(rules), "ranges": []}
    if rules:
        for i, rule in enumerate(rules):
            start = rule["startpage"] + 1
            end = rules[i + 1]["startpage"] if i + 1 < len(rules) else count
            if start <= end:
                labels["ranges"].append(
                    {
                        "pdf": page_range(start, end),
                        "printed": printed_range(doc, start, end),
                    }
                )
    else:
        labels["note"] = (
            "printed page numbers may be offset from PDF page numbers; check one page image to find the offset"
        )
    indices = (
        sorted(
            {round(i * (count - 1) / min(19, count - 1)) for i in range(min(20, count))}
        )
        if count > 1
        else [0]
    )
    text_pages = sum(bool(doc[i].get_text().strip()) for i in indices)
    for node in walk_sections(tree):
        node.pop("_start")
        node.pop("_end")
    return {
        "pages": count,
        "page_labels": labels,
        "text_layer": text_pages / len(indices),
        "text_layer_sample_pages": len(indices),
        "sections": tree,
        "suggested_parts": parts,
        "unshareable_pages": unshareable,
        "safe_window_pages": safe,
        "safe_window_estimated": True,
        "size_estimate_margin": round(MARGIN - 1, 2),
    }


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render(doc, page_number, dpi):
    import fitz

    if not 1 <= page_number <= len(doc):
        raise ValueError("page is outside the PDF page range")
    page = doc[page_number - 1]
    longest = max(page.rect.width, page.rect.height)
    if not math.isfinite(longest) or longest <= 0:
        raise ValueError(
            "The request was valid; the PDF helper failed: invalid PDF page dimensions"
        )
    scale = min(max(50, min(200, dpi)) / 72, 1598 / longest)
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
    )
    data, mime = pix.tobytes("png"), "image/png"
    if len(data) > 3 * 1024 * 1024:
        data, mime = pix.tobytes("jpeg", jpg_quality=80), "image/jpeg"
    if len(data) > 3 * 1024 * 1024 or max(pix.width, pix.height) > 1600:
        raise ValueError(
            "The request was valid; the PDF helper failed: PDF render exceeded the image byte limit"
        )
    return {
        "page": page_number,
        "pages": len(doc),
        "printed_page": page.get_label() if doc.get_page_labels() else None,
        "width": pix.width,
        "height": pix.height,
        "bytes": len(data),
        "mimeType": mime,
        "data": base64.b64encode(data).decode("ascii"),
    }


def quiet_mupdf():
    # MuPDF prints repair messages ("MuPDF error: syntax error: invalid key in
    # dict") to stdout, which corrupts the JSON reply. Keep them in its store.
    import fitz

    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    fitz.TOOLS.reset_mupdf_warnings()


def mupdf_messages():
    import fitz

    return len(
        [line for line in fitz.TOOLS.mupdf_warnings().splitlines() if line.strip()]
    )


def main(command, args):
    if command == "epub-map":
        with os.fdopen(os.dup(3), "rb") as source:
            return {"sections": epub_sections(source)}

    import fitz

    cache = Path(args["cache"])
    if cache.is_symlink() or not cache.is_dir() or cache.resolve() != cache:
        raise ValueError(
            "The request was valid; the PDF helper failed: cache directory is unavailable"
        )
    quiet_mupdf()
    with fitz.open("/proc/self/fd/3") as doc:
        builder = PdfBuilder(doc, cache, args["key"])
        if command == "render":
            return render(doc, args["page"], args.get("dpi", 100))
        if command == "map":
            result = pdf_map(doc, args["max_bytes"])
            # Non-empty MuPDF warning lines while opening/mapping, not distinct
            # repairs. No "repaired" flag: doc.is_repaired is true only
            # for a rebuilt xref, and false on files MuPDF works around
            # object by object (the malformed Johnson PDF, 2026-10-23).
            result["repair_messages"] = mupdf_messages()
            return result
        if command != "excerpt":
            raise ValueError("unknown PDF operation")
        start, end = parse_pages(args["pages"], len(doc))
        requested_end = end
        boundaries = {
            n["_start"] - 1
            for n in walk_sections(outline_tree(doc.get_toc(), len(doc)))
        }
        if builder.size(start, end) > args["max_bytes"]:
            end = fit_end(builder, start, end, args["max_bytes"], boundaries)
        if end < start:
            return {
                "too_large_page": start,
                "message": f"PDF page {start} alone is too large; use get_page_image for it",
            }
        result = builder.save(start, end)
        result["pdf_pages"] = page_range(start, end)
        label = printed_range(doc, start, end)
        if label is not None:
            result["printed_pages"] = label
        if end < requested_end:
            result["remaining"] = page_range(end + 1, requested_end)
        return result


if __name__ == "__main__":
    # Only the JSON reply reaches the real stdout; anything else written to fd 1
    # (by MuPDF or a library) goes to stderr, which the server discards.
    reply = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        output = main(sys.argv[1], json.load(sys.stdin))
    except ValueError as exc:
        output = {"error": str(exc)}
    except ImportError:
        output = {
            "error": "The request was valid; the PDF helper failed: PyMuPDF is missing; set ZLIBRARY_PDF_PYTHON to an interpreter with PyMuPDF"
        }
    except Exception:
        output = {
            "error": "The request was valid; the PDF helper failed: PDF helper could not read or process this file"
        }
    print(json.dumps(output, separators=(",", ":")), file=reply)
    reply.close()
