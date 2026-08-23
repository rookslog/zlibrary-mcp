"""Font analysis and DPI computation for adaptive resolution rendering."""

from __future__ import annotations

import logging
from typing import Optional

from .models import DPIDecision, PageAnalysis
from ..utils.deps import PYMUPDF_AVAILABLE, fitz, require_optional_dependency

logger = logging.getLogger(__name__)

# DPI bounds
DPI_FLOOR = 72
DPI_CEILING = 600
DPI_PAGE_CAP = 300
DPI_DEFAULT = 300

# Tesseract optimal pixel height range
TARGET_PIXEL_HEIGHT_MIN = 20
TARGET_PIXEL_HEIGHT_MAX = 33
TARGET_PIXEL_HEIGHT_IDEAL = 28


def compute_optimal_dpi(font_size_pt: float) -> DPIDecision:
    """Compute DPI that places text at Tesseract's optimal pixel height.

    Formula: optimal_dpi = TARGET_PIXEL_HEIGHT_IDEAL * 72 / font_size_pt
    Then quantize to nearest 50, clamp to [DPI_FLOOR, DPI_CEILING].
    """
    if font_size_pt <= 0:
        return DPIDecision(
            dpi=DPI_DEFAULT,
            confidence=0.0,
            reason="invalid_font_size",
            font_size_pt=font_size_pt,
            estimated_pixel_height=0.0,
        )

    ideal_dpi = TARGET_PIXEL_HEIGHT_IDEAL * 72 / font_size_pt
    clamped_dpi = max(DPI_FLOOR, min(DPI_CEILING, round(ideal_dpi)))

    # Quantize to multiples of 50
    quantized_dpi = round(clamped_dpi / 50) * 50
    quantized_dpi = max(DPI_FLOOR, min(DPI_CEILING, quantized_dpi))

    actual_pixel_height = font_size_pt * quantized_dpi / 72
    in_range = TARGET_PIXEL_HEIGHT_MIN <= actual_pixel_height <= TARGET_PIXEL_HEIGHT_MAX
    confidence = 1.0 if in_range else 0.7

    return DPIDecision(
        dpi=quantized_dpi,
        confidence=confidence,
        reason="computed" if in_range else "clamped",
        font_size_pt=font_size_pt,
        estimated_pixel_height=actual_pixel_height,
    )


def analyze_page_fonts(page) -> PageAnalysis:
    """Analyze font sizes on a page using text dict (no rendering).

    Extracts all span font sizes, computes dominant (median), min, max,
    and flags pages with small text relative to dominant.
    """
    blocks = page.get_text("dict")["blocks"]
    sizes: list[float] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = span.get("size", 0)
                if size > 0:
                    sizes.append(size)

    if not sizes:
        fallback_dpi = DPIDecision(
            dpi=DPI_DEFAULT,
            confidence=0.0,
            reason="no_text_layer",
            font_size_pt=0.0,
            estimated_pixel_height=0.0,
        )
        return PageAnalysis(
            page_num=0,
            dominant_size=0,
            min_size=0,
            max_size=0,
            has_small_text=False,
            page_dpi=fallback_dpi,
        )

    sizes.sort()
    dominant = sizes[len(sizes) // 2]
    min_size = min(sizes)
    max_size = max(sizes)
    has_small_text = min_size < dominant * 0.7

    page_dpi = compute_optimal_dpi(dominant)

    return PageAnalysis(
        page_num=0,
        dominant_size=dominant,
        min_size=min_size,
        max_size=max_size,
        has_small_text=has_small_text,
        page_dpi=page_dpi,
    )


def _analyze_page_worker(pdf_path: str, page_num: int) -> tuple[int, PageAnalysis]:
    """Worker function for parallel page analysis. Receives path (picklable)."""
    require_optional_dependency(PYMUPDF_AVAILABLE, "PyMuPDF (fitz)", "rag")
    with fitz.open(pdf_path) as doc:
        page = doc[page_num]
        result = analyze_page_fonts(page)
        result.page_num = page_num
        return page_num, result


def analyze_document_fonts(
    pdf_path: str, page_range: Optional[tuple[int, int]] = None
) -> dict[int, PageAnalysis]:
    """Analyze font sizes across all pages of a PDF.

    Args:
        pdf_path: Path to PDF file.
        page_range: Optional (start, end) inclusive page range.

    Returns:
        Dict mapping page number to PageAnalysis.
    """
    require_optional_dependency(PYMUPDF_AVAILABLE, "PyMuPDF (fitz)", "rag")
    results: dict[int, PageAnalysis] = {}

    with fitz.open(pdf_path) as doc:
        total = doc.page_count
        if page_range:
            start, end = page_range
            pages = range(start, min(end + 1, total))
        else:
            pages = range(total)

        if len(list(pages)) > 10:
            # Use parallel processing for large documents
            import os
            from concurrent.futures import ProcessPoolExecutor

            max_workers = min(os.cpu_count() or 4, 4)
            # Reconstruct pages range (consumed by len check)
            if page_range:
                start, end = page_range
                page_list = list(range(start, min(end + 1, total)))
            else:
                page_list = list(range(total))

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_analyze_page_worker, pdf_path, p): p
                    for p in page_list
                }
                for future in futures:
                    page_num, analysis = future.result()
                    results[page_num] = analysis
        else:
            # Sequential for small documents
            if page_range:
                start, end = page_range
                page_list = range(start, min(end + 1, total))
            else:
                page_list = range(total)

            for p in page_list:
                page = doc[p]
                analysis = analyze_page_fonts(page)
                analysis.page_num = p
                results[p] = analysis

    return results
