"""
RAG document processing orchestrator.

Contains the main entry points: process_pdf, process_epub, process_txt,
process_document, and save_processed_text. Orchestrates detection, quality
analysis, formatting, and output generation.
"""

import asyncio
import logging
import os  # noqa: F401 - used by submodules
import subprocess  # noqa: F401 - used by submodules
from pathlib import Path

import aiofiles

from lib.rag.utils.constants import SUPPORTED_FORMATS, PROCESSED_OUTPUT_DIR  # noqa: F401
from lib.rag.utils.exceptions import (
    FileSaveError,
)  # noqa: F401
from lib.rag.utils.deps import require_optional_dependency
from lib.rag.utils.text import _slugify
from lib.rag.utils.cache import _clear_textpage_cache  # noqa: F401
from lib.rag.utils.header import (  # noqa: F401
    _generate_document_header,
    _generate_markdown_toc_from_pdf,
    _find_first_content_page,
)
from lib.rag.detection.toc import (  # noqa: F401
    _extract_toc_from_pdf,
    _identify_and_remove_front_matter,
    _extract_and_format_toc,
)
from lib.rag.detection.page_numbers import (  # noqa: F401
    _detect_written_page_on_page,
    infer_written_page_numbers,
)
from lib.rag.detection.footnotes import (  # noqa: F401
    _detect_footnotes_in_page,
    _format_footnotes_markdown,
    _footnote_with_continuation_to_dict,
)
from lib.rag.ocr.spacing import detect_letter_spacing_issue
from lib.rag.quality.analysis import detect_pdf_quality  # noqa: F401
from lib.rag.quality.pipeline import QualityPipelineConfig  # noqa: F401
from lib.rag.ocr.recovery import (  # noqa: F401
    run_ocr_on_pdf,
    assess_pdf_ocr_quality,
    redo_ocr_with_tesseract,
)
from lib.rag.xmark.detection import (  # noqa: F401
    _detect_xmarks_parallel,
    _page_needs_xmark_detection_fast,
    _should_enable_xmark_detection_for_document,
)
from lib.rag.processors.pdf import _format_pdf_markdown  # noqa: F401
from lib.rag.processors.epub import process_epub
from lib.rag.processors.txt import process_txt

from lib.rag.orchestrator_pdf import process_pdf, process_pdf_structured  # noqa: F401 - re-exported

from lib.footnote_continuation import CrossPageFootnoteParser  # noqa: F401
from filename_utils import create_unified_filename, create_metadata_filename
from metadata_generator import generate_metadata_sidecar, save_metadata_sidecar
from metadata_verification import (  # noqa: F401
    extract_pdf_metadata,
    extract_epub_metadata,
    extract_txt_metadata,
    verify_metadata,
)

logger = logging.getLogger(__name__)

# OCR Dependencies (Optional)
try:
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    pytesseract = None
    convert_from_path = None
    Image = None

try:
    import ebooklib  # noqa: F401
    from ebooklib import epub  # noqa: F401

    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False

try:
    import fitz  # PyMuPDF

    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    fitz = None

# X-mark detection (opencv) - Phase 2.3
try:
    import cv2
    import numpy as np

    XMARK_AVAILABLE = True
except ImportError:
    XMARK_AVAILABLE = False
    cv2 = None
    np = None


def _get_facade():
    """Get facade module for test mockability of optional dependencies."""
    import lib.rag_processing as _rp

    return _rp


__all__ = [
    "process_pdf",
    "process_pdf_structured",
    "process_document",
    "save_processed_text",
]


async def process_document(
    file_path_str: str, output_format: str = "txt", book_details: dict = None
) -> dict:
    """
    Processes a document (PDF, EPUB, TXT) based on its extension,
    extracts text, applies preprocessing, saves it, and returns the path.
    """
    if book_details is None:  # Ensure book_details is a dict for _slugify
        book_details = {}

    file_path = Path(file_path_str)
    file_extension = file_path.suffix.lower()
    processed_text = ""

    logging.info(f"Processing document: {file_path} with format {output_format}")

    try:
        # Track structured output for multi-file writing (PDF only)
        doc_output = None
        extra_paths = {}

        if file_extension == ".pdf":
            # Use structured pipeline for multi-file output
            doc_output = await asyncio.to_thread(
                process_pdf_structured, file_path, output_format
            )
            processed_text = doc_output.body_text
        elif file_extension == ".epub":
            require_optional_dependency(
                _get_facade().EBOOKLIB_AVAILABLE, "ebooklib", "rag"
            )
            processed_text = await asyncio.to_thread(
                process_epub, file_path, output_format
            )
        elif file_extension == ".txt":
            processed_text = await process_txt(
                file_path, output_format
            )  # process_txt is already async
        else:
            raise ValueError(f"Unsupported file format: {file_extension}")

        if processed_text is None or not processed_text.strip():
            logging.warning(f"No text extracted from {file_path}")
            return {
                "processed_file_path": None,
                "metadata_file_path": None,
                "stats": None,
                "content_types_produced": [],
                "output_files": {},
            }

        # Save the processed text with metadata
        # Determine corrections applied (for metadata)
        corrections = []
        if file_extension == ".pdf":
            # Check if letter spacing correction was applied
            if detect_letter_spacing_issue(processed_text[:500]):
                corrections.append("letter_spacing_correction")

        # Pass book_details to save_processed_text for filename generation
        # This also generates and saves the metadata sidecar
        saved_path = await save_processed_text(
            original_file_path=file_path,
            processed_content=processed_text,
            output_format=output_format,  # This should be the format of the processed_text (e.g. 'md' or 'txt')
            book_details=book_details,
            ocr_quality_score=None,  # FUTURE: Calculate OCR quality score if OCR was used
            corrections_applied=corrections,
        )

        saved_path_obj = Path(saved_path)
        metadata_path = PROCESSED_OUTPUT_DIR / create_metadata_filename(
            saved_path_obj.name
        )
        written = {"body": saved_path_obj}

        # Write multi-file output (footnotes, metadata) for structured pipeline
        if doc_output is not None:
            try:
                written = doc_output.write_files(
                    saved_path_obj, output_format=output_format
                )
                logging.info(f"Multi-file output written: {list(written.keys())}")
            except Exception as mf_err:
                logging.warning(f"Multi-file output write failed (non-fatal): {mf_err}")
        if metadata_path.exists():
            written["metadata"] = metadata_path

        output_files = {key: str(path) for key, path in written.items()}
        for key, path in written.items():
            if key not in ("body", "metadata"):
                extra_paths[f"{key}_file_path"] = str(path)

        # Build content types produced list
        content_types = [
            key
            for key in ("body", "footnotes", "endnotes", "citations")
            if key in written
        ]

        # Return ONLY paths, not content (prevents MCP token overflow)
        # User can selectively read portions of the processed file
        result = {
            "processed_file_path": str(saved_path_obj),
            "metadata_file_path": str(metadata_path)
            if metadata_path.exists()
            else None,
            "stats": {
                "word_count": len(processed_text.split()),
                "char_count": len(processed_text),
                "format": output_format,
            },
            "content_types_produced": content_types,
            "output_files": output_files,
        }
        # Merge extra file paths (footnotes_file_path, metadata_file_path from pipeline, etc.)
        result.update(extra_paths)
        return result

    except Exception as e:
        logging.error(f"Error processing document {file_path}: {e}")
        # Re-raise as a RuntimeError to be caught by the bridge's main error handler
        raise RuntimeError(f"Error processing document {file_path}: {e}") from e


async def save_processed_text(
    original_file_path: str,
    processed_content: str,
    output_format: str = "txt",
    book_details: dict | None = None,  # Added book_details for slug
    ocr_quality_score: float | None = None,  # For metadata
    corrections_applied: list | None = None,  # For metadata
) -> str:
    """Saves the processed text content to a file in the output directory."""
    try:
        book_details = book_details or {}
        original_path = Path(original_file_path)
        original_filename = original_path.stem
        original_extension = original_path.suffix.lower()

        # --- Generate Filename using unified format ---
        if book_details:
            # Use unified filename generation
            # Remove 'extension' key to avoid double extension bug
            clean_book_details = {
                k: v for k, v in book_details.items() if k != "extension"
            }
            base_name = create_unified_filename(clean_book_details, extension=None)
            processed_filename = (
                f"{base_name}{original_extension}.processed.{output_format}"
            )
        else:
            # Fallback if no book_details
            base_name = _slugify(original_filename)
            processed_filename = (
                f"{base_name}{original_extension}.processed.{output_format}"
            )

        # --- Ensure Output Directory Exists ---
        PROCESSED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = PROCESSED_OUTPUT_DIR / processed_filename

        # --- Write Content Asynchronously (NO YAML frontmatter) ---
        # Main markdown should be CLEAN for RAG (only content + page markers)
        async with aiofiles.open(output_path, mode="w", encoding="utf-8") as f:
            await f.write(processed_content)

        logging.info(f"Successfully saved processed content to: {output_path}")

        # --- Generate and Save Metadata Sidecar ---
        try:
            format_type = original_path.suffix.lower().lstrip(".")

            # --- Metadata Verification Step (NEW) ---
            # Extract metadata from document for verification
            extracted_metadata = {}
            try:
                if format_type == "pdf":
                    extracted_metadata = extract_pdf_metadata(original_path)
                elif format_type == "epub":
                    extracted_metadata = extract_epub_metadata(original_path)
                elif format_type == "txt":
                    extracted_metadata = extract_txt_metadata(original_path)

                logging.info(
                    f"Extracted document metadata for verification: {list(extracted_metadata.keys())}"
                )
            except Exception as extract_err:
                logging.warning(
                    f"Could not extract document metadata for verification: {extract_err}"
                )
                extracted_metadata = {}

            # Verify API metadata against extracted metadata
            verification_report = None
            if extracted_metadata:
                try:
                    # Prepare API metadata in consistent format
                    api_metadata = {
                        "title": book_details.get("title"),
                        "author": book_details.get("author"),
                        "publisher": book_details.get("publisher"),
                        "year": book_details.get("year"),
                        "isbn": book_details.get("isbn"),
                    }

                    verification_report = verify_metadata(
                        api_metadata, extracted_metadata
                    )
                    logging.info(
                        f"Metadata verification: {verification_report['summary']}"
                    )

                    # Log any discrepancies for review
                    if verification_report["discrepancies"]:
                        logging.warning(
                            f"Metadata discrepancies found: {verification_report['discrepancies']}"
                        )
                except Exception as verify_err:
                    logging.warning(f"Could not verify metadata: {verify_err}")
                    verification_report = None

            # Extract PDF ToC and metadata for verification
            pdf_toc = None
            extracted_metadata = None

            if format_type == "pdf" and _get_facade().PYMUPDF_AVAILABLE:
                try:
                    doc = _get_facade().fitz.open(str(original_path))
                    pdf_toc = doc.get_toc()  # Returns list of [level, title, page]
                    doc.close()
                    logging.info(
                        f"Extracted {len(pdf_toc)} ToC entries from PDF for metadata"
                    )
                except Exception as toc_err:
                    logging.warning(
                        f"Could not extract PDF ToC for metadata: {toc_err}"
                    )

                # Extract and verify metadata (using module-level imports)
                try:
                    extracted_metadata = extract_pdf_metadata(str(original_path))
                    verification_report = verify_metadata(
                        book_details, extracted_metadata
                    )
                    logging.info(
                        f"Metadata verification: {verification_report.get('summary', 'N/A')}"
                    )
                except Exception as verify_err:
                    logging.warning(f"Metadata verification failed: {verify_err}")

            # Generate metadata with verification report
            metadata = generate_metadata_sidecar(
                original_filename=str(original_path),
                processed_content=processed_content,  # Use processed_content, not final_content
                book_details=book_details,
                ocr_quality_score=ocr_quality_score,
                corrections_applied=corrections_applied or [],
                format_type=format_type,
                output_format=output_format,
                pdf_toc=pdf_toc,
            )

            # Add verification report to metadata if available
            if verification_report:
                metadata["verification"] = verification_report

            # Save metadata sidecar
            metadata_filename = create_metadata_filename(processed_filename)
            metadata_path = PROCESSED_OUTPUT_DIR / metadata_filename
            save_metadata_sidecar(metadata, metadata_path)

            logging.info(f"Successfully saved metadata sidecar to: {metadata_path}")
        except Exception as meta_err:
            logging.warning(f"Failed to generate metadata sidecar: {meta_err}")
            # Don't fail the whole operation if metadata generation fails

        return str(output_path)  # Return the string representation of the path

    except ValueError as ve:  # Catch the specific error for None content
        logging.error(f"ValueError during save: {ve}")
        # Construct a meaningful path for the error message if possible
        unknown_path = (
            PROCESSED_OUTPUT_DIR
            / f"{_slugify(original_path.stem)}.processed.{output_format}"
        )
        raise FileSaveError(
            f"Failed to save processed text to {unknown_path}: {ve}"
        ) from ve
    except OSError as ose:
        logging.error(f"OS error saving processed file: {ose}")
        # Construct a meaningful path for the error message if possible
        failed_path = (
            PROCESSED_OUTPUT_DIR / processed_filename
            if "processed_filename" in locals()
            else "unknown_processed_file"
        )
        raise FileSaveError(
            f"Failed to save processed file due to OS error: {ose}"
        ) from ose
    except Exception as e:
        logging.error(f"Unexpected error saving processed file: {e}", exc_info=True)
        failed_path = (
            PROCESSED_OUTPUT_DIR / processed_filename
            if "processed_filename" in locals()
            else "unknown_processed_file"
        )
        raise FileSaveError(
            f"Unexpected error saving processed file {failed_path}: {e}"
        ) from e
