"""Offline PDF planning/excerpts/rendering. JSON stdin/stdout; writes only private cache.

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
import re
import sys
import uuid

VERSION = 1


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
        low = end
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


# Lane P's (b0) byte model, measured on five books (2026-10-23): no false fits;
# over by up to ~29 % on born-digital books (subsetting shrinks fonts), under by
# at most 4 %. Hence the margin. The map uses only this; sends weigh exactly.
MARGIN = 1.05
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
    raw, plus a fixed per-page cost, times MARGIN."""

    def __init__(self, document):
        self.document = document
        self.weights = {}
        self.references = {}
        self.pages = [self._closure(page.xref) for page in document]

    def _object(self, xref):
        try:
            return self.document.xref_object(xref, compressed=True)
        except Exception:
            return ""

    def _weight(self, xref):
        if xref not in self.weights:
            weight = len(self._object(xref))
            try:
                if self.document.xref_is_stream(xref):
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

    def _closure(self, page_xref):
        seen = {page_xref}
        stack = list(self._refs(page_xref)[1])
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
        raise ValueError("PDF has no pages")
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
        raise ValueError("invalid PDF page dimensions")
    scale = min(max(50, min(200, dpi)) / 72, 1598 / longest)
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
    )
    data, mime = pix.tobytes("png"), "image/png"
    if len(data) > 3 * 1024 * 1024:
        data, mime = pix.tobytes("jpeg", jpg_quality=80), "image/jpeg"
    if len(data) > 3 * 1024 * 1024 or max(pix.width, pix.height) > 1600:
        raise ValueError("PDF render exceeded the image byte limit")
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
    import fitz

    cache = Path(args["cache"])
    if cache.is_symlink() or not cache.is_dir() or cache.resolve() != cache:
        raise ValueError("cache directory is unavailable")
    quiet_mupdf()
    with fitz.open("/proc/self/fd/3") as doc:
        builder = PdfBuilder(doc, cache, args["key"])
        if command == "render":
            return render(doc, args["page"], args.get("dpi", 100))
        if command == "map":
            result = pdf_map(doc, args["max_bytes"])
            repairs = mupdf_messages()
            if repairs:
                # MuPDF repaired the file on open; the count is of its messages.
                result["repairs"] = repairs
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
            "error": "PyMuPDF is missing; set ZLIBRARY_PDF_PYTHON to an interpreter with PyMuPDF"
        }
    except Exception:
        output = {"error": "PDF helper could not read or process this file"}
    print(json.dumps(output, separators=(",", ":")), file=reply)
    reply.close()
