# RAG Document Processing Pipeline: Implementation Specification

**Version:** 2.1 (Download Workflow Clarification)
**Date:** 2025-04-24

## 1. Overview

This document details the implementation specifics for the RAG (Retrieval-Augmented Generation) document processing pipeline within the `zlibrary-mcp` server. It covers the necessary changes to MCP tools, Node.js handlers, and the Python bridge to enable downloading and processing of EPUB, TXT, and PDF files for RAG workflows, **saving the processed output to a file and returning the file path.**

**Reason for Update (v2.1):** Clarify the download workflow for `download_book_to_file` to align with ADR-002, emphasizing the two-step process: obtaining `bookDetails` (including the book page URL) via `search_books`, passing these details to the tool, and the internal scraping mechanism used by the Python bridge to find the actual download link.

**Reason for Update (v2.0):** Original design returned raw text, causing agent instability. This version modifies the pipeline to save processed text to `./processed_rag_output/` and return the `processed_file_path`.

Refer to the [RAG Pipeline Architecture (v2)](./architecture/rag-pipeline.md), [PDF Processing Integration Architecture (v2)](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/architecture/pdf-processing-integration.md), and [ADR-002: Reaffirm Download Workflow](./adr/ADR-002-Download-Workflow-Redesign.md) documents for the high-level design and data flow.

## 2. MCP Tool Definitions

### 2.1. `download_book_to_file` (Updated)

*   **Description:** Downloads a book file from Z-Library using details obtained from `search_books`. Internally, it fetches the book's page URL (contained within the input `bookDetails`), scrapes the page to find the actual download link, downloads the file, and optionally processes its content for RAG, saving the result to a separate file. See ADR-002 for details.
*   **Input Schema (Zod):**
    ```typescript
    import { z } from 'zod';

    const DownloadBookToFileInputSchema = z.object({
      // Changed from 'id' to 'bookDetails' to align with ADR-002
      bookDetails: z.object({}).passthrough().describe("The full book details object obtained from a search_books result, containing the necessary book page URL under the 'url' key."),
      // 'format' removed as it's determined internally during scraping/download
      outputDir: z.string().optional().default("./downloads").describe("Directory to save the original file (default: './downloads')"),
      process_for_rag: z.boolean().optional().default(false).describe("If true, process content for RAG and save to processed output file"),
      processed_output_format: z.string().optional().default("txt").describe("Desired format for the processed output file (default: 'txt')")
    });
    ```
*   **Output Schema (Zod):**
    ```typescript
    // Output depends on the 'process_for_rag' flag
    const DownloadBookToFileOutputSchema = z.object({
        file_path: z.string().describe("The absolute path to the original downloaded file"),
        processed_file_path: z.string().optional().nullable().describe("The absolute path to the file containing processed text (if process_for_rag was true and text was extracted), or null otherwise.") // Updated field, allow null
processed_output_format: z.string().optional().default("txt").describe("Desired format for the processed output file ('txt' or 'markdown', default: 'txt')")
    });
    ```

### 2.2. `process_document_for_rag` (Updated)

*   **Description:** Processes an existing local document file (EPUB, TXT, PDF) to extract plain text content for RAG, saving the result to a file.
*   **Input Schema (Zod):**
    ```typescript
    import { z } from 'zod';

    const ProcessDocumentForRagInputSchema = z.object({
      file_path: z.string().describe("The absolute path to the document file to process"),
      output_format: z.string().optional().default("txt").describe("Desired format for the processed output file (default: 'txt')")
    });
    ```
*   **Output Schema (Zod):**
    ```typescript
    const ProcessDocumentForRagOutputSchema = z.object({
      // Allow null if processing yields no text (e.g., image PDF)
      processed_file_path: z.string().nullable().describe("The absolute path to the file containing extracted and processed plain text content, or null if no text was extracted.")
    });
output_format: z.string().optional().default("txt").describe("Desired format for the processed output file ('txt' or 'markdown', default: 'txt')")
    ```

## 3. Tool Registration (`index.ts`)

The tools are registered within `index.ts` using the MCP SDK v1.8.0 pattern. Handlers map tool names to implementation functions in `lib/zlibrary-api.ts` and use the Zod schemas for validation.

```typescript
// File: src/index.ts (Conceptual Snippet)
// ... imports (Server, StdioServerTransport, z, zlibraryApi, schemas) ...

// Assume schemas are defined or imported from e.g., './lib/schemas'
import {
  DownloadBookToFileInputSchema, // Use updated schema name if changed
  DownloadBookToFileOutputSchema,
  ProcessDocumentForRagInputSchema,
  ProcessDocumentForRagOutputSchema,
  // ... other schemas
} from './lib/schemas'; // Example path
import * as zlibraryApi from './lib/zlibrary-api'; // Assuming API functions are exported

const server = new Server({
  // ... other server options
  tools: {
    list: async () => {
      return [
        // ... other tools
        {
          name: 'download_book_to_file',
          description: 'Downloads a book file from Z-Library using details obtained from search_books. Internally scrapes the book page for the download link (see ADR-002), downloads, and optionally processes for RAG.', // Updated description
          inputSchema: DownloadBookToFileInputSchema, // Use updated schema
          outputSchema: DownloadBookToFileOutputSchema,
        },
        {
          name: 'process_document_for_rag',
          description: 'Processes an existing local document file (EPUB, TXT, PDF) to extract plain text content for RAG, saving the result to a file.',
          inputSchema: ProcessDocumentForRagInputSchema,
          outputSchema: ProcessDocumentForRagOutputSchema, // Use updated schema
        },
      ];
    },
    call: async (request) => {
      // ... generic validation ...
      if (request.name === 'download_book_to_file') { // Use 'name' based on recent fixes
        const validatedArgs = DownloadBookToFileInputSchema.parse(request.arguments); // Use updated schema
        const result = await zlibraryApi.downloadBookToFile(validatedArgs); // Pass validated args containing bookDetails
        // Optional: DownloadBookToFileOutputSchema.parse(result);
        return result;
      }
      if (request.name === 'process_document_for_rag') { // Use 'name'
        const validatedArgs = ProcessDocumentForRagInputSchema.parse(request.arguments);
        const result = await zlibraryApi.processDocumentForRag(validatedArgs);
        // Optional: ProcessDocumentForRagOutputSchema.parse(result);
        return result;
      }
      // ... handle other tools
      throw new Error(`Tool not found: ${request.name}`);
    },
  },
});

// ... transport setup and server.connect() ...
```

## 4. Node.js Handlers (`lib/zlibrary-api.ts`)

This module contains the TypeScript functions that orchestrate calls to the Python bridge.

```pseudocode
// File: src/lib/zlibrary-api.ts
// Dependencies: ./python-bridge, path

IMPORT callPythonFunction FROM './python-bridge' // Assumes this handles calling Python and parsing JSON response/error
IMPORT path

// --- Updated Function ---

ASYNC FUNCTION downloadBookToFile(args):
  // args = { bookDetails, outputDir?, process_for_rag?, processed_output_format? }
  LOG `Downloading book using details, process_for_rag=${args.process_for_rag}`

  // Prepare arguments for Python script
  pythonArgs = {
    book_details: args.bookDetails, // Pass the whole bookDetails object
    output_dir: args.outputDir,
    process_for_rag: args.process_for_rag,
    processed_output_format: args.processed_output_format
  }

  TRY
    // Call the Python bridge function
    resultJson = AWAIT callPythonFunction('download_book', pythonArgs)

    // Basic validation of Python response
    IF NOT resultJson OR NOT resultJson.file_path THEN
      THROW Error("Invalid response from Python bridge during download: Missing original file_path.")
    ENDIF
    // Check if processed_file_path exists (it could be null if processing yielded no text)
    IF args.process_for_rag AND resultJson.processed_file_path IS UNDEFINED THEN
       THROW Error("Invalid response from Python bridge: Processing requested but processed_file_path key is missing.")
    ENDIF

    // Construct the response object based on the schema
    response = {
      file_path: resultJson.file_path,
      // Include processed_file_path if processing was requested, preserving null if returned
      ...(args.process_for_rag && { processed_file_path: resultJson.processed_file_path })
    }

    RETURN response

  CATCH error
    LOG `Error in downloadBookToFile: ${error.message}`
    // Propagate a user-friendly error
    THROW Error(`Failed to download/process book: ${error.message}`) // Removed ID as it's inside bookDetails
  ENDTRY
END FUNCTION

// --- Updated Function ---

ASYNC FUNCTION processDocumentForRag(args):
  // args = { file_path, output_format? }
  LOG `Processing document for RAG: ${args.file_path}`

  // Resolve to absolute path before sending to Python
  absolutePath = path.resolve(args.file_path)

  pythonArgs = {
    file_path: absolutePath,
    output_format: args.output_format
  }

  TRY
    // Call the Python bridge function
    resultJson = AWAIT callPythonFunction('process_document', pythonArgs)

    // Basic validation of Python response (allow null processed_file_path)
    IF NOT resultJson OR resultJson.processed_file_path IS UNDEFINED THEN
      THROW Error("Invalid response from Python bridge during processing. Missing processed_file_path key.")
    ENDIF

    // Return the result object matching the schema
    RETURN {
      processed_file_path: resultJson.processed_file_path // Can be null
    }

  CATCH error
    LOG `Error in processDocumentForRag: ${error.message}`
    // Propagate a user-friendly error
    THROW Error(`Failed to process document ${args.file_path}: ${error.message}`)
  ENDTRY
END FUNCTION

// --- Export Functions ---
EXPORT { downloadBookToFile, processDocumentForRag /*, ... other functions */ }
```

## 5. Python Bridge (`lib/python_bridge.py` & `lib/rag_processing.py`)

The Python bridge consists of two main files:
- `lib/python_bridge.py`: Handles the main interface, argument parsing, calling the `zlibrary` library, and orchestrating calls to the RAG processing module.
- `lib/rag_processing.py`: Contains the specific logic for processing document content (EPUB, TXT, PDF), including robustness enhancements, and saving the results.

This section details the relevant functions within `lib/rag_processing.py` and how they are called by `lib/python_bridge.py`.

```python
# File: lib/rag_processing.py (Contains processing logic)
# File: lib/python_bridge.py (Calls functions below)
# Dependencies: zlibrary, ebooklib, beautifulsoup4, lxml, PyMuPDF, httpx, aiofiles, pytesseract, Pillow
# Standard Libs: json, sys, os, argparse, logging, pathlib, asyncio, urllib.parse, re, io
import json
import sys
import os
import argparse
import logging
import asyncio
from pathlib import Path
from urllib.parse import urljoin
from zlibrary import ZLibrary # Assumed installed via venv

# --- Attempt to import processing libraries ---
try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False

try:
    import fitz # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image # Pillow for image handling with pytesseract
    import io
    OCR_LIBS_AVAILABLE = True
except ImportError:
    OCR_LIBS_AVAILABLE = False

# --- Attempt to import download/scraping libraries ---
try:
    import httpx
    import aiofiles
    DOWNLOAD_LIBS_AVAILABLE = True
except ImportError:
    DOWNLOAD_LIBS_AVAILABLE = False


# --- Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
SUPPORTED_FORMATS = ['.epub', '.txt', '.pdf']
PROCESSED_OUTPUT_DIR = Path("./processed_rag_output")
DOWNLOAD_SELECTOR = "a.btn.btn-primary.dlButton" # As per ADR-002

# --- Custom Exceptions ---
class FileSaveError(Exception):
    """Custom exception for errors during processed file saving."""
    pass

class DownloadScrapeError(Exception):
    """Custom exception for errors during download link scraping."""
    pass

class DownloadExecutionError(Exception):
    """Custom exception for errors during the final file download."""
    pass


# --- Helper Functions for Processing (in lib/rag_processing.py) ---

def _html_to_text(html_content):
    """Extracts plain text from HTML using BeautifulSoup."""
    if not html_content: return ""
    soup = BeautifulSoup(html_content, 'lxml')
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()
    lines = (line.strip() for line in soup.get_text().splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    return text

def _process_epub(file_path: Path, output_format: str = "txt") -> str:
    """Processes an EPUB file. Includes preprocessing and optional Markdown generation."""
*   **Preprocessing:** Calls helper functions (`identify_and_remove_front_matter`, `extract_and_format_toc`) to handle front matter and Table of Contents before main processing.
*   **Markdown Generation:** When `output_format='markdown'`, uses `BeautifulSoup` to parse HTML, mapping tags (`h1-h6`, lists, `epub:type="noteref/footnote"`) to Markdown. See [RAG Markdown Spec](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/rag-markdown-generation-spec.md).
    if not EBOOKLIB_AVAILABLE:
        raise ImportError("Required library 'ebooklib' is not installed.")
    logging.info(f"Processing EPUB file: {file_path}, format: {output_format}")
    # ... (Implementation includes reading EPUB, extracting HTML/text) ...
    # ... (Calls preprocessing helpers identify_and_remove_front_matter, extract_and_format_toc) ...
    # ... (If output_format == 'markdown', calls _epub_node_to_markdown) ...
    # ... (Else, calls _html_to_text) ...
    # ... (Returns final processed string, prepended with Title/ToC if applicable) ...
    # Placeholder for actual implementation details
    book = epub.read_epub(str(file_path))
    all_content_lines = [] # Collect lines for preprocessing
    items = book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
    for item in items:
        # Extract lines/structure suitable for preprocessing
        pass # Placeholder

    # cleaned_lines, title = identify_and_remove_front_matter(all_content_lines)
    # final_content_lines, formatted_toc = extract_and_format_toc(cleaned_lines, output_format)

    # Process final_content_lines based on output_format
    # full_text = process_lines_to_format(final_content_lines, output_format)

    # Prepend title and toc
    # final_output = f"# {title}\n\n{formatted_toc}\n\n{full_text}"

    # Simplified return for now
    full_text = "Placeholder for processed EPUB content"
    logging.info(f"Finished EPUB: {file_path}. Length: {len(full_text)}")
    return full_text # Return final_output in full implementation

def _process_txt(file_path: Path, output_format: str = "txt") -> str: # Added output_format, though likely unused for TXT
    """Processes a TXT file. Returns text string."""
    logging.info(f"Processing TXT file: {file_path}, format: {output_format}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f: text = f.read()
        logging.info(f"Finished TXT (UTF-8): {file_path}. Length: {len(text)}")
        return text
    except UnicodeDecodeError:
        logging.warning(f"UTF-8 failed for {file_path}. Trying 'latin-1'.")
        try:
            with open(file_path, 'r', encoding='latin-1') as f: text = f.read()
            logging.info(f"Finished TXT (latin-1): {file_path}. Length: {len(text)}")
            return text
        except Exception as e:
            logging.error(f"Failed to read TXT {file_path} even with latin-1: {e}")
            raise RuntimeError(f"Failed to read TXT file {file_path}: {e}") from e
    except Exception as e:
        logging.error(f"Failed to read TXT file {file_path}: {e}")
        raise RuntimeError(f"Failed to read TXT file {file_path}: {e}") from e

def _process_pdf(file_path: Path, output_format: str = "txt") -> str:
    """Processes a PDF using PyMuPDF, quality detection, optional OCR, and preprocessing."""
    if not PYMUPDF_AVAILABLE:
        raise ImportError("Required library 'PyMuPDF' is not installed.")
    logging.info(f"Processing PDF: {file_path}, format: {output_format}")

*   **Quality Detection & OCR:** Calls `detect_pdf_quality`. If quality is low ('IMAGE_ONLY', 'TEXT_LOW'), triggers `run_ocr_on_pdf` (requires `pytesseract`, `Pillow`). Handles OCR failures gracefully. See [RAG Robustness Spec](./rag-robustness-enhancement-spec.md#33-preprocessing--ocr-integration).
*   **Preprocessing:** Calls helper functions (`identify_and_remove_front_matter`, `extract_and_format_toc`) to handle front matter and Table of Contents before main processing.
*   **Markdown Generation:** When `output_format='markdown'`, uses `PyMuPDF`'s dictionary output (`page.get_text("dict")`) and heuristics to map structure to Markdown. See [RAG Markdown Spec](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/rag-markdown-generation-spec.md).
*   **Garbled Text Detection:** Incorporates checks (e.g., `detect_garbled_text`) to identify potentially poor extraction results.

    doc = None
    try:
        doc = fitz.open(str(file_path))
        if doc.is_encrypted:
            logging.warning(f"PDF is encrypted: {file_path}")
            raise ValueError("PDF is encrypted")

        quality_category = detect_pdf_quality(doc)
        logging.info(f"Detected PDF quality for {file_path}: {quality_category}")

        processed_text = None
        ocr_triggered = False

        if quality_category in ["IMAGE_ONLY", "TEXT_LOW"]:
            if OCR_LIBS_AVAILABLE:
                try:
                    logging.info(f"Triggering OCR for {file_path} due to quality: {quality_category}")
                    processed_text = run_ocr_on_pdf(str(file_path))
                    ocr_triggered = True
                except Exception as ocr_err:
                    logging.error(f"OCR failed for {file_path}: {ocr_err}. Falling back.")
                    if quality_category == "TEXT_LOW":
                        processed_text = _extract_text_pymupdf(doc, output_format) # Fallback extraction
                    else:
                        processed_text = "" # No text if OCR fails on image-only
            else:
                logging.warning(f"OCR libraries not available, cannot process {quality_category} PDF: {file_path}")
                processed_text = "" if quality_category == "IMAGE_ONLY" else _extract_text_pymupdf(doc, output_format)
        else: # TEXT_HIGH, MIXED, or fallback
             processed_text = _extract_text_pymupdf(doc, output_format)
             # Optional: Could add OCR fallback for MIXED if PyMuPDF yield is low

        if processed_text is None: processed_text = ""

        # --- Preprocessing ---
        # Placeholder: Assume processed_text is now a list of lines or similar
        # text_lines = processed_text.splitlines()
        # cleaned_lines, title = identify_and_remove_front_matter(text_lines)
        # final_content_lines, formatted_toc = extract_and_format_toc(cleaned_lines, output_format)
        # Re-join or re-process final_content_lines based on format
        # final_output = f"# {title}\n\n{formatted_toc}\n\n{processed_text_from_final_lines}"
        # --- End Preprocessing ---

        # --- Garbled Text Check ---
        # if detect_garbled_text(processed_text):
        #    logging.warning(f"Potential garbled text detected in {file_path}")
        # --- End Garbled Text Check ---

        # Simplified return for now
        final_output = processed_text
        logging.info(f"Finished PDF: {file_path}. Length: {len(final_output)}. OCR Triggered: {ocr_triggered}")
        return final_output

    except fitz.fitz.FitzError as fitz_error:
        logging.error(f"PyMuPDF error processing {file_path}: {fitz_error}")
        raise RuntimeError(f"Error opening/processing PDF: {file_path} - {fitz_error}") from fitz_error
    except Exception as e:
        if isinstance(e, (ValueError, FileNotFoundError)): raise e
        logging.error(f"Unexpected error processing PDF {file_path}: {e}")
        raise RuntimeError(f"Error opening/processing PDF: {file_path} - {e}") from e
    finally:
        if doc:
            try: doc.close()
            except Exception as close_error: logging.error(f"Error closing PDF {file_path}: {close_error}")


def _extract_text_pymupdf(doc: fitz.Document, output_format: str) -> str:
    """Helper to extract text using PyMuPDF based on output format."""
    # Placeholder: Implement actual extraction logic here
    # If output_format == 'markdown', use get_text("dict") and heuristics
    # Else, use get_text("text")
    all_text = []
    for page_num in range(len(doc)):
        try:
            page = doc.load_page(page_num)
            # Simplified: just get basic text for now
            text = page.get_text("text")
            if text: all_text.append(text.strip())
        except Exception as page_error:
            logging.warning(f"Could not process page {page_num + 1} with PyMuPDF: {page_error}")
    full_text = "\n\n".join(all_text).strip()
    return full_text


# --- Robustness Helper Functions (in lib/rag_processing.py) ---

def detect_pdf_quality(doc: fitz.Document) -> str:
    """Analyzes PDF and returns quality category (TEXT_HIGH, TEXT_LOW, IMAGE_ONLY, MIXED, EMPTY)."""
    # Implementation based on heuristics (text density, image area, fonts)
    # See RAG Robustness Spec for pseudocode.
    logging.debug("Detecting PDF quality...")
    # Placeholder implementation
    return "TEXT_HIGH"

def run_ocr_on_pdf(pdf_path_str: str, lang: str = 'eng') -> str:
    """Runs Tesseract OCR on a PDF. Requires pytesseract, Pillow."""
    if not OCR_LIBS_AVAILABLE:
        raise ImportError("Required libraries 'pytesseract' and 'Pillow' are not installed for OCR.")
    logging.info(f"Running OCR on {pdf_path_str}...")
    # Implementation involves rendering pages (PyMuPDF), calling pytesseract.image_to_string
    # See RAG Robustness Spec for pseudocode.
    # Placeholder implementation
    return "Placeholder OCR Text"

def identify_and_remove_front_matter(content_lines: list) -> (list, str):
     """Identifies title, removes front matter lines. Returns (cleaned_lines, title)."""
     logging.debug("Identifying and removing front matter...")
     # Implementation uses heuristics (keywords, page numbers, layout patterns).
     # See RAG Robustness Spec for pseudocode.
     # Placeholder implementation
     title = "Placeholder Title"
     return (content_lines, title) # Return original lines for now

def extract_and_format_toc(content_lines: list, output_format: str) -> (list, str):
     """Extracts ToC, formats if Markdown. Returns (remaining_lines, formatted_toc_string)."""
     logging.debug("Extracting and formatting ToC...")
     # Implementation uses heuristics (keywords, line formats like 'Chapter...Page').
     # See RAG Robustness Spec for pseudocode.
     # Placeholder implementation
     formatted_toc = "Placeholder ToC"
     return (content_lines, formatted_toc) # Return original lines for now

def detect_garbled_text(text: str) -> bool:
    """Detects if text is likely garbled (e.g., high non-alpha ratio, excessive repetition)."""
    logging.debug("Detecting garbled text...")
    # Implementation uses heuristics.
    # See RAG Robustness Spec TDD Cycle 23 for potential logic.
    # Placeholder implementation
    return False


# --- Helper Function for Saving (in lib/rag_processing.py) ---

def _save_processed_text(original_file_path: Path, text_content: str, output_format: str = "txt") -> Path:
    """Saves the processed text content to a file."""
    if text_content is None: # Allow empty string, but not None
         raise ValueError("Cannot save None content.")

    try:
        PROCESSED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        base_name = original_file_path.name
        output_filename = f"{base_name}.processed.{output_format}"
        output_file_path = PROCESSED_OUTPUT_DIR / output_filename
        logging.info(f"Saving processed text to: {output_file_path}")
        with open(output_file_path, 'w', encoding='utf-8') as f:
            f.write(text_content)
        logging.info(f"Successfully saved processed text for {original_file_path.name}")
        return output_file_path
    except OSError as e:
        logging.error(f"OS error saving processed file {output_file_path}: {e}")
        raise FileSaveError(f"Failed to save processed file due to OS error: {e}") from e
    except Exception as e:
        logging.error(f"Unexpected error saving processed file {output_file_path}: {e}")
        raise FileSaveError(f"An unexpected error occurred while saving processed file: {e}") from e


# --- Helper for Scraping and Downloading (async, in lib/python_bridge.py) ---

async def _scrape_and_download(book_page_url: str, output_dir_str: str) -> str:
    """Fetches book page, scrapes download link, and downloads the file."""
    if not DOWNLOAD_LIBS_AVAILABLE:
        raise ImportError("Required libraries 'httpx' and 'aiofiles' are not installed.")

    headers = {'User-Agent': 'Mozilla/5.0 ...'} # Add appropriate User-Agent
    timeout = 30.0

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
        # 1. Fetch book page
        try:
            logging.info(f"Fetching book page: {book_page_url}")
            response = await client.get(book_page_url)
            response.raise_for_status() # Check for HTTP errors
        except httpx.RequestError as exc:
            logging.error(f"Network error fetching book page {book_page_url}: {exc}")
            raise DownloadScrapeError(f"Network error fetching book page: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            logging.error(f"HTTP error {exc.response.status_code} fetching book page {book_page_url}")
            raise DownloadScrapeError(f"HTTP error {exc.response.status_code} fetching book page.") from exc

        # 2. Parse and Scrape download link
        try:
            soup = BeautifulSoup(response.text, 'lxml')
            link_element = soup.select_one(DOWNLOAD_SELECTOR)
            if not link_element or not link_element.has_attr('href'):
                logging.error(f"Could not find download link using selector '{DOWNLOAD_SELECTOR}' on {book_page_url}")
                raise DownloadScrapeError("Download link selector not found on book page.")
            relative_url = link_element['href']
            download_url = urljoin(str(response.url), relative_url)
            logging.info(f"Found download URL: {download_url}")
        except Exception as exc:
            logging.exception(f"Error parsing book page {book_page_url}")
            raise DownloadScrapeError(f"Error parsing book page: {exc}") from exc

        # 3. Determine output path and filename
        output_dir = Path(output_dir_str)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Try to get filename from Content-Disposition or URL
        filename = download_url.split('/')[-1].split('?')[0] or "downloaded_book" # Basic fallback

        # 4. Download the file
        try:
            logging.info(f"Starting download from {download_url}")
            async with client.stream('GET', download_url) as download_response:
                # Try to get filename from header first
                content_disposition = download_response.headers.get('content-disposition')
                if content_disposition:
                    import re
                    fname_match = re.search(r'filename\*?=(?:UTF-8\'\')?([^;\n]*)', content_disposition, re.IGNORECASE)
                    if fname_match:
                        potential_fname = fname_match.group(1).strip('"')
                        # Basic sanitization
                        potential_fname = re.sub(r'[\\/*?:"<>|]', "_", potential_fname)
                        if potential_fname:
                            filename = potential_fname

                output_path = output_dir / filename
                logging.info(f"Saving download to: {output_path}")

                download_response.raise_for_status() # Check download request status

                async with aiofiles.open(output_path, 'wb') as f:
                    async for chunk in download_response.aiter_bytes():
                        await f.write(chunk)

            logging.info(f"Download complete: {output_path}")
            return str(output_path)

        except httpx.RequestError as exc:
            logging.error(f"Network error during download from {download_url}: {exc}")
            raise DownloadExecutionError(f"Network error during download: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            logging.error(f"HTTP error {exc.response.status_code} during download from {download_url}")
            raise DownloadExecutionError(f"HTTP error {exc.response.status_code} during download.") from exc
        except OSError as exc:
            logging.error(f"File system error saving download to {output_path}: {exc}")
            raise DownloadExecutionError(f"File system error saving download: {exc}") from exc
        except Exception as exc:
            logging.exception(f"Unexpected error during download/saving from {download_url}")
            raise DownloadExecutionError(f"Unexpected error during download: {exc}") from exc


# --- Core Bridge Functions (in lib/python_bridge.py, calling rag_processing.py) ---

def process_document(file_path_str: str, output_format: str = "txt") -> dict:
    """
    Detects file type, calls the appropriate processing function, saves the result,
    and returns a dictionary containing the processed file path (or null).
    """
    file_path = Path(file_path_str)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path_str}")

    _, ext = os.path.splitext(file_path.name) # Use os.path.splitext for reliability
    ext = ext.lower()

    processed_text = None
    try:
        if ext == '.epub':
            processed_text = _process_epub(file_path, output_format)
        elif ext == '.txt':
            processed_text = _process_txt(file_path, output_format)
        elif ext == '.pdf':
            processed_text = _process_pdf(file_path, output_format)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        # Save the processed text and return the path
        if processed_text: # Only save if text was extracted
            processed_file_path = _save_processed_text(file_path, processed_text, output_format)
            return {"processed_file_path": str(processed_file_path)}
        else:
            logging.warning(f"No text extracted from {file_path}, returning null path.")
            return {"processed_file_path": None} # Return null if no text

    except (ImportError, ValueError, FileNotFoundError, RuntimeError, FileSaveError) as e:
        logging.error(f"Error processing {file_path}: {e}")
        raise e # Re-raise to be caught by main handler
    except Exception as e:
        logging.exception(f"Unexpected error processing {file_path}")
        raise RuntimeError(f"Unexpected error processing {file_path}: {e}") from e


async def download_book(book_details: dict, output_dir: str, process_for_rag: bool, processed_output_format: str) -> dict:
    """
    Downloads a book using scraping, optionally processes it, saves results,
    and returns paths.
    """
    if not book_details or 'url' not in book_details:
        raise ValueError("Missing 'book_details' or 'url' within book_details.")

    book_page_url = book_details['url']
    logging.info(f"Starting download process for URL: {book_page_url}")

    try:
        # Step 1: Scrape and Download the original file
        downloaded_file_path_str = await _scrape_and_download(book_page_url, output_dir)
        downloaded_file_path = Path(downloaded_file_path_str)

        result = {"file_path": downloaded_file_path_str, "processed_file_path": None}

        # Step 2: Optionally process the downloaded file
        if process_for_rag:
            logging.info(f"Processing downloaded file for RAG: {downloaded_file_path}")
            try:
                # Call the synchronous processing function (run in executor if truly blocking)
                processing_result = process_document(str(downloaded_file_path), processed_output_format)
                result["processed_file_path"] = processing_result.get("processed_file_path") # Can be None
            except Exception as processing_error:
                # Log error but don't fail the whole download, return original path
                logging.error(f"Failed to process document after download: {processing_error}")
                # processed_file_path remains None

        return result

    except (DownloadScrapeError, DownloadExecutionError) as e:
        logging.error(f"Download failed for {book_page_url}: {e}")
        raise RuntimeError(f"Download failed: {e}") from e
    except Exception as e:
        logging.exception(f"Unexpected error during download/processing for {book_page_url}")
        raise RuntimeError(f"Unexpected error during download/processing: {e}") from e


# --- Main Execution Block (Handles calls from Node.js) ---

async def main(func_name: str, args_json: str):
    """Main entry point called by Node.js."""
    try:
        args = json.loads(args_json)
        logging.info(f"Executing Python function: {func_name} with args: {args}")

        if func_name == 'download_book':
            result = await download_book(
                args.get('book_details'),
                args.get('output_dir', './downloads'),
                args.get('process_for_rag', False),
                args.get('processed_output_format', 'txt')
            )
        elif func_name == 'process_document':
            # Run synchronous function in executor to avoid blocking event loop if it becomes complex
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, # Use default executor
                process_document,
                args.get('file_path'),
                args.get('output_format', 'txt')
            )
        # Add other function handlers here...
        # elif func_name == 'search_books':
        #     # ... call search_books ...
        #     pass
        else:
            raise ValueError(f"Unknown function name: {func_name}")

        print(json.dumps({"success": True, "result": result}))

    except Exception as e:
        logging.exception(f"Error executing {func_name}")
        print(json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("func_name", help="Name of the function to call")
    parser.add_argument("args_json", help="JSON string of arguments")
    script_args = parser.parse_args()

    asyncio.run(main(script_args.func_name, script_args.args_json))

```

## 6. Python Dependency Management

1.  **Add New Dependencies:** Ensure the following are added to the project's Python dependencies:
    *   `ebooklib`: For parsing EPUB files.
    *   `beautifulsoup4`: For cleaning HTML content.
    *   `lxml`: Recommended HTML parser for `beautifulsoup4`.
    *   `PyMuPDF`: For parsing PDF files.
    *   `httpx`: For async HTTP requests (downloading/scraping).
    *   `aiofiles`: For async file writing (downloading).
    *   `pytesseract`: For OCR integration.
    *   `Pillow`: Image library needed by `pytesseract`.

2.  **Update `requirements.txt`:**
    ```text
    # requirements.txt
    zlibrary # Or the specific version/fork used
    ebooklib
    beautifulsoup4
    lxml
    PyMuPDF
    httpx
    aiofiles
    pytesseract
    Pillow
    ```

3.  **Update `lib/venv-manager.ts`:** Ensure `installDependencies` uses `pip install -r requirements.txt`. (Pseudocode remains the same as v2.0, assuming it correctly uses `-r`).

    ```pseudocode
    // File: src/lib/venv-manager.ts (Conceptual Snippet for installDependencies)

    FUNCTION installDependencies(venvDirPath):
      pipPath = getPlatformPipPath(venvDirPath)
      requirementsPath = path.resolve(__dirname, '..', '..', 'requirements.txt') // Adjust path relative to dist/lib

      IF NOT fs.existsSync(requirementsPath):
          LOG `requirements.txt not found at ${requirementsPath}, skipping.`
          RETURN
      ENDIF

      LOG `Installing/updating dependencies from ${requirementsPath} using: ${pipPath}`
      // Use '-r' and '--upgrade'
      // Add --no-cache-dir and --force-reinstall based on previous fixes
      command = `"${pipPath}" install --no-cache-dir --force-reinstall --upgrade -r "${requirementsPath}"`
      TRY
        EXECUTE command synchronously
        LOG "Dependencies installed/updated successfully."
      CATCH error
        THROW Error(`Failed to install Python dependencies: ${error.message}`)
      ENDTRY
    END FUNCTION
    ```

## 7. TDD Anchors (Updated)

These anchors guide the Test-Driven Development process. Tests should cover success cases, error handling (file not found, unsupported format, processing errors, save errors, OCR errors, preprocessing errors), and edge cases (empty files, encrypted PDFs, image-only PDFs, garbled text). **Refer to `docs/rag-robustness-enhancement-spec.md` for more detailed TDD anchors related to quality detection, OCR, and preprocessing.**

*   **`lib/rag_processing.py` (Python - `pytest`):**
    *   Tests for `detect_pdf_quality` (TEXT_HIGH, TEXT_LOW, IMAGE_ONLY, MIXED, EMPTY).
    *   Tests for `run_ocr_on_pdf` (success, Tesseract not found, image processing errors).
    *   Tests for `identify_and_remove_front_matter` (basic removal, title preservation, no front matter).
    *   Tests for `extract_and_format_toc` (basic extraction, Markdown formatting, no ToC).
    *   Tests for `detect_garbled_text` (garbled vs. clean text).
    *   `test_process_epub_success`: Verify text/Markdown extraction, including preprocessing.
    *   `test_process_epub_error`: Mock `epub.read_epub` error.
    *   `test_process_txt_success`: Verify text extraction.
    *   `test_process_txt_read_error`: Mock `open` error.
    *   `test_process_pdf_success_text_high`: Verify PyMuPDF extraction, including preprocessing.
    *   `test_process_pdf_triggers_ocr`: Mock `detect_pdf_quality` (LOW/IMAGE), verify `run_ocr_on_pdf` called.
    *   `test_process_pdf_handles_ocr_failure`: Mock `run_ocr_on_pdf` error, verify fallback/empty result.
    *   `test_process_pdf_encrypted`: Verify `ValueError`.
    *   `test_process_pdf_open_error`: Mock `fitz.open` error.
    *   `test_save_processed_text_success`: Verify file saving.
    *   `test_save_processed_text_os_error`: Mock `open`/`mkdir` error.
*   **`lib/python_bridge.py` (Python - `pytest`):**
    *   `test_process_document_routes_correctly`: Verify calls to `rag_processing` functions based on extension.
    *   `test_process_document_handles_processing_errors`: Verify errors from `rag_processing` are caught.
    *   `test_download_book_calls_scrape_and_download`: Verify helper call.
    *   `test_download_book_calls_process_document_when_flag_true`: Verify call to `process_document`.
    *   `test_download_book_handles_scrape_error`: Mock `_scrape_and_download` error.
    *   `test_download_book_handles_download_error`: Mock `_scrape_and_download` error.
    *   `test_download_book_handles_processing_error`: Mock `process_document` error.
    *   `test_scrape_and_download_success`: Mock `httpx`, `aiofiles`.
    *   `test_scrape_and_download_fetch_error`: Mock `client.get` error.
    *   `test_scrape_and_download_scrape_error`: Mock `BeautifulSoup` error.
    *   `test_scrape_and_download_download_error`: Mock `client.stream`/`aiofiles.open` error.
    *   `test_main_routes_correctly`: Verify `main` calls correct functions.
    *   `test_main_handles_errors`: Verify `main` catches exceptions.
*   **`lib/zlibrary-api.ts` (Node.js - Jest):**
    *   `test_downloadBookToFile_success_no_processing`: Mock `callPythonFunction`.
    *   `test_downloadBookToFile_success_with_processing`: Mock `callPythonFunction`.
    *   `test_downloadBookToFile_python_error`: Mock `callPythonFunction` rejection.
    *   `test_processDocumentForRag_success`: Mock `callPythonFunction`.
    *   `test_processDocumentForRag_python_error`: Mock `callPythonFunction` rejection.
*   **`index.ts` (Node.js - Jest):**
    *   Verify tool schemas are registered.
    *   Verify `tools/call` handler routes correctly.
    *   Verify input validation works.