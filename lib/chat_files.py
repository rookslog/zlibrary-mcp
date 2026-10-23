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
        for i, node in enumerate(nodes):
            end = nodes[i + 1]["_start"] - 1 if i + 1 < len(nodes) else parent_end
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
    # Interpolate between actual builds, then close the integer gap. This avoids
    # repeated large font-subsetting passes on near-uniform books. Bounds still
    # come only from serialized excerpts, not estimated stream weights.
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


def page_weights(doc):
    # Ranking only. Count shared font/image resources on every referencing page,
    # rather than missing the fixed font overhead in a born-digital excerpt.
    lengths = {}
    weights = []
    for page in doc:
        refs = {r[0] for r in page.get_images()} | {r[0] for r in page.get_fonts()}
        refs.update(page.get_contents())
        weight = 512
        for xref in refs:
            if xref not in lengths:
                try:
                    lengths[xref] = len(doc.xref_stream_raw(xref) or b"")
                except Exception:
                    lengths[xref] = 0
            weight += lengths[xref]
        weights.append(weight)
    return weights


def safe_window(builder, budget):
    count = len(builder.document)
    weights = page_weights(builder.document)
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    checked = {}

    def candidates(n):
        starts = list(range(1, count - n + 2))
        if count <= 80:
            return starts
        ranked = sorted(
            starts, key=lambda i: prefix[i + n - 1] - prefix[i - 1], reverse=True
        )
        return list(dict.fromkeys(ranked[:3] + [1, count - n + 1]))

    def worst(n, final=False):
        starts = candidates(n)
        if count > 80 and not final:
            starts = starts[:1]
        largest = 0
        for start in starts:
            size = builder.size(start, start + n - 1)
            checked[(start, n)] = size
            largest = max(largest, size)
        return largest

    low, high = 0, count
    low_size, high_size = 0, builder.size(1, count)
    if high_size <= budget:
        low = count
        checked[(1, count)] = high_size
    else:
        while high - low > 1:
            fraction = (budget - low_size) / max(1, high_size - low_size)
            mid = max(low + 1, min(high - 1, low + int((high - low) * fraction)))
            size = worst(mid)
            if size <= budget:
                low, low_size = mid, size
            else:
                high, high_size = mid, size
    # The endpoint can carry different fonts from the top weighted windows.
    # Shrink only the falsified candidate; restarting a full binary search here
    # needlessly repeats expensive subsetting on dozens of known-safe windows.
    while low and worst(low, final=True) > budget:
        low -= 1

    return low, {
        "method": "all windows"
        if count <= 80
        else "heaviest 3 estimated windows plus endpoints, verified with real builds",
        "planning_hint_only": count > 80,
        "verified_windows": [
            {"pdf_pages": page_range(s, s + n - 1), "bytes": size}
            for (s, n), size in checked.items()
            if n == low
        ],
    }


def pdf_map(doc, builder, budget):
    count = len(doc)
    if count == 0:
        raise ValueError("PDF has no pages")
    tree = outline_tree(doc.get_toc(), count)
    total_nodes = sum(1 for _ in walk_sections(tree))

    def measure(nodes, needed=True):
        for node in nodes:
            start, end = node["_start"], node["_end"]
            label = printed_range(doc, start, end)
            if label is not None:
                node["printed_pages"] = label
            if needed:
                size = builder.size(start, end)
                node.update(bytes=size, fits=size <= budget)
            recurse = needed and not node["fits"]
            if recurse or total_nodes < 40:
                measure(node["children"], recurse)
            else:
                # Retain complete stable IDs for section resolution without
                # paying to measure descendants of a fitting parent.
                measure(node["children"], False)

    measure(tree)
    boundaries = {n["_start"] - 1 for n in walk_sections(tree)}
    parts = []
    unshareable = []
    start = 1
    while start <= count:
        end = fit_end(builder, start, count, budget, boundaries)
        if end < start:
            unshareable.append(
                {
                    "pdf_pages": str(start),
                    "bytes": builder.size(start, start),
                    "fits": False,
                    "use": "get_page_image",
                }
            )
            start += 1
            continue
        part = {"pdf_pages": page_range(start, end), "bytes": builder.size(start, end)}
        label = printed_range(doc, start, end)
        if label is not None:
            part["printed_pages"] = label
        section = next((n for n in walk_sections(tree) if n["_start"] == start), None)
        if section:
            part["starts_at_section"] = section["id"]
        parts.append(part)
        start = end + 1
    safe, verification = safe_window(builder, budget)
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
        "safe_window_verification": verification,
        "subsetting": list(builder.measurements.values()),
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


def main(command, args):
    import fitz

    cache = Path(args["cache"])
    if cache.is_symlink() or not cache.is_dir() or cache.resolve() != cache:
        raise ValueError("cache directory is unavailable")
    with fitz.open("/proc/self/fd/3") as doc:
        builder = PdfBuilder(doc, cache, args["key"])
        if command == "render":
            return render(doc, args["page"], args.get("dpi", 100))
        if command == "map":
            return pdf_map(doc, builder, args["max_bytes"])
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
    try:
        output = main(sys.argv[1], json.load(sys.stdin))
    except ValueError as exc:
        output = {"error": str(exc)}
    except Exception:
        output = {
            "error": "PDF helper could not complete; check file and ZLIBRARY_PDF_PYTHON (PyMuPDF required)"
        }
    print(json.dumps(output, separators=(",", ":")))
