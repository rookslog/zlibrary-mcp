"""Adaptive page and region renderer using DPI decisions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .analyzer import DPI_CEILING, DPI_PAGE_CAP
from .models import PageAnalysis, RegionDPI
from ..utils.deps import (
    Image,
    PYMUPDF_AVAILABLE,
    fitz,
    require_optional_dependency,
)


def _require_renderer_dependencies() -> None:
    require_optional_dependency(PYMUPDF_AVAILABLE, "PyMuPDF (fitz)", "rag")


@dataclass
class AdaptiveRenderResult:
    """Result of adaptive page rendering."""

    page_image: Image.Image
    region_images: list[tuple[RegionDPI, Image.Image]] = field(default_factory=list)
    page_dpi: int = 300
    metadata: dict = field(default_factory=dict)


def pdf_to_pixel(coord: float, dpi: int) -> int:
    """Convert PDF point coordinate to pixel space."""
    return round(coord * dpi / 72)


def pixel_to_pdf(coord: float, dpi: int) -> float:
    """Convert pixel coordinate to PDF point space."""
    return coord * 72 / dpi


def _pixmap_to_pil(pix: fitz.Pixmap) -> Image.Image:
    """Convert a PyMuPDF Pixmap to a PIL Image."""
    _require_renderer_dependencies()
    require_optional_dependency(Image is not None, "Pillow", "scholar")
    return Image.frombytes(
        "RGB", (pix.width, pix.height), pix.samples, "raw", "RGB", pix.stride
    )


def _clip_bbox_to_page(
    bbox: tuple[float, float, float, float], page: fitz.Page
) -> fitz.Rect:
    """Clip bbox to page bounds."""
    require_optional_dependency(PYMUPDF_AVAILABLE, "PyMuPDF (fitz)", "rag")
    x0 = max(bbox[0], page.rect.x0)
    y0 = max(bbox[1], page.rect.y0)
    x1 = min(bbox[2], page.rect.x1)
    y1 = min(bbox[3], page.rect.y1)
    return fitz.Rect(x0, y0, x1, y1)


def render_region(
    page: fitz.Page, bbox: tuple[float, float, float, float], dpi: int
) -> Image.Image:
    """Render a clipped region of a page at specified DPI.

    Args:
        page: A fitz.Page object.
        bbox: Region in PDF point space (x0, y0, x1, y1).
        dpi: Target rendering DPI.

    Returns:
        PIL Image of the rendered region.
    """
    _require_renderer_dependencies()
    clip_rect = _clip_bbox_to_page(bbox, page)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip_rect)
    return _pixmap_to_pil(pix)


def render_page_adaptive(
    page: fitz.Page, analysis: PageAnalysis
) -> AdaptiveRenderResult:
    """Render a page using adaptive DPI from analysis results.

    Args:
        page: A fitz.Page object.
        analysis: PageAnalysis with DPI decisions.

    Returns:
        AdaptiveRenderResult with page image and optional region re-renders.
    """
    _require_renderer_dependencies()
    t0 = time.perf_counter()

    # Cap page DPI at DPI_PAGE_CAP (300)
    effective_page_dpi = min(analysis.page_dpi.dpi, DPI_PAGE_CAP)

    # Render full page
    mat = fitz.Matrix(effective_page_dpi / 72, effective_page_dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    page_image = _pixmap_to_pil(pix)

    # Re-render regions at elevated DPI if needed
    region_images: list[tuple[RegionDPI, Image.Image]] = []
    if analysis.has_small_text and analysis.regions:
        for region in analysis.regions:
            region_dpi = region.dpi_decision.dpi
            if region_dpi > effective_page_dpi:
                capped_dpi = min(region_dpi, DPI_CEILING)
                region_img = render_region(page, region.bbox, capped_dpi)
                region_images.append((region, region_img))

    elapsed_ms = (time.perf_counter() - t0) * 1000

    return AdaptiveRenderResult(
        page_image=page_image,
        region_images=region_images,
        page_dpi=effective_page_dpi,
        metadata={"render_time_ms": elapsed_ms},
    )
