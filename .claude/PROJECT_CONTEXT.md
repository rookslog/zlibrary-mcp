# Z-Library MCP Project Context

<!-- Last Verified: 2026-07-24 -->

## Mission Statement
Build a robust, resilient MCP server for Z-Library integration that provides comprehensive book search, download, and RAG processing capabilities for AI assistants, with emphasis on reliability despite Z-Library's infrastructure changes.

## Core Architecture Principles

### 1. Resilience First
- **Domain Agility**: Handle Z-Library's "Hydra mode" with dynamic domain discovery (`discover_eapi_domain()` in `zlibrary/src/zlibrary/util.py` resolves domains at runtime from `/eapi/info/domains`)
- **EAPI Transport**: JSON API endpoints bypass Cloudflare browser challenges — zero HTML scraping remains in the vendored `zlibrary/` fork
- **Node-Side Resilience**: Every Python bridge call is wrapped in retry with exponential backoff (`src/lib/retry-manager.ts`) and a circuit breaker (`src/lib/circuit-breaker.ts`) inside `src/lib/zlibrary-api.ts`
- **Source Diversification**: `lib/sources/` provides a `SourceAdapter` abstraction with Anna's Archive (primary) and LibGen (fallback) adapters behind a `SourceRouter`, reducing single-source risk
- **Graceful Degradation**: Continue operating despite partial failures (e.g., booklist tools degrade when EAPI lacks endpoints)
- **Drift Detection, Not Assumption**: unit suites mock all third-party calls, so the scheduled Upstream Contract Check workflow and `npm run doctor` exist to catch real-world breakage

### 2. Abstraction Layers
```
+---------------------------+
|   MCP Interface           | <- 13 tools exposed to AI assistants (MCP SDK 1.25+, server.tool())
+---------------------------+
|   Service Layer           | <- src/lib/zlibrary-api.ts: orchestration, retry + circuit breaker
+---------------------------+
|   Python Bridge           | <- Language boundary (PythonShell, per-call spawn)
+---------------------------+
|   Backends                | <- EAPIClient (zlibrary/ fork, httpx JSON)
|                           |    SourceRouter -> Anna's Archive / LibGen adapters (lib/sources/)
+---------------------------+
|   RAG Pipeline            | <- lib/rag/ domain modules; file-based structured output bundle
+---------------------------+
```

### 3. Development Philosophy
- **Test-Driven**: Write tests first; RAG features require real-PDF ground truth (`.claude/TDD_WORKFLOW.md`)
- **Observable**: Diagnostics go to **stderr only** via `src/lib/logger.ts` — stdout is the JSON-RPC protocol channel, enforced by `__tests__/stdio-purity.test.js`
- **Maintainable**: Clear separation of concerns, modules under 500 lines
- **Quality-Verified**: RAG output validated per `.claude/RAG_QUALITY_FRAMEWORK.md`

## Current State (v1.2.1, verified 2026-07-24)

### Working Features
- **13 MCP tools** via McpServer `server.tool()` API (MCP SDK 1.25+); the README tool table is kept in sync by CI (`scripts/validate-readme-tools.sh`)
- EAPI JSON transport for search, metadata, browse, and download operations
- Multi-source search (`search_multi_source`) across Anna's Archive and LibGen with automatic fallback
- RAG processing (EPUB, TXT, PDF) with quality detection pipeline and OCR recovery framework
- **Structured RAG output bundle** (v1.2.1): processing returns `processed_file_path` plus a canonical `.metadata.json` sidecar, optional sibling outputs (footnotes/endnotes/citations), and an `output_files` map — additive over the original single-path contract
- UV-based Python dependency management (project-local `.venv/`, `uv.lock`)
- Python monolith decomposed into `lib/rag/` domain modules with facade pattern
- Health check with Cloudflare detection; scheduled upstream contract monitoring

### Known Limitations
- Booklist tools gracefully degrade (no EAPI booklist endpoint)
- Full-text search routes through regular EAPI search (no dedicated mode)
- Terms and IPFS CIDs return empty defaults (not available via EAPI)
- Anna's Archive adapter (`lib/sources/annas.py`) and EPUB internals are the residual DOM-fragile (HTML-parsed) surfaces
- Z-Library itself is not yet a `SourceAdapter` — its tools bypass `SourceRouter`

### Future Direction
- RAG quality scoring harness and CI quality reporting (Phases 20-21; see `claudedocs/architecture/phase-20-21-review-2026-07-24.md`)
- Promote Z-Library to a `SourceAdapter` so all tools route through `SourceRouter`
- Windows support and publish-pipeline fixes land in the next release (v1.3.0)
- Live health assessment and roadmap: `claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md`

## Domain Model

### Core Entities (TypeScript layer)
```typescript
interface Book {
  id: string;
  title: string;
  author: string;
  year: number;
  language: string;
  extension: string;
  size: number;
  hash: string;
  bookDetails?: BookDetails; // Required for download
}

interface SearchParams {
  query: string;
  yearFrom?: number;
  yearTo?: number;
  languages?: Language[];
  extensions?: Extension[];
  limit?: number;
  page?: number;
}
```

### Multi-Source Entities (Python, lib/sources/models.py)
- `UnifiedBookResult` — normalized search result shape shared by all source adapters
- `DownloadResult` — download URL plus optional quota info (Anna's Archive fast-download quota)

### RAG Output Bundle (Python, returned through the bridge)
```json
{
  "processed_file_path": ".../book.pdf.processed.markdown",
  "metadata_file_path": ".../book.pdf.metadata.json",
  "footnotes_file_path": ".../book.pdf.processed_footnotes.markdown",
  "content_types_produced": ["body", "footnotes"],
  "stats": { "word_count": 0, "char_count": 0, "format": "markdown" },
  "output_files": { "body": "...", "metadata": "...", "footnotes": "..." }
}
```
Only file paths cross the protocol boundary — never raw document text (prevents AI context overflow).

## Technical Decisions

### Why Python Bridge?
- Z-Library community libraries are Python-based
- Better document processing libraries (PyMuPDF, ebooklib, OpenCV, pytesseract)
- Async support with asyncio; EAPI client uses httpx
- Spawned per-call via PythonShell from `src/lib/zlibrary-api.ts` (stateless, crash-isolated)

### Why Node.js Frontend?
- MCP SDK is Node.js-based (McpServer API, SDK 1.25+)
- Better TypeScript support; standard for MCP servers

### Why Vendored Z-Library Fork?
- Custom modifications for download logic and authentication flow
- Avoid breaking changes from upstream
- Custom EAPI client implementation (`zlibrary/src/zlibrary/eapi.py`)

### Why EAPI Transport?
- Cloudflare browser challenges block all HTML page requests (since Jan 2026)
- EAPI JSON endpoints bypass Cloudflare (API endpoints not challenged)
- Structured JSON responses eliminate HTML parsing fragility
- See ADR-005 for full rationale

### Why Source Adapters?
- Z-Library is a single point of failure; Anna's Archive and LibGen provide alternatives
- `SourceAdapter` ABC (`lib/sources/base.py`) fixes the contract: `search`, `get_download_url`, `close`
- `SourceRouter` handles primary/fallback selection and quota exhaustion (`ANNAS_SECRET_KEY` gates Anna's Archive)

### Why Python Decomposition?
- `rag_processing.py` was ~5,000 lines (unmaintainable)
- Decomposed into `lib/rag/` with domain modules under 500 lines
- Facade pattern preserves backward compatibility
- See ADR-009 for full rationale

## Integration Points

### Upstream Dependencies
- `sertraline/zlibrary` — base Python library (vendored fork with EAPI client)
- `@modelcontextprotocol/sdk` ^1.25 — MCP protocol (McpServer API)
- `python-shell` — Node.js to Python bridge
- Anna's Archive and LibGen — HTML-scraped alternative sources (monitored by the upstream-check workflow)

### Downstream Consumers
- Claude Code (primary), RooCode, Cline, other MCP-compatible AI assistants

## Development Workflow

### Standard Flow
1. **Planning**: Review `ISSUES.md` and the current health assessment (`claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md`)
2. **Implementation**: Follow patterns in `.claude/PATTERNS.md`
3. **RAG features**: Mandatory real-PDF TDD per `.claude/TDD_WORKFLOW.md`
4. **Testing**: Unit (Jest/Pytest) -> Integration -> live/E2E where credentialed
5. **Contribution process**: See `CONTRIBUTING.md`

### Branch Strategy
- `master` — stable, production-ready (primary branch)
- `feature/*`, `fix/*`, `hotfix/*`, `docs/*` — short-lived work branches

### Commit Convention
```
<type>(<scope>): <subject>
```
Types: feat, fix, docs, style, refactor, test, chore

## Environment Variables

```bash
# Required
ZLIBRARY_EMAIL=
ZLIBRARY_PASSWORD=

# Optional (Z-Library)
ZLIBRARY_MIRROR=
LOG_LEVEL=silent|error|warn|info|debug   # stderr logging via src/lib/logger.ts

# Optional (multi-source, lib/sources/config.py)
ANNAS_SECRET_KEY=                # Enables Anna's Archive fast downloads (primary source)
ANNAS_BASE_URL=                  # Default: https://annas-archive.li
LIBGEN_MIRROR=                   # Default: li
LIBGEN_USER_AGENT=               # Default: a desktop Firefox string. LibGen blocklists
                                 # tool UAs and answers with nginx's ~640-byte default
                                 # page at HTTP 200 — search and ads.php alike. Override
                                 # when the blocklist widens again (#141).
BOOK_SOURCE_DEFAULT=auto         # auto | annas | libgen
BOOK_SOURCE_FALLBACK_ENABLED=true

# Retry / circuit breaker (src/lib/retry-manager.ts, circuit-breaker.ts)
RETRY_MAX_RETRIES=3
RETRY_INITIAL_DELAY=1000
RETRY_MAX_DELAY=30000
RETRY_FACTOR=2
CIRCUIT_BREAKER_THRESHOLD=5
CIRCUIT_BREAKER_TIMEOUT=60000
```

## Quick Commands

```bash
# Setup
bash setup-uv.sh         # UV-based Python setup (or: uv sync)
npm install              # Node.js deps
npm run build            # Build TypeScript (validates Python file paths)

# Testing
npm test                 # All tests (Jest + Pytest)
uv run pytest            # Python tests only

# Running
node dist/index.js       # Start MCP server

# Diagnostics
npm run doctor           # Probe upstream sources (distinguish outage from bug)
LOG_LEVEL=debug node dist/index.js  # Verbose stderr logging
```

---

*This document is the source of truth for project context. Update when making architectural decisions.*
