# Tesseract Integration Guide for RAG Pipeline

## Quick Start

This guide shows how to integrate Tesseract OCR as a fallback for strikethrough-corrupted PDF pages.

## Installation

```bash
# System dependencies (Ubuntu/Debian)
sudo apt-get install tesseract-ocr

# Python dependencies
uv pip install pytesseract pdf2image Pillow

# Or via pip
pip install pytesseract pdf2image Pillow
```

## Basic Integration Pattern

### 1. Detection Function

```python
import re

def has_strikethrough_corruption(text: str) -> bool:
    """
    Detect common strikethrough corruption patterns.

    Returns:
        True if text shows signs of strikethrough corruption
    """
    patterns = [
        r'\)\(',           # )( pattern (X marks from strikethrough)
        r'\^[A-Z]©»\^',    # ^B©»^ pattern (encoded strikethrough)
        r'\^e',            # ^e pattern
        r'\^fi',           # ^fi pattern
        r'[A-Z]ekig\^',    # TJekig^ pattern
        r'Sfcf\w+',        # Sfcfös pattern
        r'wouJd',          # Common OCR-like errors that aren't OCR
        r'generaJ',        # J instead of l
        r'Jimits',         # J instead of l
    ]

    for pattern in patterns:
        if re.search(pattern, text):
            return True

    return False
```

### 2. Extraction Functions

```python
import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
from pathlib import Path

def extract_page_pymupdf(pdf_path: Path, page_num: int) -> str:
    """
    Extract text from a specific page using PyMuPDF.

    Args:
        pdf_path: Path to PDF file
        page_num: Page number (0-indexed)

    Returns:
        Extracted text
    """
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    text = page.get_text()
    doc.close()
    return text


def extract_page_tesseract(pdf_path: Path, page_num: int, dpi: int = 300) -> str:
    """
    Extract text from a specific page using Tesseract OCR.

    Args:
        pdf_path: Path to PDF file
        page_num: Page number (0-indexed)
        dpi: Resolution for image conversion (higher = better quality)

    Returns:
        Extracted text via OCR
    """
    # Convert specific page to image
    images = convert_from_path(
        pdf_path,
        dpi=dpi,
        first_page=page_num + 1,  # pdf2image uses 1-indexed pages
        last_page=page_num + 1
    )

    if not images:
        return ""

    # Run Tesseract OCR
    text = pytesseract.image_to_string(images[0], lang='eng')
    return text
```

### 3. Hybrid Extraction

```python
def extract_page_hybrid(pdf_path: Path, page_num: int) -> dict:
    """
    Extract page text with automatic fallback to Tesseract.

    Returns:
        {
            'text': str,
            'method': 'pymupdf' | 'tesseract',
            'corrupted': bool,
            'confidence': float
        }
    """
    # Try PyMuPDF first (fast)
    pymupdf_text = extract_page_pymupdf(pdf_path, page_num)

    # Check for corruption
    corrupted = has_strikethrough_corruption(pymupdf_text)

    if corrupted:
        # Fallback to Tesseract
        tesseract_text = extract_page_tesseract(pdf_path, page_num)
        return {
            'text': tesseract_text,
            'method': 'tesseract',
            'corrupted': True,
            'confidence': 0.85  # Tesseract is good but not perfect
        }
    else:
        return {
            'text': pymupdf_text,
            'method': 'pymupdf',
            'corrupted': False,
            'confidence': 1.0
        }
```

## Integration into RAG Pipeline

### Modify `lib/rag_processing.py`

```python
# Add to imports
import pytesseract
from pdf2image import convert_from_path

# Add hybrid extraction to _extract_text_from_pdf method
def _extract_text_from_pdf(self, file_path: Path) -> str:
    """Extract text from PDF using PyMuPDF with Tesseract fallback."""
    doc = fitz.open(file_path)
    all_text = []
    corrupted_pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()

        # Check for corruption
        if self._has_strikethrough_corruption(text):
            corrupted_pages.append(page_num + 1)  # 1-indexed for logging
            # Fallback to Tesseract
            text = self._extract_page_tesseract(file_path, page_num)

        all_text.append(text)

    doc.close()

    if corrupted_pages:
        self.logger.info(
            f"Used Tesseract OCR for {len(corrupted_pages)} corrupted pages: {corrupted_pages}"
        )

    return '\n\n'.join(all_text)

def _has_strikethrough_corruption(self, text: str) -> bool:
    """Detect strikethrough corruption patterns."""
    patterns = [
        r'\)\(',
        r'\^[A-Z]©»\^',
        r'\^e',
        r'\^fi',
        r'[A-Z]ekig',
        r'Sfcf',
    ]
    return any(re.search(pattern, text) for pattern in patterns)

def _extract_page_tesseract(self, file_path: Path, page_num: int) -> str:
    """Extract single page using Tesseract OCR."""
    images = convert_from_path(
        file_path,
        dpi=300,
        first_page=page_num + 1,
        last_page=page_num + 1
    )
    if images:
        return pytesseract.image_to_string(images[0], lang='eng')
    return ""
```

## Performance Optimization

### 1. Caching

```python
from functools import lru_cache
import hashlib

def get_page_hash(pdf_path: Path, page_num: int) -> str:
    """Generate unique hash for a PDF page."""
    return hashlib.md5(f"{pdf_path}:{page_num}".encode()).hexdigest()

@lru_cache(maxsize=100)
def extract_page_cached(pdf_path: str, page_num: int) -> str:
    """Cached version of page extraction."""
    return extract_page_tesseract(Path(pdf_path), page_num)
```

### 2. Parallel Processing

```python
from concurrent.futures import ThreadPoolExecutor

def extract_corrupted_pages_parallel(pdf_path: Path, corrupted_pages: list) -> dict:
    """Extract multiple corrupted pages in parallel."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(extract_page_tesseract, pdf_path, page_num): page_num
            for page_num in corrupted_pages
        }

        results = {}
        for future in futures:
            page_num = futures[future]
            results[page_num] = future.result()

    return results
```

### 3. Selective DPI

```python
def extract_page_tesseract_adaptive(pdf_path: Path, page_num: int) -> str:
    """
    Use adaptive DPI based on page size.

    Small pages (< 500px) → 400 DPI
    Normal pages → 300 DPI
    Large pages (> 1000px) → 200 DPI
    """
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    width = page.rect.width

    if width < 500:
        dpi = 400
    elif width > 1000:
        dpi = 200
    else:
        dpi = 300

    doc.close()

    images = convert_from_path(pdf_path, dpi=dpi, first_page=page_num+1, last_page=page_num+1)
    if images:
        return pytesseract.image_to_string(images[0], lang='eng')
    return ""
```

## Testing

### Run Comparison Test

```bash
# Test Tesseract vs PyMuPDF on sample PDFs
# Historical script: https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/scripts/archive/test_tesseract_comparison.py
# Run only with the separately preserved processing installation.
```

### Verify Installation

```python
import pytesseract
from pdf2image import convert_from_path

# Test Tesseract
print(pytesseract.get_tesseract_version())

# Test pdf2image
images = convert_from_path('test.pdf', first_page=1, last_page=1)
print(f"Converted {len(images)} pages")
```

## Error Handling

```python
def extract_page_robust(pdf_path: Path, page_num: int) -> dict:
    """
    Robust extraction with comprehensive error handling.
    """
    result = {
        'text': '',
        'method': None,
        'error': None,
        'corrupted': False
    }

    try:
        # Try PyMuPDF
        pymupdf_text = extract_page_pymupdf(pdf_path, page_num)
        result['corrupted'] = has_strikethrough_corruption(pymupdf_text)

        if result['corrupted']:
            # Try Tesseract
            try:
                tesseract_text = extract_page_tesseract(pdf_path, page_num)
                result['text'] = tesseract_text
                result['method'] = 'tesseract'
            except Exception as e:
                # Tesseract failed, use PyMuPDF despite corruption
                result['text'] = pymupdf_text
                result['method'] = 'pymupdf'
                result['error'] = f"Tesseract failed: {str(e)}"
        else:
            result['text'] = pymupdf_text
            result['method'] = 'pymupdf'

    except Exception as e:
        result['error'] = f"Extraction failed: {str(e)}"

    return result
```

## Configuration

### Environment Variables

```bash
# Optional: Configure Tesseract path
export TESSERACT_CMD="/usr/bin/tesseract"

# Optional: Set default DPI
export TESSERACT_DPI="300"

# Optional: Enable/disable Tesseract fallback
export ENABLE_TESSERACT_FALLBACK="true"
```

### Python Configuration

```python
import os
import pytesseract

# Set Tesseract path (if needed)
if os.getenv('TESSERACT_CMD'):
    pytesseract.pytesseract.tesseract_cmd = os.getenv('TESSERACT_CMD')

# Get DPI setting
DEFAULT_DPI = int(os.getenv('TESSERACT_DPI', '300'))

# Check if fallback enabled
ENABLE_FALLBACK = os.getenv('ENABLE_TESSERACT_FALLBACK', 'true').lower() == 'true'
```

## Monitoring

### Add Metrics

```python
class ExtractionMetrics:
    def __init__(self):
        self.pymupdf_count = 0
        self.tesseract_count = 0
        self.corrupted_count = 0
        self.tesseract_time = 0.0

    def log(self):
        total = self.pymupdf_count + self.tesseract_count
        if total > 0:
            tesseract_pct = (self.tesseract_count / total) * 100
            corruption_pct = (self.corrupted_count / total) * 100
            avg_time = self.tesseract_time / self.tesseract_count if self.tesseract_count > 0 else 0

            print(f"Extraction Stats:")
            print(f"  Total pages: {total}")
            print(f"  PyMuPDF: {self.pymupdf_count} ({100-tesseract_pct:.1f}%)")
            print(f"  Tesseract: {self.tesseract_count} ({tesseract_pct:.1f}%)")
            print(f"  Corrupted: {self.corrupted_count} ({corruption_pct:.1f}%)")
            print(f"  Avg Tesseract time: {avg_time:.2f}s")
```

## Expected Results

Based on testing with Derrida and Heidegger texts:

- **Tesseract Success Rate**: 100% on strikethrough-corrupted pages
- **Performance Impact**: +2-3 seconds per Tesseract page
- **Corruption Detection Rate**: ~10-20% of philosophical texts
- **Overall Slowdown**: ~5-10% (most pages still use fast PyMuPDF)

## References

- Test Results: `test_files/tesseract_comparison_report.txt`
- Summary: `test_files/tesseract_findings_summary.md`
- Test Script: [historical test script](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/scripts/archive/test_tesseract_comparison.py)
- JSON Data: `test_files/tesseract_comparison_data.json`
