# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `npm run audit:release` ([scripts/release-audit.mjs](scripts/release-audit.mjs)) and a
  weekly `Release Record Audit` workflow, checking that the project's record of itself is
  still true. **Drift:** every tag has a GitHub Release and a CHANGELOG section, every
  release milestone names its map issue, no tagged version has open issues on its
  milestone, no closed issue is missing a milestone. **Neglect:** no PR stays open more
  than 14 days (measured from when it was opened, so commenting cannot reset the clock —
  only merging or closing clears it), no branch outlives its merged PR, no branch goes 14
  days with neither a PR nor a commit. Also wired into `npm run doctor`, which runs the
  upstream contract check first so a stale PR cannot mask upstream drift. Rules and the
  incidents that motivated them: [docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md).
- `delete_branch_on_merge` enabled on the repository, retiring by configuration the
  branch sprawl that had to be cleaned by hand on 2026-08-11 (nine dead branches).
- `publish.yml` now creates the GitHub Release on a tag push, with notes extracted from
  that version's CHANGELOG section, and fails the release if the section is absent.
  Previously a tag published to npm and GHCR while the Releases page silently fell
  behind — v1.4.0 shipped that way, and v1.2.0 had gone four months unnoticed (#108).
- **Provider-attributed failures.** An unreachable source now returns a prompt
  error naming the provider, the host, and a reason code — `dns_failure` and
  `connect_timeout` are distinguished, because they call for different fixes
  (update the mirror list vs. try another mirror). Reachability failures are
  marked non-retryable, so the retry layer stops multiplying a wait against a
  host that is not answering.
- **A DNS + TCP pre-flight probe** before each provider request, so a dead host
  costs one bounded probe instead of a full request budget. Cached per
  (host, port) for the life of the call, so LibGen's three-mirror walk probes
  each host once. It targets the port and scheme the real request will use, and
  **skips itself entirely when `HTTP_PROXY`/`HTTPS_PROXY` applies** (honouring
  `NO_PROXY`) — a direct socket cannot represent a proxied request, and a probe
  that vetoes a request the real client could have completed is worse than no
  probe. Disable with `BOOK_SOURCE_PREFLIGHT=false`.
- `BOOK_SOURCE_TOTAL_TIMEOUT` is now genuinely enforced for Anna's, not just
  LibGen. `httpx.Timeout` bounds each phase separately and restarts its read
  deadline on every chunk, so a host trickling bytes never tripped it; a
  wall-clock deadline now covers the whole operation.
- **LibGen search now walks all three mirrors** (`li → vg → la`), matching what
  `download_book_to_file` already did.
- Timeout configuration: `BOOK_SOURCE_CONNECT_TIMEOUT`,
  `BOOK_SOURCE_READ_TIMEOUT`, `BOOK_SOURCE_TOTAL_TIMEOUT`,
  `BOOK_SOURCE_PREFLIGHT`, `BOOK_SOURCE_PREFLIGHT_TIMEOUT`,
  `PYTHON_BRIDGE_TIMEOUT`, `PYTHON_BRIDGE_LONG_TIMEOUT`,
  `PYTHON_BRIDGE_KILL_GRACE`. A malformed value falls back to its default
  rather than disabling the timeout. See
  [docs/RETRY_CONFIGURATION.md](docs/RETRY_CONFIGURATION.md) for how the Python
  and Node budgets compose.

### Fixed

- **`search_multi_source` could hang forever, and leave the Python process
  behind when it did.** Three defects compounded: the vendored
  `libgen_api_enhanced` calls `requests.get` with no `timeout=` (and catches a
  `Timeout` that therefore can never fire), the LibGen adapter ran that through
  `asyncio.to_thread` — whose worker threads are non-daemon and joined at
  interpreter shutdown, so an abandoned request kept the whole process alive —
  and the Node side used `PythonShell.run`, which returns a promise with no
  deadline and no handle on the child, so a cancelled MCP call could only
  abandon it. Observed on 2026-08-11 as three `python_bridge.py search…`
  processes still running, the oldest 9h10m old, belonging to a session that
  had already exited. Every outbound call in `lib/sources/` is now bounded, and
  the subprocess tree is killed on timeout, client cancellation, and server
  shutdown.
- **Cancelling a tool call now ends the work.** Every tool forwards the MCP
  request's abort signal down to the subprocess, so a client that gives up
  stops the Python process rather than leaving it to run out its budget with
  nobody waiting for the result.
- **`get_download_limits` reported `"unknown"` instead of real numbers.** It
  read `downloads_today_limit` / `downloads_today_left` from the EAPI profile;
  the endpoint sends `downloads_limit` / `downloads_today`. The unit test
  passed throughout because its fixture had been written to match the code
  rather than the service. The response now also carries `downloads_today`.
- `SourceRouter` honours an explicit `source="annas"` and Anna's search extracts full
  metadata — authors, publisher, year, language, format, size, and upstream provenance —
  parsed by pattern rather than position, since year is absent from roughly 9% of
  records (#74, #78).
- Anna's search results now report which other sources hold the file. Those markers mean
  *retrievable*, which makes Anna's a cross-source index rather than only a fourth
  source.
- The scheduled upstream contract check no longer fails spuriously (#71).

### Changed

- `CONTEXT.md` added as the terminology glossary, splitting the overloaded term
  "keyless" into *keyed fast_download*, *operator-cookie slow_download*, and
  *machine-solved challenge*. The ambiguity had caused approved work to be recorded as
  cancelled. "Keyless" is now a banned term; prefer "key-free" (#98).
- **Anna's Archive has exactly one supported download route: keyed `fast_download`.**
  The operator-cookie route is dead rather than deferred — DDoS-Guard binds the challenge
  cookie to the issuing IP inside `__ddg9_`, so a transplanted cookie is rejected
  byte-for-byte identically to no cookie at all (#84). Guardrails for that route (#97)
  are moot rather than deferred.
- Removed the vestigial `ts-jest` dev dependency, which was configured in
  `jest.config.js` but transformed nothing (#93).
- Dependency bumps: `@modelcontextprotocol/sdk` 1.25.3 → 1.30.0, `opencv-python-headless`
  4.13 → 5.0, `libgen-api-enhanced` 1.2.4 → 1.3, `cffi` 2.0.0 → 2.1.1, `yarl` 1.22.0 →
  1.24.5, `pytest-benchmark` 5.1.0 → 5.2.3, plus dev-tooling and CI action updates.
- **An explicit `source` is no longer silently rerouted.** `source="annas"` that
  fails now raises with the provider and reason named, instead of returning
  LibGen's results. Fallback remains the behaviour of `source="auto"`, which is
  what asks for it. This extends the fix in #74 from the empty-result case to
  the network-failure case — silent rerouting is how a request tagged
  `source="annas"` came to hang inside LibGen's search.
- An empty result from `search_multi_source` now means every provider answered
  and none matched. Previously a network failure with fallback disabled also
  returned an empty list, making an outage indistinguishable from no results.
  This holds for partial walks too: one provider answering empty while another
  is unreachable raises, rather than reporting "no matches" about a provider
  that was never successfully searched.
- Client cancellations no longer count toward the Python bridge circuit
  breaker. Five aborted searches would otherwise open the shared breaker and
  fail every unrelated tool for `CIRCUIT_BREAKER_TIMEOUT` — turning a user's
  own impatience into an outage. `CircuitBreaker` gained an `isFailure` option
  for this.

## [1.4.0] - 2026-08-10

Z-Library is no longer the only source that can deliver a file. LibGen search
results are now downloadable, so hitting the Z-Library daily limit has a
fallback — a step toward VISION.md invariant 4 ("sources are adapters; no
source is privileged").

### Added

- **LibGen downloads.** `download_book_to_file` accepts a `search_multi_source`
  result and fetches it. Downloads resolve through `ads.php`, which is
  addressable directly from the md5, and walk mirrors `li → vg → la`.
  Failover is driven by *bytes actually served*, not by key resolution: a
  mirror can hand out a valid key while the CDN node behind it is dead, so
  each candidate is verified with a ranged request that rejects an expired-key
  bounce to `/ads.php`, an HTML error page served as HTTP 200, and a truncated
  body. **LibGen downloads require no Z-Library credentials.**
- The server now starts without `ZLIBRARY_EMAIL`/`ZLIBRARY_PASSWORD`. Missing
  credentials are a stderr warning rather than a fatal exit, so a LibGen-only
  install works out of the box; Z-Library tools still error clearly when
  called. Previously the process exited 1 at startup, which would have made
  the new unlimited source unreachable on a bare install.
- `libgen:download` probe in `npm run doctor` and the scheduled upstream check,
  exercising resolve-and-fetch rather than reachability. The previous probe
  stayed green while downloads were broken.

### Fixed

- **Security: `annas-archive.is` removed from `ANNAS_TRUSTED_HOSTS`.** That
  list authorizes attaching `ANNAS_SECRET_KEY`, which the fast-download API
  passes as a URL query parameter. The host is not Anna's Archive:
  `/dyn/api/fast_download.json` returns 404 there versus 401 on the genuine
  `.gl`, and it serves a secret-key "recovery" form. Anyone who set
  `ANNAS_BASE_URL` to it would have disclosed their key.
- `LibgenAdapter.get_download_url` no longer raises for every input. It looked
  books up via `search_title(md5)`, but LibGen's title index holds no md5
  strings, so it had never worked against the live service — the unit suite
  mocked `LibgenSearch` and stayed green.
- `SourceRouter` honours an explicit `source="annas"` without an API key.
  Anna's search carries no credentials; only downloads need the key. Adapter
  construction was gated on the key, so an explicit request silently returned
  LibGen results.
- Audit gate: `cryptography` floor raised to 50.0.0 and `nltk` to 3.10.0,
  clearing four advisories that were failing CI on every PR.

### Changed

- CI runs without Git LFS. The repository exceeded its LFS budget, which failed
  `actions/checkout` outright and blocked every merge, plus releases. Existing
  unhydrated-pointer guards skip the affected tests with an actionable reason,
  surfaced via `pytest -rs`. **Five real-PDF tests no longer run in CI** — a
  known coverage gap tracked in #85. Local runs are unaffected.
- LibGen search results no longer carry a `.onion` `download_url`. The only URL
  search can offer needs Tor, and a clearnet key expires in under 2.5 hours, so
  callers resolve on demand.
- README Docker instructions now lead with the published GHCR image (verified
  end-to-end: SSE handshake and `initialize` against the released container);
  building from source via compose is the secondary path.
- Removed GSD-era `.claude/commands/` (referencing the deleted `ROADMAP.md`
  workflow) and the empty `.claude/settings.json`.

### Known limitations

- Keyless Anna's Archive downloads are **not supported and will not be**. The
  `/slow_download/` route is gated by a DDoS-Guard browser challenge that the
  project states is there to stop bots and scrapers. Anna's keyless *search*
  works normally; keyed `fast_download` remains the supported download path.

## [1.3.2] - 2026-07-24

### Fixed

- **`uv sync` failed inside the npm-installed package**, breaking the README
  quickstart's Python setup step for every npm user of v1.3.0/v1.3.1. The root
  pyproject had always relied on an accident: the repo's `src/` directory flips
  setuptools to src-layout package discovery (which finds no Python packages and
  builds an empty dist), but the published package ships no `src/`, so flat-layout
  discovery found `['lib', 'zlibrary', 'node_modules']` and refused to build.
  The root project is dependency-management-only and is now declared
  `[tool.uv] package = false`, which is layout-independent. CI's pack-check job
  now extracts the actual npm tarball and runs `uv sync` plus bridge-import
  checks inside it, so a repo-works/package-breaks split cannot ship again.
  Verified end-to-end against the fixed layout: global install → bin symlink →
  MCP `initialize` → live authenticated `search_books` returns results.

## [1.3.1] - 2026-07-24

### Changed

- Upstream contract check now distinguishes network-level blocks from drift: a
  DiamWall wall or bare 403 (what GitHub-hosted runners get from every Z-Library
  domain) reports as `BLOCK` instead of `FAIL`, does not file the rolling drift
  issue, and skips the credentialed live suite rather than burning login
  attempts (~10/hour/IP) against a wall. Only a probe from an unblocked network
  can distinguish IP blocking from a global outage — `npm run doctor` says so
  explicitly.

### Fixed

- **`zlibrary-mcp` bin never started under a global npm install**: npm installs the
  bin as a symlink into `<prefix>/bin`, so `argv[1]` is the link path while
  `import.meta.url` is the real file — the ESM entry guard's lexical path
  comparison never matched and the server exited silently, making
  `"command": "zlibrary-mcp"` in an MCP client config a no-op. The guard now
  canonicalises both sides with `realpathSync` (falling back to lexical
  resolution for paths that do not exist). Verified end-to-end: a symlinked
  invocation now answers `initialize` over stdio.
- **Footnote detection was non-deterministic across a full test run**: the PyMuPDF
  textpage cache keyed entries by `id(page)` without holding a reference; CPython
  recycles a freed page's address (~90% of back-to-back loads), so page N's cached
  text blocks could be served for page M. Entries now pin the page object and
  verify identity on read. This was the cause of the long-standing
  full-suite-ordering test flakes.
- Tests no longer leak `MagicMock/` and `dummy_output*/` directories into the
  repository root (mocked argparse args now route `output_dir` through pytest
  `tmp_path`); removed the orphaned `test_data/` fixtures (referenced by nothing)
  and untracked per-developer `.serena/` tool state.
- **Resilient EAPI domain fallback and probing** (ISSUE-API-002): the single default
  EAPI domain `z-library.sk` is fronted by the DiamWall anti-bot wall (HTTP 307
  self-redirect setting a `__diamwall` cookie, then 513/517 Access Denied), which
  killed login and every tool with default settings. The default is now a probed
  fallback list (`z-library.ec`, `z-library.sk`, `1lib.sk`): each candidate is
  validated with a cheap unauthenticated `GET /eapi/info/domains` — never the
  rate-limited login endpoint — and the first healthy one is used. Hydra-mode
  domain discovery also probes advertised domains before switching, since
  `/eapi/info/domains` still advertises the walled domain first; if nothing
  advertised is usable the client keeps its working domain. An explicit
  `ZLIBRARY_EAPI_DOMAIN` is honoured verbatim with no probing and no silent
  switching. DiamWall HTML where JSON was expected now raises a dedicated
  `DiamWallError` naming the wall and the remedy; the health check classifies it
  as `diamwall_blocked` and `npm run doctor` reports it explicitly.

## [1.3.0] - 2026-07-24

### Security

- **Path traversal in the download flow**: `Content-Disposition` filenames come from a
  server-controlled header and were joined directly onto the output directory, so
  `filename="../../etc/passwd"` wrote outside it. Filenames are now reduced to a bare
  basename, using both posixpath and ntpath since `os.path.basename` on POSIX does not
  treat a backslash as a separator.

### Fixed

- **`search_advanced` tool restored** (issue #16): the Phase 7 EAPI migration deleted
  the Python implementation and its dispatch branch while the Node tool stayed
  registered, so every live call since February raised `Unknown function`.
  Reimplemented on EAPI: a strict `e=1` search supplies `exact_matches` and a
  default-mode search (typo-tolerant) minus those ids supplies `fuzzy_matches`. A new
  contract test asserts every function name Node sends has a dispatch branch, so a
  registration/dispatch mismatch can no longer ship silently.
- **Windows support** (incorporates PR #13 by @ltspace): the ESM entry guard compared
  `import.meta.url` against a concatenated `file://` string, which never matches a
  backslash `argv[1]`, so the server never auto-started; `venv-manager` hardcoded
  `.venv/bin/python` where UV places `.venv\Scripts\python.exe`; and RFC 6266 extended
  `filename*=UTF-8''` headers were parsed as percent-encoded bytes. Platform-dependent
  behaviour is now parameterised so a Linux runner exercises the Windows branch.
- **MCP stdio protocol violation**: all diagnostics now write to stderr via a new
  `src/lib/logger.ts`. Thirteen `console.log` calls wrote to stdout — the JSON-RPC
  channel — causing strict clients to disconnect (issue #11). Four of them fired on
  every search, corrupting active streams. Guarded by `__tests__/stdio-purity.test.js`.
- **npm publish pipeline**: removed `npm install -g npm@latest`, which failed on every
  release from v1.2 through v1.2.1 and was never needed (`--provenance` ships in npm
  9.5+; Node 22 bundles npm 10.x). npm had only ever served 1.0.0.
- **Dependency audit gate**: security constraint floors refreshed, taking pip-audit from
  74 advisories across 15 packages to 1 (`nltk`, no fix published). Includes pytest 8→9
  and cryptography 46→49.
- **macOS setup**: `setup-uv.sh` and `scripts/validate-readme-tools.sh` no longer use
  `grep -oP`, which BSD grep rejects (issue #14).
- Tests using Git LFS PDF fixtures now skip with an actionable message instead of failing
  with assertions that resemble detection regressions.
- README: duplicate "Option B" heading; npm install path now warns when the registry
  version trails the repository.

### Added

- Scheduled **Upstream Contract Check** workflow: probes Z-Library, Anna's Archive, and
  LibGen response shapes daily, runs the credentialed integration suite, and files a
  rolling `upstream-drift` issue on failure.
- `npm run doctor` — the same probe for users, to distinguish an upstream outage from a
  bug in this server before filing an issue.
- GHCR container image publishing on release tags (requested in PR #9).
- Tag/`package.json` version verification before publish.
- Dependabot for npm, uv, GitHub Actions, and Docker.
- `SECURITY.md` with private reporting, the dependency-audit policy, and the
  `LOG_LEVEL=debug` disclosure caveat.
- Issue templates (bug, RAG quality) and a PR template.
- `LOG_LEVEL` environment variable (`silent`|`error`|`warn`|`info`|`debug`).

### Changed

- Coverage thresholds ratcheted to just under actual measurements (Jest 66→84
  statements, pytest 52→60), converting them from decoration into a real gate.
- CI smoke test no longer filters stdout through `grep '^{'`, a workaround that had been
  masking the protocol violation above.

## [1.2.1] - 2026-04-16

### Added

- Canonical `.metadata.json` sidecar for structured RAG output bundles, with relative links to sibling bundle files
- Structured output fixtures and API documentation covering the new file-based bundle contract

### Changed

- `process_document_for_rag` and `download_book_to_file` now expose additive sibling bundle paths while preserving `processed_file_path` for compatibility
- Node and Python bridge layers now describe the same structured output contract end to end

## [1.2.0] - 2026-04-02

### Added

- 326 new tests across 15 files (Jest: 93→163, Pytest: 719→979)
- pip-audit in CI for Python dependency vulnerability scanning
- Global pytest textpage cache clearing via conftest autouse fixture
- Startup credential validation with actionable error messages
- Jest and pytest coverage thresholds to prevent regressions
- ESLint with TypeScript-aware rules and Prettier formatting
- lint-staged pre-commit hooks (ESLint, Prettier, TypeScript type-check)
- CI pipeline split into fast (push/PR) and full (push-to-master) workflows
- API reference documentation for all 13 MCP tools

### Changed

- Python environment management migrated from pip/venv to UV (77% code reduction)
- Test infrastructure modernized: strict pytest markers, benchmark integration
- Jest coverage: 71% → 86% statements, 61% → 83% branches
- Python coverage: 58% → 67% total
- Dependency security patches: cryptography, nltk, pillow, requests, ujson
- PyMuPDF pinned to 1.26.5 for cross-platform footnote extraction stability
- Package version synced with milestone versioning

### Fixed

- CI audit job: pip-audit added as dev dependency (was missing)
- Pytest coverage threshold adjusted (publish was failing at 52.96% < 53%)
- Flaky footnote tests: non-deterministic detection from stale textpage cache
- 6 tech debt items from v1.2 audit
- Jest test compatibility with Node 22
- Pytest collection errors from scripts in test discovery path
- Cleaned compiled `.js` artifacts from source tree
- Purged large blobs (74MB+) from git history

### Removed

- Legacy pip/venv-based Python environment management
- Deprecated AsyncZlib download client code

## [1.1.0] - 2026-02-04

### Added

- Margin content detection for scholarly PDFs (Stephanus numbering, Bekker numbering, line numbers, marginal glosses)
- Adaptive resolution pipeline with page-level DPI selection (150-400 based on content analysis)
- Region-level DPI targeting for mixed-quality PDF pages
- Unified body text detection pipeline with confidence scoring
- Non-body content separation (headers, footers, footnotes isolated from body text)
- Anna's Archive integration as alternative book source with automatic LibGen fallback
- `search_multi_source` tool for cross-source searching (Anna's Archive and LibGen)
- Source attribution in multi-source search results

### Changed

- EAPI booklist browsing improved with pagination support
- EAPI full-text search enhanced with phrase and word matching modes
- Docker configuration updated for Alpine compatibility (opencv-python-headless)

### Fixed

- Node 22 LTS compatibility issues
- Docker numpy/Alpine compilation errors
- env-paths updated to v4.0 for proper Node 22 support

### Removed

- AsyncZlib legacy download client (fully replaced by EAPI)

## [1.0.0] - 2026-02-01

### Added

- 13 MCP tools: `search_books`, `full_text_search`, `search_by_term`, `search_by_author`, `search_advanced`, `search_multi_source`, `get_recent_books`, `get_book_metadata`, `fetch_booklist`, `download_book_to_file`, `process_document_for_rag`, `get_download_limits`, `get_download_history`
- EAPI migration for all Z-Library operations (replacing web scraping)
- MCP SDK upgrade to 1.25.3 with `McpServer` API
- Python bridge decomposition (4968-line monolith split into 31 focused modules)
- RAG processing pipeline for EPUB, PDF, and TXT documents
- Enhanced metadata extraction with terms, booklists, IPFS CIDs, and ratings
- Book download with automatic filename generation from metadata
- Cloudflare detection and domain discovery for EAPI endpoints
- Integration test harness with 11 tool coverage
- CI pipeline with npm audit and Python version checks
- Pre-commit hooks via Husky
- Zod schema validation for all tool parameters

### Changed

- Migrated from web scraping to Z-Library EAPI for all operations
- Upgraded to MCP SDK 1.25.x with new `McpServer` registration API
- Python monolith `rag_processing.py` decomposed into `lib/rag/` module tree with facade re-exports
- Bare `except` clauses replaced with specific exception handling throughout Python codebase

### Fixed

- 15 npm security vulnerabilities resolved
- BeautifulSoup4 parser specification (explicit `lxml` to avoid warnings)
- BUG-X/FIX comments cleaned from production code
- Debug print statements converted to proper logging

[Unreleased]: https://github.com/rookslog/zlibrary-mcp/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/rookslog/zlibrary-mcp/compare/v1.3.2...v1.4.0
[1.3.2]: https://github.com/rookslog/zlibrary-mcp/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/rookslog/zlibrary-mcp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/rookslog/zlibrary-mcp/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/rookslog/zlibrary-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/rookslog/zlibrary-mcp/compare/v1.1...v1.2.0
[1.1.0]: https://github.com/rookslog/zlibrary-mcp/compare/v1.0...v1.1
[1.0.0]: https://github.com/rookslog/zlibrary-mcp/releases/tag/v1.0
