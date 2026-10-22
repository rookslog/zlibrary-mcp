"""Bounded diagnostic PDF rasterizer. Receives checked bytes on stdin, never extracts text."""

import base64
import json
import math
import sys


def render():
    import fitz

    data = sys.stdin.buffer.read(6 * 1024 * 1024 + 1)
    if len(data) > 6 * 1024 * 1024:
        return {"error": "render_failed"}
    page_number = int(sys.argv[1])
    dpi = max(50, min(200, int(sys.argv[2])))
    with fitz.open(stream=data, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            return {"error": "page_out_of_range"}
        page = document[page_number - 1]
        rect = page.rect
        longest = max(rect.width, rect.height)
        if not math.isfinite(longest) or longest <= 0:
            return {"error": "render_failed"}
        scale = min(dpi / 72, 1600 / longest)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
        if max(pixmap.width, pixmap.height) > 1600:
            # MuPDF rounds device bounds outward; allow a little rounding slack.
            scale *= 1598 / max(pixmap.width, pixmap.height)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
        if max(pixmap.width, pixmap.height) > 1600:
            return {"error": "render_failed"}
        image = pixmap.tobytes("png")
        mime_type = "image/png"
        if len(image) > 3 * 1024 * 1024:
            mime_type = "image/jpeg"
            image = pixmap.tobytes("jpeg", jpg_quality=80)
        if len(image) > 3 * 1024 * 1024:
            return {"error": "render_failed"}
        return {
            "page": page_number,
            "pages": len(document),
            "width": pixmap.width,
            "height": pixmap.height,
            "mimeType": mime_type,
            "data": base64.b64encode(image).decode("ascii"),
        }


if __name__ == "__main__":
    try:
        result = render()
    except Exception:
        result = {"error": "render_failed"}
    print(json.dumps(result, separators=(",", ":")))
