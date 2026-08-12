# ADR-011: Promote Z-Library to a Source Adapter

**Status:** Proposed

**Date:** 2026-08-12

**Decision owner:** rookslog (acceptance occurs when this ADR is merged)

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

The live tracker is authoritative for release placement:

- As observed on 2026-08-12, #40 is open in the live `v1.5 — Anna's Archive
  is a real source` milestone.
- The closed v1.4 map, #75, explicitly moved #40 out of v1.4 and called it
  v1.5 work.
- #109's current milestone description records the release-record repair: the
  old roadmap-derived v1.4 milestone became `Source layer as a first-class
  citizen (unslotted)`, while #40 moved to v1.5.
- #95 still calls this work a successor leg named “v1.6.” That sentence is
  historical narrative, not the release index. It does not override the live
  milestone or the decision owner's instruction that #40 is current open
  work.

This ADR therefore treats #40 as v1.5 work. It does not create or preserve a
v1.6 promise.

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
- `lib/python_bridge.py::main` eagerly calls `initialize_eapi_client` for every
  operation except document processing and multi-source search. This is why a
  credential-free LibGen path can work, but it also keeps authentication and
  source dispatch outside the router.
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
ADR-002. ADR-005 migrated the live Z-Library path to JSON EAPI calls, and
`EAPIClient.download_file` now performs the Z-Library transfer. ADR-002 remains
binding only for its search-result-first decision: acquisition consumes the
book details returned by search. ADR-003's rejection of direct ID lookup also
remains binding.

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

`get_book_metadata` should likewise migrate from raw `bookId`/`bookHash`
parameters to the selected `bookDetails` result. The old parameters may be
accepted during deprecation, but they must be converted at the façade and must
not create a generic direct-ID route.

### 3. Capability protocols, not fake universal methods

`SourceCapabilities` declares locally knowable support and routes. Optional
protocols cover:

- general, full-text, author, term, and advanced search modes;
- metadata lookup from a search result;
- per-user download limits;
- account history;
- recent additions; and
- curated lists.

`SourceRouter` exposes typed entry points for the existing tool operations. It
selects only adapters declaring the required capability. An explicit request
for an unsupported source returns `unsupported_operation`. A fallback request
skips an incapable adapter and reports that decision in routing metadata; it
does not ask an adapter to fabricate a response.

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
2. Fallback requires an ordered source list supplied by the caller or operator
   configuration. The router attempts that order exactly.
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

### 5. Z-Library adapter lifecycle

`ZLibraryAdapter` wraps one lazily authenticated `EAPIClient` per Python bridge
process. It reuses `initialize_eapi_client`/`get_eapi_client` behaviour or a
factored equivalent; it never logs in once per adapter method. `close()` owns
client cleanup and remains reachable from `python_bridge.py::main`'s `finally`
path.

Missing credentials are represented as local capability unavailability or a
structured credential error when Z-Library is explicitly requested. They are
not a startup failure. Quota comes from the authenticated user's profile and
is never treated as global.

## Error vocabulary

All source-path failures use a structured envelope. The stable fields are:

```text
source       SourceType value
operation    search, acquire, metadata, limits, history, recent, or booklist
reason       stable machine-readable reason code
retryable    explicit boolean when known
detail       bounded human-readable context
host         optional host that failed
mirror       optional source-internal mirror
failures     optional ordered child failures for an aggregate
```

The transport and upstream reason codes introduced by PR #106 are retained:

- `dns_failure`, `dns_timeout`, `connect_timeout`, `connect_refused`,
  `connect_error`, `tls_error`, `read_timeout`, `search_timeout`,
  `http_error`, `quota_exhausted`, `protocol_error`, and `unknown`.

#40 adds operation/contract reasons as needed:

- `credentials_missing`, `authentication_failed`, `unsupported_operation`,
  `invalid_book_ref`, `not_found`, `dependency_unavailable`, and `aborted`.

An aggregate error retains each source failure in attempted order. An empty
search result means every attempted capable source answered and none matched;
it never means that a source was unreachable. Public error data crosses the
bridge as JSON. Tracebacks and diagnostics remain on stderr.

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
| Process lifetime | Keep the one-shot Python bridge boundary. #106's `run_bounded` may abandon a daemon thread that is tolerable only because the process exits after the MCP call; it is not a long-lived adapter-runtime primitive. |

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
- Record the existing credential-free LibGen startup/search/acquisition path.
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

- Wrap the existing shared EAPI lifecycle; do not introduce another login
  path.
- Implement general search and raw acquisition first.
- Keep the legacy direct EAPI branch selectable by one temporary dispatch
  flag. The flag chooses one path; it never performs duplicate live calls.

### Stage 3: route acquisition

- Make `download_book_to_file` pass the selected `bookDetails` to
  `SourceRouter.acquire` for every source.
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
  shape, source/ref matching, raw file output, and close semantics.
- Router tests cover explicit-source no-fallback, caller-ordered fallback,
  unsupported-capability skipping, aggregate failure order, quota exhaustion,
  and the rule that partial failure is not an empty result.
- Z-Library adapter tests use a fake EAPI client and assert exactly one login
  lifecycle per bridge call, no credentials in errors/logs, and per-user quota
  reporting.
- Acquisition tests start from a search result for every source and assert
  durable file paths plus nested provenance. No test may introduce a raw-ID
  download path.
- Bridge tests assert structured error envelopes and stderr-only diagnostics.
  `__tests__/stdio-purity.test.js` remains unchanged.
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
plus credential-free LibGen search-to-file acquisition. Do not record
credentials, cookies, query text, document content, or downloaded files in
test artifacts.

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

After the legacy branch is removed, rollback is a normal revert of the
adapter-routing stage. The shared file pipeline and search-result-first input
remain unchanged on both sides of the rollback.

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

- #109 has not decided whether passive reporting is sufficient or a separate
  active health tool remains useful.
- The final public spelling and deprecation window for ordered source input
  must be coordinated with the implementation of #101/#107 and the README
  contract.
- PR #106 was not merged when this ADR was written; conflict resolution may
  change exact class or function names. The semantic dependencies above remain
  required and must be rechecked against its merged form.
- #103 is unslotted even though its sequencing constraint is binding. If it is
  deferred, #40 can proceed through Python stages but must stop before changing
  TypeScript registration.
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
- #75, shipped v1.4 map and deferral of #40
- #95, current v1.5 map
- #96, source/route reporting decision
- #101 and #107, implementation of the #96 reporting contract
- #103, optional dependency packaging and registration sequencing
- PR #106, bounded source calls, cancellation, and structured failures
- #109, unresolved health-tool boundary
