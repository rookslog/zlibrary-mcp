# Article Support and External Citation Architecture

**Version**: 1.0
**Date**: 2025-10-18
**Status**: Design Specification
**Related**: [historical integration specification](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/specifications/PHASE_2_INTEGRATION_SPECIFICATION.md), ADR-006

---

## Executive Summary

This specification addresses two strategic architectural concerns:

1. **Article Support**: How to search, download, and process academic articles/papers (not just books)
2. **External Citation Representation**: How to represent citations in a way that enables external resolution systems (without implementing resolution ourselves)

**Key Principle**: **Separation of Concerns**
- RAG Processor: Extract + represent with rich metadata
- External Citation Resolver: Separate system for cross-document linking
- Search/Discovery: Separate system for finding referenced works

---

## Part 1: Z-Library Article Support

### Current State

**Z-Library Article Capabilities** ✅:
- Articles ARE available in Z-Library (academic papers, journal articles)
- `content_types` parameter in search API: `["book", "article"]`
- Article detection implemented: `card.get('type') == 'article'`
- Article-specific parsing (slot-based structure vs attribute-based)

**Current Implementation Status**:

| Component | Books | Articles | Gap |
|-----------|-------|----------|-----|
| **Search** | ✅ Full support | ✅ content_types filter | None |
| **Metadata Parsing** | ✅ Attribute-based | ✅ Slot-based | None |
| **Download** | ✅ Working | ✅ Same flow | None |
| **RAG Processing** | ✅ Book-optimized | ❌ Same as books | **HIGH** |
| **Metadata Generation** | ✅ Book fields | ⚠️ Missing article fields | **MEDIUM** |

**Files**: lib/advanced_search.py:61-71, lib/booklist_tools.py:104-114, lib/author_tools.py:223-233

### Article-Specific Processing Requirements

**Articles ≠ Books in Structure**:

```
BOOK STRUCTURE:              ARTICLE STRUCTURE:
- Title page                 - Title
- Copyright                  - Author(s)
- Table of Contents          - Abstract ⭐ CRITICAL
- Preface                    - Keywords ⭐ CRITICAL
- Chapters                   - Introduction
  - Sections                 - Literature Review ⭐
  - Subsections              - Methods
- Footnotes/Endnotes         - Results
- Bibliography               - Discussion
- Index                      - Conclusion
                             - References ⭐ DENSE
                             - Appendices
```

**Key Differences**:

1. **Abstract** (Critical for RAG):
   - First ~200 words summarizing entire paper
   - Should be first-class section (not just "paragraph 1")
   - Contains: research question, methods, findings, conclusions

2. **Keywords** (Critical for search/retrieval):
   - 5-10 domain-specific terms
   - Usually labeled "Keywords:", "Index terms:", etc.
   - Essential for RAG relevance ranking

3. **Structured Sections** (Predictable):
   - IMRaD format (Introduction, Methods, Results, and Discussion)
   - Consistent naming across papers
   - Should be detected and labeled

4. **Dense Reference Section** (Different processing):
   - 20-100+ citations in bibliography
   - Structured format (APA, MLA, Chicago)
   - Should be extracted as structured data, not just text

5. **Identifiers** (Critical metadata):
   - DOI (Digital Object Identifier) - universal
   - PMID/PMCID (PubMed for medical)
   - ArXiv ID (for preprints)
   - Should be extracted for cross-referencing

6. **Journal Metadata** (Context):
   - Journal name, volume, issue, pages
   - Publication date (more granular than books)
   - Impact factor, discipline

### Implementation Plan

#### Phase 1: Article Metadata Enhancement (Week 1)

**Extend DocumentMetadata** (lib/rag_data_models.py):

```python
@dataclass
class ArticleMetadata:
    """Article-specific metadata (extends base metadata)."""

    # Core identifiers
    doi: Optional[str] = None
    pmid: Optional[str] = None
    arxiv_id: Optional[str] = None

    # Journal information
    journal: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None  # "123-145"

    # Academic metadata
    abstract: Optional[str] = None
    keywords: List[str] = field(default_factory=list)

    # Discipline classification
    discipline: Optional[str] = None  # "Philosophy", "Psychology", etc.
    subdiscipline: Optional[str] = None

    # Impact metrics (optional)
    citation_count: Optional[int] = None
    impact_factor: Optional[float] = None


@dataclass
class DocumentMetadata:
    """Universal document metadata (books and articles)."""

    # Common fields
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[str] = None
    publisher: Optional[str] = None
    language: str = "en"

    # Document type
    document_type: str = "book"  # "book" | "article" | "chapter" | "thesis"

    # Type-specific metadata
    book_metadata: Optional[BookMetadata] = None
    article_metadata: Optional[ArticleMetadata] = None

    # Processing metadata
    quality_score: Optional[float] = None
    processing_version: str = "2.1.0"
    processed_date: Optional[str] = None
```

#### Phase 2: Article Section Detection (Week 2)

**Add Section Types** (lib/rag_data_models.py):

```python
class ArticleSectionType(Enum):
    """Standardized article section types."""
    TITLE = auto()
    ABSTRACT = auto()
    KEYWORDS = auto()
    INTRODUCTION = auto()
    LITERATURE_REVIEW = auto()
    METHODS = auto()
    RESULTS = auto()
    DISCUSSION = auto()
    CONCLUSION = auto()
    REFERENCES = auto()
    APPENDIX = auto()
    ACKNOWLEDGMENTS = auto()


@dataclass
class PageRegion:
    # ... existing fields ...

    # Article-specific
    section_type: Optional[ArticleSectionType] = None
```

**Section Detection** (lib/rag_processing.py):

```python
def _detect_article_section(heading_text: str, position: int, total_regions: int) -> Optional[ArticleSectionType]:
    """
    Detect article section type from heading text.

    Args:
        heading_text: Heading text (e.g., "Abstract", "1. Introduction")
        position: Region position in document (0-indexed)
        total_regions: Total regions in document

    Returns:
        ArticleSectionType if detected, None otherwise
    """
    heading_lower = heading_text.lower().strip()

    # Abstract (usually near beginning)
    if position < 5 and re.match(r'abstract|summary', heading_lower):
        return ArticleSectionType.ABSTRACT

    # Keywords (usually after abstract)
    if position < 10 and re.match(r'keywords|key words|index terms', heading_lower):
        return ArticleSectionType.KEYWORDS

    # IMRaD sections
    if re.match(r'(1\.|I\.?)\s*introduction', heading_lower):
        return ArticleSectionType.INTRODUCTION
    if re.match(r'literature review|related work|background', heading_lower):
        return ArticleSectionType.LITERATURE_REVIEW
    if re.match(r'(2\.|II\.?)\s*methods|methodology|materials and methods', heading_lower):
        return ArticleSectionType.METHODS
    if re.match(r'(3\.|III\.?)\s*results|findings', heading_lower):
        return ArticleSectionType.RESULTS
    if re.match(r'(4\.|IV\.?)\s*discussion', heading_lower):
        return ArticleSectionType.DISCUSSION
    if re.match(r'(5\.|V\.?)\s*conclusion|conclusions', heading_lower):
        return ArticleSectionType.CONCLUSION

    # References (usually near end)
    if position > (total_regions * 0.7) and re.match(r'references|bibliography|works cited', heading_lower):
        return ArticleSectionType.REFERENCES

    return None
```

#### Phase 3: Article-Specific Processing (Week 3)

**Separate Processing Path** (lib/rag_processing.py):

```python
def process_article_pdf(file_path: Path, output_format: str = "txt") -> str:
    """
    Process academic article PDF with article-specific optimizations.

    Articles have predictable structure (IMRaD) that we can leverage:
    - Abstract extraction (critical for RAG)
    - Keyword extraction
    - Section type detection
    - Dense reference parsing

    Args:
        file_path: Path to article PDF
        output_format: "txt" | "markdown" | "structured_json"

    Returns:
        Processed article content with enhanced metadata
    """
    doc = fitz.open(file_path)

    # Detect article metadata
    metadata = _extract_article_metadata(doc)

    # Extract abstract (first page, first ~200 words)
    abstract = _extract_abstract(doc)

    # Extract keywords
    keywords = _extract_keywords(doc)

    # Process with section detection
    regions = []
    for page_num, page in enumerate(doc):
        for block in page.get_text("dict").get("blocks", []):
            region = _analyze_pdf_block(block, return_structured=True)

            # Detect section type for articles
            if region.heading_level:
                region.section_type = _detect_article_section(
                    region.get_full_text(),
                    len(regions),
                    estimated_total=50  # Typical article length
                )

            regions.append(region)

    # Extract references section specially
    references = _extract_references_section(regions)

    # Format output with article structure
    return _format_article_output(
        metadata=metadata,
        abstract=abstract,
        keywords=keywords,
        regions=regions,
        references=references,
        output_format=output_format
    )
```

**Abstract Extraction**:

```python
def _extract_abstract(doc: fitz.Document) -> Optional[str]:
    """
    Extract abstract from article PDF.

    Strategy:
    1. Look for "Abstract" heading on first 2 pages
    2. Extract text until next heading or ~300 words
    3. Validate with heuristics (length 100-500 words, complete sentences)

    Returns:
        Abstract text or None if not found
    """
    for page_num in range(min(2, len(doc))):
        page = doc[page_num]
        text = page.get_text()

        # Find "Abstract" heading
        match = re.search(r'\bAbstract\b', text, re.IGNORECASE)
        if match:
            # Extract text after "Abstract" heading
            abstract_start = match.end()
            text_after = text[abstract_start:]

            # Find next heading (usually "1. Introduction")
            next_heading = re.search(r'\n\s*(?:1\.|I\.?|Introduction)', text_after)
            if next_heading:
                abstract_text = text_after[:next_heading.start()]
            else:
                # Take first 300 words as heuristic
                words = text_after.split()[:300]
                abstract_text = ' '.join(words)

            # Validate and clean
            abstract_text = abstract_text.strip()
            word_count = len(abstract_text.split())

            if 50 < word_count < 500:  # Reasonable abstract length
                return abstract_text

    return None
```

**Keyword Extraction**:

```python
def _extract_keywords(doc: fitz.Document) -> List[str]:
    """
    Extract keywords from article PDF.

    Strategy:
    1. Look for "Keywords:" label on first 2 pages
    2. Extract comma/semicolon separated list
    3. Validate with heuristics (3-15 keywords, length 2-30 chars each)

    Returns:
        List of keywords
    """
    for page_num in range(min(2, len(doc))):
        page = doc[page_num]
        text = page.get_text()

        # Find "Keywords:" label
        match = re.search(r'\b(Keywords?|Index terms?|Subject terms?):\s*(.+?)(?:\n\n|\n[A-Z]|$)',
                         text, re.IGNORECASE | re.DOTALL)
        if match:
            keywords_text = match.group(2)

            # Split by comma, semicolon, or newline
            keywords = re.split(r'[,;]\s*|\n', keywords_text)

            # Clean and validate
            cleaned = []
            for kw in keywords:
                kw = kw.strip()
                if 2 <= len(kw) <= 30 and kw:
                    cleaned.append(kw)

            if 2 <= len(cleaned) <= 15:  # Reasonable keyword count
                return cleaned

    return []
```

---

## Part 2: Representation Enhancements for External Citation Resolution

### Principle: Rich Representation, Not Resolution

**WRONG APPROACH** (trying to resolve):
```python
# Don't do this in RAG processor:
def resolve_external_citation(citation_text: str) -> Document:
    """Look up citation in library, download, link."""
    # This belongs in a SEPARATE system!
```

**RIGHT APPROACH** (rich representation):
```python
# Do this in RAG processor:
@dataclass
class CitationReference:
    """Structured representation enabling external resolution."""

    # Original text
    text: str  # "Kant, Critique of Pure Reason (A 50)"

    # Parsed components
    author_hint: Optional[str] = None  # "Kant"
    work_hint: Optional[str] = None    # "Critique of Pure Reason"
    page_hint: Optional[str] = None    # "A 50"

    # Classification
    citation_system: Optional[str] = None  # "kant_a_b"
    reference_type: str = "external"       # vs "internal" (footnote)

    # Position in document
    position: Tuple[int, int] = (0, 0)  # (start_char, end_char)
    page_num: int = 0
    region_id: Optional[str] = None

    # Context for validation
    context_before: str = ""  # 50 chars before
    context_after: str = ""   # 50 chars after

    # Confidence
    confidence: float = 0.0  # 0.0-1.0

    # Metadata for resolver
    metadata: dict = field(default_factory=dict)
```

**What This Enables** (in external system):

```python
# External Citation Resolver (separate service)
class CitationResolver:
    def __init__(self, library_catalog, embeddings_db):
        self.catalog = library_catalog  # Z-Library MCP
        self.embeddings = embeddings_db  # Vector search

    def resolve(self, citation: CitationReference) -> Optional[Document]:
        """
        Resolve citation to actual document.

        Strategy:
        1. Exact match on work_hint + author_hint
        2. Fuzzy match if exact fails
        3. Embedding similarity search
        4. Return top candidate with confidence
        """
        # Search Z-Library
        candidates = self.catalog.search_books(
            query=f"{citation.author_hint} {citation.work_hint}",
            exact=True
        )

        # Validate matches
        for candidate in candidates:
            if self._validate_match(citation, candidate):
                return candidate

        # Fallback to fuzzy
        return self._fuzzy_search(citation)

    def link_documents(self, source_doc, target_doc, citation):
        """Create bidirectional link."""
        # source_doc "cites" target_doc at citation.position
        # target_doc "cited_by" source_doc
```

**BENEFIT**: RAG processor stays focused, external resolver is flexible and reusable.

### Enhanced PageRegion for Citations

**Addition to lib/rag_data_models.py**:

```python
@dataclass
class PageRegion:
    # ... existing fields ...

    # NEW: Citation references
    citations: List[CitationReference] = field(default_factory=list)

    # NEW: Is this a bibliography/references section?
    is_bibliography: bool = False


def add_citation_reference(
    self,
    text: str,
    author_hint: str = None,
    work_hint: str = None,
    page_hint: str = None,
    citation_system: str = None,
    confidence: float = 0.0
) -> None:
    """Add citation reference to this region."""
    # Get position in region text
    full_text = self.get_full_text()
    start = full_text.find(text)
    end = start + len(text) if start != -1 else 0

    citation = CitationReference(
        text=text,
        author_hint=author_hint,
        work_hint=work_hint,
        page_hint=page_hint,
        citation_system=citation_system,
        reference_type="external",
        position=(start, end),
        page_num=self.page_num,
        region_id=f"page_{self.page_num}_region_{id(self)}",
        confidence=confidence
    )

    self.citations.append(citation)
```

### Citation Extraction (New Stage)

**Stage 4: Citation Extraction** (integrate Week 4):

```python
def _stage_4_citation_extraction(page_region: PageRegion, config: QualityPipelineConfig) -> PageRegion:
    """
    Stage 4: Extract and structure citation references.

    Detects:
    - Inline citations: "(Kant, CPR, A 50)"
    - Footnote citations: Handled by Stage 6
    - Margin citations: Detected by marginalia analysis

    Args:
        page_region: PageRegion from previous stages
        config: Pipeline configuration

    Returns:
        PageRegion with populated citations list
    """
    full_text = page_region.get_full_text()

    # Pattern: Author + Work + Page (philosophy style)
    pattern = r'((?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([^,]+),\s*([A-Z]?\s*\d+[a-z]?))'

    for match in re.finditer(pattern, full_text):
        author = match.group(1)
        work = match.group(2)
        page = match.group(3)

        # Detect citation system from page format
        citation_system = _detect_citation_system_from_page(page)

        page_region.add_citation_reference(
            text=match.group(0),
            author_hint=author,
            work_hint=work,
            page_hint=page,
            citation_system=citation_system,
            confidence=0.8  # High confidence for pattern match
        )

    return page_region
```

---

## Part 3: Article Processing Architecture

### Router Pattern (Detect Document Type)

**Document Type Detection** (lib/rag_processing.py):

```python
def detect_document_type(file_path: Path, book_details: dict = None) -> str:
    """
    Detect document type: book | article | chapter | thesis.

    Strategy:
    1. Check book_details['type'] if available (from Z-Library)
    2. Check metadata (journal name = article)
    3. Heuristics (page count, structure)

    Returns:
        "book" | "article" | "chapter" | "thesis"
    """
    # Priority 1: Z-Library metadata
    if book_details and 'type' in book_details:
        return book_details['type']

    # Priority 2: PDF metadata
    doc = fitz.open(file_path)
    metadata = doc.metadata

    # Journal articles usually have these
    if metadata.get('subject') or metadata.get('journal'):
        return 'article'

    # Priority 3: Heuristics
    page_count = len(doc)

    # Articles: typically 5-50 pages
    if 5 <= page_count <= 50:
        # Check for abstract on first page
        first_page = doc[0].get_text()
        if re.search(r'\bAbstract\b', first_page, re.IGNORECASE):
            return 'article'

    # Books: typically 100+ pages
    if page_count >= 100:
        return 'book'

    # Chapters: 15-40 pages, often no ToC
    if 15 <= page_count <= 40:
        return 'chapter'

    # Default
    return 'book'


async def process_document(file_path_str: str, output_format: str = "txt", book_details: dict = None) -> dict:
    """
    Main entry point - routes to appropriate processor.

    Args:
        file_path_str: Path to file
        output_format: Output format
        book_details: Metadata from Z-Library (includes type)

    Returns:
        Processing result
    """
    file_path = Path(file_path_str)

    # Detect document type
    doc_type = detect_document_type(file_path, book_details)

    # Route to appropriate processor
    if doc_type == 'article':
        content = process_article_pdf(file_path, output_format)
    elif doc_type == 'book':
        content = process_pdf(file_path, output_format)
    elif doc_type == 'chapter':
        content = process_chapter_pdf(file_path, output_format)
    else:
        # Fallback to book processing
        content = process_pdf(file_path, output_format)

    # ... rest of function
```

### Article Output Format

**Enhanced Output** (optimized for LLM consumption):

```markdown
---
type: article
title: "Phenomenology and Deconstruction"
authors: ["Jacques Derrida"]
journal: "Philosophy Today"
volume: 34
issue: 2
pages: 123-145
year: 1990
doi: 10.5840/philtoday199034216
keywords: [phenomenology, deconstruction, Husserl, intentionality]
abstract: "This article examines the relationship between..."
quality_score: 0.92
---

## Abstract

[SECTION type="abstract" id="abstract-1"]
This article examines the relationship between Husserlian phenomenology
and Derridean deconstruction...
[/SECTION]

## Keywords

[KEYWORDS] phenomenology, deconstruction, Husserl, intentionality [/KEYWORDS]

## 1. Introduction

[SECTION type="introduction" id="section-1"]
The question of phenomenology's relationship to deconstruction...

[CITATION author="Husserl" work="Ideas I" page="51" system="page_number" type="external"]
As Husserl argues in Ideas I (p. 51)...
[/CITATION]
[/SECTION]

## 2. Methods

[SECTION type="methods" id="section-2"]
This analysis employs close textual reading...
[/SECTION]

## References

[SECTION type="references" id="references"]
[REFERENCE id="ref-1" type="book"]
Husserl, E. (1913). Ideas Pertaining to a Pure Phenomenology. Springer.
[/REFERENCE]

[REFERENCE id="ref-2" type="article"]
Derrida, J. (1967). "Structure, Sign, and Play." Writing and Difference, 278-294.
[/REFERENCE]
[/SECTION]
```

**Benefits for External Systems**:
- ✅ Section types explicit (`[SECTION type="introduction"]`)
- ✅ Citations marked with author/work hints
- ✅ References structured (enables lookup)
- ✅ Keywords for relevance ranking
- ✅ Abstract for summarization
- ✅ DOI for identifier-based linking

---

## Part 4: Extensibility Architecture

### Strategy: Configuration-Based Extensibility (Not Plugins)

**DECISION**: Use **configuration files** instead of code-based plugins

**RATIONALE**:
- ✅ No dynamic code loading (security)
- ✅ Easy to add new citation systems (no code changes)
- ✅ Versioned with git (traceable)
- ✅ Portable (copy config file)
- ❌ Less flexible than true plugins
- ❌ Requires restart to reload config

### Citation System Configuration File

**File**: `config/citation_systems.yaml`

```yaml
# Citation Systems Configuration
# Add new systems here without code changes

systems:
  kant_a_b:
    name: "Kant A/B Editions"
    description: "Critique of Pure Reason 1781 (A) and 1787 (B) editions"
    pattern: '^[AB]\s*\d+$'
    examples: ['A 50', 'B 75', 'A123']
    parser:
      type: "regex"
      groups:
        edition: '([AB])'
        page: '(\d+)'
    formatter: "{edition} {page}"
    increment_logic: "simple_numeric"  # Just increment page number

  stephanus:
    name: "Stephanus Pagination"
    description: "Standard pagination for Plato's dialogues"
    pattern: '^\d+[a-e]$'
    examples: ['245c', '246a', '247d']
    parser:
      type: "regex"
      groups:
        page: '(\d+)'
        section: '([a-e])'
    formatter: "{page}{section}"
    increment_logic: "letter_cycle_with_page_wrap"  # a→b→c→d→e→(next page)a

  bekker:
    name: "Bekker Numbers"
    description: "Standard reference system for Aristotle"
    pattern: '^\d+[ab]\d+$'
    examples: ['184b15', '1094a1']
    parser:
      type: "regex"
      groups:
        page: '(\d+)'
        column: '([ab])'
        line: '(\d+)'
    formatter: "{page}{column}{line}"
    increment_logic: "line_increment_with_column_wrap"

  # Easy to add new systems:
  nietzsche_kwg:
    name: "Nietzsche KGW"
    description: "Kritische Gesamtausgabe (critical edition)"
    pattern: '^KGW\s*[IVX]+\s*\d+\s*\[\d+\]$'
    examples: ['KGW VI 2 [12]', 'KGW III 1 [234]']
    parser:
      type: "regex"
      groups:
        series: '(KGW\s*[IVX]+)'
        volume: '(\d+)'
        page: '(\d+)'
    formatter: "{series} {volume} [{page}]"
    increment_logic: "simple_numeric"
```

**Loading Configuration** (lib/citation_config.py - NEW):

```python
import yaml
from pathlib import Path
from typing import Dict

def load_citation_systems(config_path: Path = None) -> Dict:
    """
    Load citation system configurations from YAML.

    Args:
        config_path: Path to citation_systems.yaml (default: config/citation_systems.yaml)

    Returns:
        Dict of citation system configurations
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / 'config' / 'citation_systems.yaml'

    if not config_path.exists():
        # Fallback to hard-coded CITATION_PATTERNS
        return CITATION_PATTERNS

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    return config.get('systems', {})


# Use in marginalia_extraction.py:
CITATION_PATTERNS = load_citation_systems()
```

**BENEFIT**: Add new citation system by editing YAML, no code changes!

### Article Type Configuration

**File**: `config/article_processors.yaml`

```yaml
# Article Processing Configuration
# Customize section detection for different disciplines

disciplines:
  philosophy:
    section_patterns:
      introduction: ['^(1\.|I\.?)\s*introduction', '^introduction$']
      methods: ['^methodology', '^approach', '^method']
      analysis: ['^analysis', '^examination', '^interpretation']
      conclusion: ['^conclusion', '^concluding remarks']
    citation_style: "inline"  # (Author, Work, Page)
    reference_format: "bibliography"

  empirical_science:
    section_patterns:
      introduction: ['^(1\.|I\.?)\s*introduction']
      methods: ['^(2\.|II\.?)\s*methods', '^materials and methods']
      results: ['^(3\.|III\.?)\s*results']
      discussion: ['^(4\.|IV\.?)\s*discussion']
      conclusion: ['^(5\.|V\.?)\s*conclusion']
    citation_style: "numbered"  # [1], [2], [3]
    reference_format: "numbered_list"

  humanities:
    section_patterns:
      introduction: ['^introduction$', '^I\.']
      literature_review: ['^literature review', '^previous research']
      analysis: ['^analysis', '^findings']
      conclusion: ['^conclusion', '^implications']
    citation_style: "footnotes"
    reference_format: "bibliography"
```

---

## Part 5: Implementation Roadmap

### Immediate (Weeks 1-2): Foundation
1. ✅ Article support already exists in search (content_types)
2. Add ArticleMetadata dataclass
3. Add CitationReference dataclass
4. Add section_type to PageRegion

### Short-term (Weeks 3-4): Article Processing
5. Implement detect_document_type()
6. Implement _extract_abstract()
7. Implement _extract_keywords()
8. Implement _detect_article_section()
9. Create process_article_pdf()

### Mid-term (Weeks 5-6): Citation Extraction
10. Add Stage 4: Citation extraction to quality pipeline
11. Implement _stage_4_citation_extraction()
12. Test with philosophy articles
13. Validate citation detection accuracy

### Long-term (Weeks 7-8): Configuration System
14. Create config/citation_systems.yaml
15. Create config/article_processors.yaml
16. Implement load_citation_systems()
17. Add configuration hot-reload

---

## Part 6: Representation Strategy

### Output Modes for Different Consumers

```python
class OutputMode(Enum):
    """Output optimization for different consumers."""
    HUMAN_READABLE = "human"          # Clean markdown for humans
    LLM_OPTIMIZED = "llm"             # Explicit semantic markers
    STRUCTURED_JSON = "json"          # Full structure as JSON
    HYBRID = "hybrid"                 # Markdown + hidden metadata
```

**Human-Readable** (current):
```markdown
As Kant argues in the Critique of Pure Reason (A 50), the categories...
```

**LLM-Optimized** (proposed):
```markdown
As [AUTHOR]Kant[/AUTHOR] argues in the [WORK system="kant_a_b"]Critique of Pure Reason[/WORK] ([PAGE system="kant_a_b"]A 50[/PAGE]), the categories...

[CITATION id="cite-1" author="Kant" work="Critique of Pure Reason" page="A 50" system="kant_a_b" type="external" confidence="0.95"]
```

**Structured JSON** (for external resolvers):
```json
{
  "page_num": 5,
  "regions": [
    {
      "text": "As Kant argues in the Critique...",
      "citations": [
        {
          "text": "Critique of Pure Reason (A 50)",
          "author_hint": "Kant",
          "work_hint": "Critique of Pure Reason",
          "page_hint": "A 50",
          "citation_system": "kant_a_b",
          "position": [7, 41],
          "confidence": 0.95
        }
      ]
    }
  ]
}
```

**BENEFIT**: External resolver gets structured data, doesn't need NLP parsing.

---

## Part 7: Testing Strategy for Articles

### Test Coverage Matrix

| Scenario | Test Data | Expected Behavior | Status |
|----------|-----------|-------------------|--------|
| **Philosophy article** | Derrida_Structure_Sign_Play.pdf | Inline citations extracted | ❌ Missing |
| **Science article** | Neuroscience_Paper.pdf | IMRaD sections detected | ❌ Missing |
| **Multi-citation systems** | Plato_Commentary.pdf | Stephanus + page numbers | ❌ Missing |
| **Dense references** | Philosophy_Review.pdf | All 50+ refs extracted | ❌ Missing |
| **Abstract extraction** | Sample_Article.pdf | Abstract detected, 100-300 words | ❌ Missing |
| **Keyword extraction** | Sample_Article.pdf | 5-10 keywords extracted | ❌ Missing |

### Sample Test

**File**: `__tests__/python/test_article_processing.py` (NEW)

```python
import pytest
from pathlib import Path
from lib.rag_processing import process_article_pdf, detect_document_type

def test_detect_article_type():
    """Test article detection from Z-Library metadata."""
    book_details = {'type': 'article', 'title': 'Test Article'}
    doc_type = detect_document_type(Path('fake.pdf'), book_details)
    assert doc_type == 'article'

def test_extract_abstract():
    """Test abstract extraction from article PDF."""
    # Use real article PDF with known abstract
    result = process_article_pdf(Path('test_files/sample_article.pdf'))

    # Verify abstract extracted
    assert 'abstract' in result.metadata
    assert 100 <= len(result.metadata['abstract'].split()) <= 500

def test_extract_keywords():
    """Test keyword extraction."""
    result = process_article_pdf(Path('test_files/sample_article.pdf'))

    assert 'keywords' in result.metadata
    assert 3 <= len(result.metadata['keywords']) <= 15

def test_section_detection():
    """Test IMRaD section detection."""
    result = process_article_pdf(Path('test_files/sample_article.pdf'))

    # Verify sections detected
    sections = [r.section_type for r in result.regions if r.section_type]
    assert ArticleSectionType.ABSTRACT in sections
    assert ArticleSectionType.INTRODUCTION in sections
    assert ArticleSectionType.REFERENCES in sections

def test_citation_extraction():
    """Test inline citation extraction."""
    result = process_article_pdf(Path('test_files/philosophy_article.pdf'))

    # Find citations
    citations = [c for r in result.regions for c in r.citations]

    # Verify citations extracted
    assert len(citations) > 0

    # Verify structure
    for citation in citations:
        assert citation.author_hint is not None
        assert citation.work_hint is not None
        assert citation.confidence > 0.5
```

---

## Part 8: Benefits of This Architecture

### For RAG Processor (This System)
✅ **Focused scope**: Extract + represent, don't resolve
✅ **Simpler**: No cross-document lookup complexity
✅ **Faster**: No network calls to external libraries
✅ **Testable**: No dependencies on external data

### For External Citation Resolver (Separate System)
✅ **Rich input**: Structured citations with hints and confidence
✅ **Flexible**: Can use different resolution strategies
✅ **Cacheable**: Resolution results can be cached separately
✅ **Evolvable**: Improve resolver without changing RAG processor

### For End Users
✅ **Modular**: Can use RAG processor without citation resolver
✅ **Customizable**: Choose resolution strategy per use case
✅ **Transparent**: See what was detected vs what was resolved
✅ **Control**: Human-in-loop for resolution, not extraction

---

## Part 9: Current Status and Gaps

### Z-Library Article Support

| Feature | Status | Gap | Priority |
|---------|--------|-----|----------|
| **Search with content_types** | ✅ Implemented | None | - |
| **Article detection** | ✅ Implemented | None | - |
| **Article download** | ✅ Same as books | None | - |
| **Article metadata** | ⚠️ Partial | Missing DOI, journal info | 🟡 HIGH |
| **Article RAG processing** | ❌ Not implemented | Same as books | 🔴 CRITICAL |

### Citation Representation

| Feature | Status | Gap | Priority |
|---------|--------|-----|----------|
| **Inline citation detection** | ❌ Not implemented | No extraction | 🔴 CRITICAL |
| **Citation structure** | ❌ Not implemented | No CitationReference model | 🔴 CRITICAL |
| **Margin citations** | ✅ Detection exists | Not integrated | 🟡 HIGH |
| **Footnote citations** | ⚠️ Marker detection | No linking | 🟡 HIGH |

### Extensibility

| Feature | Status | Gap | Priority |
|---------|--------|-----|----------|
| **Citation system patterns** | ✅ 5 systems | Hard-coded | 🟢 MEDIUM |
| **Config-based systems** | ❌ Not implemented | No YAML config | 🟢 MEDIUM |
| **Article section detection** | ❌ Not implemented | No section types | 🟡 HIGH |
| **Document type routing** | ❌ Not implemented | No type detection | 🔴 CRITICAL |

---

## Part 10: Recommended Priority

### CRITICAL (Do First)
1. **Document type detection** - Route articles vs books
2. **CitationReference data model** - Foundation for everything
3. **Inline citation extraction** - Stage 4 of quality pipeline

### HIGH (Do Second)
4. **Article-specific processing** - process_article_pdf()
5. **Abstract/keyword extraction** - Critical for article RAG
6. **Section type detection** - Structured output

### MEDIUM (Do Third)
7. **Configuration-based citation systems** - YAML config
8. **Article metadata enhancement** - DOI, journal info
9. **Output mode selection** - Human vs LLM optimized

---

## Timeline

| Week | Phase | Deliverables |
|------|-------|--------------|
| **1-2** | Foundation | ArticleMetadata, CitationReference, document type detection |
| **3-4** | Article Processing | process_article_pdf(), abstract/keyword extraction |
| **5-6** | Citation Extraction | Stage 4, inline citation detection |
| **7-8** | Configuration | YAML-based citation systems, article processors |
| **9-10** | Testing & Refinement | Integration tests, real article validation |

**Total**: 10 weeks to full article support with citation representation

---

## Appendix: Example External Resolver

This is OUT OF SCOPE for this project, but shows how external system would use our representation:

```python
# EXTERNAL SERVICE (separate project/repository)

class PhilosophyLibraryResolver:
    """
    Resolves citations to documents using Z-Library MCP.

    Consumes rich citation representation from RAG processor,
    resolves to actual documents, creates cross-document links.
    """

    def __init__(self, zlibrary_mcp_client):
        self.zlib = zlibrary_mcp_client
        self.cache = {}

    async def resolve_citation(self, citation: CitationReference) -> Optional[ResolvedCitation]:
        """
        Resolve citation to actual document.

        Args:
            citation: Rich citation from RAG processor

        Returns:
            ResolvedCitation with document reference, or None if not found
        """
        # Build search query from hints
        query = f"{citation.author_hint} {citation.work_hint}"

        # Search Z-Library
        results = await self.zlib.search_books(query=query, exact=True)

        # Find best match
        for result in results:
            if self._is_match(citation, result):
                return ResolvedCitation(
                    citation=citation,
                    document_id=result['id'],
                    document_title=result['title'],
                    confidence=0.95
                )

        return None

    async def create_citation_graph(self, documents: List[ProcessedDocument]):
        """
        Create graph of citation relationships across documents.

        Nodes: Documents
        Edges: Citations (directed: source cites target)
        """
        graph = CitationGraph()

        for doc in documents:
            for citation in doc.get_all_citations():
                resolved = await self.resolve_citation(citation)
                if resolved:
                    graph.add_edge(doc.id, resolved.document_id, citation)

        return graph
```

**This external resolver can**:
- Resolve citations across entire library
- Build citation networks
- Find related works
- Recommend reading order
- Identify influential texts

**WITHOUT the RAG processor needing to know about any of this!**

---

**Document Status**: Design specification for article support and citation representation architecture

**Next Steps**: Review with user, prioritize features, begin implementation

**Estimated Implementation**: 10 weeks for complete article support with rich citation representation
