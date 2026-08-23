# ADR-005: EAPI Migration

**Date:** 2026-02-01

**Status:** Accepted — decision 4 (downloads) superseded by ADR-011

## Context

In January 2026, Z-Library deployed Cloudflare browser verification (JavaScript challenges) on all HTML page requests. The `httpx` client used by the Python bridge cannot execute JavaScript, so every GET request for HTML pages returned a "Checking your browser..." challenge page instead of actual content. This rendered the entire MCP server non-functional -- all search, browse, and metadata operations failed (ISSUE-API-001).

The Z-Library EAPI (at `/eapi/` path) provides JSON endpoints that are not subject to Cloudflare browser challenges, as API endpoints are typically excluded from bot protection rules.

## Decision

Migrate all API calls from HTML scraping to EAPI JSON endpoints:

1. **Search operations**: Use `/eapi/book/search` instead of HTML search pages
2. **Book metadata**: Use `/eapi/book/{id}/{hash}` instead of scraping book detail pages
3. **Recent books**: Use EAPI recent endpoint instead of HTML browse pages
4. ~~**Downloads**: Keep legacy AsyncZlib client for file downloads (EAPI returns download URL, but actual file download requires cookies from the legacy client)~~ — **superseded by ADR-011.** The implementation diverged from this clause: `EAPIClient` grew its own `download_file` and the live Z-Library transfer runs through it. ADR-011 records that as the decision it had become.

Implement an `EAPIClient` class in the vendored zlibrary fork (`zlibrary/eapi.py`) that:
- Uses httpx with lazy initialization and cookie-based auth
- Recreates client on re-authentication (fresh cookies after login)
- Normalizes EAPI JSON responses to the internal Book format

## Consequences

### Positive
- **Cloudflare bypass**: EAPI JSON endpoints not subject to browser challenges
- **No HTML parsing in hot path**: Eliminates BeautifulSoup dependency for search/metadata
- **Structured responses**: JSON is more stable than HTML DOM selectors
- **Health monitoring**: Can detect Cloudflare challenges via response fingerprinting

### Negative
- **Booklists gracefully degrade**: EAPI has no booklist endpoint; tools return empty/search fallback
- **Full-text search limited**: No dedicated full-text search mode in EAPI; routes through regular search
- **Terms/IPFS CIDs unavailable**: EAPI does not expose these fields; return empty defaults
- ~~**Downloads still use legacy client**: EAPI returns URL but file download needs AsyncZlib with session cookies~~ — no longer holds; see ADR-011.

### Neutral
- ~~**Dual client architecture**: EAPIClient for queries, AsyncZlib for downloads (acceptable complexity)~~ — the split closed; see ADR-011.
- **EAPI client initialized in main()**: Avoids import side effects, requires passing client to tool functions
