# Rigorous Real-World TDD Strategy

**Date**: 2025-10-18
**Context**: Shift from unit testing to real-world validation
**Lesson**: Today's real PDF testing caught architectural flaw that unit tests missed

---

## The Problem with Current Testing

### What We Had (Insufficient)

```python
# Unit test (mocked):
@patch('detect_strikethrough')
def test_xmark_detection(mock_detect):
    mock_detect.return_value = XMarkResult(has_xmarks=True)
    result = process_region(...)
    assert result.is_strikethrough()  # ✅ Passes
```

**Problem**: Test passes but doesn't validate:
- Real PDF rendering works
- opencv actually detects X-marks
- Bboxes align correctly
- Performance is acceptable
- Output is philosophically accurate

**Result**: Architecture bug (Stage 2 dependency on Stage 1) not caught until real PDF testing!

---

## Rigorous Real-World TDD

### The New Approach

```python
# Real-world test (no mocks):
def test_derrida_sous_rature_detection():
    """Test with ACTUAL Derrida PDF containing known X-marks."""

    # Ground truth (manually verified)
    pdf_path = Path('test_files/derrida_of_grammatology.pdf')
    expected_xmarks = [
        {'page': 135, 'word': 'is', 'bbox': (312.21, 172.998, 326.38, 186.87)},
        # ... all known X-marks
    ]

    # Process with real pipeline
    result = process_pdf(pdf_path, output_format='markdown')

    # Validate actual output
    assert 'is' in result or '~~is~~' in result, "Sous-rature word not recovered"
    assert 'sous_rature' in quality_flags, "X-mark not detected"

    # Validate performance
    assert processing_time < 10.0, f"Too slow: {processing_time}s"
```

**Benefits**:
- ✅ Tests entire pipeline end-to-end
- ✅ Real PDF rendering, real opencv, real PyMuPDF
- ✅ Catches integration issues
- ✅ Performance validation
- ✅ Output quality verification

---

## Test Corpus Design

### Required Test PDFs (Ground Truth)

#### Category 1: Sous-Rature (Philosophy)

**1. Derrida - Of Grammatology** ✅ EXISTS
- File: test_files/derrida_pages_110_135.pdf
- X-marks: 4 known instances
- Ground truth: Page 135 "is" at bbox (312.21, 172.998, 326.38, 186.87)
- Tests: X-mark detection, text recovery, preservation

**2. Derrida - Margins of Philosophy** ⏳ NEED
- File: test_files/derrida_margins_of_philosophy_full.pdf
- X-marks: Unknown count (need to create ground truth)
- Structure: Essay collection (tests sampling assumptions)
- Tests: Non-clustered X-marks, essay boundaries

**3. Heidegger - Being and Time** ✅ EXISTS
- File: test_files/heidegger_pages_79-88.pdf
- X-marks: 2 known instances ("Being", "Sein")
- Ground truth: Page 80 at bbox (91.68, 138.93, 379.99, 150.0)
- Tests: German text, philosophical terminology

#### Category 2: Complex Structure

**4. Kant - Critique of Pure Reason (A/B editions)** ⏳ NEED
- Tests: Dual pagination, A/B citation system, margin citations
- Expected: Stephanus-style marginal page numbers
- Ground truth: Known A/B number locations

**5. Plato - Republic (Stephanus pagination)** ⏳ NEED
- Tests: Stephanus citation system (245c, 246a, etc.)
- Expected: Margin citations, dialogue structure
- Ground truth: Known Stephanus numbers

#### Category 3: Quality Issues

**6. Heavily Scanned PDF (Poor OCR)** ⏳ CREATE
- Create: Intentionally corrupt a clean PDF
- Tests: Garbled detection, OCR recovery, confidence scoring
- Ground truth: Known corruption locations and original text

**7. Mixed Quality PDF** ⏳ CREATE
- Pages 1-10: Clean
- Pages 11-15: Corrupted (trigger OCR)
- Pages 16-20: Sous-rature (preserve)
- Tests: Selective OCR, mixed handling

#### Category 4: Edge Cases

**8. Footnote-Heavy Academic Paper** ⏳ NEED
- Tests: Footnote detection, reference linking, orphaned refs
- Expected: <5% orphaned footnotes
- Ground truth: Manual footnote count and links

**9. Multi-Column Journal Article** ⏳ NEED
- Tests: Column detection, reading order, abstract extraction
- Expected: Correct reading order (down then across)
- Ground truth: Known article structure

**10. Annotated PDF (Handwritten Notes)** ⏳ NEED
- Tests: Distinguish X-marks from underlines/annotations
- Expected: Only Derrida's X-marks detected, not reader's marks
- Ground truth: Known X-marks vs known annotations

---

## Ground Truth Data Structure

### test_files/ground_truth.json

```json
{
  "derrida_of_grammatology": {
    "file": "test_files/derrida_pages_110_135.pdf",
    "pages": 26,
    "language": "en",
    "author": "Derrida",
    "xmarks": [
      {
        "page": 1,
        "word": "is",
        "bbox": [312.21, 172.998, 326.38, 186.87],
        "context": "the sign is that ill-named",
        "corrupted_extraction": ")(",
        "correct_recovery": "is",
        "confidence_expected": 0.7
      }
    ],
    "footnotes": [],
    "quality_score_expected": 0.92,
    "processing_time_max_ms": 5000
  },
  "heidegger_being_time": {
    "file": "test_files/heidegger_pages_79-88.pdf",
    "pages": 10,
    "xmarks": [
      {
        "page": 2,
        "word": "Being",
        "bbox": [91.68, 138.93, 379.99, 150.0],
        "context": "Das Zeichen der Durchkreuzung",
        "corrupted_extraction": "~",
        "correct_recovery": "Being"
      },
      {
        "page": 1,
        "word": "Sein",
        "bbox": [94.32, 141.88, 377.76, 154.06],
        "language": "de"
      }
    ],
    "quality_score_expected": 0.88
  }
}
```

### Usage in Tests

```python
import json

def load_ground_truth(test_name):
    """Load ground truth data for test PDF."""
    with open('test_files/ground_truth.json') as f:
        data = json.load(f)
    return data[test_name]

def test_derrida_xmark_detection_real():
    """Test X-mark detection with real Derrida PDF and ground truth."""
    gt = load_ground_truth('derrida_of_grammatology')

    # Process with real pipeline
    result = process_pdf(Path(gt['file']), output_format='markdown')

    # Validate against ground truth
    for xmark in gt['xmarks']:
        expected_word = xmark['correct_recovery']

        # Check if word recovered (either as-is or with strikethrough)
        assert expected_word in result or f'~~{expected_word}~~' in result, \
            f"X-marked word '{expected_word}' not found in output"

    # Validate quality score within range
    assert abs(result.quality_score - gt['quality_score_expected']) < 0.1

    # Validate performance
    assert result.processing_time_ms < gt['processing_time_max_ms']
```

---

## TDD Workflow: Test-First with Real PDFs

### For Each New Feature

**Example**: Implementing formatting application (Stage 7)

**Step 1: Get Real PDF with Known Formatting**
```bash
# Use Derrida PDF which has italics in French terms
test_files/derrida_with_italics.pdf
```

**Step 2: Create Ground Truth**
```json
{
  "derrida_with_italics": {
    "formatting_instances": [
      {"page": 5, "text": "être-là", "formatting": ["italic"]},
      {"page": 12, "text": "différance", "formatting": ["italic"]},
      {"page": 23, "text": "sous rature", "formatting": ["italic"]}
    ]
  }
}
```

**Step 3: Write Failing Test**
```python
def test_italic_preservation_real():
    """Test italic formatting preserved in output."""
    gt = load_ground_truth('derrida_with_italics')
    result = process_pdf(Path(gt['file']), output_format='markdown')

    for fmt in gt['formatting_instances']:
        expected = f"*{fmt['text']}*"  # Markdown italic
        assert expected in result, \
            f"Italic formatting not preserved for '{fmt['text']}'"
```

**Step 4: Run Test (Should FAIL)**
```bash
pytest test_formatting_real.py::test_italic_preservation_real
# FAIL: Italic formatting not preserved ❌
```

**Step 5: Implement Feature**
```python
def format_text_spans_as_markdown(spans):
    """Apply formatting from TextSpan to markdown."""
    result = []
    for span in spans:
        text = span.text
        if 'italic' in span.formatting:
            text = f'*{text}*'
        result.append(text)
    return ' '.join(result)
```

**Step 6: Run Test (Should PASS)**
```bash
pytest test_formatting_real.py::test_italic_preservation_real
# PASS ✅
```

**Step 7: Validate Manually**
```bash
# Open output file, visually verify italics
cat processed_rag_output/derrida_with_italics.md | grep "*être-là*"
```

---

## Ground Truth Creation Process

### Manual Verification Workflow

```bash
# 1. Open PDF in viewer
evince test_files/derrida_of_grammatology.pdf

# 2. Identify features (X-marks, italics, footnotes)
# 3. Record exact locations and expected outputs
# 4. Create ground truth JSON

# 5. Create validation script
python scripts/validation/create_ground_truth.py \
    --pdf test_files/derrida_of_grammatology.pdf \
    --output test_files/ground_truth/derrida.json
```

**create_ground_truth.py**:
```python
#!/usr/bin/env python3
"""
Interactive ground truth creation.

Opens PDF, allows user to click X-marks, footnotes, etc.
Records bboxes and expected outputs.
Generates ground_truth.json.
"""

import fitz
import sys
from pathlib import Path

def create_ground_truth_interactive(pdf_path):
    """Interactive ground truth creation."""
    doc = fitz.open(pdf_path)

    ground_truth = {
        'file': str(pdf_path),
        'pages': len(doc),
        'xmarks': [],
        'footnotes': [],
        'formatting': []
    }

    for page_num, page in enumerate(doc):
        print(f"\nPage {page_num + 1}/{len(doc)}")
        print("Does this page have X-marks? (y/n)")

        if input().lower() == 'y':
            print("How many X-marks on this page?")
            count = int(input())

            for i in range(count):
                print(f"\nX-mark {i+1}/{count}")
                print("Enter word under erasure:")
                word = input()

                # Record
                ground_truth['xmarks'].append({
                    'page': page_num,
                    'word': word,
                    'bbox': None,  # TODO: Interactive bbox selection
                    'context': None  # TODO: Extract context
                })

    # Save
    output_path = Path('test_files/ground_truth') / f'{pdf_path.stem}.json'
    output_path.parent.mkdir(exist_ok=True)

    with open(output_path, 'w') as f:
        json.dump(ground_truth, f, indent=2)

    print(f"\n✅ Ground truth saved to: {output_path}")
```

---

## Automated Test Suite

### test_files/README.md

```markdown
# Test PDF Corpus

## Ground Truth Files

Each PDF has corresponding ground truth data in test_files/ground_truth/

- `{pdf_name}.json`: Feature locations and expected outputs
- `{pdf_name}_expected.md`: Expected markdown output snippets
- `{pdf_name}_quality.json`: Expected quality scores and flags

## Adding New Test PDFs

1. Add PDF to test_files/
2. Create ground truth: `python scripts/validation/create_ground_truth.py --pdf your.pdf`
3. Add test case: `__tests__/python/test_real_pdfs.py`
4. Verify: `pytest __tests__/python/test_real_pdfs.py::test_your_pdf`
```

### __tests__/python/test_real_pdfs.py

```python
"""
Real-world PDF tests with ground truth validation.

NO MOCKING - tests entire pipeline with actual PDFs.
"""

import pytest
import json
from pathlib import Path
from lib.rag_processing import process_pdf

# Ground truth directory
GT_DIR = Path('test_files/ground_truth')

def load_ground_truth(test_name):
    """Load ground truth for test PDF."""
    gt_file = GT_DIR / f'{test_name}.json'
    if not gt_file.exists():
        pytest.skip(f"Ground truth not found: {gt_file}")

    with open(gt_file) as f:
        return json.load(f)


class TestDerridaSousRature:
    """Real-world tests with Derrida's sous-rature."""

    def test_xmark_detection_accuracy(self):
        """Validate X-mark detection on real Derrida PDF."""
        gt = load_ground_truth('derrida_of_grammatology')

        # Process with pipeline
        result = process_pdf(Path(gt['file']), output_format='markdown')

        # Validate each known X-mark
        detected_count = 0
        for xmark in gt['xmarks']:
            expected_word = xmark['correct_recovery']

            # Check if word appears (recovered or preserved)
            if expected_word in result or f'~~{expected_word}~~' in result:
                detected_count += 1

        # Require 100% detection
        assert detected_count == len(gt['xmarks']), \
            f"Only {detected_count}/{len(gt['xmarks'])} X-marks detected"

    def test_sous_rature_text_recovery(self):
        """Validate text under erasure is recovered."""
        gt = load_ground_truth('derrida_of_grammatology')
        result = process_pdf(Path(gt['file']))

        # Check each X-marked word was recovered (not left as ")(")
        for xmark in gt['xmarks']:
            corrupted = xmark.get('corrupted_extraction', '')(')
            correct = xmark['correct_recovery']

            # Should NOT have corrupted version in output
            assert corrupted not in result, \
                f"Corrupted text '{corrupted}' not recovered"

            # Should have correct word (with or without strikethrough)
            assert correct in result or f'~~{correct}~~' in result, \
                f"Correct word '{correct}' not in output"

    def test_performance_under_threshold(self):
        """Validate processing time is acceptable."""
        gt = load_ground_truth('derrida_of_grammatology')

        import time
        start = time.time()
        result = process_pdf(Path(gt['file']))
        elapsed_ms = (time.time() - start) * 1000

        max_time = gt.get('processing_time_max_ms', 10000)  # 10s default
        assert elapsed_ms < max_time, \
            f"Too slow: {elapsed_ms:.0f}ms > {max_time}ms"

    def test_quality_score_in_expected_range(self):
        """Validate quality score matches expectations."""
        gt = load_ground_truth('derrida_of_grammatology')
        # TODO: Extract quality score from result
        # assert abs(result.quality_score - gt['quality_score_expected']) < 0.1


class TestFootnoteHandling:
    """Real-world tests with footnote-heavy texts."""

    def test_footnote_detection_accuracy(self):
        """Validate footnote markers detected."""
        gt = load_ground_truth('heidegger_being_time')
        result = process_pdf(Path(gt['file']))

        # Check all known footnote markers detected
        for footnote in gt.get('footnotes', []):
            marker = footnote['marker']  # e.g., "1", "23"
            # Should appear as superscript or [^marker]
            assert f'^{marker}' in result or f'[^{marker}]' in result

    def test_orphaned_footnotes_below_threshold(self):
        """Validate orphaned footnote rate < 5%."""
        gt = load_ground_truth('philosophy_footnote_heavy')
        result = process_pdf(Path(gt['file']))

        # Count footnote references and definitions
        # Calculate orphaned rate
        # assert orphaned_rate < 0.05


class TestCitationSystems:
    """Real-world tests with various citation systems."""

    def test_kant_ab_citation_detection(self):
        """Validate Kant A/B citations extracted."""
        gt = load_ground_truth('kant_critique')
        result = process_pdf(Path(gt['file']))

        # Check known A/B citations
        for citation in gt['citations']:
            # e.g., "A 50", "B 75"
            assert citation['text'] in result

    def test_stephanus_citation_detection(self):
        """Validate Stephanus pagination (Plato) extracted."""
        gt = load_ground_truth('plato_republic')
        result = process_pdf(Path(gt['file']))

        # Check known Stephanus numbers (245c, 246a, etc.)
        for citation in gt['citations']:
            assert citation['text'] in result


class TestPerformance:
    """Real-world performance validation."""

    @pytest.mark.parametrize('test_name,max_time_ms', [
        ('derrida_of_grammatology', 5000),  # 5s for 26 pages
        ('heidegger_being_time', 3000),     # 3s for 10 pages
        ('margins_of_philosophy', 10000),   # 10s for 330 pages
    ])
    def test_processing_time(self, test_name, max_time_ms):
        """Validate processing time under threshold."""
        gt = load_ground_truth(test_name)

        import time
        start = time.time()
        result = process_pdf(Path(gt['file']))
        elapsed_ms = (time.time() - start) * 1000

        assert elapsed_ms < max_time_ms, \
            f"{test_name}: {elapsed_ms:.0f}ms > {max_time_ms}ms"


class TestQualityPipelineRealWorld:
    """Integration tests with real PDFs."""

    def test_garbled_page_triggers_xmark_check(self):
        """Validate pre-filter correctly identifies pages needing X-mark check."""
        # Use PDF with mixed clean/garbled pages
        gt = load_ground_truth('mixed_quality_pdf')

        # Process with logging
        result = process_pdf(Path(gt['file']))

        # Check logs to verify:
        # - Clean pages skipped X-mark detection
        # - Garbled pages ran X-mark detection
        # TODO: Parse logs or add instrumentation

    def test_sous_rature_preserved_not_recovered_as_corruption(self):
        """Validate sous-rature flagged correctly, not treated as OCR error."""
        gt = load_ground_truth('derrida_of_grammatology')
        result = process_pdf(Path(gt['file']))

        # Should have 'sous_rature' in quality_flags
        # Should NOT have 'recovered_corruption'
        # Should have text with strikethrough formatting
```

---

## Continuous Integration Testing

### GitHub Actions Workflow (Future)

```yaml
name: Real PDF Validation

on: [push, pull_request]

jobs:
  test-real-pdfs:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v3

      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          sudo apt-get install tesseract-ocr

      - name: Run real PDF tests
        run: |
          pytest __tests__/python/test_real_pdfs.py -v

      - name: Validate performance benchmarks
        run: |
          python scripts/validation/validate_quality_pipeline.py

      - name: Check quality score improvements
        run: |
          python scripts/validation/measure_quality_improvements.py
```

---

## Test Coverage Matrix

### What We Need to Test

| Feature | Real PDF Test | Ground Truth | Status |
|---------|---------------|--------------|--------|
| **X-mark detection** | ✅ Derrida, Heidegger | ✅ 6 instances | Tested |
| **Text recovery under X-mark** | ⏳ Need test | ⏳ Need GT | Week 1 |
| **Formatting (bold/italic)** | ⏳ Need PDF | ⏳ Need GT | Week 1 |
| **Footnote detection** | ⏳ Need PDF | ⏳ Need GT | Week 2 |
| **Footnote linking** | ⏳ Need PDF | ⏳ Need GT | Week 3 |
| **Citation extraction** | ⏳ Need PDF | ⏳ Need GT | Week 2 |
| **Kant A/B citations** | ⏳ Need PDF | ⏳ Need GT | Week 3 |
| **Stephanus citations** | ⏳ Need PDF | ⏳ Need GT | Week 3 |
| **Marginalia detection** | ⏳ Need PDF | ⏳ Need GT | Week 2 |
| **Selective OCR** | ⏳ Need mixed PDF | ⏳ Need GT | Week 2 |
| **Multi-column** | ⏳ Need article | ⏳ Need GT | Week 4 |
| **Abstract extraction** | ⏳ Need article | ⏳ Need GT | Week 4 |
| **Handwritten annotations** | ⏳ Need annotated | ⏳ Need GT | Week 3 |

---

## Building Test Corpus (Week 1 Task)

### Acquisition Strategy

**1. Use Existing Test PDFs** ✅
- test_files/derrida_pages_110_135.pdf
- test_files/heidegger_pages_79-88.pdf

**2. Download Full Books via Z-Library**
```python
# Use our own MCP server to get test PDFs!
from zlibrary import search_books, download_book

# Search for philosophy texts with known features
books = search_books("Derrida Margins of Philosophy")
download_book(books[0], output_dir='test_files/corpus')

books = search_books("Kant Critique Pure Reason")
download_book(books[0], output_dir='test_files/corpus')

# etc.
```

**3. Create Synthetic Test Cases**
```python
# Create controlled test PDFs
def create_test_pdf_with_known_features():
    """Generate PDF with specific features for testing."""
    # Use reportlab or similar to create PDF with:
    # - Known bold/italic text
    # - Footnote markers
    # - Specific symbol patterns
```

---

## Regression Testing

### Snapshot Testing for Outputs

```python
def test_derrida_output_unchanged():
    """Validate output doesn't regress (snapshot test)."""
    result = process_pdf('test_files/derrida_of_grammatology.pdf')

    # First run: Create snapshot
    snapshot_file = Path('test_files/snapshots/derrida_output.md')
    if not snapshot_file.exists():
        snapshot_file.write_text(result)
        pytest.skip("Snapshot created, run again to validate")

    # Subsequent runs: Compare with snapshot
    expected = snapshot_file.read_text()

    # Allow minor differences (timestamps, etc.) but require 95% similarity
    similarity = calculate_similarity(result, expected)
    assert similarity > 0.95, \
        f"Output changed: {similarity:.0%} similar (expected >95%)"
```

---

## Performance Regression Suite

### Automated Performance Monitoring

```python
class TestPerformanceRegression:
    """Ensure performance doesn't regress."""

    @pytest.mark.benchmark
    def test_xmark_detection_performance(self, benchmark):
        """Benchmark X-mark detection performance."""
        result = benchmark(
            detect_xmarks,
            'test_files/derrida_of_grammatology.pdf',
            page_num=0
        )

        # Require <5ms per page
        assert benchmark.stats['mean'] < 0.005, \
            f"X-mark detection too slow: {benchmark.stats['mean']*1000:.1f}ms"

    @pytest.mark.benchmark
    def test_end_to_end_performance(self, benchmark):
        """Benchmark complete pipeline."""
        result = benchmark(
            process_pdf,
            Path('test_files/heidegger_being_time.pdf')
        )

        # 10 pages should process in <3s
        assert benchmark.stats['mean'] < 3.0
```

---

## Week 1 Implementation Plan

### Day 1: Ground Truth Creation (Today)

**Tasks** (3-4 hours):
1. Create ground_truth.json for existing PDFs (manual)
2. Document expected outputs for each test case
3. Record X-mark locations, footnote counts, etc.

**Deliverables**:
- test_files/ground_truth/derrida.json
- test_files/ground_truth/heidegger.json

### Day 2: Real PDF Test Suite

**Tasks** (4-5 hours):
1. Create test_real_pdfs.py with ground truth validation
2. Implement load_ground_truth() helper
3. Write tests for each known PDF
4. Run and validate all pass

**Deliverables**:
- __tests__/python/test_real_pdfs.py
- 10+ real-world tests

### Day 3: Fast Pre-Filter Implementation

**Tasks** (2-3 hours):
1. Implement _page_needs_xmark_detection_fast()
2. Integrate into pipeline
3. Benchmark before/after
4. Validate no false negatives

**Deliverables**:
- Fast pre-filter in lib/rag_processing.py
- Performance benchmarks
- 31× speedup on X-mark detection

---

## Success Criteria

### Validation Gates

Before merging any feature:
- [ ] Real PDF test exists
- [ ] Ground truth documented
- [ ] Test passes with actual PDF
- [ ] Manual output verification
- [ ] Performance within budget
- [ ] No regressions on other PDFs

### Quality Metrics

- **Test Coverage**: >85% (with real PDFs, not mocks)
- **Ground Truth Accuracy**: 100% (manual verification)
- **False Negatives**: 0% (can't miss features)
- **False Positives**: <5% (acceptable)
- **Performance**: All operations under target thresholds

---

## Example: TDD for Formatting Application

### Week 1 Feature: Format TextSpan to Markdown

**Day 1 Morning: Ground Truth**
```json
{
  "test_formatting_bold_italic": {
    "file": "test_files/philosophy_formatted.pdf",
    "formatting": [
      {"page": 1, "text": "Dasein", "format": ["italic"]},
      {"page": 2, "text": "Being", "format": ["bold"]},
      {"page": 3, "text": "différance", "format": ["italic", "bold"]}
    ],
    "expected_output": {
      "page_1": "*Dasein*",
      "page_2": "**Being**",
      "page_3": "***différance***"
    }
  }
}
```

**Day 1 Afternoon: Failing Test**
```python
def test_formatting_bold():
    gt = load_ground_truth('test_formatting_bold_italic')
    result = process_pdf(Path(gt['file']), output_format='markdown')

    # Should have **Being** for bold
    assert '**Being**' in result
    # FAIL: Formatting not applied ❌
```

**Day 2: Implement**
```python
def format_text_spans_as_markdown(spans):
    for span in spans:
        if 'bold' in span.formatting:
            span.text = f'**{span.text}**'
        if 'italic' in span.formatting:
            span.text = f'*{span.text}*'
    return ' '.join(s.text for s in spans)
```

**Day 2: Test Passes**
```bash
pytest test_real_pdfs.py::test_formatting_bold
# PASS ✅
```

**Day 2: Manual Verification**
```bash
cat processed_rag_output/philosophy_formatted.md | grep "**Being**"
# Visual verification: Bold formatting preserved ✅
```

---

## Summary: Rigorous TDD Strategy

### Principles

1. **Test with REAL PDFs** - No mocking for integration tests
2. **Ground truth first** - Document expected behavior before implementing
3. **Manual verification** - Human review of output quality
4. **Performance gates** - Every feature must meet performance budget
5. **No regressions** - Snapshot testing prevents breaking existing functionality

### Timeline

**Week 1**: Build test corpus + ground truth (8-10 hours)
**Week 2**: Real PDF test suite for all features (10-12 hours)
**Ongoing**: Add ground truth for each new feature (TDD workflow)

### Expected Outcomes

- ✅ Catch architectural flaws early (like Stage 2 dependency)
- ✅ Validate with real philosophy texts
- ✅ Performance validated on actual documents
- ✅ Ground truth prevents regressions
- ✅ High confidence in production quality

**User's emphasis on real-world testing is EXACTLY right** - this is how we build robust systems!
