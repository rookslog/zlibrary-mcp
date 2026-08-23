# API Reference

This is the API reference for the **zlibrary-mcp** MCP (Model Context Protocol) server. Tools are invoked via MCP protocol -- not HTTP REST.

**Credentials.** Most tools read from Z-Library and need `ZLIBRARY_EMAIL` and `ZLIBRARY_PASSWORD` in the MCP client environment. Two tools do not:

- `search_multi_source` reads from Library Genesis and Anna's Archive.
- `download_book_to_file` needs no credentials when you pass it a Library Genesis result.

The server starts without credentials. The Z-Library tools then fail when you call them, and the other tools continue to work.

All tools return MCP content arrays in the format:

```json
{
  "content": [{ "type": "text", "text": "<JSON string or error message>" }]
}
```

On error, responses include `isError: true` and a descriptive error message.

## Table of Contents

- [Search Tools](#search-tools)
  - [search_books](#search_books)
  - [full_text_search](#full_text_search)
  - [search_by_term](#search_by_term)
  - [search_by_author](#search_by_author)
  - [search_advanced](#search_advanced)
  - [search_multi_source](#search_multi_source)
  - [get_recent_books](#get_recent_books)
- [Metadata Tools](#metadata-tools)
  - [get_book_metadata](#get_book_metadata)
- [Collection Tools](#collection-tools)
  - [fetch_booklist](#fetch_booklist)
- [Download and Processing Tools](#download-and-processing-tools)
  - [download_book_to_file](#download_book_to_file)
  - [process_document_for_rag](#process_document_for_rag)
- [Utility Tools](#utility-tools)
  - [get_download_limits](#get_download_limits)
  - [get_download_history](#get_download_history)

---

## Search Tools

### search_books

**Description:** Search for books in Z-Library by title, author, or keywords. Returns matching books with metadata including title, author, year, format, and file size. Use `exact=true` for precise title matching. Filter results by year range, language, or file format.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| query | string | Yes | -- | Search query |
| exact | boolean | No | `false` | Whether to perform an exact match search |
| fromYear | integer | No | -- | Filter by minimum publication year |
| toYear | integer | No | -- | Filter by maximum publication year |
| languages | string[] | No | `[]` | Filter by languages (e.g., `["english", "russian"]`) |
| extensions | string[] | No | `[]` | Filter by file extensions (e.g., `["pdf", "epub"]`) |
| content_types | string[] | No | `[]` | Filter by content types (e.g., `["book", "article"]`) |
| count | integer | No | `10` | Number of results to return per page |

**Returns:** JSON array of book objects, each containing fields such as `title`, `author`, `name`, `authors`, `year`, `extension`, `filesize`, and additional metadata.

**Example Usage:**

```json
{
  "tool": "search_books",
  "arguments": {
    "query": "Being and Time Heidegger",
    "languages": ["english"],
    "extensions": ["pdf", "epub"],
    "count": 5
  }
}
```

**Error Cases:**
- Invalid or missing `query` parameter
- Authentication failure (invalid or expired Z-Library credentials)
- Network errors (Z-Library unreachable, Cloudflare block)
- Rate limiting by Z-Library

---

### full_text_search

**Description:** Search for books containing specific text within their content. Useful for finding books that discuss particular topics, quotes, or concepts.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| query | string | Yes | -- | Text to search for in book content |
| exact | boolean | No | `false` | Whether to perform an exact match search |
| phrase | boolean | No | `true` | Whether to search for the exact phrase (requires at least 2 words) |
| words | boolean | No | `false` | Whether to search for individual words |
| languages | string[] | No | `[]` | Filter by languages (e.g., `["english", "russian"]`) |
| extensions | string[] | No | `[]` | Filter by file extensions (e.g., `["pdf", "epub"]`) |
| content_types | string[] | No | `[]` | Filter by content types (e.g., `["book", "article"]`) |
| count | integer | No | `10` | Number of results to return per page |

**Returns:** JSON array of book objects containing the searched text, with the same metadata fields as `search_books`.

**Example Usage:**

```json
{
  "tool": "full_text_search",
  "arguments": {
    "query": "transcendental unity of apperception",
    "phrase": true,
    "languages": ["english"]
  }
}
```

**Error Cases:**
- Invalid or missing `query` parameter
- Single-word query with `phrase=true` (requires at least 2 words)
- Authentication failure
- Network errors or rate limiting

---

### search_by_term

**Description:** Search for books by conceptual term. Books in Z-Library are tagged with 60+ conceptual terms (e.g., "phenomenology", "dialectic", "epistemology"). This enables conceptual navigation beyond keyword matching.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| term | string | Yes | -- | Conceptual term to search for (e.g., "dialectic", "phenomenology") |
| yearFrom | integer | No | -- | Filter by minimum publication year |
| yearTo | integer | No | -- | Filter by maximum publication year |
| languages | string[] | No | `[]` | Filter by languages |
| extensions | string[] | No | `[]` | Filter by file extensions |
| count | integer | No | `25` | Number of results to return |

**Returns:** JSON array of book objects tagged with the specified conceptual term.

**Example Usage:**

```json
{
  "tool": "search_by_term",
  "arguments": {
    "term": "phenomenology",
    "yearFrom": 1900,
    "yearTo": 2020,
    "languages": ["english"],
    "count": 15
  }
}
```

**Error Cases:**
- Invalid or missing `term` parameter
- Term not recognized by Z-Library (returns empty results, not an error)
- Authentication failure
- Network errors or rate limiting

---

### search_by_author

**Description:** Advanced author search with support for various name formats. Use `exact=true` for precise matching. Filter by publication year, language, or file format. Supports "Lastname, Firstname" format.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| author | string | Yes | -- | Author name (supports "Lastname, Firstname" format) |
| exact | boolean | No | `false` | Use exact author name matching |
| yearFrom | integer | No | -- | Filter by minimum publication year |
| yearTo | integer | No | -- | Filter by maximum publication year |
| languages | string[] | No | `[]` | Filter by languages |
| extensions | string[] | No | `[]` | Filter by file extensions |
| count | integer | No | `25` | Number of results to return |

**Returns:** JSON array of book objects by the specified author.

**Example Usage:**

```json
{
  "tool": "search_by_author",
  "arguments": {
    "author": "Hegel, Georg Wilhelm Friedrich",
    "exact": true,
    "extensions": ["pdf"],
    "count": 20
  }
}
```

**Error Cases:**
- Invalid or missing `author` parameter
- Authentication failure
- Network errors or rate limiting

---

### search_advanced

**Description:** Advanced search with automatic separation of exact matches from fuzzy/approximate matches. Returns two arrays: `exact_matches` and `fuzzy_matches`, making it easier to distinguish precise hits from related results.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| query | string | Yes | -- | Search query |
| exact | boolean | No | `false` | Whether to perform exact match search |
| yearFrom | integer | No | -- | Filter by minimum publication year |
| yearTo | integer | No | -- | Filter by maximum publication year |
| count | integer | No | `10` | Number of results to return |

**Returns:** JSON object with two arrays:
- `exact_matches`: Books that precisely match the query
- `fuzzy_matches`: Books that approximately match the query

Each entry contains standard book metadata fields.

**Example Usage:**

```json
{
  "tool": "search_advanced",
  "arguments": {
    "query": "Critique of Pure Reason",
    "yearFrom": 1990,
    "count": 10
  }
}
```

**Error Cases:**
- Invalid or missing `query` parameter
- Authentication failure
- Network errors or rate limiting

---

### search_multi_source

**Description:** Search for books in Library Genesis or Anna's Archive. This tool is an alternative to the Z-Library search tools. It needs no Z-Library account.

Results from this tool can be downloaded. Pass a result to `download_book_to_file` as `bookDetails`.

**Note:** Anna's Archive results contain the MD5 hash and the record URL only, unless you set `ANNAS_SECRET_KEY`. Library Genesis results contain full metadata and can be downloaded without a key.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| query | string | Yes | -- | Search query |
| source | enum | No | `"auto"` | Source selection: `"auto"` (Anna's Archive if key available, else LibGen), `"annas"` (force Anna's Archive), or `"libgen"` (force LibGen) |
| count | integer | No | `10` | Maximum number of results to return |

**Returns:** JSON object with `books` and `routing`.

`books` is an array of book objects with the fields `md5`, `title`, `author`, `year`, `extension`, `size`, and `source`.

`routing` reports what you asked for, what answered, and what each source can currently do. Every fact in it is read from configuration, so it costs no extra network request. Facts that would cost one live in `get_download_limits` instead.

| Field | Meaning |
|-------|---------|
| `requested` | The `source` value you passed, verbatim (`"auto"`, `"annas"`, or `"libgen"`) |
| `served_by` | Canonical names of the sources that supplied the returned books |
| `fell_back` | `true` when the source that answered is not the one the server meant to try first |
| `sources` | One entry per source, with the same fields for every source |

`fell_back` is the reason this block exists. Ask for one source, receive another, and nothing else in the response tells you the substitution happened.

Each entry in `routing.sources` has `available` (at least one route is usable now), `routes` (which of `search` and `download`), `note` (a sentence naming the constraint), and `daily_limit`. A `daily_limit` is never a bare `null`: its `state` says which of four situations you are in.

| `state` | Meaning |
|---------|---------|
| `none` | This source imposes no daily limit. Library Genesis. |
| `known` | The source reported its cap. `total`, `used`, and `remaining` carry the numbers it reported; a value it omitted stays `null` with the reason in `note`, never a derived guess |
| `unknown` | A limit exists and its value is not available here. `note` says what would answer it. |
| `not_applicable` | The source has no download route right now, so there is no quota |

**Example routing block:**

```json
{
  "requested": "annas",
  "served_by": ["libgen"],
  "fell_back": true,
  "sources": {
    "annas_archive": {
      "available": true,
      "routes": ["search"],
      "note": "no ANNAS_SECRET_KEY; search only, download unavailable",
      "daily_limit": { "state": "not_applicable", "total": null, "used": null,
                       "remaining": null, "note": "no download route without ANNAS_SECRET_KEY" }
    },
    "libgen": {
      "available": true,
      "routes": ["search", "download"],
      "note": "no account required; mirror li first",
      "daily_limit": { "state": "none", "total": null, "used": null,
                       "remaining": null, "note": "LibGen applies no daily download limit" }
    },
    "zlibrary": {
      "available": true,
      "routes": ["search", "download"],
      "note": "credentials configured",
      "daily_limit": { "state": "unknown", "total": null, "used": null,
                       "remaining": null, "note": "costs an EAPI profile call; ask get_download_limits" }
    }
  }
}
```

**Note:** `sources_used` was removed. It was computed from the results, so it could not distinguish a fallback from a direct hit. Read `routing.served_by` and `routing.fell_back` instead.

**Note:** `download_url` is empty for Library Genesis results. The server resolves a download link only when you request the file, because a Library Genesis link expires in less than 2.5 hours.

**Example Usage:**

```json
{
  "tool": "search_multi_source",
  "arguments": {
    "query": "Phenomenology of Spirit",
    "source": "auto",
    "count": 5
  }
}
```

**Error Cases:**
- Invalid or missing `query` parameter
- Both sources unavailable (network errors, rate limiting)
- Invalid `source` value (must be `"auto"`, `"annas"`, or `"libgen"`)

**Note:** `source="annas"` works without an API key. Anna's Archive needs a key for downloads only.

---

### get_recent_books

**Description:** Get recently added books to Z-Library. Optionally filter by file format.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| count | integer | No | `10` | Number of books to return |
| format | string | No | -- | Filter by file format (e.g., `"pdf"`, `"epub"`) |

**Returns:** JSON array of recently added book objects with standard metadata fields.

**Example Usage:**

```json
{
  "tool": "get_recent_books",
  "arguments": {
    "count": 20,
    "format": "epub"
  }
}
```

**Error Cases:**
- Authentication failure
- Network errors or rate limiting

---

## Metadata Tools

### get_book_metadata

**Description:** Get comprehensive metadata for a book. By default returns core fields: title, author, year, publisher, language, pages, isbn, rating, cover, categories, extension, filesize. Use the `include` parameter to add optional field groups: terms (60+ conceptual keywords), booklists (11+ curated collections), ipfs (IPFS CIDs), ratings (quality score), description (full text description).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| bookId | string | Yes | -- | Z-Library book ID |
| bookHash | string | Yes | -- | Book hash (can be extracted from book URL) |
| include | string[] | No | -- | Optional field groups to include beyond core defaults. Allowed values: `"terms"`, `"booklists"`, `"ipfs"`, `"ratings"`, `"description"` |

**Returns:** JSON object with core metadata fields, plus any requested optional field groups.

**Example Usage:**

```json
{
  "tool": "get_book_metadata",
  "arguments": {
    "bookId": "12345678",
    "bookHash": "abc123def456",
    "include": ["terms", "description", "ratings"]
  }
}
```

**Error Cases:**
- Invalid or missing `bookId` or `bookHash`
- Book not found (invalid ID/hash combination)
- Authentication failure
- Network errors or rate limiting

---

## Collection Tools

### fetch_booklist

**Description:** Fetch books from an expert-curated booklist. Z-Library books belong to 11+ booklists with up to 954 books per list. Get booklist IDs and hashes from the `get_book_metadata` tool (using `include: ["booklists"]`).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| booklistId | string | Yes | -- | Booklist ID from book metadata |
| booklistHash | string | Yes | -- | Booklist hash from book metadata |
| topic | string | Yes | -- | Booklist topic name |
| page | integer | No | `1` | Page number for pagination |

**Returns:** JSON object with the booklist title, topic, and an array of book objects in the collection.

**Example Usage:**

```json
{
  "tool": "fetch_booklist",
  "arguments": {
    "booklistId": "42",
    "booklistHash": "abc123",
    "topic": "Continental Philosophy",
    "page": 1
  }
}
```

**Error Cases:**
- Invalid or missing `booklistId`, `booklistHash`, or `topic`
- Booklist not found
- Authentication failure
- Network errors or rate limiting

---

## Download and Processing Tools

### download_book_to_file

**Description:** Download a book to a local file. Pass the full `bookDetails` object from a search result.

This tool accepts results from `search_books` (Z-Library) and from `search_multi_source` (Library Genesis or Anna's Archive). The server reads the `source` field to select the download path. A Library Genesis download needs no Z-Library account. Optionally process the document for RAG after download. When processing is enabled, the response stays additive: `processed_file_path` remains the body-text anchor and sibling bundle paths are returned when available.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| bookDetails | object | Yes | -- | The full book object from `search_books` or `search_multi_source` |
| outputDir | string | No | `"./downloads"` | Directory to save the file to |
| process_for_rag | boolean | No | -- | Whether to process the document content for RAG after download |
| processed_output_format | string | No | -- | Desired output format for RAG processing (e.g., `"text"`, `"markdown"`) |

**Returns:** JSON object with `file_path` (downloaded file path) and `provenance`. If RAG processing was requested, the response also includes `processed_file_path` (body text), `metadata_file_path`, optional `footnotes_file_path` / `endnotes_file_path` / `citations_file_path`, `content_types_produced`, `stats`, and `output_files`.

`provenance` records what actually served the file. It is a nested object, not four top-level fields, because a source's own metadata is spread into the book object and a flat `source` key would collide with it silently.

| Field | Meaning |
|-------|---------|
| `source` | Canonical name of the source that served the file |
| `route` | The named route taken: `get.php` for Library Genesis, `fast_download` for Anna's Archive, `eapi` for Z-Library |
| `mirror` | The mirror or domain the route was taken against |
| `host` | The host that actually sent the bytes, after redirects |

`mirror` and `host` differ on purpose. A Library Genesis mirror hands the transfer to a content delivery network node, and a mirror can issue a working key while its node is dead — which is why failover is driven by bytes served rather than by key resolution. A field the route has no analogue for is `null` rather than invented.

Provenance describes the transfer, never the document. No file content travels in it.

```json
"provenance": { "source": "libgen", "route": "get.php", "mirror": "vg", "host": "cdn3.booksdl.lc" }
```

**Example Usage:**

```json
{
  "tool": "download_book_to_file",
  "arguments": {
    "bookDetails": {
      "id": "12345678",
      "hash": "abc123",
      "title": "Being and Time",
      "author": "Martin Heidegger",
      "extension": "pdf"
    },
    "outputDir": "./downloads",
    "process_for_rag": true,
    "processed_output_format": "text"
  }
}
```

**Example Processed Response:**

```json
{
  "file_path": "./downloads/HeideggerMartin_BeingAndTime_12345678.pdf",
  "provenance": { "source": "zlibrary", "route": "eapi", "mirror": "z-library.sk",
                  "host": "dl.z-library.sk" },
  "processed_file_path": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.processed.markdown",
  "metadata_file_path": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.metadata.json",
  "footnotes_file_path": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.processed_footnotes.markdown",
  "content_types_produced": ["body", "footnotes"],
  "stats": {
    "word_count": 102345,
    "char_count": 654321,
    "format": "markdown"
  },
  "output_files": {
    "body": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.processed.markdown",
    "metadata": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.metadata.json",
    "footnotes": "./processed_rag_output/HeideggerMartin_BeingAndTime_12345678.pdf.processed_footnotes.markdown"
  }
}
```

**Error Cases:**
- Invalid or missing `bookDetails` object
- Download failed (book unavailable, credentials invalid)
- Output directory not writable
- Daily download limit exceeded (check with `get_download_limits`)
- RAG processing failure (unsupported format, corrupted file)

---

### process_document_for_rag

**Description:** Process a downloaded document (EPUB, TXT, PDF) into a file-based RAG bundle. The body-text output remains available at `processed_file_path`, and the tool additively reports metadata and optional sibling outputs so callers do not need to guess filenames.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| file_path | string | Yes | -- | Path to the downloaded file to process |
| output_format | string | No | -- | Desired output format (e.g., `"text"`, `"markdown"`) |

**Returns:** JSON object with `processed_file_path` (body-text output path), `metadata_file_path`, optional `footnotes_file_path` / `endnotes_file_path` / `citations_file_path`, `content_types_produced`, `stats`, and `output_files`.

**Example Usage:**

```json
{
  "tool": "process_document_for_rag",
  "arguments": {
    "file_path": "./downloads/Being_and_Time_Heidegger.pdf",
    "output_format": "text"
  }
}
```

**Example Response:**

```json
{
  "processed_file_path": "./processed_rag_output/test-book.pdf.processed.markdown",
  "metadata_file_path": "./processed_rag_output/test-book.pdf.metadata.json",
  "footnotes_file_path": "./processed_rag_output/test-book.pdf.processed_footnotes.markdown",
  "content_types_produced": ["body", "footnotes"],
  "stats": {
    "word_count": 12345,
    "char_count": 67890,
    "format": "markdown"
  },
  "output_files": {
    "body": "./processed_rag_output/test-book.pdf.processed.markdown",
    "metadata": "./processed_rag_output/test-book.pdf.metadata.json",
    "footnotes": "./processed_rag_output/test-book.pdf.processed_footnotes.markdown"
  }
}
```

**Error Cases:**
- Invalid or missing `file_path`
- File not found at specified path
- Unsupported file format (only EPUB, TXT, and PDF are supported)
- Corrupted or encrypted file
- Insufficient disk space for output

---

## Utility Tools

### get_download_limits

**Description:** Report each source's daily download limit. Not Z-Library only: the tool's name never named a source, and answering it with one source's numbers privileged that source in a surface that must not.

Z-Library is the only source whose answer costs a network round-trip, because it is read from the authenticated account profile. Library Genesis and Anna's Archive are answered from configuration. Pass `sources` to ask about those without paying for the Z-Library call.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| sources | array | No | all sources | Which sources to report: `"annas_archive"`, `"libgen"`, `"zlibrary"`. Omit for all three. An empty array is rejected rather than treated as all three. |

**Returns:** JSON object with `requested` (the sources reported, in canonical order) and `sources` (one entry per source).

Each entry has the same fields as an entry in `search_multi_source`'s `routing.sources` — `available`, `routes`, `note`, `daily_limit` — plus `details` for anything a single source reports that has no counterpart elsewhere. `daily_limit.state` distinguishes *no limit exists* from *a limit exists and is not known here* from *a concrete number*; see [search_multi_source](#search_multi_source) for the table.

| Source | What it reports |
|--------|-----------------|
| Library Genesis | `state: "none"` — known to have no daily limit, not merely unknown |
| Z-Library | `state: "known"` with `total` / `used` / `remaining` from the account profile. Without credentials, `state: "not_applicable"` and `available: false` |
| Anna's Archive | With `ANNAS_SECRET_KEY`, `state: "unknown"` — Anna's reports the keyed quota only in a fast-download response, so it cannot be read without spending a download. Without a key, `state: "not_applicable"` and search-only routes |

A Z-Library failure — missing credentials, a refused login, a profile call that times out — degrades that one entry to `state: "unknown"` with the reason in `note`. The other sources still answer, because a credential-free Library Genesis setup is supported.

**Example Usage:**

```json
{
  "tool": "get_download_limits",
  "arguments": { "sources": ["libgen"] }
}
```

**Example Response:**

```json
{
  "requested": ["libgen", "zlibrary"],
  "sources": {
    "libgen": {
      "available": true,
      "routes": ["search", "download"],
      "note": "no account required; mirror li first",
      "daily_limit": { "state": "none", "total": null, "used": null,
                       "remaining": null, "note": "LibGen applies no daily download limit" },
      "details": {}
    },
    "zlibrary": {
      "available": true,
      "routes": ["search", "download"],
      "note": "credentials configured",
      "daily_limit": { "state": "known", "total": 10, "used": 3, "remaining": 7, "note": "" },
      "details": { "is_premium": false }
    }
  }
}
```

**Error Cases:**
- `sources` names something the server does not read from
- `sources` is an empty array

A Z-Library authentication or network failure is not an error case here. It appears as that source's `daily_limit.state: "unknown"` with the reason attached.

---

### get_download_history

**Description:** Get the user's Z-Library download history. Returns a list of previously downloaded books with their metadata.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| count | integer | No | `10` | Number of results to return |

**Returns:** JSON array of previously downloaded book objects with metadata.

**Example Usage:**

```json
{
  "tool": "get_download_history",
  "arguments": {
    "count": 20
  }
}
```

**Error Cases:**
- Authentication failure
- Network errors or rate limiting
