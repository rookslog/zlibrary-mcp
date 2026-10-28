# Offline agent interface evaluation

2026-10-19. Implements DESIGN-v2.1 §9 (and its explicitly inherited v2 §9).
This directory is evaluation infrastructure, not a new product interface. All
book data and file bytes are synthetic. No model call happens by default.

## Offline checks

From a baseline-compatible evaluator checkout with Node dependencies and its uv
`.venv` available (these commands do not select an interface checkout as A):

Full local Git history is required for the settled-baseline and parent controls.
A shallow clone cannot establish source equivalence; missing historical objects
remain a fail-closed prerequisite error, not a product misclassification. CI's
full-Jest checkouts explicitly use `fetch-depth: 0` to provide that history.

```sh
npm run build
node __tests__/agent-eval/run.mjs --self-test --out test_output/agent-eval-self-test.json
node __tests__/agent-eval/run.mjs --dry-run --driver codex --out test_output/codex-plan.json
node __tests__/agent-eval/matrix.mjs > test_output/agent-eval-matrix.json
npm run test:rehearsal
npm test -- --runInBand
```

The self-test starts the **actual 12-tool A server**, uses a scripted MCP client,
checks the proxy's tool schemas, runs T1–T12's A paths, reads resulting files and
checks fixture identity bytes. It also checks real empty results for unknown
queries, format mismatch, history-only books and empty mode. Jest checks exact
matching, languages, inclusive years, translator separation, cursor/page replay,
wrong-decision argument mutations, usage parsing and comparison bars. Neither is
an agent-performance result. T5/T6/T10 are explicitly outside A's comparison bar.

The settled baseline is `e2517b3e347584333f9c127a0217a3c574f6a22e`. By default,
the runner derives the selected A checkout's **current exact HEAD** only after
proving clean production/config inputs and source equality with that settled
baseline. Evaluator-only descendants therefore work with the default command;
an interface/B checkout cannot silently become A. An explicit common baseline
identity remains supported with `--a-pin SHA --server-root /checkout`; the A
checkout's HEAD must equal that resolved pin. For B without an explicit pin,
`--a-root /baseline-checkout` selects the separate compatible baseline to resolve.
Receipts retain the resolved SHA, selection mode and requested explicit pin.
It records SHA, source tree hashes (including vendored Z-Library), observed tools/list count, characters and UTF-8
bytes; it never checks out, stages or commits anything. Build the selected checkout
before running it. Both A and B must pass the executable build proof below before
the server starts; B remains a separately built worktree.

## Fixtures and isolation

[inventory.json](inventory.json) lists every fixed record, edition, translator,
source, paging row and account-history row. `../rehearsal/fake_catalogue.py` is
authoritative; the JSON is its dated inventory. There are 13 core Z-Library rows,
6 LibGen rows, 15 fixed rehearsal rows, 60 paging rows, and 2 history rows (one
also a rehearsal row). Account-history-only ID 901 is _Being and Time_, never
pre-created on disk and never returned by search. Each test task starts with two
remaining Z-Library downloads, except T11, which starts with zero. Transfers
spend quota at link issuance, even if subsequent acquisition fails; local reuse
cannot decrement this fake-side counter.

LibGen has _Persuasion_ EPUB; Z-Library has PDF. The Graver–Long and Campbell
Seneca editions have distinct IDs, years, detail descriptions and bytes. _The
Glass Orchard_ exists only on LibGen. _Phenomenology_ has 60 stable volumes.
Arendt has three in-range and two out-of-range books. Anna's search is an
explicitly empty account catalogue, allowing ordered fallback without real
network access; it has no configured download key. Browser/challenge routes are
not simulated. The original rehearsal LibGen row retains its reserved advertised
MD5 for compatibility; it is **not an evaluation download target**. All five
evaluation LibGen downloads advertise the actual synthetic payload MD5.

The server receives only fake credentials and an allowlisted environment. Its PYTHONPATH
selects the build’s vendored Z-Library. Schema 5 additionally rejects foreign editable
metadata even if that PYTHONPATH would shadow it; a shared foreign environment is not
accepted. The
rehearsal Python/Node fences reject non-fixture networking. Evaluation's Python
sitecustomize redirects LibGen httpx resolution/transfer URLs to the same TLS
fake; the real production HTTP clients, parsers, acquisition and digest checks
still execute. The fake exposes per-operation arguments and quota spend through
its loopback control endpoint, never login bodies or cookies. Model drivers do
not receive the fake control endpoint. Server diagnostics stay off stdout.

## Campaign matrix and future paid launch

`matrix.mjs --a-root /A --a-pin SHA --b-root /B --out /receipts` prints **15
commands**, never executes them: A/B, Q1 without text copy, Q2 configured order,
and B without history, each on Codex and Claude; Claude additionally has tool
search on. Main Claude arms have tool search off. The optional C arm is deferred.
A runner invocation needs `--paid` to execute a driver; do not use it until the
lead authorizes the real comparison. Example commands for that later campaign:

```sh
node __tests__/agent-eval/run.mjs --paid --arm A --driver codex --server-root /A --a-pin SHA --runs 5 --warm-runs 1 --out /receipts/A-codex.json
node __tests__/agent-eval/run.mjs --paid --arm B --driver claude --server-root /B --a-pin SHA --runs 5 --warm-runs 1 --out /receipts/B-claude.json
node __tests__/agent-eval/run.mjs --paid --arm B --driver claude --tool-search --server-root /B --a-pin SHA --runs 5 --warm-runs 1 --out /receipts/B-claude-search.json
node __tests__/agent-eval/report.mjs /receipts/*.json > /receipts/comparison.json
```

**Cell definition (implementation choice):** one full ordered T1–T12 episode,
with five independently identified replicates. Each replicate starts a fresh
model session for the cold episode, then continues that exact session for a warm
episode (more with `--warm-runs`). This keeps T2→T3 conversational context real;
T6 receives the actual scripted first page/cursor as stated prior context. T6's
setup is excluded from model decisions/usage; its server response is included in
the prompt. Cold/warm labels apply to episodes, not a claim that all twelve tasks
are individually first-turn cold. Each episode uses a fresh artifact subdirectory
to separate prompt-cache effects from pre-existing files. Provider limits are
reset per task, but counters are differenced rather than cleared. The underlying
server survives driver reconnects so refs remain live. Independent replicates
use fresh model sessions and destinations; they share the same server process.

**Q1:** proxy removes only the text copy when structuredContent exists; schemas
stay fixed. **Q2:** `BOOK_SOURCE_DEFAULT_ORDER=libgen,zlibrary` versus unset;
`--default-order-env NAME` can adapt a future build (recorded in receipt).
`ZLIBRARY_OUTPUT_ROOT` bounds B's destinations. **History-disabled:** proxy
unregisters history on tools/list and rejects calls; account history is not
replaced with disk receipts. T7 must admit inability; any download/quota is wrong.

## Driver pins and settings

[drivers.js](drivers.js) constructs exact argv arrays (no shell interpolation).
`--dry-run --driver NAME` records the full command, environment changes and model.
Offline-observed CLIs: **Codex 0.165.0**, **Claude Code 2.1.303**. Launch refuses a
version mismatch. Default model IDs are **gpt-6.1-sol** and
**claude-opus-5-5**, both with explicit high effort, default temperature. Model
availability and the Anthropic full model ID have **not** been established by a
paid invocation; `--model EXACT_ID` is recorded when the lead supplies a different
pin. No silent fallback model is configured.

Claude flags: `-p --model claude-opus-5-5 --effort high --output-format stream-json
--verbose --setting-sources '' --strict-mcp-config --mcp-config /CELL/mcp.json
--tools '' --allowedTools 'mcp__books__*' --permission-mode dontAsk
--disable-slash-commands --system-prompt 'Evaluation NONCE…' --session-id UUID`.
Warm turns replace `--session-id` with `--resume UUID`. Tool-search-on uses
`ENABLE_TOOL_SEARCH=true`, `alwaysLoad:false`, `--tools ToolSearch`, and also
allows `ToolSearch`; main arms use `ENABLE_TOOL_SEARCH=false`, `alwaysLoad:true`.
`MCP_CONNECTION_NONBLOCKING=0` fixes startup behavior. Every
`DISABLE_PROMPT_CACHING*` model override is explicitly removed from the inherited
environment. Caching is on, not set to the string `false` (which can still disable
it). The actual driver request behavior is untested without paid execution.

Codex flags: `exec --ignore-user-config --ignore-rules --skip-git-repo-check
--json --model gpt-6.1-sol --disable code_mode_tool_search --disable code_mode
--disable multi_agent --disable memories --disable plugins --disable shell_tool
--disable apps --disable image_generation --disable browser_use --disable computer_use`,
plus recorded `-c` values for high effort, read-only sandbox,
`approval_policy="never"`, books-only tool approval, web search disabled,
the single stdio `mcp_servers.books` proxy and fixed developer
instructions. Only the last completed Codex `agent_message` supplies the final
answer; earlier progress messages are excluded. Prose inside that final message
still fails the JSON-only format gate. Warm turns use `exec resume UUID` with
the same settings. Local `codex features list` shows `code_mode_tool_search` and
`code_mode` available;
legacy `tool_search` and `tool_search_always_defer_mcp_tools` are removed and are
**not** passed. A supported provider-cache flush/off control was not found. Tool
exposure/resume were exercised only on the lead's supplied Codex 0.162.1 smoke
path. On 2026-10-28, Codex 0.165.0 `--version`, `exec --help`,
`exec resume --help`, and `features list` retained every switch listed above;
no flag was renamed or substituted. This updates launch admission only. The
0.165.0 model event and tool-exposure behavior remains untested offline. The
cumulative tracker below deliberately stays restricted to 0.162.1. Any Codex
command/web/file-change event invalidates the cell; fixture metrics are captured
at the broker, not inferred from model narration.

Sources checked 2026-10-19: [Codex non-interactive usage](https://developers.openai.com/codex/noninteractive)
(`turn.completed.usage`, JSONL and resume), [configuration reference](https://developers.openai.com/codex/config-reference)
(removed tool-search switches), [Claude MCP](https://code.claude.com/docs/en/mcp#configure-tool-search)
(search env and alwaysLoad), [Claude env vars](https://code.claude.com/docs/en/env-vars)
(non-empty DISABLE_PROMPT_CACHING disables caching), plus installed CLI `--help`,
`--version`, and Codex `features list`. No claim about account settings follows.

## Scoring, receipts and strict cold limit

`tasks.js` is the explicit per-arm oracle table; `oracles.js` scores tool **and
argument** decisions with original argument values, selection provenance, file
identity bytes, edition/source/format/year/cursor correctness, required details,
read-only tasks and honest negative answers. Calls, invalid calls,
ref_unavailable, per-task fake quota spend, final answers and raw tool replies
are saved. File paths in receipts are ephemeral: the bytes are checked before
cleanup, not promised to remain available. No provider quota is real.

Tokens come only from driver usage: Codex `turn.completed.usage` (now normalized
from cumulative totals per the round-1 repair below); Claude's final
`result.usage`, without double-counting streaming assistant usage blocks. Claude
input total includes uncached + cache-read + cache-write; Codex input total
already includes cached reads. Missing usage is an error, never zero. Input
cache writes not exposed by Codex are null, not guessed. Neither driver exposes
cached output; `outputCached:null` is distinct from generated `outputUncached`.
Raw events and incremental receipts persist under `OUT.artifacts`, including
uniquely named per-attempt output/stderr and a latest-output alias on driver errors. Temporary TLS credentials, servers
and files are removed on normal completion/error.

A fresh session is **not** evidence of an empty provider cache. A unique cell
nonce reduces reuse but cannot establish which definitions were cached. Claude's
first-request cache-read counter is retained. Codex's final turn aggregate does
not establish first-request cache status, so its strict cold eligibility stays
unknown. `report.mjs` deliberately returns **inconclusive** rather than passing
the strict cold bar without that evidence. Resolving this needs a measured
driver/transport receipt from the lead, not a fabricated cache-disable flag.

The report gives median and min/max spread, cold input totals and summed warm
input totals separately, wrong-decision/zero-where-A-zero and overall/per-task
success checks, tool-list size, no-quota absolutes, and history usage. A history
call in any enabled arm means retain history. Zero observed calls in an
incomplete matrix does not authorize removing it. Scripted/dry receipts are
never compared as model results. Live providers, tunnels, ChatGPT rendering,
want lists, and strict first-request Codex cache telemetry remain untested.

## Round-1 repair contract (supersedes the original scoring/scope wording above)

2026-10-19. Authority: `EVAL-FIXTURES-sol/FIX-EVAL-r1.md`,
`ZLIB-EVAL-GATE-astra-r1.md` and root's `ZLIB-EVAL-GATE-DISPOSITION-r1.md`
under campaign evidence. Root corroborated six concrete evaluator counterexamples;
this repair does not claim a new paid matrix or a fresh review gate.

### Answers are not paths (EVAL-R1-01)

Every scored row now retains `pathCorrect` separately from `answer.status`.
T2 checks translator/year associations, T4 the actual fake-side remaining quota
and LibGen cap, T7 affirmative membership in the **observed account history**, and
T12 the observed matching titles. T5 and the A filename/history-disabled negative
controls also have explicit fact contracts. An unknown answer cannot pass merely
because its tool trace is correct. `success:null` / `manual-review` means the
prose cannot be confidently scored by this deterministic evaluator; it is not
silently counted as success or omitted from a denominator. Such evidence is
ineligible for an automatic comparison until resolved outside this scorer.

The runner appends a JSON-only answer-shape instruction for these tasks. The
instruction is identical across every arm sharing a prompt, including B/B′ T7
and T10. Neutral keys/types do not name a scored mode, source, tool or capability.
It uses placeholders, never the expected translations, years, quota
number or result titles. Arrays must contain all matching observed facts; the
example is an item shape, not a count. `answers.js` compares exact keys/types and
facts; swapped editions, duplicate translators, extra contradictory properties
and duplicate JSON keys cannot pass. The original scripted control's _whole_
canonical sentences/list grammar also remains supported. Arbitrary prose,
including correct-looking text followed by a contradictory sentence, goes to
manual review. This is intentionally not a general semantic judge. Other
acquisition tasks still require their real artifact and decision checks.

T4 A accepts either `unknown` or `none` for the LibGen cap; B requires `none`.
Exact legacy JSON schemas remain a compatibility grammar, not driver instructions.
B′ T7 accepts the neutral `{"answer":null}` or the full-string inability grammar
(`I cannot tell.` / `I cannot determine that.` / the original account-history
unavailable sentence). `false` is not an inability claim. A read-only limits call
is permitted on T5 and B′ T7; search/acquisition remains wrong. T10 uses the same
neutral instruction in every arm, but B acquisition success still depends on its
real named inbox artifact, not an extra fact-answer requirement. N4's exact-string
fact mismatch policy is deliberately unchanged.

Each call contributes at most one wrong decision while retaining every reason
and its call index. Task-level missing-evidence/provider violations remain separate
diagnostics. T4 argument checks apply only to limits calls. In Q2 T11, omitted
sources require the declared default to put LibGen ahead of exhausted Z-Library
and an observed `routing.served_by: "libgen"`; T8 still requires an explicit
LibGen-only request. T9 starts at three remaining downloads and retains three
distinct same-task search requirements; its A Z-Library control uses `search_books`,
never `search_multi_source` with the unsupported `source: "zlibrary"`.

Only arm B receives an A-bar verdict. `variantComparisons` reports B versus Q1,
Q2 and B′ under identical driver/search/settings identity, descriptively without
an A-bar verdict. Every phase/task includes a quota-spent distribution. B′ T7
correct inability has its own count, not equivalent task-completion success.
History removal is expressible only with all three enabled B arms admitted for
Codex/search-off and Claude/search-off/on, no within-cohort compatibility drift,
and zero history use anywhere in those cells. Any admitted enabled use retains it.

[Driver environment identity](driver-environment.md) documents the exact value /
presence-only key inventory, primary sources, unconfirmed candidates and limits.
Missing or drifting per-row records are refused; differing cell digests do not
pair. This does not attest settings/auth file contents or resolved provider state.

### Evidence admission and bars (EVAL-R1-02/03/05)

`admission.js` now requires receipt schema 5 (schemas 2/3/4 are legacy diagnostic), the exact completed measured status,
`sourceStable:true`, `fencePassed:true`, no receipt/task driver/fence/source/cleanup
failure, fixed model/CLI/build/settings, complete nonnegative usage and decision
metrics, and the exact unique `(run, phaseIndex, task)` grid. There must be five
replicates; each has T1–T12 at cold index 0 and at every configured warm index.
Sessions remain identical through a replicate and differ between replicates.
CLI version/requested model fields must agree throughout. Build SHA, baseline
pin and all production tree digests must be present and consistent.

Invalid, legacy, failed, ambiguous-answer and incomplete receipts remain in
`diagnostics`, with rejection reasons, but are never aggregated as eligible
model evidence. Multiple same-key receipts are refused as duplicate or
incompatible complete cells, not concatenated. A/B may have different build
identities and the explicitly selected arm changes, but their driver/model/CLI,
baseline and other comparison settings must agree. No receipt format can attest
an unreported server-side model alias change; these are requested model pins.
The runner now persists final source/fence/cleanup failures as failed status.

Root settled the success-estimator interpretation: gate empirical success
**counts/fractions** overall and per common task, not binary-success medians.
Tokens, calls and wrong decisions retain medians and min/max spread. When A's
median wrong decisions for a task is zero, **every** B observation for that task
must be zero. The nine common tasks exclude T5/T6/T10 as before; B's T5 additionally
has a B-only negative guard requiring correct inability, no forbidden
search/download, zero wrong decisions and zero quota. A's T5 remains outside
relative totals. Invalid evidence or a failed guard cannot become `bar met`.

### Attempts, including rejected calls (EVAL-R1-04)

The broker traces the attempted name/arguments before checking exposure. Unknown
or disabled calls record rejection/error and `forwarded:false`, without invoking
the production server. They count as calls, invalid calls and off-oracle decisions.
The real-A scripted self-test also sends an unknown request through the actual
proxy and checks one retained invalid/off-oracle attempt with no forwarding;
this is not a B-runtime integration claim.

`reconcileAttempts` consumes explicitly identified Codex `mcp_tool_call` events
for server `books` and Claude `mcp__books__…` tool-use/result events. Started and
completed forms are merged by driver call ID; completed events may use arguments
already observed at start. Matching against broker calls is a name/argument
multiset: each broker call can match only one driver attempt. A rejected call
seen at both layers is counted once. A recognized CLI-side rejection with no
broker call is added to the trace with its exposed arguments. If the name/ID is
known but arguments are unavailable, the attempted call remains diagnostic and
telemetry is marked incomplete; arguments are not invented. An apparently
successful unmatched attempt invalidates the cell rather than disappearing.

The drivers may suppress attempts before emitting any identifiable event. That
signal is unavailable: zero _recognized_ extra attempts is not an attestation
that a CLI never rejected anything internally. Opaque CLI errors remain driver
failures, not invented tool names/counts. These parsers are deterministic fixture
tests, not a new paid confirmation of either driver's event coverage.

### Usage scope is CLI/path bounded (EVAL-R1-06)

Root's supplied two-turn smoke and exact-OK probe recorded Codex 0.162.1 totals:
input/cache-read/output **39031/19328/118**, **78605/58240/236**,
**101125/77952/241** in the same session. The +5 output for exact OK corroborated
cumulative, not per-invocation, counters on that path. This lane reuses those
numeric receipts only; it does not rerun models.

`usageFrom('codex')` preserves raw cumulative counters/snapshots and selects the
last total, never sums snapshots. A version-bounded tracker starts from zero for
the first invocation, then differences the exact resumed session's counters
before task/episode aggregation. T7 deltas are 39574 input, 38912 cache-read,
662 uncached and 118 output; the third turn is 22520/19712/2808/5. Cache writes
are differenced when exposed; missing writes stay null. Negative/incomplete
counters, disappearing optional counters, derived negative uncached input,
unexpected session IDs and uncorroborated CLI versions fail closed. Receipts
retain the raw totals, prior totals, normalized `scope:session-delta` and source
provenance. Admission independently recomputes the deltas in exact task order.

Claude's `result.usage` remains invocation-scoped, not differenced on an assumption
borrowed from Codex. No Claude process ran in this repair. Neither driver exposes
a cached-output split here; outputCached stays null. **Codex first-request cache
eligibility remains unknown**, and its aggregate cumulative counters cannot close
the strict cold bar even after normalization.

### Offline repair checks

```sh
node --experimental-vm-modules node_modules/jest/bin/jest.js __tests__/agent-eval/ --coverage=false --runInBand
node __tests__/agent-eval/run.mjs --self-test --out /private/tmp/eval-r1-self-test.json
```

`regression-controls.json` preserves the root's actual A read-only traces and
recorded numeric usage probe with provenance. `receipt-controls.js` constructs
synthetic complete receipts solely for positive/negative report controls; these
are not observations about model performance. The tests reproduce all six
reviewer/root counterexamples plus malformed/duplicate grids, warm indices,
CLI/model/session mismatch, missing usage, ambiguous facts, delta resets and
cross-layer rejection deduplication. The real A positive exercise remains
separate. No B runtime/full matrix/live-provider boundary is closed by these
checks; root owns the fresh round-2 gate and git operations.

## Connector round-1 proof and capture contract

2026-10-19. Authority: `FIX-CONNECTOR-r1.md` and lead
`ZLIB-EVAL-CONNECTOR-DISPOSITION-r1.md`; this is a connector repair loop, not
a third native review or a model-performance result.

**EVAL-C1-01:** Before starting even the fixture/MCP server,
`build-proof.js` checks selected source/config bytes against the pinned Git blobs,
rejects dirty/untracked production inputs, and copies those checked TypeScript
inputs plus `tsconfig.json`, `package.json` and `package-lock.json` into disposable
scratch. It invokes only the existing local TypeScript compiler directly, with
the pinned config, scratch `--outDir dist`, `--incremental false` and
`--noEmitOnError true`. No package scripts, installs or model-supplied commands run.
It compares the exact runtime file set and SHA256 of every file against selected
dist. Wrong/stale/missing/extra files and symlinks fail before launch. Source,
config, HEAD, compiler package identity and selected outputs must stay unchanged
across verification; source/runtime identity is checked again before MCP launch.

Proof covers executable `.js/.mjs/.cjs` and runtime `.json/.node/.wasm` outputs.
Maps, declarations and incremental metadata are excluded from byte equality
because they are not executed and maps may describe scratch-relative paths.
Unclassified outputs fail closed. The **full selected dist hash** (including that
metadata) remains recorded and checked for evaluation drift. Compiler version and
package-content digest, pinned input manifests and both runtime manifests are
retained as `build.artifactProof`. Existing installed dependencies are used;
this is not a package-registry or dependency-supply-chain attestation.

Schema 5 retains that concrete, internally consistent build proof and its source/
full-dist bindings at admission. Legacy/unproven/malformed receipts stay diagnostic,
even if they contain an arbitrary syntactically valid dist hash. Receipt JSON is
not a cryptographic signature; the trusted local producer performs the actual
comparison. Startup failures after pin resolution persist failed receipts with
no completed cell. Matrix generation resolves one exact A identity from `--a-root`
and writes that same `--a-pin` and absolute `--a-root` into **every** generated A/B command. Copy that SHA
into all manually written paid commands too; do not independently infer A from B.

**EVAL-C1-03:** T9 requires three distinct successful same-task search calls,
each returning the downloaded title and identity (including source and B ref)
before its corresponding download. Interleaved search→download remains valid;
all three searches need not precede the first download, and no literal query
spelling is required. One broad search, unrelated/failed searches, Pride-only
repeats and prior-episode selections alone cannot satisfy the three titles.
Download count, identity/observed selection and exact synthetic bytes stay strict.

**EVAL-C1-04:** Drivers wait for process/stream `close` before persisting/parsing
stdout/stderr. A five-minute invocation deadline, two-second TERM→KILL grace and
two-second post-exit/post-KILL drain deadline bound failures. Timeout, spawn error,
signal and never-closing inherited pipes cannot become successful measured usage.
Each attempt retains unique stdout, stderr and `.capture.json` telemetry before
usage parsing; incomplete output is retained but not promoted. Schema 5 requires
complete, closed, successful capture for every row. The synthetic child tests
exercise this real path without launching either model CLI and retain their raw
attempts at the temporary evidence directory printed by Jest.

Original six round-1 scoring/usage/attempt controls, the canonical inbox alias
positive/strict negatives, and strict-cold **inconclusive** behavior remain in
the focused suite. No new cross-vendor, B model, strict-cold or merge acceptance
follows from these local controls. Lead owns clean B preflight and connector round 2.

## Connector round-2 common evaluator, Python metadata and process-group proof

Schema **5** additionally requires all three stable identities: production/build
inputs, the actual **executing** common evaluator, and the selected Python runtime
metadata. Schemas 2/3/4 remain diagnostic only. Different production A/B HEADs are
expected; compatibility uses the common evaluator manifest and normalized Python
metadata digests, not equality of those HEAD locators.

`evaluator-identity.js` enumerates **all** files in `__tests__/agent-eval/` and
`__tests__/rehearsal/` from its own module root (not `--server-root`). Actual bytes
must match committed Git blobs; tracked dirt hidden by status, missing files,
symlinks and ignored/untracked additions fail. Only generated CPython-named `.pyc`
files under `__pycache__` are excluded, not code placed inside that directory.
Prompts, scorers, fences, fixtures, tests and docs are conservatively included.
Schema5 also binds root `package.json` and `package-lock.json` as clean committed
common evaluator bytes; selected-build manifests remain independently bound.
The check runs before any fake/model, immediately before MCP, and at evaluation
end. Relocated identical manifests compare equally even with different HEADs;
different scorers and end drift are ineligible. During implementation an
uncommitted harness correctly refuses self-test; the final committed-head positive
control belongs to the lead, not a cleanliness waiver.

Pinned production inputs and default-A source equivalence now include
`pyproject.toml` and `uv.lock`. Scratch TypeScript compilation remains independent
of Python/uv/environment readiness. `python-runtime.js` separately executes the
selected **project-local** `.venv/bin/python` launcher (`Scripts/python.exe` on
Windows), never its resolved base binary in place of the launcher. It records
that binary's hash and interpreter/version/platform/cache tag, uv binary/version,
manifest hashes, import selection and a canonical distribution metadata inventory.
The inventory includes selected site packages and the validated local zlibrary
source metadata exposed by the declared PYTHONPATH; that source record cannot
substitute for the installed editable's direct URL. Origins are validated before
root normalization. A foreign editable is refused even with local import shadowing.

Readiness invokes only bounded, read-only:

```text
uv sync --check --frozen --offline --no-default-groups --group dev --no-config --project /selected/root
```

No extras, default groups, install fallback or silent sync. UV/Python redirection
and config environment variables are not inherited; the project environment and
launcher are explicit, user site is disabled, and probe HOME/config/cache/log
working directories are disposable. The same explicit Python descriptor is
passed to the fake through a backward-compatible optional rehearsal argument and
to the evaluator's MCP environment. Production routing is unchanged. Metadata is
rechecked before MCP/end; missing, failed, foreign or drifting proof is ineligible.
Root paths normalize only after validation, so A/B relocation remains compatible.

**Scope limit:** this is selected interpreter and resolved dependency **metadata**
proof, not installed package payload/.pth/stdlib/shared-library identity,
hermeticity, supply-chain attestation, or independent attestation of every Python
process. Change-and-restore between checks is not established by these snapshots.
Receipt JSON is structural evidence from a trusted local producer, not a signature.

Model-driver capture is POSIX-only and refuses unsupported platforms **before**
CLI version/model invocation; this does not alter product Windows support.
Detached `ChildProcess.pid` is the sole trusted owned PGID. Normal drained zero-exit
leaders are checked for residual members too. Only a drained, zero-exit, clean
close gets a **100 ms natural-exit window**, measured against one monotonic
deadline; observations never reset that bound. Only an observed ESRCH before the
bound proves absence. EPERM remains unknown (even if ESRCH arrives later), never
success. A group still alive at the deadline is residual; later strict cleanup
cannot promote that capture to complete. Timeout, drain timeout or residual
groups trigger one idempotent bounded TERM → KILL → observation sequence;
leader exit never cancels escalation. ESRCH is gone; EPERM/error or a group still
observable after the deadline is unknown/failed. Signals stop once gone. Readers
stay attached through bounded drain/cleanup; unique raw streams and capture JSON
are retained before parsing. Successful admission requires drained close and
proven group cleanup; timeout/residual capture remains incomplete even if cleanup
succeeds. This contains ordinary same-PGID descendants only, not deliberate setsid
escape or numerical PGID reuse; no broad process scans, stdout-derived signal
PIDs or unref-based escape are used by capture.

Successful capture admission additionally requires TERM/KILL both false, reason
`closed`, and observations ending in `gone` with no unknown/invalid states. The
test-only residual teardown has a separate bounded observation window: transient
EPERM after KILL is observed until ESRCH; persistent EPERM fails loudly. A teardown
failure does not stop attempts for other owned children. Evidence is copied in
`finally`; failures are collected and rethrown, not used to admit a capture.

T1/T8 now require a successful corresponding current-task search before each
download, matching title/source/identity and B ref, with no literal query rule.
T3/T10 retain intended prior-task reuse. T11 requires successful limits followed
by successful LibGen search returning nonempty source/title plus A id or md5 /
B ref and identity. Empty Anna's or LibGen, errors, and unusable rows do not pass;
no URL/hash/format requirement or actual download is added. T9's distinct-search
matching and valid interleaving remain unchanged.

Only real compiler tests/hooks have a 30-second Jest deadline; the runtime
compiler's 60-second limit and every assertion remain unchanged. These local
repairs are not connector-3, hosted-CI, model/bar, merge or release acceptance.

## Connector round-3 selected Node runtime-code/data proof (schema5)

Comment **4318878598**: `node-runtime.js` is one builtin-only producer for the
executing common evaluator **and** selected `--server-root`. `node`/`nodeEnd`
and `build.node`/`build.nodeEnd` must be verified/stable and bind their own
common Git or selected artifact input manifest hashes. Common Node digest is
comparison identity; selected-build Node digest is per-cell identity only, so
intentionally different A/B installations are not conflated with common code.
Missing, legacy, malformed, failed or drifting proof is diagnostic, not measured.

The helper binds the actual `process.execPath` binary/version/platform/arch/ABI,
root manifests, canonical logical production dependency/active-peer/optional
edges and nested package locators. Every selected package's `package.json` and
classified executable/runtime-data bytes are hashed, including both SDK ESM and
CJS exports, transitives, JSON, native/WASM assets and declared extensionless bins.
Docs/tests/examples/fixtures/maps/declarations/cache and dev runtime are skipped
BEFORE payload reads; dev package metadata may be needed by npm inventory.
No `.env`, `.npmrc`/builtin npmrc, credentials, key or auth-fixture payload is
read. Unknown required assets, missing/invalid edges, local/file/workspace/link
deps, interior links, outside-install resolutions and unsupported mappings refuse.
This is a bounded selected graph, not a whole-install content scan.

### Exact inventory/read boundary

The recorded method is guarded npm-bundled **Arborist.loadActual at the actual
selected root**, not npm CLI config loading, a custom resolver or root-view npm
ls. Explicit options: offline, ignoreScripts, no audit/fund/global/workspaces,
packageLock enabled and owned scratch cache. Minimal environment sets selected
Node PATH and owned scratch HOME/TMPDIR/XDG_CONFIG_HOME/cache, without inherited
npm/Node overrides. Owned copies of the two pinned manifests and empty config
files are scratch support only, not graph-selection evidence. Tool identity
records npm/Arborist versions, CLI/entry/guard hashes and hashes of observed
allowlisted npm tool code/metadata content reads. The selection policy and
normalized recipe are part of the digest. Diagnostics are bounded error codes
and graph locators, never raw config/error contents.

`node-inventory-guard.cjs` preloads path-content guards for fs readFileSync,
readFile, openSync, open, createReadStream and promises.readFile/open; numeric
content handles and raw read/readSync/readv/readvSync APIs fail closed. Metadata
ancestor traversal is permitted, not ancestor content. Allowlisted content is
only pinned root manifests, installed package/hidden-lock metadata, npm runtime
code/metadata and owned scratch. Writes outside scratch, child/lifecycle/worker
launch and net/tls/dns/dgram entry points are denied and audited; any observed
write/network/child/unsupported-fd attempt makes proof fail. The trace retains
paths/operation/allowed, not contents. Synthetic original-config sentinel reads
exercise all seven supported path methods and deny before bytes. Native Node
ESM and CJS resolution anchors known SDK imports from the real evaluator/build
contexts; it executes no dependency code during inventory.

This is **observed-tool JavaScript API instrumentation**, not adversarial OS
isolation; native internal module resolution is not a complete content-read
trace. No OS-sandbox/hermetic guarantee follows. The Node permission-mode
ancestor-lstat failure and isolated-root-view mixed physical/logical graph
failures remain retained evidence, not silently retried by weakening controls.

### Supported shared topology and sampling limits

Ordinary hidden-lock-absent installs are supported. The sole permitted install
symlink is the top-level node_modules mapping to a physical directory whose
basename is **node_modules**, with an **existing npm-accepted hidden lock**.
Arborist shared/hidden-lock-absent topology is unsupported and fails closed
(lead ruling 2026-10-19T21:37:43Z); there is no fsParent mutation/resolver patch.
Interior links and arbitrary shared-install basenames are refused. Physical
roots are retained locally for drift, normalized only after bounds checks;
portable logical locators/edges/bytes determine identity. Shared fixture controls
also actually import SDK + nested/transitive/peer dependencies with default
Node realpath behavior. No real installation is changed to manufacture support.

Checks run before fake, before MCP, immediately before model-child spawn and at
end; each helper takes two samples. The owned MCP fence preload is bound by
common source identity. NODE_OPTIONS/NODE_PATH and loaders/preloads/conditions/
preserve-symlinks overrides refuse; only runner vm-modules/input-type flags
are supported. Scratch TypeScript compiler checks never require Node inventory.

Qualification: sampled installed runtime-code/classified-data + graph consistency,
**not** full package/tarball/registry/supply-chain trust, OS/shared-library,
hermeticity or loaded-memory identity. Static SDK imports precede startup; races
and change-and-restore between samples are not excluded. Receipt JSON is
structural evidence from the trusted local producer, not a signature. The lead
owns clean committed-head startup, independent full/severe checks, exact-head
hosted CI and post-cap/model admission; no model was launched for these controls.

### Reopened declaration/classification guard

The initial19-file handoff missed a transitive package whose `main` pointed to
`docs/runtime.cjs`: the real import marker changed while that excluded payload
was not in the digest. The revised helper preflights **all selected package
metadata before any runtime payload read**, including root declarations, and
refuses excluded/forbidden/unclassified/escaping runtime `main`, applicable
conditional `exports`, and local `imports` targets. Literal target components
are checked with metadata only; directory/interior-link targets refuse.
This never reads an excluded target to classify it and does not resolve imports.

Applicable branches are default Node `node`, `node-addons`, `import`, `require`,
`default`, plus `module-sync` when the selected Node supports require_module.
Types/typings metadata and types-only branches are nonruntime, not exceptions
for `.d.ts` files placed in a runtime branch. Inactive custom branches (such as
source/@zod/source) stay inactive: custom `--conditions` is already refused.
Only local imports mappings are supported; unsupported target forms fail closed.
Conservative wildcard checks require one wildcard in the final target segment,
with safe static prefixes/roles and no escape syntax. They do **not** enumerate
or resolve arbitrary substitutions. Existing real-root native SDK anchors bind
the known requested paths; this is not full dynamic import/dataflow closure.
Legacy extensionless main declarations receive static role/topology checks;
classified runtime files remain byte-bound by the existing package walk, without
a new extension/fallback resolver. Policy identity includes this guard.

The initial READY, hashes and lead's old excluded-entry control are retained as
history; the revised READY identifies final bytes and new red/green/full checks.
The same guarded-read, unsupported shared-no-hidden-lock, no OS/hermetic/memory/
supply-chain claim and lead-owned committed startup/CI boundaries still apply.

## E fixtures: offline diagnostics and runner selection

E1–E18 are separate from the unchanged T1–T12 matrix. Each diagnostic case
starts a fresh MCP process, fake provider, refs and artifact root. The self-test
runs pairs of independent isolated cases, preserving every check; receipts
separate actual current-interface success, hypothetical future-contract
witnesses, replay controls, alternative good paths and executed wrong moves.

```sh
node __tests__/agent-eval/run.mjs --e-self-test --out /tmp/e-self-test.json
node __tests__/agent-eval/run.mjs --suite E --cold-session --dry-run --out /tmp/e-plan.json
```

`--e-cases E4-query,E11-filter` restricts diagnostics or runner selection.
The default model plan additionally includes E4-second, E11-first, E6-rows,
E16-first and E13-real-timeout. E admission uses the same 180-second compiler
allowance as the E self-test; the non-E compiler default remains 60 seconds.
The E dry plan records command arguments, environment edits and the prompt,
using explicit placeholders for the runtime socket, downloads and nonce. The E
runner uses `runs` and `session-mode`; it does not require or consume `warm-runs`.
The runner prints and records C/F totals: current cases (passed/failed) and
future cases (gap-only/also wrong). Classification uses the declared task or
context status, never the presence of gap marks: a current E10 run that sends
`French` and earns a gap remains a current failure. A dry plan has zero scored cases.
Runner `--session-mode shared` keeps compatible MCP/driver sessions alive;
`--cold-session` forces fresh ones. Reader, account and real-timeout environment
changes force a new process even in shared mode. Model execution remains behind
explicit paid admission, committed evaluator identity, exact build reproduction,
runtime identity checks and network fences. No diagnostic invokes a driver or
qualifies a settled-A comparison. The original A baseline remains unchanged.
The E model path is implemented but deliberately not executed in this lane.

`answers.js` supplies E answer fields. Scoring compares observed fields, not an
exact sentence, key order, JSON whitespace, query spelling or an agent-selected
limit. Unknown answer keys are rejected; declared optional variant fields remain
allowed. No-tools answers still fail. E4 and E11 prefer integer fixture identities
from the selected receipt. The downloaded field also accepts a string identity
or exact observed title; E4 also accepts the full observed translator. These
are documented alternative facts, not mnemonic placeholders (`graver` and
`niggli-1981` are no longer answers). Independent selection, translator/year,
provider, quota, exact bytes and receipt checks still apply, including when a
title alone cannot distinguish editions.
Answer strings normalize case, whitespace and `&`/`and`; booleans and numbers
retain their types, except the documented downloaded string-ID alternatives.
E7 `retries` counts repeats after the first explicit-source search attempt, so
the timeout plus successful search means one retry. One preceding
`sources_required` rejection is excluded because it issued no provider request;
more than one rejection, or one after searching began, fails. This bounded
validation recovery is not another search retry. E5 offers are optional and accept Z-Library spelling;
blocked Anna calls support both `searched:true` (attempted) and the legacy
`searched:false` (not completed). Both require the actual blocked attempt;
invoked alternatives and invented searches remain wrong.
E-only scoring permits a structured `sources_required` error when a later
explicit-source search succeeds or yields a declared task outcome. Transport
errors, abandoned calls and missing task evidence still fail. E18 keeps its
no-default-order error exercise; E1-recover and E4-recover execute it as well.
Every declared expected error specifies `requestIssued`; contradictory issuance
and transport failures cannot be hidden by that declaration.

E4 accepts a translator-targeted query and Graver-second order. E6 includes
Anna-empty and Anna-rows controls. E8 accepts honest untried status or the
observed result of an explicit later Anna search. E11/E16 fixed pools put the
target beyond the default ten rows, with target-first and alternate-limit
controls. Correct year-filter/direct-target solutions also pass. Prompts do not
instruct a solver to turn a page. All E-only catalogue/history/LibGen records
are mode-gated; healthy T search pools remain the pre-E pools.

E1/E2 separate complete-book/translator records from review/author decoys;
E3 explicitly requests the review. Acquisitions check exact bytes, a matching
completed receipt and provider/quota evidence. E12 really restarts the process,
recovers the same edition through a fresh search. The expired-ref failure is
tested but need not be attempted when the restart is already known. Targeted searches
that identify the right record can acquire it without an extra detail call;
both paths have executed good controls. E1/E2/E3/E12 also execute targeted searches
requesting `["libgen", "zlibrary"]`: the matched selected row must be served by
Z-Library, while issuance, transfer, bytes and receipt checks remain unchanged.
Synthetic controls relabel the matching row as LibGen and must fail selection;
the fake LibGen catalogue cannot serve these same target identities. At least one bad
trace per E fixture executes a wrong call with its own provider events/quota/
files; replay/tamper controls add narrower checks.

E13's header seam is a fallback. `E13-real-timeout` uses the production acquire
coroutine with a controlled external transfer callback: it issues a real fake
link, GETs its bytes and remains pending. A short test budget drives the actual
asyncio timeout, commit inspection and cancellation branch. A source-bound
line receipt records issued=true/published=false/task pending and cancellation.
This qualifies that production branch under the controlled callback; it does
not establish production transfer timing or a real provider timeout. Recovery
is scored through boolean destination/receipt/fresh-intent/no-auto-retry fields.

E5/E17-person replay a frozen HTTP 403 check. E7 injects one read timeout after
an actual loopback request, waits the declared 50 ms and repeats once for rows.
E17 pacing/runtime/ceiling are frozen HTTP outcome replays. This checkout has
no browser route: missing structured reason/wait/reset scope remains an explicit
future-contract gap. No browser/runtime absence or visit accounting is measured.
E9 has 20 LibGen rows, two French; honesty about ignored filters is distinct from
filter enforcement. E10 `fr` normalization/unknown disclosure and E18 malformed
success-without-books classification also retain absent future contracts.
E10's default prompt requests French editions without dictating an input code;
its current scripted path and E10-canonical use `french`. E10-fr and E10-unknown
have explicit saved-input prompts and future-only reasons (normalization and
unknown-value disclosure are absent). E18 no-account discovery is current.
Every wrong mark in a future-only good trace must match a declared contract gap.
Hypothetical edited replies test oracle reachability only; they are never counted
as current-interface successes or model evidence.

E14/E15 alone expose reader tools, using valid deterministic PDFs with labels
i, 40, 41 and Chapter 8 at PDF index 3. E14 has no outline; E15 has one section
and exceeds a 1,000,000-byte limit. Returned image and resource-link blocks remain
intact; E14 requires the chapter image, while E15 may use the map without images.
E15 accepts section IDs or any valid page range, including all three pages when
the resulting PDF fits. A success-shaped oversize
refusal is classified honestly; a link does not establish client delivery.
An existing PyMuPDF interpreter is required (`EVAL_PDF_PYTHON` overrides discovery);
no install occurs. Temporary artifacts are scored before cleanup.

E3 deployment-parity boundary: no E default source order was invented. The
active tunnel uses profile zlibrary-local; its profile lives below ~/.config,
which this lane is forbidden to read. The checked repository launcher sources
a forbidden secrets/env file and has no literal default-source assignment.
This does not establish the deployed effective source order. Lead must supply
an allowed non-secret launcher/config copy before claiming deployment parity.
