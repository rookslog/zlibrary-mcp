# Architecture Overview

**Status**: Acquisition-only 2.0 interface; text processing is parked.
See [interface migration](../docs/interface-migration.md) for the breaking changes.

---

## System Architecture

### High-Level Components

```
+-----------------------------------------------------------+
|                    MCP Client (Claude)                     |
+-----------------------------+-----------------------------+
                              | MCP Protocol (SDK 1.32.1, stdio)
+-----------------------------v-----------------------------+
|              MCP Server (Node.js/TypeScript)               |
|  - Five tools via lower-level SDK Server (src/index.ts)    |
|  - Read retries + dependency breakers                     |
|  - No automatic download retries                          |
|    (src/lib/retry-manager.ts, src/lib/circuit-breaker.ts)  |
|  - stderr-only logging (src/lib/logger.ts)                 |
+-----------------------------+-----------------------------+
                              | PythonShell (per-call spawn)
+-----------------------------v-----------------------------+
|              Python Bridge (lib/)                          |
|  - python_bridge.py: dispatch, auth, EAPI init             |
|  - Tool modules (author_tools, term_tools)                 |
|  - Multi-source router (lib/sources/)                      |
+--------------+-----------------------------+--------------+
               |                             |
   EAPI JSON + httpx              HTML scrape / JSON (httpx)
+--------------v--------------+ +------------v--------------+
|  Z-Library EAPI Endpoints   | |  lib/sources/ adapters     |
|  /eapi/user/login           | |  Anna's Archive adapter    |
|  /eapi/book/search          | |  LibGen adapter            |
|  /eapi/info/domains (hydra) | |  via SourceRouter          |
+-----------------------------+ +---------------------------+
```

### Data Flow Patterns

**Search**: Client -> MCP -> Python -> EAPI JSON endpoint -> normalized results
**Ordered search**: Client -> MCP source order -> Python EAPI or explicit-source `SourceRouter` call -> compact rows and refs
**Download**: Client ref -> MCP -> guarded acquisition receipt/lease check -> EAPI or source transfer when needed -> local file and record paths

**Critical designs**:
- stdout carries JSON-RPC and nothing else; all diagnostics go to stderr via `src/lib/logger.ts`, enforced by `__tests__/stdio-purity.test.js`
- Hydra-mode domain discovery: `discover_eapi_domain()` (`zlibrary/src/zlibrary/util.py`) resolves working domains at runtime from `/eapi/info/domains`

---

## Current Implementation Status

### MCP Tools (5 total)

- `search`, `get_book`, `download`, `get_limits`, `get_history`

The lower-level SDK `Server` dispatches `tools/list` and `tools/call` through
`AcquisitionTools`. The local `registerTool()` helper selects tool definitions;
it is not McpServer registration. The README tool
table is CI-enforced against `src/index.ts` by `scripts/validate-readme-tools.sh` —
if the sets diverge, the build fails.

The old metadata/navigation names below are removed from the public surface;
this retained 1.x list is not the current tool list:

- get_book_metadata, search_by_term, search_by_author, fetch_booklist

### Multi-Source Layer (lib/sources/)

| Module | Responsibility |
|--------|---------------|
| `base.py` | `SourceAdapter` ABC: `search()`, `get_download_url(md5)`, `close()` — each takes the walk's shared `deadline` |
| `models.py` | `UnifiedBookResult`, `DownloadResult` (shared result shapes) |
| `config.py` | `DEFAULT_WALK_BUDGET`, `worst_case_search_seconds()`, `worst_case_download_seconds()`, and `SourceConfig` from env (`ANNAS_SECRET_KEY`, `ANNAS_BASE_URL`, `LIBGEN_MIRROR`, `LIBGEN_USER_AGENT`, `BOOK_SOURCE_DEFAULT`, `BOOK_SOURCE_FALLBACK_ENABLED`) |
| `annas.py` | Anna's adapter — shared HTML search parser, keyed download resolver, opt-in browser URL candidates |
| `annas_browser.py`, `annas_usage.py` | Optional visible Chrome page resolution, bounded cleanup, profile locking and shared durable visit limits; file bytes stay on HTTP |
| `libgen.py` | LibGen adapter (fallback source) |
| `router.py` | `SourceRouter`: Anna's primary when key configured, LibGen fallback on error/quota exhaustion; source selection `auto`/`annas`/`libgen`. Creates one `WalkDeadline` per walk and every provider and mirror spends it |

The public `search` tool walks explicit ordered sources; for LibGen/Anna's it
calls the internal `search_multi_source` bridge with one source per attempt.
Z-Library itself does **not** yet
route through `SourceRouter` — promoting it to a `SourceAdapter` is planned work.

Without a key, internal `auto` downloads remain LibGen-first and add Anna's only
when `ANNAS_BROWSER_ENABLED` is on. Anna's uses the browser only without a key or
after an explicit trusted-endpoint credential rejection; keyed success and quota
are unchanged. Slow partners yield in site order, with browser cleanup before
each URL is handed to the existing HTTP/MD5/resume/receipt path. No browser
cookies or storage enter that transfer. Browser results use the existing
`mirror_download` receipt route; keyed results retain `fast_download`.
For browser receipts the existing internal `quota_estimate` bridge field is
explicitly null, including receipt reuse. The public facade consumes that marker,
clears any cached membership estimate and omits the field from its public reply.
Keyed responses retain their existing quota/decrement behavior.

### Testing Posture

- Node.js (Jest, ESM with `jest.unstable_mockModule`); Python (Pytest with fake-provider acquisition validation)
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
| ADR-006 | Quality pipeline: Statistical -> Visual -> OCR | Parked; historical decision |
| ADR-007 | Phase 2 integration complete | Parked; historical decision |
| ADR-008 | Stage 2 independence (X-mark not conditional on garbled) | Parked; historical decision |
| ADR-009 | Python processing decomposition | Parked; historical decision |
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
| `author_tools.py` / `term_tools.py` | Author / term query helpers via EAPI |
| `enhanced_metadata.py` | Book metadata extraction via EAPI |
| `filename_utils.py` | Stable acquisition filenames and publication helpers |

### Vendored Fork (zlibrary/src/zlibrary/)

| Component | Purpose |
|-----------|---------|
| `eapi.py` | `EAPIClient`: httpx client for `/eapi/` JSON endpoints; response normalization |
| `util.py` | `discover_eapi_domain()` — hydra-mode runtime domain discovery |
| `libasync.py`, `profile.py`, `booklists.py` | Async client, profile, and booklist support |

Zero HTML scraping remains in the fork — all Z-Library access is EAPI JSON. The
residual DOM-fragile surfaces in the codebase are `lib/sources/annas.py`.

---

## Technology Stack

**Runtime**:
- Node.js 22+ (MCP server), Python 3.10+ (acquisition bridge)
- UV (Python dependency management, `uv.lock`, project-local `.venv/`)

**Key Dependencies**:
- `@modelcontextprotocol/sdk` — MCP protocol (locked to 1.32.1; SDK Server API)
- `python-shell` — Node.js to Python bridge
- `httpx` — EAPI and source-adapter HTTP client
- `beautifulsoup4` — Anna's Archive HTML parsing

**Development**:
- TypeScript, ESLint + Prettier, Jest (ESM); Pytest + pytest-mock
- lint-staged pre-commit hooks; coverage thresholds enforced in CI

---

## Directory Structure

```
zlibrary-mcp/
+-- src/                          # Node.js MCP server
|   +-- index.ts                  # Entry point (five tools via SDK Server)
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
|
+-- zlibrary/                     # Vendored fork (EAPI JSON client)
|   +-- src/zlibrary/eapi.py      # EAPIClient
|   +-- src/zlibrary/util.py      # Hydra-mode domain discovery
|
+-- __tests__/                    # Jest (ESM) + integration; python/ for Pytest
+-- scripts/                      # CI helpers (validate-readme-tools.sh, ...)
+-- docs/adr/                     # ADR-001 through ADR-012
+-- docs/api.md                   # Tool-by-tool API reference (acquisition tools)
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
- Anna's Archive: HTML-scraped; keyed fast downloads or opted-in visible-browser URL resolution; shared page-visit limits
- LibGen: mirror-configurable fallback
- Both monitored daily by the Upstream Contract Check workflow

### MCP Protocol
- SDK Server with tools/list + tools/call handlers, JSON-RPC 2.0, stdio transport
- stdout purity is a hard constraint (`__tests__/stdio-purity.test.js`)

---

## Security Architecture

- **Credentials**: environment variables only (`ZLIBRARY_EMAIL`, `ZLIBRARY_PASSWORD`, `ANNAS_SECRET_KEY`); never committed
- **Isolation**: Python bridge runs in UV-managed `.venv/`; download directory configurable
- **Download safety**: `Content-Disposition` filenames are reduced to a bare basename (posixpath + ntpath) before joining onto the output directory (path-traversal defense)
- **Resilience**: circuit breaker + retry on all bridge calls; fields EAPI does not supply (terms, booklists, IPFS ids) are omitted, not faked (#256)
- **Disclosure**: see `SECURITY.md` for reporting policy and the `LOG_LEVEL=debug` caveat

---

## Quick Reference

- **Health assessment / roadmap**: [claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md](../claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md)
- **Project context**: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — mission and domain model
- **Code patterns**: [PATTERNS.md](PATTERNS.md)
- **Parked text implementation**: [migration](../docs/text-processing-migration.md)
- **ADRs**: [docs/adr/README.md](../docs/adr/README.md)
- **Contributing**: [CONTRIBUTING.md](../CONTRIBUTING.md)
