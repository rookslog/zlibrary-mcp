# Footnote Continuation Quick Start Guide

**Module**: `lib/footnote_continuation.py`
**Purpose**: Track scholarly footnotes across multiple pages
**Status**: ✅ Production Ready (v1.0)

## Quick Usage

### Basic Example

```python
from lib.footnote_continuation import CrossPageFootnoteParser, is_footnote_incomplete

# Initialize parser
parser = CrossPageFootnoteParser()

# Process document page by page
for page_num in range(1, total_pages + 1):
    page_footnotes = extract_footnotes_from_page(page_num)  # Your extraction logic

    # Optional: Detect incomplete with NLTK (recommended)
    for footnote in page_footnotes:
        incomplete, confidence, reason = is_footnote_incomplete(footnote['content'])
        footnote['is_complete'] = not incomplete

    # Process page through state machine
    completed = parser.process_page(page_footnotes, page_num)

    # Handle completed footnotes
    for fn in completed:
        output_footnote(fn)

# Finalize any remaining incomplete footnotes
final_footnotes = parser.finalize()
for fn in final_footnotes:
    output_footnote(fn)
```

## Input Format

Each footnote dict should have:

```python
footnote_dict = {
    'marker': str,              # Required: "1", "a", "*", etc.
    'content': str,             # Required: Footnote text
    'is_complete': bool,        # Recommended: True/False (use NLTK if unsure)
    'bbox': Dict,               # Recommended: {'x0', 'y0', 'x1', 'y1'}
    'font_name': str,           # Optional: Font family
    'font_size': float,         # Optional: Font size in points
    'note_source': NoteSource,  # Optional: AUTHOR/TRANSLATOR/EDITOR
    'classification_confidence': float,  # Optional: 0.0-1.0
}
```

**Minimal Example** (just marker and content):
```python
{'marker': '*', 'content': 'This footnote continues'}
```

**Recommended Example** (with NLTK detection):
```python
incomplete, confidence, reason = is_footnote_incomplete(content)
{
    'marker': '*',
    'content': 'This footnote continues',
    'is_complete': not incomplete,
    'font_name': 'TimesNewRoman',
    'font_size': 9.0
}
```

## Output Format

Returns `FootnoteWithContinuation` objects:

```python
footnote = FootnoteWithContinuation(
    marker='*',
    content='Complete multi-page footnote content.',
    pages=[64, 65],
    bboxes=[
        {'x0': 50, 'y0': 700, 'x1': 550, 'y1': 780},
        {'x0': 50, 'y0': 50, 'x1': 550, 'y1': 120}
    ],
    is_complete=True,
    continuation_confidence=0.92,
    note_source=NoteSource.TRANSLATOR
)
```

**Access Data**:
```python
print(f"Marker: {footnote.marker}")
print(f"Content: {footnote.content}")
print(f"Pages: {footnote.pages}")  # [64, 65]
print(f"Confidence: {footnote.continuation_confidence}")  # 0.92
print(f"Summary: {footnote.get_summary()}")
```

## NLTK-Based Incomplete Detection

### Basic Usage

```python
from lib.footnote_continuation import is_footnote_incomplete

text = "The concept refers to"
incomplete, confidence, reason = is_footnote_incomplete(text)

print(f"Incomplete: {incomplete}")  # True
print(f"Confidence: {confidence}")  # 0.88
print(f"Reason: {reason}")  # 'nltk_incomplete+continuation_word'
```

### Confidence Levels

| Range | Interpretation | Examples |
|-------|---------------|----------|
| 0.95-1.0 | Very strong signal | Hyphenation: `"concept-"` |
| 0.85-0.95 | Strong signal | Incomplete phrase: `"refers to"` |
| 0.75-0.85 | Good signal | NLTK incomplete: `"The argument"` |
| 0.60-0.75 | Moderate signal | Continuation word only: `"of"` |
| <0.60 | Weak/complete | Complete: `"See p. 42."` |

**Recommended Threshold**: 0.75 (use `get_incomplete_confidence_threshold()`)

### Batch Processing

```python
from lib.footnote_continuation import analyze_footnote_batch

footnotes = [
    "Complete sentence.",
    "Incomplete refers to",
    "word-"
]

results = analyze_footnote_batch(footnotes)
# [(False, 0.92, 'nltk_complete'),
#  (True, 0.88, 'nltk_incomplete+continuation_word'),
#  (True, 0.95, 'hyphenation')]
```

**Performance**: <1ms per item (with LRU caching)

## State Machine Behavior

### Continuation Detection Signals

Ranked by reliability (highest to lowest):

1. **Font matches** (conf: 0.92): Same font name/size as incomplete
2. **In footnote area** (conf: 0.85): Spatial analysis (bottom 20% of page)
3. **Starts lowercase/conjunction** (conf: 0.70-0.75): Textual patterns
4. **Sequence order** (conf: 0.65): No intervening markers

### State Transitions

```
NEW footnote → is_complete=False?
              → Yes: Add to incomplete_footnotes
              → No: Add to completed_footnotes

INCOMPLETE footnote on next page → continuation found?
                                 → Yes: Merge & check if complete
                                 → No (new marker): Mark as complete (false positive)

DOCUMENT END → All incomplete → Mark as complete
```

### False Incomplete Handling

**Scenario**: Footnote marked incomplete but next page has new marker (no continuation)

**Behavior**: Automatically mark as complete (false positive detection)

**Example**:
```python
# Page 1: Footnote '1' marked incomplete
page1 = [{'marker': '1', 'content': 'Text', 'is_complete': False}]
parser.process_page(page1, page_num=1)  # Returns []

# Page 2: New footnote '2' (no continuation)
page2 = [{'marker': '2', 'content': 'New text', 'is_complete': True}]
completed = parser.process_page(page2, page_num=2)
# Returns both '1' (marked complete) and '2'
```

## Integration with RAG Pipeline

### Step 1: Import

```python
from lib.footnote_continuation import (
    CrossPageFootnoteParser,
    is_footnote_incomplete,
    get_incomplete_confidence_threshold
)
```

### Step 2: Initialize Parser

```python
# At start of PDF processing
parser = CrossPageFootnoteParser()
threshold = get_incomplete_confidence_threshold()  # 0.75
```

### Step 3: Process Each Page

```python
for page_num, page in enumerate(pdf_pages, start=1):
    # Extract footnotes (your existing logic)
    footnotes = extract_footnotes(page)

    # Prepare for state machine
    page_footnotes = []
    for fn in footnotes:
        # Use NLTK to detect incomplete
        incomplete, confidence, reason = is_footnote_incomplete(fn['content'])

        # Only mark as incomplete if high confidence
        is_incomplete = incomplete and confidence >= threshold

        page_footnotes.append({
            'marker': fn['marker'],
            'content': fn['content'],
            'is_complete': not is_incomplete,
            'bbox': fn.get('bbox'),
            'font_name': fn.get('font_name'),
            'font_size': fn.get('font_size'),
        })

    # Process through state machine
    completed = parser.process_page(page_footnotes, page_num)

    # Output completed footnotes to markdown
    for fn in completed:
        markdown_output += format_footnote_markdown(fn)
```

### Step 4: Finalize

```python
# After all pages processed
final_footnotes = parser.finalize()
for fn in final_footnotes:
    markdown_output += format_footnote_markdown(fn)
```

## Example: Complete Integration

```python
def process_pdf_with_continuation(pdf_path):
    """Process PDF with footnote continuation tracking."""
    from lib.footnote_continuation import (
        CrossPageFootnoteParser,
        is_footnote_incomplete
    )

    parser = CrossPageFootnoteParser()
    threshold = 0.75
    all_completed = []

    # Process each page
    for page_num, page in enumerate(extract_pages(pdf_path), start=1):
        page_footnotes = []

        for fn_text, fn_marker, fn_bbox, fn_font in extract_footnotes(page):
            # NLTK detection
            incomplete, confidence, _ = is_footnote_incomplete(fn_text)
            is_incomplete = incomplete and confidence >= threshold

            page_footnotes.append({
                'marker': fn_marker,
                'content': fn_text,
                'is_complete': not is_incomplete,
                'bbox': fn_bbox,
                'font_name': fn_font['name'],
                'font_size': fn_font['size']
            })

        # State machine processing
        completed = parser.process_page(page_footnotes, page_num)
        all_completed.extend(completed)

    # Finalize
    final = parser.finalize()
    all_completed.extend(final)

    # Summary
    summary = parser.get_summary()
    print(f"Total completed: {summary['total_completed']}")
    print(f"Multi-page: {summary['multi_page_count']}")
    print(f"Average confidence: {summary['average_confidence']:.2f}")

    return all_completed
```

## Troubleshooting

### Issue: All footnotes marked complete (no continuation detection)

**Cause**: Not setting `is_complete=False` for incomplete footnotes

**Solution**: Use NLTK detection:
```python
incomplete, confidence, _ = is_footnote_incomplete(content)
footnote['is_complete'] = not incomplete
```

### Issue: Low continuation confidence

**Cause**: Missing font metadata or bbox

**Solution**: Include font and spatial data:
```python
footnote['font_name'] = 'TimesNewRoman'
footnote['font_size'] = 9.0
footnote['bbox'] = {'x0': 50, 'y0': 700, 'x1': 550, 'y1': 780}
```

### Issue: False positives (content incorrectly merged)

**Cause**: Lowercase start or continuation word at beginning of new footnote

**Solution**: Adjust NLTK confidence threshold or add marker validation

### Issue: Performance degradation

**Cause**: NLTK cache not being used

**Solution**: Ensure text is consistent (whitespace normalization handled automatically)

## Performance Tips

1. **Use LRU Cache**: NLTK detection cached for 1024 entries
2. **Batch Processing**: Use `analyze_footnote_batch()` for multiple footnotes
3. **Minimal Input**: Only provide required fields if performance critical
4. **Font Matching**: Include font metadata for high-confidence detection

## Limitations (v1.0)

1. **Single Incomplete**: Tracks only one incomplete footnote at a time
   - Multiple simultaneous incomplete deferred to v1.1

2. **Spatial Metadata**: Requires bbox for robust spatial detection
   - Optional but recommended

3. **Font Metadata**: Optional but improves confidence
   - Fallback to pattern-based detection

## API Reference

### Main Classes

- **`CrossPageFootnoteParser`**: State machine for cross-page tracking
  - `process_page(page_footnotes, page_num)` → List[FootnoteWithContinuation]
  - `finalize()` → List[FootnoteWithContinuation]
  - `get_all_completed()` → List[FootnoteWithContinuation]
  - `get_summary()` → Dict

- **`FootnoteWithContinuation`**: Data model for multi-page notes
  - `append_continuation(content, page_num, bbox, confidence)`
  - `get_summary()` → str

### Main Functions

- **`is_footnote_incomplete(text)`** → Tuple[bool, float, str]
  - Returns: (is_incomplete, confidence, reason)

- **`analyze_footnote_batch(footnotes)`** → List[Tuple[bool, float, str]]
  - Batch processing for multiple footnotes

- **`get_incomplete_confidence_threshold()`** → float
  - Returns recommended threshold (0.75)

### Enums

- **`ContinuationSignal`**: Enum for continuation indicators
  - NO_MARKER_AT_START
  - STARTS_LOWERCASE
  - STARTS_CONJUNCTION
  - IN_FOOTNOTE_AREA
  - FONT_MATCHES
  - SEQUENCE_FOLLOWS

## Testing

Run comprehensive test suite:

```bash
# All tests (57)
pytest __tests__/python/test_footnote_continuation.py -v

# Specific test class
pytest __tests__/python/test_footnote_continuation.py::TestCrossPageFootnoteParser -v

# Specific test
pytest __tests__/python/test_footnote_continuation.py::TestIsFootnoteIncomplete::test_hyphenation_incomplete -v
```

**Expected Result**: 57/57 passing

## Support

- **Documentation**: See module docstrings in `lib/footnote_continuation.py`
- **Session Notes**: [`claudedocs/session-notes/2025-10-28-footnote-continuation-state-machine.md`](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/claudedocs/session-notes/2025-10-28-footnote-continuation-state-machine.md)
- **Tests**: `__tests__/python/test_footnote_continuation.py`

---

**Version**: 1.0
**Status**: Production Ready
**Last Updated**: 2025-10-28
