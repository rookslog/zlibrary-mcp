# Architecture Overview

<!-- Last Verified: 2026-07-24 -->

**Last Updated**: 2026-07-24
**Status**: v1.2.1 current (19 development phases complete through v1.2.1; v1.3.0 release imminent)

---

## System Architecture

### High-Level Components

```
+-----------------------------------------------------------+
|                    MCP Client (Claude)                     |
+-----------------------------+-----------------------------+
                              | MCP Protocol (SDK 1.25+, stdio)
+-----------------------------v-----------------------------+
|              MCP Server (Node.js/TypeScript)               |
|  - 13 tools via McpServer server.tool() API (src/index.ts) |
|  - Retry + circuit breaker around every bridge call        |
|    (src/lib/retry-manager.ts, src/lib/circuit-breaker.ts)  |
|  - stderr-only logging (src/lib/logger.ts)                 |
+-----------------------------+-----------------------------+
                              | PythonShell (per-call spawn)
+-----------------------------v-----------------------------+
|              Python Bridge (lib/)                          |
|  - python_bridge.py: dispatch, auth, EAPI init             |
|  - Tool modules (author_tools, term_tools, booklist_tools) |
|  - Document processing (lib/rag/ domain modules)           |
|  - Multi-source router (lib/sources/)                      |
+--------------+-----------------------------+--------------+
               |                             |
   EAPI JSON + httpx              HTML scrape / JSON (httpx)
+--------------v--------------+ +------------v--------------+
|  Z-Library EAPI Endpoints   | |  lib/sources/ adapters     |
|  /eapi/user/login           | |  Anna's Archive (primary)  |
|  /eapi/book/search          | |  LibGen (fallback)         |
|  /eapi/info/domains (hydra) | |  via SourceRouter          |
+-----------------------------+ +---------------------------+
```

### Data Flow Patterns

**Search**: Client -> MCP -> Python -> EAPI JSON endpoint -> normalized results
**Multi-source search**: Client -> MCP -> Python -> `SourceRouter` -> Anna's Archive (primary) with LibGen fallback -> `UnifiedBookResult` list
**Download**: Client -> MCP -> Python -> EAPIClient (URL resolution + file download) -> local file path
**RAG**: File -> `lib/rag/` pipeline -> quality analysis -> structured output bundle (body text + `.metadata.json` sidecar + optional siblings) -> file paths

**Critical designs**:
- RAG returns **file paths**, never raw text (prevents context overflow)
- stdout carries JSON-RPC and nothing else; all diagnostics go to stderr via `src/lib/logger.ts`, enforced by `__tests__/stdio-purity.test.js`
- Hydra-mode domain discovery: `discover_eapi_domain()` (`zlibrary/src/zlibrary/util.py`) resolves working domains at runtime from `/eapi/info/domains`

---

## Current Implementation Status

### MCP Tools (13 total)

- search_books, full_text_search, get_download_history, get_download_limits
- get_recent_books, download_book_to_file, process_document_for_rag
- get_book_metadata, search_by_term, search_by_author, fetch_booklist
- search_advanced, search_multi_source

All registered via `server.tool()` (McpServer API, MCP SDK 1.25+). The README tool
table is CI-enforced against `src/index.ts` by `scripts/validate-readme-tools.sh` —
if the sets diverge, the build fails.

### Structured RAG Output Bundle (v1.2.1)

`process_document_for_rag` and `download_book_to_file` (with `process_for_rag`)
return a path-first bundle rather than a single output path:

| Field | Meaning |
|-------|---------|
| `processed_file_path` | Body-text output (backward-compatible anchor) |
| `metadata_file_path` | Canonical `.metadata.json` sidecar with relative links to sibling files |
| `footnotes_file_path` / `endnotes_file_path` / `citations_file_path` | Optional sibling outputs when the pipeline separates those content types |
| `content_types_produced` | e.g. `["body", "footnotes"]` |
| `stats` | word/char counts, output format |
| `output_files` | Map of content type -> path (callers never guess filenames) |

The contract is additive: pre-bundle consumers reading only `processed_file_path`
keep working. See `docs/api.md` for the full response shapes.

### Multi-Source Layer (lib/sources/)

| Module | Responsibility |
|--------|---------------|
| `base.py` | `SourceAdapter` ABC: `search()`, `get_download_url(md5)`, `close()` |
| `models.py` | `UnifiedBookResult`, `DownloadResult` (shared result shapes) |
| `config.py` | `SourceConfig` from env (`ANNAS_SECRET_KEY`, `ANNAS_BASE_URL`, `LIBGEN_MIRROR`, `BOOK_SOURCE_DEFAULT`, `BOOK_SOURCE_FALLBACK_ENABLED`, the `ANNAS_BROWSER_*` family) |
| `annas.py` | Anna's Archive adapter — HTML scraping (BeautifulSoup); DOM-fragile surface |
| `libgen.py` | LibGen adapter (fallback source) |
| `router.py` | `SourceRouter`: Anna's primary when key configured, LibGen fallback on error/quota exhaustion; source selection `auto`/`annas`/`libgen` |

Exposed through the `search_multi_source` tool. Z-Library itself does **not** yet
route through `SourceRouter` — promoting it to a `SourceAdapter` is planned work.

### RAG Pipeline

- Stage 1: Statistical garbled-text detection
- Stage 2: Visual X-mark/strikethrough detection (independent of Stage 1, ADR-008)
- Stage 3: OCR recovery framework (corruption detection, recovery, spacing)
- Formatting preservation: bold, italic, strikethrough
- Content separation: footnotes, front matter, ToC, page numbers, margins routed to
  distinct output streams (`lib/rag/pipeline/` compositor -> writer)
- Next: quality scoring harness + CI quality reporting (Phases 20-21; see
  `claudedocs/architecture/phase-20-21-review-2026-07-24.md`)

### Testing Posture

- Node.js (Jest, ESM with `jest.unstable_mockModule`); Python (Pytest with real-PDF validation)
- Integration tests run in recorded mode (fixtures) by default; `TEST_LIVE=true` exercises the real bridge
- Unit suites mock all third-party calls, so upstream drift is caught by the scheduled
  Upstream Contract Check workflow (`.github/workflows/upstream-check.yml`) and `npm run doctor`, not by the unit suite

---

## Key Design Decisions (ADRs)

| ADR | Decision | Status |
|-----|----------|--------|
| ADR-001 | Jest ESM migration | Implemented |
| ADR-002 | Download via bookDetails page scraping | Superseded by EAPI |
| ADR-003 | Deprecate get_book_by_id | Implemented |
| ADR-004 | Python scripts in lib/, path resolution from dist/ | Implemented |
| ADR-005 | EAPI migration (bypass Cloudflare) | Implemented |
| ADR-006 | Quality pipeline: Statistical -> Visual -> OCR | Implemented (Stages 1-3) |
| ADR-007 | Phase 2 integration complete | Implemented |
| ADR-008 | Stage 2 independence (X-mark not conditional on garbled) | Implemented |
| ADR-009 | Python monolith decomposition into lib/rag/ | Implemented |
| ADR-010 | MCP SDK upgrade to 1.25+ (McpServer API) | Implemented |

Full index: [docs/adr/README.md](../docs/adr/README.md)

---

## Module Structure

### Node Server Modules (src/lib/)

| Module | Responsibility |
|--------|---------------|
| `zlibrary-api.ts` | Service layer: wraps every Python call in retry + circuit breaker, parses bridge responses |
| `python-bridge.ts` | Low-level PythonShell invocation of `lib/python_bridge.py` |
| `venv-manager.ts` | UV-managed `.venv/` discovery and Python path resolution |
| `retry-manager.ts` | `withRetry()` — exponential backoff, retryable-error classification |
| `circuit-breaker.ts` | `CircuitBreaker` — failure threshold + reset timeout (env-configurable) |
| `logger.ts` | stderr-only leveled logging (`LOG_LEVEL`); keeps stdout pure for JSON-RPC |
| `errors.ts` | `ZLibraryError` and typed error hierarchy with context enrichment |
| `paths.ts` | Path helpers (`getPythonScriptPath`, `getPythonLibDirectory`) per ADR-004 |

### Python Bridge Modules (lib/)

| Module | Responsibility |
|--------|---------------|
| `python_bridge.py` | Main bridge: function dispatch, EAPI init, auth, search, download |
| `author_tools.py` / `term_tools.py` / `booklist_tools.py` | Author / term / booklist operations via EAPI (booklists degrade gracefully) |
| `enhanced_metadata.py` | Book metadata extraction via EAPI |
| `rag_processing.py` | Legacy facade (delegates to `lib/rag/`) |
| `rag_data_models.py` | TextSpan, PageRegion data structures |
| `garbled_text_detection.py` | Stage 1 statistical analysis |
| `strikethrough_detection.py` | Stage 2 X-mark detection |
| `footnote_continuation.py` / `footnote_corruption_model.py` | Multi-page footnote tracking and corruption modeling |
| `formatting_group_merger.py` | Span grouping for markdown |
| `marginalia_extraction.py` / `note_classification.py` | Margin content and note-type classification |
| `filename_utils.py` / `metadata_generator.py` / `metadata_verification.py` | Bundle filenames, `.metadata.json` generation and verification |
| `quality_verification.py` | Quality checks and reporting |

### RAG Domain Modules (lib/rag/)

| Package | Responsibility |
|---------|---------------|
| `__init__.py` | Facade (backward compat with `rag_processing.py`) |
| `orchestrator.py` / `orchestrator_pdf.py` | Main and PDF-specific orchestration |
| `processors/` | Format extractors: epub, pdf, txt |
| `detection/` | Footnotes, front matter, headings, ToC, page numbers, margins, registry |
| `pipeline/` | Block classification -> content streams: compositor, runner, writer, models |
| `quality/` | Quality analysis, pipeline, OCR stage |
| `ocr/` | Corruption detection, recovery, spacing |
| `resolution/` | Ambiguity resolution: analyzer, renderer, models |
| `xmark/` | X-mark/strikethrough detection |
| `utils/` | Cache, constants, deps, exceptions, header, text utils |

Modules kept under 500 lines; facade pattern preserves the pre-decomposition API.

### Vendored Fork (zlibrary/src/zlibrary/)

| Component | Purpose |
|-----------|---------|
| `eapi.py` | `EAPIClient`: httpx client for `/eapi/` JSON endpoints; response normalization |
| `util.py` | `discover_eapi_domain()` — hydra-mode runtime domain discovery |
| `libasync.py`, `profile.py`, `booklists.py` | Async client, profile, and booklist support |

Zero HTML scraping remains in the fork — all Z-Library access is EAPI JSON. The
residual DOM-fragile surfaces in the codebase are `lib/sources/annas.py` and EPUB
internals.

---

## Technology Stack

**Runtime**:
- Node.js 18+ (MCP server), Python 3.10+ (bridge and processing)
- UV (Python dependency management, `uv.lock`, project-local `.venv/`)

**Key Dependencies**:
- `@modelcontextprotocol/sdk` ^1.25 — MCP protocol (McpServer API)
- `python-shell` — Node.js to Python bridge
- `httpx` — EAPI and source-adapter HTTP client
- `ebooklib` (EPUB), `PyMuPDF`/fitz (PDF), `beautifulsoup4` (Anna's Archive adapter)
- `opencv-python` (visual X-mark detection), `pytesseract` (OCR, optional)

**Development**:
- TypeScript, ESLint + Prettier, Jest (ESM); Pytest + pytest-mock
- lint-staged pre-commit hooks; coverage thresholds enforced in CI

---

## Directory Structure

```
zlibrary-mcp/
+-- src/                          # Node.js MCP server
|   +-- index.ts                  # Entry point (13 tools via server.tool())
|   +-- lib/                      # Server utilities
|       +-- zlibrary-api.ts       # Service layer (retry + circuit breaker)
|       +-- python-bridge.ts      # PythonShell invocation
|       +-- venv-manager.ts       # UV-based venv management
|       +-- retry-manager.ts      # Retry with exponential backoff
|       +-- circuit-breaker.ts    # Circuit breaker pattern
|       +-- logger.ts             # stderr-only logging (stdout purity)
|       +-- errors.ts             # Typed error hierarchy
|       +-- paths.ts              # Path resolution helpers (ADR-004)
|
+-- lib/                          # Python source
|   +-- python_bridge.py          # Main bridge (dispatch, EAPI init, auth)
|   +-- sources/                  # Multi-source layer
|   |   +-- base.py               # SourceAdapter ABC
|   |   +-- router.py             # SourceRouter (Anna's primary, LibGen fallback)
|   |   +-- annas.py              # Anna's Archive adapter (HTML)
|   |   +-- libgen.py             # LibGen adapter
|   |   +-- config.py / models.py # Env config, unified result models
|   +-- rag/                      # Decomposed RAG pipeline
|   |   +-- orchestrator.py       # Main orchestration
|   |   +-- orchestrator_pdf.py   # PDF orchestration
|   |   +-- processors/           # Format-specific extractors
|   |   +-- detection/            # Footnotes, headings, ToC, margins, ...
|   |   +-- pipeline/             # Compositor -> writer content streams
|   |   +-- quality/  ocr/        # Quality analysis, OCR recovery
|   |   +-- resolution/  xmark/   # Ambiguity resolution, X-mark detection
|   |   +-- utils/                # Shared utilities
|   +-- *.py                      # Tool modules + RAG support modules
|
+-- zlibrary/                     # Vendored fork (EAPI JSON client)
|   +-- src/zlibrary/eapi.py      # EAPIClient
|   +-- src/zlibrary/util.py      # Hydra-mode domain discovery
|
+-- __tests__/                    # Jest (ESM) + integration; python/ for Pytest
+-- scripts/                      # CI helpers (validate-readme-tools.sh, ...)
+-- docs/adr/                     # ADR-001 through ADR-010
+-- docs/api.md                   # Tool-by-tool API reference (incl. bundle contract)
+-- .claude/                      # Development guides
```

---

## Integration Points

### Z-Library EAPI
- JSON endpoints under `/eapi/` (`/eapi/user/login`, `/eapi/book/search`, `/eapi/info/domains`)
- httpx client with lazy initialization and cookie-based auth
- Responses normalized to internal Book format
- Cloudflare bypass: API endpoints not subject to browser challenges

### Alternative Sources
- Anna's Archive: HTML-scraped; fast downloads gated on `ANNAS_SECRET_KEY`; quota-aware
- Anna's Archive, key-free: `annas_browser.py` drives a headful browser on the operator's
  machine to *resolve* download links, then hands the URL back for the ordinary httpx
  transfer. The browser never sees the file bytes, so content-md5 verification, throughput
  bounding and atomic staging stay in one place. Serialised and rate-limited in the same
  module, because the route's scope is conditional on the limits (#143, #144)
- LibGen: mirror-configurable fallback
- Both monitored daily by the Upstream Contract Check workflow

### MCP Protocol
- McpServer API with `server.tool()` registration, JSON-RPC 2.0, stdio transport
- stdout purity is a hard constraint (`__tests__/stdio-purity.test.js`)

---

## Security Architecture

- **Credentials**: environment variables only (`ZLIBRARY_EMAIL`, `ZLIBRARY_PASSWORD`, `ANNAS_SECRET_KEY`); never committed
- **Isolation**: Python bridge runs in UV-managed `.venv/`; download directory configurable
- **Download safety**: `Content-Disposition` filenames are reduced to a bare basename (posixpath + ntpath) before joining onto the output directory (path-traversal defense)
- **Resilience**: circuit breaker + retry on all bridge calls; graceful degradation for booklists/terms/IPFS
- **Disclosure**: see `SECURITY.md` for reporting policy and the `LOG_LEVEL=debug` caveat

---

## Quick Reference

- **Health assessment / roadmap**: [claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md](../claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md)
- **Project context**: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — mission and domain model
- **Code patterns**: [PATTERNS.md](PATTERNS.md)
- **RAG feature workflow**: [TDD_WORKFLOW.md](TDD_WORKFLOW.md), [RAG_QUALITY_FRAMEWORK.md](RAG_QUALITY_FRAMEWORK.md)
- **ADRs**: [docs/adr/README.md](../docs/adr/README.md)
- **Contributing**: [CONTRIBUTING.md](../CONTRIBUTING.md)
