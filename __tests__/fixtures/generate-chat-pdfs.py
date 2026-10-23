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


if __name__ == "__main__":
    generate(sys.argv[1])
