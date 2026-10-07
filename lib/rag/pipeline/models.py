"""Data models for the unified detection pipeline.

Defines the contract between detectors, compositor, and writer.
All types use stdlib only (dataclasses, enum, typing, pathlib, json).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.filename_utils import create_metadata_filename
from lib.rag.utils.atomic_write import atomic_write_text


class ContentType(Enum):
    """Classification of text block content."""

    BODY = "body"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"
    MARGIN = "margin"
    HEADING = "heading"
    PAGE_NUMBER = "page_number"
    TOC = "toc"
    FRONT_MATTER = "front_matter"
    HEADER = "header"
    FOOTER = "footer"
    CITATION = "citation"


class DetectorScope(Enum):
    """Whether a detector operates on individual pages or the whole document."""

    PAGE = "page"
    DOCUMENT = "document"


@dataclass
class BlockClassification:
    """A classified text block with spatial and confidence information."""

    bbox: Tuple[float, float, float, float]  # (x0, y0, x1, y1)
    content_type: ContentType
    text: str
    confidence: float = 1.0
    detector_name: str = ""
    page_num: int = 0  # 1-indexed
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectionResult:
    """Output from a single detector run."""

    detector_name: str
    classifications: List[BlockClassification] = field(default_factory=list)
    page_num: int = 0  # 1-indexed, 0 for document-level
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentOutput:
    """Final processed document ready for output.

    Contains separated content streams and metadata.
    """

    body_text: str = ""
    footnotes: Optional[str] = None
    endnotes: Optional[str] = None
    citations: Optional[str] = None
    document_metadata: Optional[dict] = None
    processing_metadata: Optional[dict] = None

    def write_files(
        self, base_path: Path, output_format: str = "markdown"
    ) -> Dict[str, Path]:
        """Write document content to separate files.

        Args:
            base_path: Path to the source document (used for stem).
            output_format: Output format, currently only 'markdown'.

        Returns:
            Dict mapping content type name to written file path.
        """
        base_path = Path(base_path)
        out_dir = base_path.parent
        if ".processed." in base_path.name:
            body_path = base_path
        else:
            body_path = out_dir / f"{base_path.name}.processed.{output_format}"

        stem = body_path.stem
        ext = body_path.suffix
        written: Dict[str, Path] = {}

        # Body text (always written)
        atomic_write_text(body_path, self.body_text)
        written["body"] = body_path

        # Optional content streams
        for name, content in [
            ("footnotes", self.footnotes),
            ("endnotes", self.endnotes),
            ("citations", self.citations),
        ]:
            if content:
                p = out_dir / f"{stem}_{name}{ext}"
                atomic_write_text(p, content)
                written[name] = p

        # Metadata (always written)
        meta_path = out_dir / create_metadata_filename(body_path.name)
        meta: Dict[str, Any] = {}
        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    meta.update(existing)
            except (json.JSONDecodeError, OSError):
                pass

        outputs = {name: {"relative_path": path.name} for name, path in written.items()}
        outputs["metadata"] = {"relative_path": meta_path.name}
        meta.update(
            {
                "document_metadata": self.document_metadata,
                "processing_metadata": self.processing_metadata,
                "outputs": outputs,
            }
        )
        atomic_write_text(meta_path, json.dumps(meta, indent=2, default=str))
        written["metadata"] = meta_path

        return written
