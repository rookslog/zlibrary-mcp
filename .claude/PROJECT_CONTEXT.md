# Z-Library MCP Project Context

<!-- Last Verified: 2026-07-24 -->

For the current public interface and contributor commands, use `README.md` and
`AGENTS.md`. Older implementation details below are not a current capability
checklist.

## Mission Statement

Build a robust, resilient MCP server for Z-Library integration that provides comprehensive book search and download capabilities for AI assistants, with emphasis on reliability despite Z-Library's infrastructure changes.

## Core Architecture Principles

### 1. Resilience First

- **Domain Agility**: Handle Z-Library's "Hydra mode" with dynamic domain discovery (`discover_eapi_domain()` in `zlibrary/src/zlibrary/util.py` resolves domains at runtime from `/eapi/info/domains`)
- **EAPI Transport**: JSON API endpoints bypass Cloudflare browser challenges — zero HTML scraping remains in the vendored `zlibrary/` fork
- **Node-Side Resilience**: Every Python bridge call is wrapped in retry with exponential backoff (`src/lib/retry-manager.ts`) and a circuit breaker (`src/lib/circuit-breaker.ts`) inside `src/lib/zlibrary-api.ts`
- **Source Diversification**: `lib/sources/` provides a `SourceAdapter` abstraction with Anna's Archive (primary) and LibGen (fallback) adapters behind a `SourceRouter`, reducing single-source risk
- **Graceful Degradation**: Optional detail fields absent upstream are omitted from `get_book` results
- **Drift Detection, Not Assumption**: unit suites mock all third-party calls, so the scheduled Upstream Contract Check workflow and `npm run doctor` exist to catch real-world breakage

### 2. Abstraction Layers

```
+---------------------------+
|   MCP Interface           | <- 5 tools exposed to AI assistants (Server request handlers)
+---------------------------+
|   Service Layer           | <- src/lib/zlibrary-api.ts: orchestration, retry + circuit breaker
+---------------------------+
|   Python Bridge           | <- Language boundary (PythonShell, per-call spawn)
+---------------------------+
|   Backends                | <- EAPIClient (zlibrary/ fork, httpx JSON)
|                           |    SourceRouter -> Anna's Archive / LibGen adapters (lib/sources/)
+---------------------------+
+---------------------------+
```

### 3. Development Philosophy

- **Observable**: Diagnostics go to **stderr only** via `src/lib/logger.ts` — stdout is the JSON-RPC protocol channel, enforced by `__tests__/stdio-purity.test.js`
- **Maintainable**: Clear separation of concerns, modules under 500 lines

## Acquisition-only implementation

### Working Features

- **Five public MCP tools**: `search`, `get_book`, `download`, `get_limits`, and `get_history`. `TOOL_DEFINITIONS` and `createServer()` in `src/index.ts` define and register them; CI compares the registrations with the README (`scripts/validate-readme-tools.sh`)
- EAPI JSON transport for search, metadata, download, account quota and history operations
- Multi-source search (`search_multi_source`) across Anna's Archive and LibGen with automatic fallback
- UV-based Python dependency management (project-local `.venv/`, `uv.lock`)
- Health check with Cloudflare detection; scheduled upstream contract monitoring

### Known Limitations

- No public booklist tools (no EAPI booklist endpoint)
- Public `search` searches catalogue metadata, not text inside books
- `get_book` omits terms, booklists and IPFS IDs (not available via EAPI)
- Anna's Archive adapter (`lib/sources/annas.py`) is the residual HTML-parsed surface
- Z-Library itself is not yet a `SourceAdapter` — its tools bypass `SourceRouter`

### Future Direction

- Promote Z-Library to a `SourceAdapter` so all tools route through `SourceRouter`
- Windows support and publish-pipeline fixes land in the next release (v1.3.0)
- Read current priorities from GitHub milestones; dated reports are historical.

## Domain Model

### Public selections and search

Every `search` row gets a process-local `ref` handle. `get_history` rows get a ref
only when sufficient download identity is available. Public `get_book` and
`download` accept that ref; callers do not pass a `Book` object or `bookDetails`.

`search` accepts `query`, `author`, an ordered `sources` list, `exact`,
`year_from`, `year_to`, `languages`, `formats`, and `limit`. Continuation uses
`cursor` alone. See the [public API](../docs/api.md) for field constraints and
response shapes.

### Multi-Source Entities (Python, lib/sources/models.py)

- `UnifiedBookResult` — normalized search result shape shared by all source adapters
- `DownloadResult` — download URL plus optional quota info (Anna's Archive fast-download quota)

## Technical Decisions

### Why Python Bridge?

- Z-Library community libraries are Python-based
- Async HTTP, source adapters, and file acquisition libraries
- Async support with asyncio; EAPI client uses httpx
- Spawned per-call via PythonShell from `src/lib/zlibrary-api.ts` (stateless, crash-isolated)

### Why Node.js Frontend?

- MCP interface uses the Node.js SDK (Server request handlers, SDK 1.25+)
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

## Integration Points

### Upstream Dependencies

- `sertraline/zlibrary` — base Python library (vendored fork with EAPI client)
- `@modelcontextprotocol/sdk` ^1.25 — MCP protocol (Server request handlers)
- `python-shell` — Node.js to Python bridge
- Anna's Archive and LibGen — HTML-scraped alternative sources (monitored by the upstream-check workflow)

### Downstream Consumers

- Claude Code (primary), RooCode, Cline, other MCP-compatible AI assistants

## Development Workflow

### Standard Flow

1. **Planning**: Read current priorities from GitHub milestones and consult `ISSUES.md` for context.
2. **Implementation**: Follow patterns in `.claude/PATTERNS.md`
3. **Testing**: Unit (Jest/Pytest) -> Integration -> live/E2E where credentialed
4. **Contribution process**: See `CONTRIBUTING.md`

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

Jest and pytest are separate suites. Passing unit or offline tests does not
establish live provider availability; see [README, Development](../README.md#development).

## Quick Commands

```bash
# Setup
bash setup-uv.sh         # UV-based Python setup (or: uv sync)
npm install              # Node.js deps
npm run build            # Build TypeScript (validates Python file paths)

# Testing
npm test                 # Jest with coverage; does not run pytest
uv run pytest -m "not integration"  # Separate Python suite, excluding integration tests

# Running
node dist/index.js       # Start MCP server

# Diagnostics
npm run doctor           # Probe upstream sources (distinguish outage from bug)
LOG_LEVEL=debug node dist/index.js  # Verbose stderr logging
```

---

_Use README.md, AGENTS.md and current source for behavior and commands; keep this context aligned with them._
