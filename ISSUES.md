# Z-Library MCP - Issues & Technical Debt

<!-- Last Verified: 2026-08-11 -->

> **Contributing?** This file is the maintainer's internal issue ledger (history,
> evidence, resolutions). For anything you want to report or pick up, use
> [GitHub Issues](https://github.com/rookslog/zlibrary-mcp/issues) — issues
> labeled `good first issue` and `help wanted` are curated entry points.

## Executive Summary

**Last Updated**: 2026-08-11
**Status**: v1.4.0 shipped 2026-08-10 (LibGen is a real download source). Current
release is **v1.5 — Anna's Archive is a real source**, map issue #95.

**What is being worked on lives in GitHub milestones, not in this file or any other
document.** See [docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md) for the scheme and
for the 2026-08-11 tracker audit that motivated it. This ledger records *resolved*
issues and their evidence; it is history, not a plan.

Evidence for the 2026-07-24 findings below:
[claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md](claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md)
— historical, and superseded for anything forward-looking.

---

## Resolved Issues

### ISSUE-STDIO-001: Server Wrote Non-JSON to stdout, Breaking the MCP Protocol [RESOLVED]
**Severity**: Was CRITICAL
**Impact**: Strict stdio clients dropped the connection — the "server disconnected"
symptom in GitHub issue #11. Thirteen `console.log` calls wrote to stdout, which is
the JSON-RPC channel; four fired on every search, corrupting an active stream.
**Resolution** (2026-07-24): All diagnostics moved to stderr via `src/lib/logger.ts`.
Guarded by `__tests__/stdio-purity.test.js` (static scan + live handshake). The CI
smoke test previously piped stdout through `grep '^{'`, masking the bug; that filter
is removed and the job now fails on any non-MCP line.

### ISSUE-REL-001: npm Publish Failed on Every Release Since v1.2 [RESOLVED]
**Severity**: Was CRITICAL
**Impact**: npm served 1.0.0 (2025-04-11) while README recommended `npm install -g
zlibrary-mcp`. All four publish runs failed at `npm install -g npm@latest`, whose
engines field outran the Node 22 runner. Build and tests passed every time.
**Resolution** (2026-07-24): Step removed — `--provenance` has shipped in npm since
9.5 and Node 22 bundles npm 10.x. Added a tag/package.json version check and a GHCR
image publish job (closes the request in PR #9).
**Remaining**: a release must actually be cut to update the registry.

### ISSUE-AUDIT-002: Stale Security Floors Kept CI Red [RESOLVED]
**Severity**: Was HIGH
**Impact**: CI red on master from 2026-04-03 onward. Seven of eight jobs passed; only
`audit` failed, with 74 advisories across 15 packages because
`tool.uv.constraint-dependencies` floors had gone stale.
**Resolution** (2026-07-24): Floors raised to the lowest fixed version of each
advisory: 74 advisories in 15 packages down to 1 in 1 (`nltk` PYSEC-2026-597, no fix
published). Includes pytest 8 to 9 and cryptography 46 to 49 with no test changes.
Dependabot now keeps floors current. Audit policy documented in SECURITY.md.

### ISSUE-DRIFT-001: No Detection of Upstream Contract Changes [RESOLVED]
**Severity**: Was HIGH
**Impact**: Every third-party call is mocked, so the suite stays green indefinitely
after the real integrations break. 26 credentialed integration tests existed but
nothing ever ran them.
**Resolution** (2026-07-24): Daily `.github/workflows/upstream-check.yml` probes each
source's live response shape and runs the integration suite, filing a rolling
`upstream-drift` issue on failure. Same probe exposed to users as `npm run doctor`.

### ISSUE-PLAT-001: setup-uv.sh Failed on macOS [RESOLVED]
**Severity**: Was Medium
**Impact**: `grep -oP` is GNU-only; BSD grep rejects it with "invalid option -- P"
(GitHub issue #14), so setup failed before it began.
**Resolution** (2026-07-24): Version detected via `python3 -c`. A second occurrence in
`scripts/validate-readme-tools.sh` was fixed too — it escaped notice because CI runs
on Linux.

### ISSUE-SEC-001: Path Traversal via Content-Disposition [RESOLVED]
**Severity**: Was HIGH
**Impact**: The download filename came from a server-controlled `Content-Disposition`
header and was joined straight onto the output directory, so
`filename="../../etc/passwd"` escaped it. Verified: `Path("/downloads") /
"../../etc/passwd"` resolves outside the directory, and the response stream was written
there.
**Resolution** (2026-07-24): `sanitize_download_filename()` reduces the value to a bare
basename. Applies both posixpath and ntpath, because `os.path.basename` on POSIX does
not split on backslashes — a `..\..\x` payload would otherwise pass through a Linux
server untouched. Covered by 34 tests including the real path-join property.

### ISSUE-PLAT-002: Windows Unsupported [RESOLVED]
**Severity**: Was High for affected users
**Impact**: Three independent bugs prevented the server from running on Windows. The ESM
auto-start guard compared `import.meta.url` against a `file://` string concatenated from
a backslash `argv[1]`, so it never matched and the server never started. `venv-manager`
hardcoded `.venv/bin/python` where UV places `.venv\Scripts\python.exe`, so the venv
was never found. `Content-Disposition` filenames were parsed with `split('filename=')`,
breaking on RFC 6266 extended notation.
**Resolution** (2026-07-24): All three fixed, incorporating PR #13 by @ltspace, with 46
tests. Platform-dependent behaviour is parameterised (`venvPythonSegments(platform)`,
`isProcessEntryPoint(url, argv1)`) so a Linux CI runner exercises the Windows branch —
these bugs reached users precisely because the broken paths only execute on the platform
they are broken for. PR #13 can be closed as incorporated with thanks.

### ISSUE-LFS-001: Missing LFS Fixtures Produced Misleading Test Failures [RESOLVED]
**Severity**: Was Low (but high nuisance)
**Impact**: A clone without Git LFS leaves pointer files where PDFs should be. Five
tests then failed with assertions resembling detection regressions
(`assert 'ERROR' == 'MIXED'`) rather than naming the real cause.
**Resolution** (2026-07-24): `require_real_fixture()` in `__tests__/python/conftest.py`
skips with the cause and the fix (`git lfs pull`).

### ISSUE-NET-001: Multi-Source Search Hung Forever on an Unreachable Provider [RESOLVED]
**Severity**: Was High
**Discovered**: 2026-08-11, on dionysus
**Impact**: `search_multi_source` had no network deadline anywhere in its path.
When a provider was unreachable the call never returned and never errored; the
MCP client eventually abandoned it, and the Python subprocess kept running with
nothing waiting on it. Three `python_bridge.py search...` processes were found
alive simultaneously — elapsed 9h10m, 8h43m and 30m, the two long ones left over
from a session that had already exited. A `source=libgen` call was aborted by the
client at its 1800s idle timeout with no response and no progress.
**Root cause** (three defects, each independently sufficient):
1. `libgen_api_enhanced/search_request.py:177` calls `requests.get(...)` with no
   `timeout=`, and catches a `requests.exceptions.Timeout` that can therefore
   never fire. A host that drops SYNs blocks it forever.
2. `LibgenAdapter.search` ran that call through `asyncio.to_thread`, whose worker
   threads are non-daemon and are joined at interpreter shutdown — so an
   abandoned request kept the entire process alive.
3. `PythonShell.run` in `src/lib/zlibrary-api.ts` returned a promise with no
   timeout and no handle on the child, so a client-side cancellation could only
   abandon the promise, never kill the process.
An explicit `source="annas"` also fell back to LibGen on failure, which is how a
request tagged `annas` came to hang inside LibGen's un-timed search.
**Provider state at the time** (measured, this host): `annas-archive.org` had no
DNS record at all and `.se`/`.li` failed DNS in ~15ms; `libgen.is` resolved to
193.218.118.42 but every TCP connect timed out, as did `libgen.rs` and
`libgen.st`. General egress was fine (example.com and archive.org both 200), and
the Z-Library EAPI path was unaffected.
**Resolution**: `lib/sources/net.py` adds a pre-flight DNS+TCP probe that
distinguishes `dns_failure` from `connect_timeout`, an httpx timeout builder
covering every phase, and `run_bounded`, which runs uncancellable third-party
calls on a **daemon** thread under a wall-clock budget. `lib/sources/errors.py`
attributes every failure to a provider, host and stable reason code, surfaced to
the MCP caller as `details` in the error envelope. LibGen search now walks the
same mirror list as `get_download_url`. Router fallback is tied to
`source=auto`; an explicit source raises rather than silently rerouting, and a
reachable provider with no matches still returns `[]`. On the Node side,
`src/lib/python-runner.ts` replaces `PythonShell.run` with a runner that enforces
`PYTHON_BRIDGE_TIMEOUT`, escalates SIGTERM to SIGKILL, honours the MCP request's
AbortSignal, and reaps any surviving child at server exit.
**Tests**: `__tests__/python/test_source_net.py`,
`__tests__/python/test_multi_source_timeouts.py`,
`__tests__/python-runner.test.js` (spawns real subprocesses and asserts the pid
is gone, since a mock cannot show what happens to an OS process).

### ISSUE-API-003: get_download_limits Always Returned "unknown" [RESOLVED]
**Severity**: Was Medium
**Discovered**: 2026-08-11
**Impact**: The tool returned `{"daily_limit": "unknown", "daily_remaining":
"unknown"}` on every call, so callers could not tell whether quota remained
before spending it — the only question the tool exists to answer.
**Root cause**: `get_download_limits` read `downloads_today_limit` and
`downloads_today_left` from `/eapi/user/profile`. The endpoint has never sent
those names; it sends `downloads_limit` and `downloads_today` (verified against a
live response 2026-08-11). Both lookups fell through to the `"unknown"` default.
The unit test passed throughout because its fixture had been written to match the
code rather than the service.
**Resolution**: read the real field names, derive `daily_remaining` clamped at
zero (the server counts a download when issued and can report `downloads_today`
above the cap), and log a warning naming the response keys if `downloads_limit`
disappears again. The test fixture now mirrors a captured live response.

### ISSUE-API-001: Z-Library Cloudflare Bot Protection [RESOLVED]
**Severity**: Was CRITICAL
**Resolution**: EAPI migration (Phase 7, Feb 2026). All API calls now use EAPI JSON endpoints which bypass Cloudflare browser challenges. Health check with Cloudflare detection added.
**ADR**: [ADR-005-EAPI-Migration](docs/adr/ADR-005-EAPI-Migration.md)

### ISSUE-API-002: Default EAPI Domain (z-library.sk) Fronted by DiamWall Anti-Bot [RESOLVED]
**Severity**: Was High
**Discovered**: 2026-07-24, first automated run of the credentialed integration suite
**Impact**: The default EAPI domain `z-library.sk` no longer serves `/eapi/*` to
programmatic clients. All requests get an HTTP 307 self-redirect from "DiamWall"
that sets a `__diamwall` cookie, then 513/517 "Access Denied" on retry — including
with the EAPIClient's browser User-Agent. `1lib.sk` shows the same wall. With the
old defaults, `initialize_eapi_client()` failed at login and every tool was dead.
**Evidence** (2026-07-24, unrestricted network):
- `POST https://z-library.sk/eapi/user/login` → 307 → `Set-Cookie: __diamwall=…` → retry → 517 Access Denied (DiamWall-branded page, cdn.diamwall.com assets)
- Same request against `z-library.ec` → normal EAPI JSON (`{"success":0,"error":"Incorrect email or password"}` for bad creds; real login succeeds)
- `/eapi/info/domains` (queried via z-library.ec) still advertises `z-library.sk` FIRST, so Hydra-mode domain discovery in `initialize_eapi_client()` would actively switch a working client back to the walled domain.
**Related finding**: `/eapi/user/login` rate-limits repeated logins from one IP —
after ~10 logins in an hour it returns 400 `{"success":0,"error":"Incorrect email
or password"}` even for valid credentials, recovering after a cooldown. The
integration suite logs in once per module (shared `zlib_client` fixture) to stay
under this limit — and the domain probe deliberately uses `GET /eapi/info/domains`,
never login, for the same reason.
**Resolution** (2026-07-24, PR #36 "fix: resilient EAPI domain fallback and probing"):
(1) single default replaced by the probed fallback list
`DEFAULT_EAPI_DOMAINS = ["z-library.ec", "z-library.sk", "1lib.sk"]` in
`zlibrary/src/zlibrary/eapi.py`; an explicit `ZLIBRARY_EAPI_DOMAIN` is honoured
verbatim with no probing and no silent switching; (2) hydra discovery
(`select_advertised_domain()` / `discover_eapi_domain()`) probes each advertised
domain and skips walled ones, keeping the current working domain when nothing
advertised is usable; (3) DiamWall HTML-where-JSON-expected now raises
`DiamWallError`, classified as `diamwall_blocked` by the health check and reported
explicitly by `npm run doctor`, with the `export ZLIBRARY_EAPI_DOMAIN=<working-domain>`
remedy in the message. Covered by `__tests__/python/test_eapi_domain_resilience.py`.
**CI caveat** (2026-07-25, post-fix dispatch of upstream-check): GitHub-hosted
runners get a bare HTTP 403 from **every** Z-Library domain including
`z-library.ec` — datacenter-IP blocking, unrelated to which domain is default.
The upstream check therefore classifies walls/403s as `BLOCK` (environmental,
no drift issue filed, live suite skipped) rather than `FAIL`; only a probe from
a residential network can distinguish IP blocking from a global outage.

### ISSUE-002: Venv Manager Test Failures [CLOSED]
**Severity**: Was High
**Resolution**: UV migration (Phase 2, Jan 2026) eliminated the entire venv manager complexity. Code reduced 77% (406 to 92 lines), tests reduced 90% (833 to 85 lines). The undefined `.trim()` error and venv creation failures no longer occur.

### ISSUE-005: Missing Error Recovery Mechanisms [RESOLVED]
**Resolution**: Retry logic with exponential backoff (`src/lib/retry-manager.ts`), circuit breaker pattern (`src/lib/circuit-breaker.ts`). Configurable via environment variables.

### ISSUE-006: Test Suite Warnings [RESOLVED]
**Resolution**: All warning scenarios covered with proper tests. 28/28 tests passing.

### ISSUE-007: Documentation Gaps [RESOLVED]
**Resolution**: 10 ADRs documented (ADR-001 through ADR-010). Comprehensive `.claude/` docs. Phase 6 documentation cleanup complete.

### ISSUE-FN-001 through ISSUE-FN-004: Footnote Detection Bugs [RESOLVED]
**Resolution**: All 4 footnote critical bugs fixed (Oct-Nov 2025). Marker detection, data contract, corruption recovery, and pairing all working.

### ISSUE-DOCKER-001: Docker numpy/Alpine Compilation [RESOLVED]
**Severity**: Was Low
**Resolution**: Phase 16-01 switched Docker base to `python:3.11-slim` (Debian) with pre-built wheels, eliminating Alpine numpy compilation failures.

### ISSUE-GT-001: Footnote Tests Broken by Ground Truth v3 Migration [RESOLVED]
**Severity**: Was Medium
**Resolution**: Phase 18-01 updated test assertions in test_real_footnotes.py and test_inline_footnotes.py to use v3 schema accessors (`marker["symbol"]`, `definition["content"]`).

### ISSUE-PERF-001: Performance Tests Flaky on CI Runners [RESOLVED]
**Severity**: Was Low
**Resolution**: Phase 18-01 loosened thresholds with 3x multiplier (0.005->0.015, 0.001->0.003, 0.010->0.030) to account for CI and dev machine variance while still catching algorithmic regressions.

---

## Open Issues

### ISSUE-001: No Official Z-Library API
**Severity**: Medium (downgraded from Critical after EAPI migration)
**Impact**: Core functionality relies on reverse-engineered EAPI
**Status**: Mitigated by EAPI transport + health check monitoring
**Mitigation**: EAPI JSON endpoints are more stable than HTML scraping. Health check detects upstream changes. Future: Anna's Archive as alternative source.

### ISSUE-003: Z-Library Infrastructure Changes (Hydra Mode)
**Severity**: Medium
**Impact**: Domain discovery and session management
**Status**: Handled by vendored zlibrary fork with EAPI client
**Note**: EAPI endpoints appear more stable than HTML pages for Hydra mode domains.

### ISSUE-008: Performance Optimizations Needed
**Severity**: Low
**Remaining**:
- No result caching layer (search results, metadata)
- No performance profiling tools
**Resolved items**:
- HTTP connection pooling (shared httpx.AsyncClient)
- Parallel processing (ProcessPoolExecutor for CPU-bound work)

### ISSUE-009: Development Experience Issues
**Severity**: Low
**Remaining**:
- No hot reload for Python changes
- No performance profiling tools
- Limited development fixtures/mocks
**Resolved items**:
- Debug mode with verbose logging (`ZLIBRARY_DEBUG=1`)

### ISSUE-SRC-001: Z-Library Bypasses the Source Abstraction
**Severity**: Medium (architectural)
**Impact**: `lib/sources/` has a working `SourceAdapter` interface, unified result
model, and quota-aware `SourceRouter` — but Z-Library does not implement it, going
through `python_bridge.py` directly. Of 13 tools, 12 are Z-Library-only and one
reaches the router. A new source therefore cannot inherit metadata enrichment,
booklists, term search, or the RAG pipeline, and Z-Library remains a single point of
failure.
**Direction**: Wrap the EAPI client in `SourceAdapter`, register it in the router,
route all tools through it. See section 6 of the health assessment.

### ISSUE-DOCS-001: Overlapping Documentation Trees
**Severity**: Low (public-facing polish)
**Impact**: `docs/` (40+ files) and `claudedocs/` overlap, with stale one-off
analyses. Reads as sprawl on a public repo and buries the ~6 documents a user
needs. (Previously four trees with three separate roadmaps — the GSD `.planning/`
tree was removed 2026-07-24 with its durable content preserved in `claudedocs/`,
and `.claude/` was pruned to living guides only, so the overlap has shrunk to the
two doc trees with a single live roadmap.)
**Direction**: Consolidate `docs/` into `{guides,reference,adr,archive}`; move
superseded analyses to `archive/`.

### ISSUE-AUDIT-001: pip-audit False Positives for Vendored Fork
**Severity**: Low
**Component**: zlibrary/ vendored fork
**Status**: Known limitation
**Impact**: pip-audit reports vulnerabilities for the vendored zlibrary package that are false positives (custom fork, not the upstream package)
**Workaround**: Exclude vendored package from audit or add to allowlist

---

## Technical Debt

### Architecture
1. **Tight Coupling**: Node.js and Python layers coupled through PythonShell (acceptable trade-off)
2. **Legacy Facades**: rag_processing.py still exists as facade delegating to lib/rag/ (intentional for backward compat)

### Testing
1. **No Performance Tests**: Missing load testing, stress testing
2. **Limited E2E with Live Credentials**: Full workflow needs `TEST_LIVE=true`
3. **No cross-language contract test**: Jest mocks the Python bridge and pytest tests
   Python directly, so nothing asserts that the JSON shape TypeScript expects is the
   shape Python emits. The Phase 19 bundle contract is exactly what drifts here.
4. **Multi-source fallback under-tested**: `router.py` shows 97% line coverage, but the
   branch users hit when a source degrades — Anna's quota exhausted mid-request,
   falling back to LibGen, reconciling two result shapes — is not exercised.
5. **Platform-branch coverage**: now covered for the venv path and entry guard
   (ISSUE-PLAT-002); other platform-dependent behaviour remains untested.

### Code Quality
1. **Inconsistent Error Handling**: Mix of exceptions across Python modules
2. **Magic Numbers**: Some hardcoded timeouts/limits remain

---

## Improvement Opportunities

### Search Enhancements
- **SRCH-001**: Fuzzy/approximate matching - *Partially implemented* (search_advanced tool provides fuzzy detection)
- **SRCH-002**: Missing advanced filters (size, quality, edition)
- **SRCH-003**: No search result ranking/scoring

### Download Management
- **DL-001**: No queue management for batch downloads
- **DL-002**: Cannot resume interrupted downloads

### RAG Processing
- **RAG-001**: No semantic chunking strategies
- **RAG-002**: OCR for scanned PDFs - *Partially implemented* (framework exists, ML models pending)
- **RAG-005**: No support for MOBI, AZW3, DJVU formats

---

## Broken Functionality

### BRK-001: Download Book Combined Workflow
**Status**: Investigated (2026-01-29) - Code path exists, likely resolved
**Note**: Cannot fully confirm without live credentials. Needs `TEST_LIVE=true` with real book.

### BRK-002: Book ID Lookup
**Status**: Deprecated (ADR-003). Use search_books instead.

### BRK-003: History Parser
**Status**: Fragile. Parser may break with EAPI response format changes.

---

## Future Direction

### Anna's Archive Expansion
- Planned as additional/alternative book source
- Reduces single-source risk (Z-Library availability)
- Architecture supports adding new backends behind service layer
- Would provide broader book coverage and redundancy

---

## Security Considerations

### SEC-001: Credential Storage
**Status**: Environment variables (standard practice for MCP servers)
**Risk**: Low (credentials in process env, not persisted)

### SEC-002: Input Validation
**Status**: EAPI JSON transport provides some inherent sanitization vs raw HTML injection
**Risk**: Low

---

*Document maintained as part of Phase 6 documentation quality gates.*
*Next Review: As needed when new issues discovered.*
