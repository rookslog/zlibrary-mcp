# ADR-011: Promote Z-Library to a Source Adapter

**Status:** Accepted

**Date:** 2026-08-12

**Decision owner:** rookslog (accepted by this ADR's merge; implementation remains #40)

**Issue:** [#40](https://github.com/rookslog/zlibrary-mcp/issues/40)

## Decision summary

Promote Z-Library behind the same source boundary as Anna's Archive and
LibGen, but do not widen the current MD5-shaped abstract base class or make
Z-Library the first entry in a hard-coded fallback chain.

Replace the current source boundary with:

1. a small required adapter contract for search, acquisition, and lifecycle;
2. an opaque, source-scoped reference carried by every search result;
3. optional capability protocols for metadata, quota, history, recent-book,
   curated-list, and specialised-search operations; and
4. a registry-backed `SourceRouter` that follows caller- or operator-supplied
   source order and reports what it did without ranking results.

The MCP surface remains file-oriented and search-result-first. Existing tool
names remain compatibility façades during migration, but their Python paths
must enter through `SourceRouter`. No direct-from-ID download path is added.

## Decision state and tracker reconciliation

**This ADR records no release placement.** `AGENTS.md` requires priorities to be
read from GitHub milestones rather than from any document in this repo, and a
document that names one becomes a second, slower copy of the release plan — the
exact drift the milestone scheme exists to prevent, and which this repo has
already suffered once. Read #40's milestone from the tracker:

```bash
gh issue view 40 --json milestone
```

One reconciliation is worth recording, because it is a contradiction a reader
will hit and cannot resolve from the tracker alone: **#95's prose calls this
work a successor leg named "v1.6."** That sentence is historical narrative, not
the release index, and it does not override #40's live milestone. It is noted
here so the next reader stops at "the document is stale" rather than concluding
the tracker is wrong — not to assert what the placement is.

## Context

The source abstraction is only partial on current `master`.

- `lib/sources/base.py::SourceAdapter` requires `search(query)`,
  `get_download_url(md5)`, and `close()`. The `md5` parameter is an Anna's
  Archive/LibGen identifier, not a source-neutral book reference.
- `lib/sources/models.py::SourceType` contains only `ANNAS_ARCHIVE` and
  `LIBGEN`. `UnifiedBookResult` requires `md5`, and `DownloadResult` exposes a
  resolved URL plus optional Anna's quota information.
- `lib/sources/router.py::SourceRouter` owns two hard-coded lazy adapter fields
  and selects between those two names. It is not a registry.
- `lib/python_bridge.py::search_multi_source` reaches the router, but
  `search`, `full_text_search`, metadata, limits, history, recent books,
  author/term searches, and booklist fallback reach the EAPI client directly.
- `lib/python_bridge.py::download_book` contains the architectural fork: an
  Anna's/LibGen result goes through `_fetch_from_source`, while a Z-Library
  result goes directly through `EAPIClient.download_file`.
- `lib/python_bridge.py::main` gates EAPI initialisation on
  `_requires_eapi_client`, which already exempts document processing,
  multi-source search, **and** a `download_book` whose result came from a
  multi-source search (#129). Credential-free LibGen search *and* acquisition
  therefore both work today. This is existing behaviour the migration must
  preserve, not a failure it must fix: the eager-initialisation defect this
  section originally described was fixed before this ADR's rebase parent, and
  Stage 0 records the working path as a baseline accordingly.
- `lib/term_tools.py`, `lib/author_tools.py`, `lib/booklist_tools.py`, and
  `lib/enhanced_metadata.py` each know how to create and authenticate an EAPI
  client when a shared client is not supplied. Their public shapes are richer
  than Anna's Archive or LibGen can necessarily implement.
- `src/index.ts` registers 13 tools individually and exposes separate Zod
  schemas and handlers for the Z-Library and multi-source paths. Its
  `validateCredentials` function correctly warns rather than exiting when
  Z-Library credentials are absent.
- `zlibrary/src/zlibrary/eapi.py::EAPIClient` already centralises Z-Library
  login, domain discovery, JSON search, profile data, book metadata, download,
  and client close.

The current download mechanism is not the detail-page scraping recorded in
ADR-002. ADR-005 migrated the live Z-Library query path to JSON EAPI calls;
ADR-002 remains binding only for its search-result-first decision, that
acquisition consumes the book details returned by search. ADR-003's rejection
of direct ID lookup also remains binding.

**This ADR supersedes ADR-005's download clause.** ADR-005 decided that
downloads would *keep* the legacy `AsyncZlib` client, on the reasoning that
"EAPI returns download URL, but actual file download requires cookies from the
legacy client." The implementation subsequently diverged: `EAPIClient` grew its
own `download_file`, and the live Z-Library transfer runs through it today. The
divergence was never recorded, so ADR-005 has been stating a decision the code
stopped honouring — and an implementer reading it could not tell whether the
EAPI download path was an intentional decision or drift.

It is a decision, and it is recorded here: the Z-Library transfer runs on
`EAPIClient.download_file`, and ADR-005's "Downloads: keep legacy AsyncZlib
client" decision, together with its two matching consequence bullets, no longer
binds. The Z-Library adapter this ADR designs targets `EAPIClient`, not
`AsyncZlib`. ADR-005's search, metadata, and domain-discovery decisions are
untouched.

## Forces and invariants

The design must satisfy these constraints together:

- **Files, not payloads.** MCP acquisition returns paths to durable files, not
  document text or a resolved download URL as the public result.
- **stdout purity.** JSON-RPC remains the only stdout traffic. Diagnostics use
  the existing loggers and stderr.
- **Per-user credentials and quotas.** The adapter reads the current user's
  Z-Library credentials and quota. The router never pools accounts or hides a
  per-user limit behind another source.
- **No privileged source.** Registration does not imply preference. The
  router does not rank, score, or deduplicate sources or results.
- **Search-result-first acquisition.** The caller passes back the result it
  selected. A source-specific identifier inside that result is not a new
  direct-ID tool.
- **Credential-free operation remains valid.** Missing Z-Library credentials
  disable Z-Library capabilities; they do not prevent LibGen search or
  acquisition and do not make server startup fatal.
- **Undocumented upstreams drift.** Every new Z-Library adapter surface needs
  a doctor/upstream probe or an explicit explanation that an existing EAPI
  probe covers it.
- **Heavy processing remains optional.** Source registration must compose
  with #103's core/RAG/scholar packaging split rather than reinstating eager
  RAG imports.

## Alternatives considered

### A. Keep `get_download_url(md5)` and special-case Z-Library in the router

Rejected. It preserves an interface shaped around two sources and moves the
existing `download_book` branch into `SourceRouter` instead of removing it.
Z-Library needs an EAPI `id` and `hash`; proposed Gutenberg and DOAB adapters
also should not be required to invent an MD5.

### B. Pass the complete search result to a wider adapter contract

This is the basis of the decision, with one refinement: the common acquisition
operation returns a raw file path and provenance, not necessarily a URL.
Anna's Archive and LibGen can continue to share a resolved-URL streaming
helper, while Z-Library can keep using `EAPIClient.download_file` internally.

### C. Give every adapter every current Z-Library method

Rejected. History, recent books, enriched metadata, and curated booklists are
capabilities, not universal source properties. Mandatory no-op or fake methods
would make the contract secretly Z-Library-shaped.

### D. Put Z-Library first in a fixed `zlibrary → annas → libgen` chain

Rejected. Credentials and quota can make Z-Library available, but availability
is not permission for the server to prefer it. A fixed order is source ranking
encoded as control flow. The caller or operator must supply order when
fallback is wanted.

### E. Replace the 13 MCP tools with one generic source-operation tool

Rejected. It would discard useful typed schemas, create a broad stringly typed
surface, and combine an internal architecture migration with an unnecessary
public API break. Tool count and discoverability are not the problem #40
solves.

## Decision

### 1. Required adapter contract

The required contract is intentionally small. Names below are normative
concepts; exact Python spelling may follow repository conventions.

```python
class SourceAdapter(ABC):
    source: SourceType

    def capabilities(self) -> SourceCapabilities: ...
    async def search(self, request: SearchRequest) -> SourceSearchResult: ...
    async def acquire(
        self, book: UnifiedBookResult, output_dir: str
    ) -> RawAcquisitionResult: ...
    async def close(self) -> None: ...
```

`acquire` receives the full selected search result and writes the upstream
bytes to disk. `RawAcquisitionResult` contains the raw file path and nested
transfer provenance. The existing shared rename and optional RAG-processing
pipeline remains outside adapters, after raw acquisition, so every source
produces the same final file bundle.

Each acquisition attempt writes to an attempt-specific temporary path beneath
`output_dir`. On failure or cancellation, the adapter removes that temporary
file; only a completed raw file is atomically promoted to the path returned in
`RawAcquisitionResult`. This prevents a failed attempt or retry from leaving a
truncated artifact that a later stage could mistake for an acquisition.

File-writing acquisition must not rely on an abandoned `run_bounded` worker to
perform that cleanup. If an adapter must place blocking acquisition work behind
that boundary, the parent creates and owns the attempt path plus a completion
lease. Timeout or cancellation invalidates the lease and removes the path;
after invalidation the worker may neither create nor promote a result. The
implementation must join or cooperatively cancel file-writing workers where the
underlying API permits it. Contract tests hold a worker past its deadline and
prove that a late completion cannot recreate or promote the temporary file.

An adapter may use an internal `resolve_download` step, but a resolved URL is
not part of the required cross-source contract and is never returned as the
MCP acquisition result.

### 2. Source-scoped book reference

`UnifiedBookResult` stops requiring MD5 as its universal identity. It carries:

```text
source              stable SourceType value
source_ref          opaque JSON object owned and validated by that adapter
title               display title
author/year/format  common optional display fields
source_metadata     nested source-specific fields
```

Examples are `{id, hash}` for Z-Library and `{md5}` for Anna's Archive or
LibGen. The router checks only that `book.source` selects the same adapter that
will consume `book.source_ref`; it must not inspect identifier keys.

For a compatibility interval, existing top-level `id`, `hash`, `book_hash`,
and `md5` fields may remain in tool responses. New routing and acquisition code
uses `source_ref`. Tests must prove that removing a compatibility field does
not change adapter dispatch.

The compatibility facade normalises a full pre-migration Z-Library
`bookDetails` search result before router validation: it adds
`source=zlibrary` and constructs `source_ref={id, hash}` from the legacy
identifier fields. The exact legacy predicate is an object containing non-empty
`id` and `hash`/`book_hash` values plus the search-result display keys
`title`/`name` and `url` (key presence is required even when an upstream value
is empty). That is the stable shape emitted by
`normalize_eapi_book`; `{id, hash}` alone is not sufficient. This structural
predicate preserves existing stateless callers but is not a claim of
unforgeable provenance. It applies only inside the existing `bookDetails`
compatibility facade; raw `bookId`/`bookHash` parameters and identifier-only
objects remain rejected. Contract tests accept a representative legacy result
and reject both raw parameter and identifier-only forms so the interval does
not recreate a generic direct-ID path.

Acquisition is bound to the source that produced the selected result. Ordered
fallback applies to discovery and other operations that do not consume a
source-scoped reference; it must not pass a Z-Library `{id, hash}` reference to
LibGen or invent an alternate lookup. A quota or acquisition failure is
returned with the selected source's provenance. The caller may then select a
result from another source. Cross-source availability hints remain hints under
#96 and do not authorize automatic acquisition fallback; source-to-source
deduplication or matching remains #52.

`get_book_metadata` should likewise migrate from raw `bookId`/`bookHash`
parameters to the selected `bookDetails` result. The old parameters may be
accepted during deprecation, but they must be converted at the façade and must
not create a generic direct-ID route.

### 3. Capability protocols, not fake universal methods

`SourceCapabilities` declares static, locally knowable implementation support;
it does not change when credentials, quota, or an upstream host are
unavailable. A separate per-operation availability check reports whether the
current user can invoke a supported operation and, when not, the stable reason
such as `credentials_missing` or `quota_exhausted`. Optional protocols cover:

- general, full-text, author, term, and advanced search modes;
- metadata lookup from a search result;
- per-user download limits;
- account history;
- recent additions; and
- curated lists.

`SourceRouter` exposes typed entry points for the existing tool operations. It
selects only adapters declaring the required capability. An explicit request
for an unsupported source returns `unsupported_operation`; a supported but
currently unavailable explicit source returns its attributed availability
reason, for example Z-Library without credentials returns
`credentials_missing`. A fallback request skips statically incapable adapters
in routing metadata. It records a supported but unavailable adapter as an
attributed failed attempt and may then continue in the supplied order.

The no-network rule applies to **locally knowable** unavailability only —
absent credentials, a missing optional dependency, a capability the adapter
does not declare. Those are decidable from configuration and must never cost a
request. Availability that is *not* locally knowable may cost a **bounded
probe**: Z-Library learns the current account's quota through
`EAPIClient.get_profile()`, and host reachability is established by the source
preflight probe. Forbidding those outright would force an implementation to
choose between stale state and reclassifying quota and reachability as ordinary
operation failures — which is the fallback-metadata inconsistency this section
exists to prevent. What stays forbidden is running the *requested operation*
against an adapter already known to be unavailable: the probe is allowed, the
search or download it would have replaced is not. If every source in an ordered request is skipped as
incapable, the router returns an aggregate `unsupported_operation` with no
top-level `source` and with the ordered skipped attempts in routing metadata.
It does not return an empty result or require an invented child failure.

The existing Z-Library helper modules become implementation details of
`ZLibraryAdapter` or thin query-shaping helpers called by it. They must receive
the adapter's shared EAPI client; they must not log in independently.

### 4. Registry and routing policy

`SourceRouter` receives or builds a mapping from `SourceType` to lazy adapter
factory. Adding a source means adding a registration and its capability tests,
not another `_get_*` field plus conditionals.

Routing rules are:

1. One explicitly requested source means one attempt. It never silently falls
   back.
2. Fallback for search and other reference-free operations requires an ordered
   source list supplied by the caller or operator configuration. The router
   attempts that order exactly. Acquisition remains source-bound as specified
   above.
3. The legacy `source="auto"` input is a compatibility alias for configured
   order. Adapter registration order is never used as the fallback order, and
   registering Z-Library does not silently put it first.
4. The router does not merge, score, deduplicate, or reorder successful
   results. Cross-source deduplication remains #52.
5. Responses follow #96: `requested`, `served_by`, `fell_back`, and symmetric
   per-source route constraints are reported. `sources_used` is deprecated.
6. Transfer results carry nested `provenance` with source, route, mirror, and
   host when each value is applicable. Provenance describes the transfer, not
   document content.

Legacy Z-Library-named tool behaviour may supply an explicit Z-Library source
at its compatibility façade. That is a property of the legacy tool contract,
not a default inside the generic router.

`source="auto"` has an explicit migration path. A new optional
`BOOK_SOURCE_ORDER` is a validated, non-empty ordered list of canonical
`SourceType` values (`annas_archive`, `libgen`, or `zlibrary`) and controls
`auto` only when set. Selector aliases are normalised before validation:
legacy `annas` becomes `annas_archive`, while canonical values pass unchanged;
unknown values fail rather than becoming registry keys. 

**`BOOK_SOURCE_DEFAULT` is currently inert, and this migration must not quietly
activate it.** `SourceRouter._resolve_source` never reads
`config.default_source`; it resolves an omitted or `auto` request from
`has_annas_key` alone. So an operator who set `BOOK_SOURCE_DEFAULT=annas`
without a key is being routed to LibGen today, and one who set `libgen` with a
key is being routed to Anna's first. "Normalising the legacy scalar into an
order" would change both, under a label that says compatibility — and one of
those changes is worse than cosmetic, since it would select Anna's key-free
search for an operator whose only supported Anna's acquisition path requires
the key they do not have.

The migration therefore preserves `auto` exactly as it behaves now:

- **Unset or `auto`, no key** → `[libgen]`.
- **Unset or `auto`, key present** → `[annas_archive, libgen]` when fallback is
  enabled, `[annas_archive]` when it is not.
- **`BOOK_SOURCE_DEFAULT` set to anything** → *ignored for ordering*, exactly as
  today, and logged once at startup as inert with a pointer to
  `BOOK_SOURCE_ORDER`. Silence would leave an operator believing a setting
  works; honouring it would change routing under their feet.

Legacy fallback stays one-way: when Anna's Archive is the primary and fallback
is enabled, LibGen is appended; LibGen never implicitly falls through to Anna's
Archive. `BOOK_SOURCE_ORDER` is the single opt-in way to choose an order, and
adopting it is what makes a previously inert preference take effect. Z-Library is not added to
the derived legacy order merely by registration; its named compatibility tools
remain explicit. Invalid legacy or new order values fail configuration
validation before a network call. Contract tests cover the normaliser for
keyed, unkeyed, fallback-disabled, explicit-default, and new-order cases,
including the unkeyed no-Anna's invariant.

### 5. Z-Library adapter lifecycle

`ZLibraryAdapter` wraps one lazily authenticated `EAPIClient` per Python bridge
process. It reuses `initialize_eapi_client`/`get_eapi_client` behaviour or a
factored equivalent; it never logs in once per adapter method. `close()` owns
client cleanup and remains reachable from `python_bridge.py::main`'s `finally`
path.

Missing credentials leave Z-Library's static support declarations intact but
make its credentialed operations dynamically unavailable. An explicit request
returns `credentials_missing`; an ordered request records that attributed
failure without attempting login and continues to the next source. Missing
credentials are not a startup failure. Quota comes from the authenticated
user's profile and is never treated as global.

## Error vocabulary

All source-path failures use a structured envelope. A single-source failure
contains `source`; an aggregate omits it and attributes each child failure.
The stable fields are:

```text
source       SourceType value, required only for a single-source failure
operation    search, acquire, metadata, limits, history, recent, or booklist
reason       stable machine-readable reason code
retryable    explicit boolean when known
detail       bounded human-readable context
host         optional host that failed
mirror       optional source-internal mirror
failures     optional ordered child failures for an aggregate
attempts     optional ordered source outcomes for a mixed aggregate
```

The transport and upstream reason codes introduced by PR #106 are retained:

- `dns_failure`, `dns_timeout`, `connect_timeout`, `connect_refused`,
  `connect_error`, `tls_error`, `read_timeout`, `search_timeout`,
  `http_error`, `quota_exhausted`, `protocol_error`, `integrity_mismatch`,
  `configuration_error`, and `unknown`.

`integrity_mismatch` and `configuration_error` are load-bearing and easy to
drop by accident, because neither is a transport failure: the first is emitted
when a completed transfer's digest does not match what was requested, the
second when a provider cannot run with the configuration it was given. Both are
covered by tests. Retained means retained — an adapter that folds either into
`protocol_error` or `unknown` has lost a stable classification callers already
depend on, and has done so while believing it preserved #106.

#40 adds operation/contract reasons as needed:

- `credentials_missing`, `authentication_failed`, `unsupported_operation`,
  `invalid_book_ref`, `not_found`, `dependency_unavailable`, `aborted`, and
  `partial_failure`.

Aggregates also use `all_sources_failed` when every attempted capable source
failed. The aggregate keeps each child reason in order; it never copies one
child reason to the top level or uses `partial_failure` when nothing answered.

An aggregate error retains each source failure in attempted order. A complete
empty search result means every attempted capable source answered and none
matched; it never means that a source was unreachable. If one source answers
empty while another fails, the bridge returns an aggregate
`partial_failure`—not `[]`—with ordered `attempts` recording the empty and
failed outcomes and `failures` holding the attributed error details. Public
error data crosses the bridge as JSON. Tracebacks and diagnostics remain on
stderr.

When every source in an ordered request lacks the requested capability, the
bridge returns aggregate `unsupported_operation`. Its ordered `attempts`
record each capability skip, `failures` may be empty, and the aggregate omits
`source` because no adapter owned the outcome.

The permanent vocabulary says `source`, matching project language. If #106
lands with its current serialized `provider` field, the migration serializer
must emit `source` and retain `provider` only as a documented compatibility
alias until callers have moved. No new code should branch on the alias.

## Dependencies and sequencing

### PR #106: bounded calls and structured source failures

This ADR was reviewed against PR #106 at head
`35886c2f1a76b176009b6a288e59ef2eed3f59ec`. #106 was open and not merged at
the time of this decision, so every dependency below is conditional on it
landing substantially as reviewed.

| ADR element | Dependency on #106 as reviewed |
|---|---|
| Structured failures | Reuse `SourceError`, `AllSourcesFailedError`, `REASON_TEXT`, and the bridge's structured `details`; extend them rather than creating a parallel hierarchy. |
| Explicit-source semantics | Preserve #106's rule that an explicit source never falls back and that an empty list means every attempted source answered. |
| Network budgets | Reuse `SourceConfig` connect/read/total/preflight budgets and apply the same bounded policy to Z-Library operations. |
| Cancellation | Preserve `CallOptions`, MCP `AbortSignal` forwarding, `runPythonBridge`, and child cleanup while changing handlers or registration. |
| Adapter registry | Replace #106's two-source `_adapter_for` conditional with the registry; do not add a third branch. |
| Process lifetime | Keep the one-shot Python bridge boundary. #106's `run_bounded` may abandon a daemon thread for non-file-writing calls; it is not a long-lived adapter-runtime primitive and must not own acquisition cleanup or promotion without the parent-owned lease defined above. |

This ADR does **not** depend on #106's current two-source primary order or on
`provider` being the permanent wire term. If #106 does not land, the #40
implementation must first supply equivalent bounded execution, cancellation,
structured failures, and explicit-source semantics. It must not omit those
protections to make the adapter migration smaller.

Issues #101 and #107 already specify the #96 reporting contract and are
blocked on #106. #40 consumes their routing, provenance, and per-source limit
shapes; it does not reopen or duplicate those decisions.

### Issue #103: optional dependency packaging

#103 must establish the final conditional tool-registration surface before
#40 changes TypeScript registration. The safe implementation order is:

1. land/rebase #106's timeout and error surfaces;
2. implement #103 against the resulting `lib/sources/config.py` and make
   core/RAG/scholar capability detection authoritative;
3. land the already-decided #101/#107 reporting work in whichever order avoids
   overlapping router edits; and
4. implement #40 against those merged surfaces.

Python-only contract work may be prepared earlier, but no temporary
`src/index.ts` registration abstraction should be merged. If #103 has not
landed, #40 stops before the registration stage. This prevents a first rewrite
for source capabilities followed by a second rewrite for installed extras.

## Migration stages

Each stage is independently reviewable and keeps the legacy path available
until parity is demonstrated.

### Stage 0: rebase and characterise

- Rebase on merged #106, #103, #101, and #107 as applicable.
- Record contract tests for current Z-Library search, metadata, limits,
  history, recent books, booklist degradation, and acquisition.
- Record failing privacy regressions for the current raw bridge payload and
  query logging, using sentinel values rather than credentials or live URLs.
- Record the existing credential-free LibGen startup, search **and
  acquisition** paths as a baseline to preserve. The eager-EAPI-initialisation
  failure this stage originally characterised no longer exists: the rebase
  parent carries `python_bridge.py::_requires_eapi_client`, which exempts
  downloads of multi-source results from EAPI initialisation (#129). Treat that
  source-conditional initialisation as an existing constraint the adapter must
  not regress, not as a defect to fix in Stage 2 — reimplementing a landed fix
  is how a plan silently loses one.
- Confirm test counts before and after every stage; a lower count is a failure
  signal, not a simplification.

### Stage 1: introduce neutral models and registry

- Add `ZLIBRARY` to `SourceType`.
- Add `source_ref`, nested `source_metadata`, capabilities, acquisition result,
  and the registry without routing Z-Library traffic through it yet.
- Adapt Anna's Archive and LibGen to `acquire(book, output_dir)` while reusing
  their current URL resolution and streaming helpers.
- Add compatibility serializers for existing result fields.

### Stage 2: add `ZLibraryAdapter` behind a dispatch gate

- Preserve the source-conditional EAPI initialisation that already exists
  (`_requires_eapi_client`): a LibGen result must keep reaching router
  acquisition without Z-Library credentials, while legacy Z-Library operations
  still initialise the shared client when selected. Routing Z-Library through
  an adapter changes what `_requires_eapi_client` has to answer for, so this is
  a regression to guard against, not a behaviour to introduce.
- Remove raw bridge-payload, copied-argument, query-text, and secret-bearing URL
  logging before routing production traffic through adapters; retain only the
  bounded structured fields in the observability plan.
- Wrap the existing shared EAPI lifecycle; do not introduce another login
  path.
- Implement general search and raw acquisition first.
- Keep the legacy direct EAPI branch selectable by one temporary dispatch
  flag. The flag chooses one path; it never performs duplicate live calls.

### Stage 3: route acquisition

- Make `download_book_to_file` pass the selected `bookDetails` to
  `SourceRouter.acquire` for every source.
- Before router validation, normalise a full legacy Z-Library `bookDetails`
  result to `source=zlibrary` and `source_ref={id, hash}`; do not normalise raw
  direct-ID parameters.
- Keep rename, validation, RAG processing, and final bundle construction in
  the shared post-acquisition pipeline.
- Reject source/ref mismatches and raw-ID-only input at the router boundary.

### Stage 4: route search and optional capabilities

- Move general/full-text/advanced/author/term operations to typed router entry
  points.
- Move metadata, limits, history, recent books, and booklists through optional
  capability protocols.
- Preserve source-specific degraded behaviour only inside the adapter that
  owns it; do not make it a universal contract.

### Stage 5: update TypeScript registration once

- Build on #103's installed-extra-aware registration.
- Add source selection/order and search-result-shaped metadata input without
  changing the 13-tool count.
- Preserve #106's abort-signal forwarding for every registered handler.
- Keep Z-Library credential validation non-fatal and make capability messages
  source-specific.
- Update tool documentation and the README tool list in the same production
  change.

### Stage 6: remove the legacy branch

- Flip the adapter path on only after parity, cancellation, packaging, and
  upstream checks pass.
- Remove duplicated EAPI initialization and the direct/non-source branch in
  `download_book`.
- Remove the temporary dispatch flag in the same release or create a dated
  removal issue; it must not become a permanent second architecture.

## Test plan

### Deterministic tests

- One shared adapter-contract suite runs against Anna's Archive, LibGen, and
  Z-Library fakes: source attribution, capability declarations, search result
  shape, source/ref matching, raw file output, atomic promotion/failed-attempt
  cleanup, late-worker lease invalidation, and close semantics.
- Router tests cover explicit-source no-fallback, caller-ordered fallback,
  canonical source-order values and legacy alias normalisation, static support
  versus dynamic availability, unsupported-capability skipping, aggregate
  failure order and `all_sources_failed`, source-bound acquisition quota
  exhaustion, and the rule that a mixed empty/failure search is a
  `partial_failure`, not an empty result.
- Z-Library adapter tests use a fake EAPI client and assert exactly one login
  lifecycle per bridge call, no credentials in errors/logs, and per-user quota
  reporting.
- Acquisition tests start from a search result for every source and assert
  durable file paths plus nested provenance. No test may introduce a raw-ID
  download path.
- Bridge tests assert structured error envelopes and stderr-only diagnostics.
  Sentinel credentials, raw payloads, full `bookDetails`, and secret-bearing
  URLs must be absent from captured stderr **and** stdout.
  `__tests__/stdio-purity.test.js` remains unchanged.
- **The query is exempt on stdout, and only there.** The bridge deliberately
  serialises it into successful responses — `retrieved_from_url` carries
  `f"EAPI search: {query}"` for general and full-text search, and advanced
  search returns a `query` field. Those are response contract, not leakage, and
  a blanket "no queries in stdout" assertion would either fail the migration
  tests or force an undocumented protocol break under a privacy label. The
  privacy defect this work addresses was diagnostic query *logging*, so the
  assertion is scoped to stderr. Removing the query from the public response is
  a separate decision, and this ADR does not make it.
- Node tests assert all registered handlers preserve #106 cancellation and
  that the README/tool registry check still reports 13 tools.
- #103 packaging tests run a core-only environment and prove source search and
  acquisition do not import RAG/scholar dependencies. RAG tools are present
  only when their extras are installed.
- Tests compare both aggregate and per-file counts against the pre-stage
  baseline so accidental test loss cannot hide behind a green exit status.

### Upstream and representative checks

Unit tests mock third-party calls and cannot establish upstream compatibility.
Before flipping the default, run the existing doctor/upstream probes and one
credentialed Z-Library search-to-file acquisition in an isolated test account,
one keyed Anna's Archive `fast_download` search-to-file acquisition using an
isolated key, plus credential-free LibGen search-to-file acquisition. The
Anna's check is an owner-authorised, quota-consuming release gate, not an
unauthenticated doctor or routine CI probe. Use a bounded temporary directory,
assert a non-empty expected file signature, and remove the downloaded artifact
after the check. If the isolated key or approval is unavailable, the migration
does not flip; fake-adapter coverage is not a substitute. Do not record
credentials, API keys, cookies, query text, document content, download URLs, or
downloaded files in test artifacts.

If a Z-Library adapter method exercises an EAPI endpoint not covered by the
current probe, extend the probe or record why an existing field assertion
catches its drift.

## Rollback plan

Before Stage 6, set the temporary dispatch gate back to the legacy EAPI path
and rerun search-to-file acquisition. Because this migration changes routing
and serialized metadata but creates no server-side account or catalogue
state, rollback does not require data migration.

Schema evolution is additive during the compatibility interval: old identifier
fields remain readable while `source_ref` becomes authoritative. If a caller
breaks on the new response, restore the old serializer while leaving internal
adapter routing enabled; do not restore direct-ID acquisition.

After the legacy branch is removed, rollback restores a known pre-migration
revision or reverts the migration stages in reverse dependency order: restore
Stage 6's removed legacy initialisation/direct branch first, then revert the
dependent registration, capability-routing, acquisition-routing, adapter, and
model stages as needed. Reverting only the earlier adapter-routing change after
Stage 6 is not a valid rollback because that path depends on code Stage 6
removed. The shared file pipeline and search-result-first input remain
unchanged on both sides of the rollback.

## Observability plan

Source operations log structured, bounded fields to stderr:

- source, operation, route, mirror/host when applicable;
- duration and outcome;
- attempted order, served source, and whether fallback occurred; and
- stable error reason and retryability.

Logs must not include credentials, API keys, cookies, full search result
objects, query text, download URLs containing secrets, document content, or
raw bridge payloads.

The MCP response carries routing constraints, final provenance, and structured
errors. It does not carry diagnostic traces. #109 remains the owner of whether
a separate active health tool is still justified; this ADR neither adds that
tool nor treats passive failure reporting as a decided replacement.

## Consequences

### Positive

- Z-Library uses the same lifecycle, routing, error, provenance, and
  cancellation boundaries as other sources.
- The common contract is shaped around the project workflow rather than MD5 or
  EAPI identifiers.
- Future sources can implement the small required contract and only the
  optional capabilities they actually have.
- Credential-free LibGen remains usable, and Z-Library quotas remain attached
  to the current user.
- The direct/non-source acquisition branch and duplicated EAPI setup can be
  removed after parity.

### Costs and trade-offs

- This is a staged migration across Python models, router, bridge dispatch,
  TypeScript schemas, tests, and documentation; it is not a one-file adapter.
- The router cannot statically validate the contents of an opaque
  `source_ref`; validation belongs to the selected adapter and must produce a
  structured error.
- Compatibility fields temporarily duplicate identifier data.
- Existing `auto` callers need a documented configured order before Z-Library
  can participate in fallback without becoming a hidden preferred source.
- Z-Library-only capabilities remain visible as such. Uniform routing does not
  imply uniform upstream features.

## Remaining uncertainty

- #109 retained a narrow active-liveness tool rather than a broad health
  surface. Its implementation remains separate from this adapter migration;
  #40 must not duplicate routing, quota, or request-failure reporting there.
- The final public spelling and deprecation window for ordered source input
  must be coordinated with the implementation of #101/#107 and the README
  contract.
- PR #106 was not merged when this ADR was written; conflict resolution may
  change exact class or function names. The semantic dependencies above remain
  required and must be rechecked against its merged form.
- #40 is recorded in the tracker as blocked by #103, using GitHub's native issue
  dependency. That relation is where the sequencing is enforced and read. This
  ADR deliberately does not name #103's milestone: AGENTS.md requires priorities
  to be read from milestones rather than from any document in this repo, and an
  earlier revision of this line asserted a slot that moved underneath it within
  days. #40 must still stop before changing TypeScript registration until #103
  lands; the dependency records that ordering without making unfinished
  registration work safe to build against.
- The EAPI is undocumented. Adapter parity under fake-client tests does not
  predict endpoint drift, domain rotation, authentication throttling, or live
  quota behaviour.

## Load-bearing assumption and flip condition

**Load-bearing assumption:** the source-neutral commonality is the workflow:
a search emits a result containing an adapter-owned reference, and acquisition
consumes that result. Richer operations can remain declared capabilities. The
router therefore never needs to understand source-specific identifier keys.

**Flip condition:** if an implementation spike covering Z-Library and one
non-MD5 future source shows that the router must inspect source-specific keys,
retain session-only lookup state, or fabricate capability data to complete a
normal search-to-file flow, stop the migration. Replace `UnifiedBookResult`'s
opaque reference with operation-specific typed envelopes and reduce the router
to a registry/dispatcher before adding more adapters. Do not add another
source-specific conditional to the common path.

## Related decisions and work

- `VISION.md`, invariants 1–4, 6, and 7
- ADR-002, search-result-first download workflow (mechanism updated by ADR-005)
- ADR-003, direct ID lookup deprecation
- ADR-005, EAPI migration
- ADR-010, current `McpServer.tool()` registration surface
- #40, Z-Library adapter work
- #75, the shipped map that deferred #40
- #95, Anna's Archive map issue
- #96, source/route reporting decision
- #101 and #107, implementation of the #96 reporting contract
- #103, optional dependency packaging and registration sequencing
- PR #106, bounded source calls, cancellation, and structured failures
- #109, narrow active-liveness health-tool verdict
