"""Deterministic, valid PDF bytes; no third-party generator or real book data."""


def pdf_fixture(oversize=False):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /PageLabels << /Nums [0 << /S /r >> 1 << /S /D /St 40 >>] >>"
        + (b" /Outlines 10 0 R" if oversize else b"")
        + b" >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
    ]
    for content in (7, 8, 9):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] /Resources << /Font << /F1 6 0 R >> >> /Contents {content} 0 R >>".encode()
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in (
        "Contents: Chapter 8 begins on printed page 41.    i",
        "Chapter 7    40",
        "Chapter 8    41",
    ):
        stream = f"BT /F1 12 Tf 30 400 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    if oversize:
        objects.extend(
            [
                b"<< /Type /Outlines /First 11 0 R /Last 11 0 R /Count 1 >>",
                b"<< /Title (Chapter 8) /Parent 10 0 R /Dest [5 0 R /Fit] >>",
                b"<< /Length 1200000 >>\nstream\n" + b"X" * 1200000 + b"\nendstream",
            ]
        )
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(data)
