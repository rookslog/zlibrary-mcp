"""Adaptive resolution pipeline for optimal DPI selection."""

from importlib import import_module

from .models import DPIDecision, PageAnalysis, RegionDPI


_LAZY_EXPORTS = {
    "compute_optimal_dpi": (".analyzer", "compute_optimal_dpi"),
    "analyze_page_fonts": (".analyzer", "analyze_page_fonts"),
    "analyze_document_fonts": (".analyzer", "analyze_document_fonts"),
    "AdaptiveRenderResult": (".renderer", "AdaptiveRenderResult"),
    "render_page_adaptive": (".renderer", "render_page_adaptive"),
    "render_region": (".renderer", "render_region"),
}


def __getattr__(name: str):
    """Load implementation exports only when callers request them."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error

    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "AdaptiveRenderResult",
    "DPIDecision",
    "PageAnalysis",
    "RegionDPI",
    "compute_optimal_dpi",
    "analyze_page_fonts",
    "analyze_document_fonts",
    "render_page_adaptive",
    "render_region",
]
