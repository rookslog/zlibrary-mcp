# Repo Health Assessment & Forward Roadmap

> **HISTORICAL — superseded 2026-08-11. Do not plan from this document.**
>
> The forward-roadmap half of this file is superseded by the milestone + map-issue
> scheme in [docs/RELEASE_PROCESS.md](../../docs/RELEASE_PROCESS.md). The health
> assessment and its evidence remain accurate as of 2026-07-24 and are worth reading
> for the reasoning they record.
>
> This document is *why* that scheme exists. Its roadmap section proposed a v1.4
> themed "source layer as a first-class citizen", which was turned into a GitHub
> milestone. v1.4 was later re-chartered as "any source can be downloaded" (map issue
> #75) and shipped under that scope on 2026-08-10. Nobody reconciled the two, so a
> released version sat at 5 open / 0 closed while the issues that actually shipped it
> carried no milestone. The milestone has since been retitled *Source layer as a
> first-class citizen (unslotted)* — it is a real theme, with no release slot.
>
> Roadmap intent belongs in a milestone and its map issue, where the tracker can
> contradict it. A document cannot be contradicted by anything.

**Date:** 2026-07-24
**Baseline commit:** `0289b78` (`release: v1.2.1`, 2026-04-16)
**Scope:** open PRs and issues, CI/release infrastructure, test coverage, upstream
integration viability, public-facing polish, multi-source expansion, feature roadmap

---

## Executive summary

The codebase is in considerably better shape than its public presentation
suggests. At the baseline, 1,114 tests passed (165 Jest, 949 Python), the
architecture is clean, and seven of eight CI jobs were green. The problems were concentrated almost entirely
in the **seam between the repo and its users**:

| | Finding | Status |
|---|---|---|
| P0 | Server violated the MCP stdio contract — 13 `console.log` calls wrote to stdout, the JSON-RPC channel | **Fixed** |
| P0 | Every npm publish since v1.2 failed; npm serves 1.0.0 from April 2025 while README advertises `npx zlibrary-mcp` | **Fixed** (needs a release cut) |
| P0 | CI red on master since 2026-04-03 — solely the `audit` job, 74 stale dependency advisories | **Fixed** |
| P1 | Nothing detected upstream drift; 26 live integration tests existed but never ran | **Fixed** |
| P1 | Windows unusable (3 bugs) — plus an unreported path traversal in the same code | **Fixed** (PR #13 incorporated, with tests) |
| P2 | macOS setup script failed on `grep -oP` | **Fixed** (issue #14) |
| P2 | Four overlapping documentation trees, load-bearing docs factually stale | Partly addressed |

The single most consequential finding is the first, because it explains the most
visible user complaint. **Issue #11 ("Is this still a working MCP?" — server
disconnected)** is not an upstream problem or a setup problem: the server printed
non-JSON to the protocol stream.

---

## 1. Is it still working? Is anything broken?

### 1.1 The premise has changed: this is no longer DOM-based

The framing in `CLAUDE.md` — "Z-Library has no public API, using EAPI via web
scraping" and "Hydra Mode: domains change frequently" — described the pre-Phase-7
architecture. The EAPI migration (Feb 2026) replaced HTML scraping with JSON
endpoints. Measured on the current tree:

```
BeautifulSoup selectors in zlibrary/src/zlibrary/  →  0
```

Z-Library access is now `POST /eapi/book/search`, `/eapi/user/login`,
`/eapi/info/domains` — form-encoded requests returning JSON. That is a
substantially more durable contract than CSS selectors, and it means the project's
central historical risk has already been retired.

**The remaining DOM-fragile surface is narrow and worth naming precisely:**

| Surface | Fragility | Blast radius |
|---|---|---|
| `lib/sources/annas.py` — 1 selector on Anna's Archive search HTML | High | `search_multi_source` only; router falls back to LibGen |
| `lib/rag/processors/epub.py` — 5 selectors | Low | EPUB internal XHTML, a stable standard |
| Z-Library EAPI JSON shape | Medium | Everything — silent field renames are the real risk, not layout |

The genuine exposure is not scraping anymore. It is that **an undocumented JSON
contract can change field names without any visible error**, and the entire test
suite mocks those calls. That is what section 4 addresses.

### 1.2 Live verification was not possible from this environment

This session's sandbox blocks egress to all three sources at the proxy:

```
z-library.sk      CONNECT tunnel failed, 403
annas-archive.org CONNECT tunnel failed, 403
libgen.is         CONNECT tunnel failed, 403
```

So **whether the upstreams currently respond is unverified**, and no amount of
local testing would establish it. Rather than guess, this work added a probe that
answers the question on demand and on a schedule (section 4). Someone with
unrestricted network access should run `npm run doctor` and record the result; the
scheduled workflow will then keep it current.

### 1.3 What was actually broken

**(a) MCP stdio contract violation — the cause of "server disconnected".**

The stdio transport uses stdout as the JSON-RPC channel; the specification
requires a server write nothing else there. Reproduced against the built server
at the baseline commit:

```
Log directory 'logs/' ensured.
Z-Library MCP server (ESM/TS) is running via Stdio...
{"result":{"protocolVersion":"2025-11-25",...}
```

Two non-JSON lines precede the first protocol message. Worse, four of the
thirteen offending calls fired **per search request**, injecting text into an
active stream mid-session, and they echoed user queries while doing it.

The CI smoke test filtered stdout through `grep '^{'` before asserting — which
masked exactly this bug for as long as it existed. Both the bug and the mask are
now gone, with a static scan plus a live handshake assertion guarding the
regression.

**(b) The release pipeline never worked.**

All four publish runs failed. Every one failed at the same step — and not at
build or test:

```
Run tests                            success
Upgrade npm for provenance support   failure   ← npm install -g npm@latest
Publish with provenance              skipped
```

`npm@latest`'s `engines` field outran the Node 22 runner. The step was never
necessary: `--provenance` has shipped in npm since 9.5, and Node 22 bundles npm
10.x. Consequences worth stating plainly:

- npm has only ever had **1.0.0**, published 2025-04-11.
- README's "Option A: npm (recommended) — `npm install -g zlibrary-mcp`" installs
  a build 15 months stale.
- The v1.2 release notes claim "**npm package:** `npx zlibrary-mcp` (v2.0.0)" — a
  version that has never existed anywhere.
- Local `git tag` is empty; five tags exist only on the remote, and a **draft**
  release for v1.2.0 sits at `untagged-2f888cb62d7894f40bf5`.

**(c) The audit gate, not the tests, kept master red.**

Job-level results for the latest master run: 7 of 8 green, `audit` failing at
`pip-audit`. 74 advisories across 15 packages, because the security floors in
`tool.uv.constraint-dependencies` had gone stale. Raising each floor to its
lowest fixed version:

```
74 advisories in 15 packages  →  1 advisory in 1 package
```

The remainder is `nltk` PYSEC-2026-597, which has no published fix. The upgrade
crossed two majors (pytest 8→9, cryptography 46→49) with **no test changes
required**.

**(d) Platform support gaps, and a path traversal hiding among them.**

Windows was unusable for three independent reasons, all correctly diagnosed in
PR #13. macOS setup failed on `grep -oP` (GNU-only PCRE mode), fixed here along
with a second occurrence in `scripts/validate-readme-tools.sh` that only escaped
notice because CI runs on Linux.

The same `Content-Disposition` code carried a security bug that PR #13 quietly
fixed without mentioning it. The parsed filename is joined onto the output
directory and originates in a server-controlled header, so a response bearing
`filename="../../etc/passwd"` wrote outside the download directory. Confirmed at
the baseline: `Path("/downloads") / "../../etc/passwd"` resolves outside, and the
stream was written to the resolved location.

**(e) A diagnostic trap in the test suite.**

PDF fixtures live in Git LFS. A clone without LFS leaves ~130-byte pointer files,
and five tests then failed with assertions that read like detection regressions:

```
AssertionError: assert 'ERROR' == 'MIXED'
```

The actual cause was `FileDataError: no objects found`. These now skip while
naming the cause and the fix. This matters more than it looks — it is precisely
the kind of false signal that trains a maintainer to distrust the suite.

---

## 2. Open PRs and issues: recommended disposition

### PR #13 — Windows compatibility (`ltspace`) → **incorporated; close with thanks**

Three real bugs, each correctly diagnosed. Verified against the current tree:

| Fix | Confirmed present at baseline? |
|---|---|
| ESM entry guard compares `import.meta.url` to `` `file://${process.argv[1]}` `` — always false on Windows, so the server never auto-starts | Yes — `src/index.ts` |
| `venv-manager` hardcodes `.venv/bin/python`; UV puts it at `.venv\Scripts\python.exe` on Windows | Yes — zero `win32` references in `src/` |
| `Content-Disposition` parsed by `split('filename=')`, breaking on RFC 6266 `filename*=UTF-8''` | Yes — `zlibrary/src/zlibrary/eapi.py` |

A good contribution. Two things held it back: **no tests** for three
platform-specific parsing/path behaviours, and CI that had never run (GitHub shows
`action_required`, the fork-PR approval gate).

It also contained a **security fix its own description did not mention**: the parsed
filename is joined onto the output directory and comes from a server-controlled
header, so `filename="../../etc/passwd"` escaped it. Confirmed against the baseline
— `Path("/downloads") / "../../etc/passwd"` resolves outside the directory and the
stream was written there. That deserved to be called out, not slipped in.

**Resolution:** the fixes are incorporated on this branch with 46 tests, following
this repository's own precedent for PR #9 → PR #10 ("Based on PR #9 by @zaggash").
Attribution to @ltspace is in the commit message, the tests, and the changelog. The
sanitizer additionally applies `ntpath` alongside `posixpath`, because
`os.path.basename` on POSIX does not treat a backslash as a separator — the PR's
`os.path.basename` call would have let a `..\..\x` payload through a Linux server
untouched.

Both platform behaviours are now parameterised by platform
(`venvPythonSegments(platform)`, `isProcessEntryPoint(url, argv1)`) rather than
reading `process.platform` inline, so a Linux runner exercises the Windows branch.
This is the structural fix: these bugs reached users because the broken code only
executes on the platform it is broken for.

Close #13 as incorporated, thanking the contributor and noting the traversal finding.

### PR #12 — "Configurando o MCP da Z-Library" → **close**

Not a contribution. A fork pushed **this repository's own `.claude/` directory
back at it** — 30 files, ~11,000 lines, comprising GSD agent definitions, slash
commands, and internal workflow docs. Its base is `e405d764`, four months stale.
Nothing in it is authored change. Close with a brief, friendly note; if the author
intended to ask a configuration question, point them at the README and issue
templates.

### Issue #11 — "Is this still a working MCP?" → **root-caused; close after release**

The reporter was right and the cause was ours, not theirs. Their screenshot shows
a stdio disconnect, which the stdout pollution fully explains. The v1.2 release
notes recorded this as "reporter's setup path works with updated docs" — that
closed the report without fixing the defect. Worth replying with the actual cause
once a release carrying the fix is published.

### Issue #14 — macOS `grep -oP` → **fixed here**

Reporter's suggested fix was the right one and is what was implemented.

### Issue #9 — "would be nice to build an image on new release" → **addressed**

A GHCR job now publishes semver-tagged images on release tags.

### Housekeeping

- Delete or publish the dangling **draft release** for v1.2.0.
- `git fetch --tags` locally; the working clone has none of the five remote tags.
- Add the `upstream-drift` label, which the scheduled workflow files against.

---

## 3. Test coverage: where the real gaps are

Current state — healthy in aggregate, uneven in distribution:

| Suite | Result | Coverage |
|---|---|---|
| Jest | 165 passed | 86% statements, 83% branches |
| Pytest (fast) | 949 passed, 8 skipped | 62% statements |

Coverage percentage is not the useful lens here. **What the tests do not exercise
is:**

1. **Every third-party call.** The entire upstream integration is mocked. Section
   4 is the answer, and it is the highest-value coverage work available.
2. **The multi-source fallback path.** `lib/sources/router.py` reports 97% line
   coverage, but the branch that matters — Anna's quota exhausted mid-request,
   falling back to LibGen, and reconciling two different result shapes — is the
   one users hit when a source degrades. Line coverage flatters it.
3. **Platform branches.** Was zero coverage of Windows/macOS path and parsing
   behaviour, which is why PR #13's bugs reached users at all. Now covered for the
   venv path, entry-point detection, and `Content-Disposition` parsing; other
   platform-dependent behaviour remains untested.
4. **`lib/python_bridge.py`** — the widest surface in the codebase and the
   dispatch point for all 13 tools.
5. **Cross-language contract.** Jest mocks the Python bridge; pytest tests Python
   directly. Nothing asserts that the JSON shape TypeScript expects is the shape
   Python emits. Phase 19's structured bundle contract is exactly the kind of
   thing that drifts silently here.

The coverage thresholds (Jest 66%, pytest 52%) sit well below actual coverage.
Ratcheting them to just under current values converts them from decoration into a
real regression gate.

---

## 4. Infrastructure added in this pass

Each item exists because a specific failure above had no detector.

**Upstream Contract Check** (`.github/workflows/upstream-check.yml`) — daily
probe of each source's live response *shape*, plus the 26-test credentialed
integration suite, filing a single rolling `upstream-drift` issue on failure.
Deliberately never gates a PR: an upstream outage is not a reason to block a
merge. Anna's and LibGen are optional (the router falls back); Z-Library failures
are actionable.

**`npm run doctor`** (`scripts/check_upstream.py`) — the same probe, for users.
The distinction between "the server is broken" and "the upstream moved" is
invisible from an MCP client, and until now every such case arrived as a bug
report. The bug template asks for this output first.

**stdout purity guards** (`__tests__/stdio-purity.test.js`) — a static scan
rejecting `console.log` in `src/`, and a live handshake asserting every stdout
line parses as JSON-RPC. The CI smoke test no longer filters stdout.

**Dependabot** (npm, uv, actions, docker) — the audit gate rotted because nothing
kept floors current. `pymupdf` is excluded because its pin is deliberate and
documented.

**Release integrity** — tag/`package.json` version check before publish; GHCR
image publishing alongside npm.

**`SECURITY.md`** — private reporting, and a written dependency policy: any
advisory with a published fix must be resolved by raising a floor, never by adding
an ignore. Each of the three surviving ignores carries its reason inline in CI.
Also documents that `LOG_LEVEL=debug` echoes search arguments to stderr, which
clients capture.

**Issue and PR templates** — routing the recurring failure modes, and flagging the
stdout constraint to future contributors so the P0 cannot silently return.

---

## 5. Public-facing polish

**What is already good:** clear README with badges and a Mermaid architecture
diagram, `docs/api.md` covering all 13 tools, a CI-enforced check that README and
`src/index.ts` agree on the tool list, CONTRIBUTING.md, keep-a-changelog
CHANGELOG.

**What undercuts it:**

1. **The install instructions do not work as advertised.** "Option A: npm
   (recommended)" installs 1.0.0 from April 2025. Until a release is cut, this is
   the single most damaging thing on the page. It is also self-inflicted trust
   damage: a user who follows the recommended path gets a build that predates
   every feature the README describes.
2. **Two "Option B"s.** The Installation section has Option A, Option B (source),
   and Option B (Docker).
3. **Load-bearing docs are factually wrong.** `CLAUDE.md` describes DOM scraping
   and lists as "top priorities" four issues that are all resolved (ISSUE-002 was
   closed by the UV migration; ISSUE-005 by the retry manager). It says "Working
   on: `master` branch". This is the first file an AI assistant or contributor
   reads, and it misdescribes the architecture.
4. **Four overlapping documentation trees** — `docs/` (40+ files), `claudedocs/`,
   `.claude/`, `.planning/` — with real duplication (three roadmaps: `.claude/`,
   `.planning/`, and milestone archives) and stale one-off analyses
   (`docs/rag-output-qa-report-rerun-20250429.md`). For a public repo this reads
   as sprawl and buries the ~6 documents a user actually needs.
5. **No SECURITY.md, no issue templates** before this pass — for a project that
   handles user credentials.

**Recommended sequence:** cut a release so the README is true → fix the duplicate
heading and rewrite `CLAUDE.md` against the current architecture → consolidate
`docs/` into `docs/{guides,reference,adr,archive}` and move superseded analyses
into `archive/`.

---

## 6. Expanding beyond Z-Library

The foundation is better than expected: `lib/sources/` already has an abstract
`SourceAdapter` (search / get_download_url / close), a `UnifiedBookResult` model,
Anna's Archive and LibGen adapters, and a `SourceRouter` with quota-aware
fallback — 703 lines, all tested.

**The problem is that it is a parallel universe.** Z-Library does not implement
`SourceAdapter`; it goes through `python_bridge.py` directly. So of 13 tools, 12
are Z-Library-only and one (`search_multi_source`) reaches the router. Metadata
enrichment, booklists, term search, and the RAG pipeline are all wired to
Z-Library specifically. A second source therefore cannot inherit the features
that make this project distinctive.

**The strategic move is to make Z-Library an adapter like any other.** Wrap the
EAPI client in `SourceAdapter`, register it in the router, and let every tool
route through it. That converts multi-source from a single tool into a property
of the whole server, and it removes the single point of failure: if Z-Library
blocks a user's region, everything else keeps working.

**On which sources to add next — the most valuable ones are the legitimate ones.**
The current three all sit in legally contested territory, which caps who will
adopt this in an institutional setting. Adding sources with documented, stable,
public APIs changes the project's character:

| Source | API | Why it matters |
|---|---|---|
| **Open Library** | Documented REST, no key | Authoritative bibliographic metadata; would improve *every* result, not just its own |
| **Project Gutenberg** | Gutendex, stable | ~75k public-domain texts, clean EPUB — ideal RAG input, zero legal ambiguity |
| **DOAB / OAPEN** | OAI-PMH | ~90k open-access scholarly monographs — directly serves the philosophy/humanities use case this pipeline was built for |
| **arXiv** | Documented, rate-limited | Preprints; different shape (papers not books) so it tests the abstraction honestly |
| **HathiTrust / Internet Archive** | Documented | Scanned scholarly corpus; exercises the OCR pipeline against material it was designed for |

Gutenberg and DOAB together give the server a fully legitimate operating mode.
That is worth real weight: it is the difference between a tool an institution can
adopt and one it cannot.

---

## 7. Feature roadmap

Ordered by ratio of user value to risk.

### v1.3 — Trust and reach *(mostly landed in this pass)*

The theme is making the project's claims true.

1. ~~stdout purity~~ **done**
2. ~~release pipeline~~ **done** — remaining: cut v1.3.0, verify npm and GHCR
3. ~~audit gate~~ **done**
4. ~~upstream drift detection~~ **done**
5. ~~Windows support~~ **done** — PR #13 incorporated with tests, plus the
   path-traversal fix its description omitted
6. Reply to and close #11, #14; close #12; clean up the draft release
7. Rewrite `CLAUDE.md`; fix README's duplicate Option B
8. Ratchet coverage thresholds to just under current actuals
9. Phases 20–21 from the existing `.planning/ROADMAP.md` (quality scoring harness,
   CI reporting) — already scoped, unblocked, and complementary

### v1.4 — Source layer as a first-class citizen

10. Z-Library implements `SourceAdapter`; all tools route through `SourceRouter`
11. Open Library adapter for metadata enrichment across all sources
12. Project Gutenberg + DOAB adapters — a legitimate operating mode
13. Per-source health surfaced as an MCP tool, backed by the same probe
14. Result deduplication and merge across sources (same work, several sources)
15. Contract tests per adapter — recorded fixtures for shape, live tests for drift

### v1.5 — RAG output as the product

16. Citation-grade output: stable page anchors, quotable spans, verifiable
    provenance (the standing ask in the closed issue #7)
17. MCP **resources** and **prompts** — currently only tools are implemented, so
    two-thirds of the protocol surface is unused. Exposing processed bundles as
    resources lets clients reference a corpus without re-reading it through tool
    calls, which is a natural fit for what this server produces.
18. Streamable HTTP transport natively, replacing the SuperGateway shim
19. Download queue with quota awareness across sources
20. Cursor-based pagination for search results

### Deliberately not proposed

- **A hosted/shared instance.** Credentials are per-user and quotas are per-account;
  a shared deployment would pool both. Wrong shape for this tool.
- **Reviving `get_book_by_id`.** ADR-003 deprecated it for good reasons that have
  not changed.
- **Bundling OCR models.** `ocrmypdf` and `opencv` already inflate the dependency
  tree and account for most of the transitive advisory surface. Keep OCR optional.

---

## 8. Verification of this pass

```
Jest                177 passed  (12 suites, +2 new: stdio purity, platform compat)
Pytest (fast)       983 passed, 8 skipped, 184 deselected, 7 xfailed
pip-audit           1 advisory in 1 package  (was 74 in 15)
npm audit           passes at --audit-level=critical
eslint + prettier   clean
docs-check          13/13 tools documented
stdout handshake    every line valid JSON-RPC, including at LOG_LEVEL=debug
```

**Not verified, and needing someone with unrestricted network access:**

- Live reachability of Z-Library, Anna's Archive, and LibGen — sandbox egress is
  proxy-blocked (403 CONNECT). Run `npm run doctor`.
- The credentialed integration suite — requires real credentials.
- The publish workflow end to end — only a real tag push will prove it, though
  the failing step has been removed and the surviving steps all passed previously.
