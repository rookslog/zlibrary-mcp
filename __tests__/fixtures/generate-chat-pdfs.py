"""Deterministic, generated-only PDF fixtures shared by Jest and pytest."""

import pathlib
import random
import sys
import fitz


def generate(root):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    d = fitz.open()
    for i in range(12):
        p = d.new_page(width=300, height=300)
        p.insert_text((20, 30), f"PRIVATE page {i + 1}")
        pix = fitz.Pixmap(
            fitz.csRGB, 300, 300, random.Random(i).randbytes(270000), False
        )
        p.insert_image(fitz.Rect(0, 50, 250, 300), pixmap=pix)
    d.set_toc(
        [
            [1, "Front", 1],
            [1, "Body", 3],
            [2, "First", 3],
            [2, "Second", 7],
            [1, "End", 11],
        ]
    )
    d.set_page_labels(
        [
            {"startpage": 0, "prefix": "Cover"},
            {"startpage": 1, "style": "r", "firstpagenum": 1},
            {"startpage": 3, "style": "D", "firstpagenum": 1},
        ]
    )
    d.save(root / "nested.pdf")
    d.close()
    d = fitz.open()
    for i in range(4):
        p = d.new_page()
        pix = fitz.Pixmap(
            fitz.csRGB, 100, 100, random.Random(i).randbytes(30000), False
        )
        p.insert_image(p.rect, pixmap=pix)
    d.save(root / "scan.pdf")
    d.close()
    d = fitz.open()
    for i in range(5):
        d.new_page().insert_text((20, 30), f"PRIVATE {i}")
    d.save(root / "plain.pdf")
    d.close()
    # An integer key in the font dictionary: MuPDF prints "invalid key in dict"
    # and repairs on open. Same length, so the xref offsets stay valid.
    d = fitz.open()
    for i in range(3):
        d.new_page().insert_text((72, 72), f"PRIVATE {i}")
    d.set_toc([[1, "A", 1], [1, "B", 2]])
    data = d.tobytes(garbage=0, deflate=False)
    d.close()
    good, bad = b"<</Type/Font/Subtype", b"<<1 2/Font 3/Subtype"
    assert data.count(good) == 1
    (root / "malformed.pdf").write_bytes(data.replace(good, bad))
    inherited(root)


def inherited(root):
    # Pages that inherit /Resources from the page tree (PDF 32000-1, 7.7.3.4),
    # after the review of 6ad5cfb (F1): directly from the root /Pages node, and
    # from two levels up through an intermediate /Pages node with none.
    d = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, 300, 300, random.Random(7).randbytes(270000), False)
    for i in range(4):
        d.new_page(width=300, height=300).insert_image(
            fitz.Rect(0, 0, 300, 300), pixmap=pix
        )
    d.set_toc([[1, "A", 1], [1, "B", 3]])
    resources = d.xref_get_key(d[0].xref, "Resources")[1]
    pages = int(d.xref_get_key(d[0].xref, "Parent")[1].split()[0])
    d.xref_set_key(pages, "Resources", resources)
    kids = [p.xref for p in d]
    for xref in kids:
        d.xref_set_key(xref, "Resources", "null")
    data = d.tobytes(garbage=4, deflate=True)
    d.close()
    (root / "inherited.pdf").write_bytes(data)
    d = fitz.open(stream=data, filetype="pdf")
    pages = int(d.xref_get_key(d[0].xref, "Parent")[1].split()[0])
    kids = [p.xref for p in d]
    middle = d.get_new_xref()
    refs = " ".join(f"{x} 0 R" for x in kids)
    d.update_object(
        middle, f"<</Type/Pages/Kids[{refs}]/Count {len(kids)}/Parent {pages} 0 R>>"
    )
    d.xref_set_key(pages, "Kids", f"[{middle} 0 R]")
    for xref in kids:
        d.xref_set_key(xref, "Parent", f"{middle} 0 R")
    d.save(root / "inherited-nested.pdf", garbage=4, deflate=True)
    d.close()


if __name__ == "__main__":
    generate(sys.argv[1])
