> Historical (1.x, HTML era): the terms, booklists and IPFS ids described here are not exposed by Z-Library's API and are not returned in 2.0 (#256). Current `get_book` fields are in [api.md](api.md); retired tools are in [tool migration](interface-migration.md).

# Z-Library MCP Research Tools Specification

**Version**: 2.0
**Date**: 2025-01-30
**Status**: Design Phase
**Implementation Approach**: Test-Driven Development (TDD)

## Philosophy: AI-First Research Workflows

These tools are designed for **autonomous AI research agents**, not just human users. They enable:
- **Multi-hop reasoning**: Traverse knowledge graphs automatically
- **Hypothesis-driven exploration**: Test research questions systematically
- **Corpus building**: Assemble comprehensive research collections
- **Serendipitous discovery**: Find unexpected but relevant connections
- **Evidence gathering**: Collect supporting materials efficiently

---

## Tool Categories

### 🔍 Category 1: Enhanced Search & Discovery
Tools for finding materials with precision and serendipity

### 🏷️ Category 2: Concept Exploration
Tools for navigating the conceptual landscape

### 📚 Category 3: Collection Management
Tools for working with curated book collections

### 🕸️ Category 4: Knowledge Graph Navigation
Tools for multi-hop exploration and relationship mapping

---

## Tool Specifications

### 1. `search_books_advanced`

**Purpose**: Enhanced search with exact and nearest match separation

**Rationale**: Researchers need both precision (exact matches) and discovery (near matches). Current `search_books` mixes them, losing valuable information about match quality.

**Interface**:
```typescript
interface SearchBooksAdvancedParams {
  query: string;
  exact_limit?: number;        // Max exact matches (default: 10)
  nearest_limit?: number;      // Max nearest matches (default: 10)
  include_nearest?: boolean;   // Return nearest matches (default: true)

  // Standard filters
  yearFrom?: number;
  yearTo?: number;
  languages?: string[];
  extensions?: string[];
  content_types?: string[];
  order?: string;              // "popular" | "date_created" | "date_updated"
}

interface SearchBooksAdvancedResponse {
  exact_matches: Book[];
  nearest_matches: Book[];
  exact_count: number;
  nearest_count: number;
  search_url: string;
  has_more_exact: boolean;
  has_more_nearest: boolean;
}
```

**Implementation Logic**:
1. Perform search with combined limit (`exact_limit + nearest_limit`)
2. Parse HTML for `<div class="fuzzyMatchesLine">` separator
3. Books before separator = exact matches
4. Books after separator = nearest matches
5. Respect individual limits for each category

**Test Cases**:
- Query with only exact matches (no fuzzyMatchesLine)
- Query with both exact and nearest matches
- Query with more nearest than exact
- Empty query handling
- Limit enforcement (exact_limit=5, nearest_limit=3)

---

### 2. `explore_term`

**Purpose**: Discover books related to a specific term/concept

**Rationale**: Researchers explore by concept, not just title search. Terms provide thematic clustering (e.g., all books discussing "dialectic" or "phenomenology").

**Interface**:
```typescript
interface ExploreTermParams {
  term: string;               // e.g., "reflection", "dialectic"
  limit?: number;            // Max books to return (default: 50)
  filters?: {
    yearFrom?: number;
    yearTo?: number;
    languages?: string[];
    extensions?: string[];
  };
}

interface ExploreTermResponse {
  term: string;
  term_url: string;
  books: Book[];
  total_books: number;        // Total books with this term
  related_terms?: string[];   // Other common terms in these books
  statistics?: {
    languages_distribution: { [lang: string]: number };
    year_distribution: { [year: string]: number };
    format_distribution: { [ext: string]: number };
  };
}
```

**Implementation Logic**:
1. Construct URL: `{mirror}/terms/{term}`
2. Parse search results (same structure as regular search)
3. Extract books with term metadata
4. Optionally scrape related terms from sidebar/suggestions
5. Calculate statistics from results

**Test Cases**:
- Common term ("reflection") with many results
- Rare term with few results
- Non-existent term (empty results)
- Term with special characters
- Pagination handling for large term results

---

### 3. `get_book_metadata`

**Purpose**: Extract comprehensive metadata including terms and booklists

**Rationale**: Book detail pages contain rich metadata (terms, lists, related books) that enable knowledge graph traversal. Current implementation only extracts basic metadata.

**Interface**:
```typescript
interface GetBookMetadataParams {
  book_id: string;
  include_terms?: boolean;      // Extract term list (default: true)
  include_booklists?: boolean;  // Extract booklists (default: true)
  include_related?: boolean;    // Extract related books (default: false)
}

interface GetBookMetadataResponse {
  book: Book;                   // Basic book data
  terms?: string[];             // All terms (e.g., ["dialectic", "logic", "being"])
  term_count?: number;
  booklists?: BooklistReference[];
  booklist_count?: number;
  related_books?: Book[];
  download_link?: string;       // Direct download URL
}

interface BooklistReference {
  id: string;
  hash: string;
  name: string;
  url: string;
  book_count?: number;          // If visible on page
}
```

**Implementation Logic**:
1. Fetch book detail page: `{mirror}/book/{id}/{hash}/{title}.html`
2. Extract download link: `<a class="addDownloadedBook" href="/dl/{id}/{hash}">`
3. Extract terms: All `<a href="/terms/{term}">` links
4. Extract booklists: All `<a href="/booklist/{id}/{hash}/{name}.html">` links
5. Optionally extract related books from recommendations section

**Test Cases**:
- Book with many terms (philosophy book ~60 terms)
- Book with no terms
- Book in multiple booklists
- Book not in any booklists
- Invalid book ID
- Book with related books section
- Download link extraction accuracy

---

### 4. `get_booklist`

**Purpose**: Retrieve all books from a curated booklist

**Rationale**: Booklists are expert-curated collections. Downloading entire lists enables comprehensive topic coverage (e.g., "Philosophy" list with 954 books).

**Interface**:
```typescript
interface GetBooklistParams {
  list_id: string;              // e.g., "409997"
  list_hash: string;            // e.g., "370858"
  list_name?: string;           // e.g., "philosophy" (optional, for URL construction)

  limit?: number;               // Max books to return (default: 100)
  page?: number;                // Pagination support (default: 1)
  fetch_all_pages?: boolean;    // Get all books, not just one page (default: false)
}

interface GetBooklistResponse {
  booklist: {
    id: string;
    hash: string;
    name: string;
    url: string;
    description?: string;
    total_books: number;        // Total in entire list
    creator?: string;
  };
  books: Book[];                // Books on this page/request
  current_page: number;
  total_pages: number;
  has_next_page: boolean;
}
```

**Implementation Logic**:
1. Construct URL: `{mirror}/booklist/{id}/{hash}/{name}.html?page={page}`
2. Parse booklist metadata from page header
3. Extract books (standard z-bookcard parsing)
4. Detect pagination info (21 books per page typical)
5. If `fetch_all_pages=true`, iterate through all pages

**Test Cases**:
- Small list (<21 books, single page)
- Large list (954 books, needs pagination)
- Empty booklist
- Invalid list ID
- Page beyond total pages
- Fetch all pages functionality
- Rate limiting respect (don't spam requests)

---

### 5. `download_booklist`

**Purpose**: Download all books from a booklist, respecting limits

**Rationale**: Build comprehensive research corpora automatically (e.g., download all 954 philosophy books for NLP analysis).

**Interface**:
```typescript
interface DownloadBooklistParams {
  list_id: string;
  list_hash: string;
  list_name?: string;

  max_downloads?: number;       // Safety limit (default: 50)
  skip_existing?: boolean;      // Skip already downloaded (default: true)
  output_dir?: string;          // Where to save (default: configured output root/booklists/{name})
  include_rag?: boolean;        // Process for RAG (default: false)

  filters?: {
    extensions?: string[];      // Only download specific formats
    yearFrom?: number;
    yearTo?: number;
    max_size_mb?: number;       // Skip large files
  };
}

interface DownloadBooklistResponse {
  booklist_name: string;
  total_in_list: number;
  attempted: number;
  successful: number;
  failed: number;
  skipped: number;
  downloads: Array<{
    book_id: string;
    title: string;
    status: "success" | "failed" | "skipped";
    file_path?: string;
    error?: string;
  }>;
  daily_downloads_remaining: number;
}
```

**Implementation Logic**:
1. Get booklist with `fetch_all_pages=true`
2. Apply filters to book list
3. Check download limits before starting
4. Download each book sequentially (respect rate limits)
5. Track success/failure for each
6. Stop if daily limit reached
7. Optionally process for RAG after each download
8. Return comprehensive report

**Test Cases**:
- Download small list (5 books)
- Download with filters (only PDF, only English)
- Download respecting max_downloads limit
- Download hitting daily limit mid-operation
- Skip existing files
- Failed download handling
- RAG processing integration
- Insufficient download quota

**Safety Considerations**:
- Respect daily download limits (998/999)
- Rate limiting between downloads (1 sec delay)
- Disk space checking
- Graceful failure handling
- Progress tracking for long operations

---

### 6. `search_articles`

**Purpose**: Search academic articles with author-specific syntax

**Rationale**: Articles require different search patterns (author:, content_type) and have different metadata structures (use slots vs attributes).

**Interface**:
```typescript
interface SearchArticlesParams {
  author?: string;              // Search by author name
  query?: string;               // General query
  exact?: boolean;

  // Standard filters
  yearFrom?: number;
  yearTo?: number;
  languages?: string[];
  extensions?: string[];

  limit?: number;
}

interface SearchArticlesResponse {
  articles: Article[];          // Different from Book structure
  total_count: number;
  search_url: string;
}

interface Article {
  id: string;
  title: string;                // From <div slot="title">
  authors: string[];            // From <div slot="author">, semicolon-separated
  year: number;
  extension: string;
  filesize: string;
  url: string;
  download_link: string;        // Articles include direct download link!
  doi?: string;                 // If ISBN is DOI format
}
```

**Implementation Logic**:
1. Construct URL with `content_type=article` parameter
2. If author specified: add `q=author:{author_name}` (URL-encoded)
3. Parse results (z-bookcard with different structure)
4. Extract from slots: `<div slot="title">`, `<div slot="author">`
5. Articles have direct download attribute: `download="/dl/{id}/{hash}"`

**Test Cases**:
- Search by author only ("author:heidegger")
- Search by author + query ("author:heidegger phenomenology")
- Search by query only (no author)
- Articles vs books distinction
- Slot-based parsing vs attribute-based
- Direct download link extraction

---

### 7. `get_term_books`

**Purpose**: Get all books associated with a specific term (simplified version of explore_term)

**Interface**:
```typescript
interface GetTermBooksParams {
  term: string;
  limit?: number;
  page?: number;
}

interface GetTermBooksResponse {
  term: string;
  books: Book[];
  total_books: number;
  current_page: number;
  has_more: boolean;
}
```

---

### 8. `get_book_terms`

**Purpose**: Extract all terms from a book's detail page

**Rationale**: Build concept maps, find related topics, enable graph traversal

**Interface**:
```typescript
interface GetBookTermsParams {
  book_id: string;
  book_hash?: string;           // Optional, will fetch if not provided
  limit?: number;               // Max terms to return (default: all)
  min_frequency?: number;       // Only terms appearing X times (future)
}

interface GetBookTermsResponse {
  book_id: string;
  book_title: string;
  terms: string[];              // Alphabetically sorted
  term_count: number;
  sample_terms_with_urls?: Array<{
    term: string;
    url: string;
  }>;
}
```

**Implementation Logic**:
1. Fetch book detail page
2. Extract all `<a href="/terms/{term}">` links
3. Deduplicate terms
4. Sort alphabetically
5. Return structured data

---

### 9. `get_book_booklists`

**Purpose**: Find all booklists containing a specific book

**Interface**:
```typescript
interface GetBookBooklistsParams {
  book_id: string;
  book_hash?: string;
}

interface GetBookBooklistsResponse {
  book_id: string;
  book_title: string;
  booklists: BooklistReference[];
  booklist_count: number;
}
```

---

### 10. `build_knowledge_graph` (Advanced)

**Purpose**: Multi-hop exploration starting from a book/term/author

**Rationale**: Enable autonomous research agents to map conceptual territories automatically

**Interface**:
```typescript
interface BuildKnowledgeGraphParams {
  starting_point: {
    type: "book" | "term" | "author" | "booklist";
    identifier: string;
  };
  max_hops?: number;            // Depth of exploration (default: 2)
  max_nodes?: number;           // Max total items (default: 100)
  strategies?: Array<"terms" | "booklists" | "authors" | "citations">;
}

interface KnowledgeGraphResponse {
  graph: {
    nodes: Array<{
      id: string;
      type: "book" | "term" | "author" | "booklist";
      label: string;
      metadata: any;
    }>;
    edges: Array<{
      from: string;
      to: string;
      relationship: "has_term" | "in_booklist" | "by_author" | "cites";
      weight?: number;
    }>;
  };
  statistics: {
    total_nodes: number;
    total_edges: number;
    hops_completed: number;
  };
}
```

**Implementation Strategy**:
1. Start with seed node
2. Expand using configured strategies
3. Track visited nodes to avoid cycles
4. Respect max_hops and max_nodes limits
5. Build graph structure during traversal
6. Return networkx-compatible format

---

## Implementation Plan (TDD Approach)

### Phase 1: Enhanced Search (Week 1)

**Day 1-2: Tests First**
- Write test suite for `search_books_advanced`
- Mock HTML responses with exact + nearest structure
- Test edge cases (no nearest, no exact, empty)

**Day 3-4: Implementation**
- Enhance parsing logic in `abs.py`
- Add fuzzyMatchesLine detection
- Implement result separation

**Day 5: Integration**
- Update `python_bridge.py`
- Add MCP tool registration
- Test with real Z-Library

### Phase 2: Term & Metadata Tools (Week 2)

**Day 1: Tests**
- `explore_term` test suite
- `get_book_terms` test suite
- `get_book_booklists` test suite

**Day 2-3: Implementation**
- Term page parsing
- Book metadata extraction enhancement
- URL construction and navigation

**Day 4-5: Integration & Validation**
- Real data testing
- Performance optimization
- Documentation

### Phase 3: Collection Tools (Week 3)

**Day 1-2: Tests**
- `get_booklist` with pagination tests
- `download_booklist` with limits and filters

**Day 3-4: Implementation**
- Booklist parsing and pagination
- Batch download with safety limits
- Progress tracking

**Day 5: Polish**
- Error handling
- Rate limiting
- Documentation

### Phase 4: Advanced Features (Week 4)

**Optional**: Knowledge graph building

---

## Technical Implementation Details

### Parsing Patterns Discovered

#### Nearest Match Separator
```html
<!-- Exact matches -->
<div class="book-item resItemBoxBooks">
  <div class="counter">1</div>
  <z-bookcard>...</z-bookcard>
</div>
<!-- ... more exact matches ... -->

<!-- Separator -->
<div class="fuzzyMatchesLine">
  <div>The books listed below don't fit your search query exactly but very close to it.</div>
</div>

<!-- Nearest matches -->
<div class="book-item resItemBoxBooks">
  <div class="counter">33</div>
  <z-bookcard>...</z-bookcard>
</div>
```

**Parser Logic**:
```python
soup = BeautifulSoup(html, 'html.parser')
fuzzy_line = soup.find('div', {'class': 'fuzzyMatchesLine'})

if fuzzy_line:
    # Split results at fuzzy line
    all_items = soup.find_all('div', {'class': 'book-item'})
    fuzzy_index = # find index of fuzzy_line in parent
    exact_items = all_items[:fuzzy_index]
    nearest_items = all_items[fuzzy_index:]
else:
    # All results are exact matches
    exact_items = soup.find_all('div', {'class': 'book-item'})
    nearest_items = []
```

#### Term Links Extraction
```python
# From book detail page
term_links = soup.find_all('a', href=lambda x: x and x.startswith('/terms/'))
terms = [link.get('href').split('/terms/')[-1] for link in term_links]
# Result: ["reflection", "dialectic", "being", ...]
```

#### Booklist Links Extraction
```python
booklist_links = soup.find_all('a', href=lambda x: x and '/booklist/' in x)
booklists = []
for link in booklist_links:
    href = link.get('href')
    # Parse: /booklist/409997/370858/philosophy.html
    parts = href.split('/')
    booklists.append({
        'id': parts[2],
        'hash': parts[3],
        'name': parts[4].replace('.html', ''),
        'url': f"{mirror}{href}",
        'display_name': link.text.strip()
    })
```

#### Article-Specific Parsing
```python
# Articles use slot structure
article_card = soup.find('z-bookcard')
title = article_card.find('div', {'slot': 'title'})
authors = article_card.find('div', {'slot': 'author'})
download_attr = article_card.get('download')  # Direct download link!

article = {
    'id': article_card.get('id'),
    'title': title.text if title else None,
    'authors': authors.text.split(';') if authors else [],
    'download_link': download_attr,  # No need to fetch detail page!
    # ... other attributes same as books
}
```

---

## API Design Principles

### 1. Separation of Concerns
- Parsing logic in Python (`abs.py`, `libasync.py`)
- Business logic in Python bridge (`python_bridge.py`)
- Type safety in TypeScript layer (`zlibrary-api.ts`)
- MCP registration in Node.js (`index.ts`)

### 2. Progressive Enhancement
- Basic tools work without advanced features
- Advanced features add value, don't replace basics
- Backward compatible with existing tools

### 3. Error Handling Layers
```
MCP Tool → TypeScript → Python Bridge → AsyncZlib → HTTP
    ↓          ↓            ↓              ↓          ↓
Validation  Type Check  Safe Wrapper   Retry    Network
```

### 4. Data Structures
- Consistent Book/Article interfaces
- Rich metadata with optional fields
- URLs always included for manual access
- Counts for pagination decisions

---

## Testing Strategy

### Test Pyramid

```
        /\
       /  \        E2E Tests (5%)
      /____\       - Real Z-Library integration tests
     /      \
    /        \     Integration Tests (15%)
   /__________\    - Python bridge <-> zlibrary tests
  /            \
 /              \  Unit Tests (80%)
/________________\ - Parsing logic, URL construction, data transformation
```

### Test Data Strategy

**Mock HTML Fixtures**:
- `test_files/search_exact_only.html` - No nearest matches
- `test_files/search_with_nearest.html` - Both types
- `test_files/term_page.html` - Term exploration page
- `test_files/booklist_small.html` - Small list (21 books)
- `test_files/booklist_large.html` - Large list (954 books)
- `test_files/book_with_metadata.html` - Rich metadata
- `test_files/article_results.html` - Article search results

**Real Integration Tests** (with auth):
- `__tests__/integration/test_advanced_search.py`
- `__tests__/integration/test_term_exploration.py`
- `__tests__/integration/test_booklist_operations.py`

### Test Requirements

**Every new tool must have**:
1. Unit tests for parsing logic
2. Unit tests for URL construction
3. Unit tests for error handling
4. Integration test with mock HTTP
5. E2E test with real Z-Library (manual/optional)
6. Minimum 80% code coverage

---

## Performance Considerations

### Caching Strategy

**Cache expensive operations**:
```typescript
// Cache term exploration results (1 hour)
cache.set(`term:${term_name}`, results, 3600);

// Cache booklist metadata (24 hours)
cache.set(`booklist:${id}:metadata`, metadata, 86400);

// Cache book metadata (6 hours)
cache.set(`book:${id}:metadata`, metadata, 21600);
```

### Rate Limiting

**Respect Z-Library's infrastructure**:
- Max 10 concurrent requests (existing semaphore)
- 1 second delay between downloads
- Exponential backoff on errors (existing retry logic)
- Stop if success rate < 80%

### Pagination Optimization

**For large booklists (954 books)**:
- Don't fetch all pages by default
- Use lazy pagination (fetch on demand)
- Cache fetched pages
- Parallel page fetching with limit (max 3 concurrent)

---

## Research Workflow Examples

### Example 1: Topic Deep Dive

**Scenario**: Research Hegel's philosophy comprehensively

```typescript
// 1. Start with term exploration
const dialectic = await explore_term({ term: "dialectic", limit: 20 });

// 2. Get key book's metadata
const book_meta = await get_book_metadata({
  book_id: "1252896",  // Hegel's Science of Logic
  include_terms: true,
  include_booklists: true
});

// 3. Explore related terms
for (const term of book_meta.terms.slice(0, 5)) {
  const related = await explore_term({ term, limit: 10 });
  // Build concept map
}

// 4. Download relevant booklists
for (const list of book_meta.booklists) {
  if (list.name.includes('hegel') || list.name.includes('philosophy')) {
    await download_booklist({
      list_id: list.id,
      list_hash: list.hash,
      max_downloads: 20,
      filters: { extensions: ["PDF"], yearFrom: 2000 }
    });
  }
}
```

### Example 2: Author Study

**Scenario**: Collect all of Heidegger's works and related materials

```typescript
// 1. Search Heidegger's articles
const articles = await search_articles({
  author: "heidegger",
  languages: ["english"],
  limit: 50
});

// 2. Search Heidegger's books
const books = await search_books_advanced({
  query: "author:heidegger",
  exact_limit: 50,
  include_nearest: false
});

// 3. Explore key terms from his works
const being_books = await explore_term({ term: "being" });
const dasein_books = await explore_term({ term: "dasein" });

// 4. Find curated collections
const phenomenology_list = await get_booklist({
  list_id: "123456",
  fetch_all_pages: true
});
```

### Example 3: Corpus Building

**Scenario**: Build training corpus on phenomenology

```typescript
// 1. Get comprehensive booklist
const phenom_list = await download_booklist({
  list_id: "409997",  // Philosophy list
  list_hash: "370858",
  max_downloads: 100,
  include_rag: true,
  filters: {
    extensions: ["PDF", "EPUB"],
    yearFrom: 1900,
    max_size_mb: 50
  }
});

// 2. Download term-related books
const terms = ["phenomenology", "intentionality", "consciousness"];
for (const term of terms) {
  const term_results = await explore_term({ term, limit: 20 });
  // Download top results
}

// 3. Result: Comprehensive corpus in ./processed_rag_output/
// Ready for NLP analysis, RAG indexing, etc.
```

---

## Implementation Files

### New Files to Create

**Python**:
- `lib/advanced_search.py` - Enhanced search parsing
- `lib/term_explorer.py` - Term-based navigation
- `lib/booklist_manager.py` - Collection operations
- `lib/article_search.py` - Article-specific logic

**TypeScript**:
- `src/lib/research-tools.ts` - TypeScript interfaces
- `src/lib/knowledge-graph.ts` - Graph building logic

**Tests**:
- `__tests__/python/test_advanced_search.py`
- `__tests__/python/test_term_explorer.py`
- `__tests__/python/test_booklist_manager.py`
- `__tests__/python/test_article_search.py`
- `__tests__/advanced-search.test.js` (Node.js integration)

### Files to Modify

**Python Bridge**:
- `lib/python_bridge.py` - Add new function exports

**Z-Library Wrapper**:
- `zlibrary/src/zlibrary/abs.py` - Enhanced parsing logic
- `zlibrary/src/zlibrary/libasync.py` - New search methods

**MCP Server**:
- `src/index.ts` - Register new tools

**Type Definitions**:
- `src/types.ts` - Add new interfaces

---

## Success Metrics

### Functionality Metrics
- ✅ All 9 new tools implemented and working
- ✅ 80%+ test coverage on new code
- ✅ Zero regressions on existing tools
- ✅ All tests passing (Jest + Pytest)

### Research Workflow Metrics
- ✅ Can build 100-book corpus in <10 minutes
- ✅ Can explore term with 50 books in <5 seconds
- ✅ Can traverse 3-hop knowledge graph in <30 seconds
- ✅ Nearest match accuracy >90%

### Code Quality Metrics
- ✅ TypeScript compilation with no errors
- ✅ Linting passes
- ✅ No console.log statements in production
- ✅ Comprehensive error messages
- ✅ All async operations have timeouts

---

## Risk Assessment

### Implementation Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| HTML structure changes | Medium | High | Robust parsing with fallbacks |
| Performance degradation | Low | Medium | Caching + rate limiting |
| Download limit exhaustion | Medium | Low | User configurable limits |
| Booklist pagination breaks | Low | Medium | Test with multiple list sizes |
| Term extraction inaccuracy | Low | Low | Validate with real data |

### Research Workflow Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Graph traversal infinite loop | Medium | High | Cycle detection + max depth |
| Corpus too large for disk | Medium | Medium | Size limits + disk checks |
| Download blocking mid-corpus | Low | High | Checkpoint and resume |
| Irrelevant results in nearest | Medium | Low | User controls limits |

---

## Documentation Requirements

### For Each Tool

1. **API Documentation**
   - Purpose and use cases
   - Parameter descriptions
   - Return value structure
   - Error conditions
   - Example usage

2. **Implementation Notes**
   - Parsing logic explanation
   - Performance characteristics
   - Caching behavior
   - Rate limiting impact

3. **Research Workflow Examples**
   - Real-world scenarios
   - Multi-tool combinations
   - Best practices

---

## Future Enhancements

### Version 2.1 (Post-MVP)
- Citation network mapping
- Co-author network analysis
- Temporal analysis (publication trends)
- Similarity scoring between books
- Recommendation engine

### Version 2.2
- Full-text search integration
- PDF content analysis
- Automated literature review generation
- Concept extraction from text
- Cross-reference validation

---

## Appendix: Discovered Data Structures

### Article Structure (Different from Books)
```html
<z-bookcard
  id="81025351"
  isbn="10.1163/156916479x00156"     <!-- DOI, not ISBN -->
  termshash="d9149f41fcc506bc3175c807e447860c"
  href="/book/81025351/1ee20a/..."
  download="/dl/81025351/e975a3"    <!-- Direct download link! -->
  deleted=""
  publisher=""
  language="English"
  year="1979"
  extension="pdf"
  filesize="626 KB"
  rating="0.0"
  quality="0.0"
  premium
  contentTypeParam="?content_type=article">

  <img data-src="/img/cover-not-exists.png" />
  <div slot="title">Article Title Here</div>
  <div slot="author">Author; One; Author; Two</div>
</z-bookcard>
```

**Key Differences**:
- `isbn` contains DOI for articles
- `download` attribute = direct link (no detail page needed!)
- `slot="title"` and `slot="author"` instead of attributes
- `contentTypeParam` distinguishes articles
- Often no cover image

### Booklist Structure

**URL**: `/booklist/409997/370858/philosophy.html`
**Books per page**: 21
**Total books**: 954 (from metadata)
**Pagination**: `?page=2`, `?page=3`, etc.

### Terms Structure

**URL**: `/terms/reflection`
**Results**: Standard book search results
**Related terms**: May be in sidebar (need to check)
**Usage**: Conceptual clustering of books

---

**Implementation Priority**: Start with `search_books_advanced` (builds on existing), then term tools, then booklist tools. Follow TDD strictly - tests before code!