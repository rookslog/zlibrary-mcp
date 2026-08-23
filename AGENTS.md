# AGENTS.md

The working contract for any coding agent in this repo — Codex, Claude Code, or
otherwise. It is the single canonical copy; `CLAUDE.md` imports this file rather than
restating it, so there is nothing here to drift out of sync with a second version.

## What this is

A Model Context Protocol server that finds books, downloads them, and turns them into
files a RAG pipeline can use. Node.js/TypeScript MCP layer (`src/`) over a Python bridge
(`lib/`) that does the Z-Library API work and document processing.

`VISION.md` states the seven invariants and the non-goals. Read it before proposing
anything architectural — it is short, and it is the document that says what this project
refuses to become.

## Setup and commands

Prerequisites: Node 22+, Python 3.10+, and [uv](https://docs.astral.sh/uv/). The Python
side uses **uv, never pip**.

```bash
npm install          # Node deps
uv sync              # Python deps into .venv/ (or: bash setup-uv.sh)
npm run build        # tsc + validates that every Python bridge file exists
```

| What | Command |
|---|---|
| Node tests | `node --experimental-vm-modules node_modules/jest/bin/jest.js` |
| Python tests | `uv run pytest -m "not slow and not integration and not performance" --benchmark-disable -rs` |
| Full Python suite | `uv run pytest -rs` |
| Lint | `npx eslint src/` and `npx prettier --check src/` |
| Health / drift check | `npm run doctor` |
| Release-record audit | `npm run audit:release` |

**`npm test` does not run pytest.** It runs Jest with coverage. The two suites are
separate and a green `npm test` says nothing about the Python side — which is where most
of the actual logic lives.

**Five real-PDF tests do not run in CI** (issue #85): the repo exceeded its Git LFS
budget, so CI checks out with `lfs: false` and guards in `__tests__/python/conftest.py`
skip those tests with a stated reason. Locally, `git lfs pull` hydrates them. Do **not**
run `git lfs install` — it writes hooks that conflict with the repo's Husky-managed
`core.hooksPath`.

## Invariants that break silently

These are the ones where a plausible-looking change passes every test and breaks
production.

1. **stdout is the protocol channel.** Under the stdio transport stdout carries JSON-RPC
   and nothing else. Use `logger` from `src/lib/logger.ts`, which writes to stderr. A
   single `console.log` in `src/` disconnects strict MCP clients.
   `__tests__/stdio-purity.test.js` fails the build if one appears — do not weaken it.
2. **Files, not payloads.** Tools return paths to artifacts on disk, never raw document
   text through the context window. This is invariant 1 in `VISION.md`, not a style
   preference.
3. **Python scripts live in `lib/`, not `dist/`.** The build does not copy them. Runtime
   resolution walks `dist/lib/` → `dist/` → project root → `lib/`. Use the helpers in
   `src/lib/paths.ts` (`getPythonScriptPath`, `getPythonLibDirectory`) rather than
   hand-rolling a relative path. Rationale: [ADR-004](docs/adr/ADR-004-Python-Bridge-Path-Resolution.md).
4. **Unit tests mock every third-party call**, so the suite stays green after real
   integrations break. Upstream drift is caught by `.github/workflows/upstream-check.yml`
   and `npm run doctor` — not by the tests. Green tests are not evidence the world hasn't
   moved (invariant 6).
5. **The root `pyproject.toml` sets `package = false`.** Without it, the repo's `src/`
   directory flips setuptools to src-layout discovery while the published npm package
   (which ships no `src/`) falls back to flat-layout and refuses to build, breaking
   `uv sync` for every npm-installed user. Do not remove it.

## Architecture

- `src/index.ts` — MCP server entry point and tool definitions
- `src/lib/zlibrary-api.ts` — bridge to Python via PythonShell
- `src/lib/venv-manager.ts` — uv environment lifecycle
- `lib/python_bridge.py` — core Z-Library operations
- `lib/rag_processing.py` — EPUB/TXT/PDF processing
- `lib/sources/` — source adapters
- `zlibrary/` — vendored fork of sertraline/zlibrary with custom download logic

Two behavioural decisions that are easy to reverse by accident:

- **Downloads start from a search result, and there is no direct-from-ID path.**
  `download_book_to_file` takes the `bookDetails` object a search returned.
  `lib/python_bridge.py::download_book` then routes multi-source results through
  `SourceRouter` and Z-Library results through `EAPIClient.download_file`, whose link
  comes from the JSON EAPI. **Neither path scrapes a book detail page** — the
  detail-page scraping described in [ADR-002](docs/adr/ADR-002-Download-Workflow-Redesign.md)
  predates the Phase 7 EAPI migration and no longer exists in the code. The part of
  ADR-002 that still holds is the search-result-first workflow, not the mechanism.
- **`get_book_by_id` is deprecated** as unreliable ([ADR-003](docs/adr/ADR-003-Book-ID-Lookup-Deprecation.md)).
  Find books with `search_books`.

The full tool list lives in `README.md` and is checked against the code by
`scripts/validate-readme-tools.sh` in CI — add a tool, update the README in the same
commit or `docs-check` fails. Retry, timeout, and circuit-breaker tuning is in
[docs/RETRY_CONFIGURATION.md](docs/RETRY_CONFIGURATION.md); the implementations are
`src/lib/retry-manager.ts`, `src/lib/circuit-breaker.ts`,
`src/lib/python-runner.ts`, `lib/sources/config.py`, and `lib/sources/net.py`.

Z-Library publishes no public API. Since the Phase 7 EAPI migration (Feb 2026), access
goes through undocumented **JSON** endpoints (`/eapi/book/search`, `/eapi/user/login`,
`/eapi/info/domains`) — there are zero BeautifulSoup selectors left in `zlibrary/`. The
remaining DOM-fragile surfaces are `lib/sources/annas.py` and EPUB internals. Domains
rotate frequently, so `discover_eapi_domain()` resolves them at runtime.

## How releases are tracked

**A release is a GitHub milestone whose description names its map issue** (`Map: #N`).
The milestone indexes what shipped; the map issue narrates why. A theme with no release
slot carries no version number — it is titled `(unslotted)`.

The rules and the failures that produced them are in
[docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md). They are enforced by
`npm run audit:release`, which also runs inside `npm run doctor` and weekly in CI.

**Read priorities from GitHub milestones, not from any document in this repo.** That is
the entire point of the scheme. On 2026-08-11 the tracker was found holding two
conflicting definitions of "v1.4" — one derived from a roadmap document, one from map
issue #75 — of which only the second shipped. A hand-maintained copy of the plan is what
drifted, so this file deliberately does not keep one.

```bash
gh issue list --milestone "v1.5 — Anna's Archive is a real source"
```

## Working agreements

**Destructive git operations require approval first.** Before any `git filter-repo`,
`git rm`, `git reset --hard`, force-push, history rewrite, or bulk deletion: search the
codebase for references to the affected paths, present what will break, and wait for
explicit confirmation. This applies to "cleanup" too — a file that looks stale may be a
test fixture or a script input. One step at a time; never chain destructive operations.

**Merging is not autonomous.** Opening a pull request is where an agent's authority ends.
Do not merge and do not enable auto-merge without an instruction in the current session.
Auto-merge being available repo-wide is not permission to use it.

**Pull request review is automated and app-side.** The Codex code review connector
(`chatgpt-codex-connector`) runs on the first push to a PR. Subsequent pushes do **not**
re-trigger it — re-request by commenting `@codex review`. Reviews take a while; an empty
review list minutes after opening means *not posted yet*, never *not configured*.
CodeRabbit was retired 2026-08-11; its comments on older PRs are historical.

**Commits follow conventional-commit format**, and carry no AI attribution — no
`Co-Authored-By` for a model, no "generated with" trailer, in commits or PR bodies.

**Branches**: `feature/`, `fix/`, `chore/`, `docs/`. A branch older than two weeks is a
defect with a clock on it, and `npm run audit:release` will say so.

**Secrets** live in the environment, never in code or committed files. `ZLIBRARY_EMAIL`
and `ZLIBRARY_PASSWORD` are **required for Z-Library operations only**, not for the server
as a whole: `src/index.ts::validateCredentials` deliberately warns and continues when they
are absent, because LibGen search and download need no account. A credential-free LibGen
setup is supported — do not turn that warning into fatal startup validation.
`ZLIBRARY_MIRROR` is optional.

## RAG pipeline work has a stricter process

Anything touching the RAG pipeline follows `.claude/TDD_WORKFLOW.md`: acquire a **real**
PDF exhibiting the feature, write ground truth into `test_files/ground_truth/`, write the
failing test against the real file (no mocks), then implement. Verify side-by-side
against the source document and check the budgets in
`test_files/performance_budgets.json`. Quality criteria are in
`.claude/RAG_QUALITY_FRAMEWORK.md`.

## Where the documentation is — and which of it lies

| Need | Read |
|---|---|
| What the project refuses to become | `VISION.md` |
| Current state of sources and routes | `CONTEXT.md` |
| Known problems | `ISSUES.md` |
| System structure | `.claude/ARCHITECTURE.md` |
| Code patterns | `.claude/PATTERNS.md` |
| Troubleshooting | `.claude/DEBUGGING.md` |
| Git and PR workflow | `.claude/VERSION_CONTROL.md` |
| CI/CD | `.claude/CI_CD.md` |
| Decisions | `docs/adr/` |

New documentation goes in kebab-case, in the directory that matches its kind: session
notes in `claudedocs/session-notes/YYYY-MM-DD-topic.md`, research in
`claudedocs/research/{topic}/`, architecture analysis in `claudedocs/architecture/`,
specs in `docs/specifications/` (SCREAMING_SNAKE), decisions in `docs/adr/ADR-NNN-Title.md`.
Date the artifacts; never date the living documents — git tracks those.
`claudedocs/README.md` is the directory index.

There are ~890 markdown files in this repo and a good number of them describe a state
that no longer holds. Two known traps:

- `claudedocs/architecture/repo-health-and-roadmap-2026-07-24.md` is **historical**. It
  carries a banner saying so. Its reasoning is worth reading; its plan is not current.
- Anything under `claudedocs/` is a dated session artifact, not a live specification.

When a document and the code disagree, the code is right and the document is a bug.

## Two terms with sharp edges

**"Keyless" is banned.** Anna's Archive has one *server-side* download route: keyed
`fast_download`. The free route sits behind a DDoS-Guard challenge, and the
operator-cookie route was ruled out in #84 — DDoS-Guard binds the cookie to the issuing
IP inside `__ddg9_`, so a transplanted cookie is rejected exactly as an absent one is.
Anna's **key-free search** works and is why Anna's remains a source at all. Writing
"keyless" conflates *no API key* with *no human in the loop*, and that conflation already
caused approved work to be recorded as cancelled.

**A second route is in scope: the browser-resident session** (operator ruling, measured
in #142). A real browser on the operator's machine holds the clearance and issues search
and download requests from inside it — nothing is exported to another client, so #84's
IP-binding finding does not apply to it. This reverses the "permanently out of scope"
clause that #76 and #95 carried: non-API Anna's access is a hard requirement, because
Anna's aggregates LibGen, Z-Library and IA and uniquely carries the scholarly editions
that are the reason it is a source at all.

Read that honestly rather than as a re-scoping. It does route around an anti-abuse control
Anna's operates deliberately, and Anna's states its reason plainly — *"browser verification
for our slow downloads, because otherwise bots and scrapers will abuse them."* What keeps
this on the right side of that claim is that #144's rate limiting ships **in the same pass**
as the route rather than after it, bounded to the personal-use scale #95 fixed: 10–15 books
typical, 30 maximum, per roughly a four-hour reading session. A route that shipped without
those limits would make the politeness claim rhetorical, which is the one outcome this
paragraph exists to prevent. #76 stays closed as written; what changed is the clause, not
the finding.

The reversal is scoped to that route and nothing wider. Fingerprint work is in scope only
where it keeps a real, person-established browser session from being walled for its
automation markers. A **machine-solved challenge** — an unattended headless solver, or
spoofing standing in for a browser session nobody opened — is still not the route being
built, and it is not merely deferred: #142 measured that headless fails while holding
clearance that works headful minutes earlier. "No human in the loop" remains ruled out, as
the `CONTEXT.md` glossary says.

**LibGen needs no account and has no daily limit**, which is why it matters: it is the
fallback for Z-Library's ~10 downloads/day ceiling. Failover across mirrors (`li → vg →
la`) is driven by bytes actually served, not by key resolution — a mirror can return a
valid key while its CDN node is dead.
