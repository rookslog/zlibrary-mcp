# ISSUE-106 Lifecycle and Error-Envelope Design Correction

Date: 2026-08-12
Status: Proposed for owner review
Scope: PR #106, `fix/ISSUE-NET-001-multi-source-timeouts`

## Decision

Correct #106 as one cross-layer design rather than continuing site-by-site review fixes.

The implementation will enforce two invariants:

1. One lifecycle owner bounds every Python process, process group, DNS lookup, and provider operation until the owned resource is observed gone.
2. One normalized provider-error envelope retains provider, host, reason, operation, and child failures from the Python adapter through MCP `structuredContent`.

Source selection must happen before Z-Library authentication so credential-free LibGen acquisition remains supported.

## Why This Correction Is Required

Three substantive review rounds found the same classes at different boundaries:

- timeouts existed at individual awaits while process groups, resolver workers, or validation subprocesses could outlive them;
- source failures were reclassified or flattened at adapters, aggregates, stderr parsing, Node wrappers, retry/circuit logic, and handlers;
- `download_book` initialized Z-Library before determining that a LibGen result required no Z-Library account.

The third-round audit reproduced remaining descendant survival, joined resolver shutdown, pre-routing credential failure, CDN-error misclassification, unbounded Python validation, and a secondary exported wrapper with different error semantics. Passing unit suites therefore does not establish the stated #106 contract.

## Alternatives Considered

### A. Centralize lifecycle and error semantics in #106 — selected

Build one lifecycle boundary and one error-envelope boundary, then make every consumer use them. This is the smallest approach that closes the contract #101 and #107 depend on.

Trade-off: it is a cross-layer correction and requires another full review pass.

### B. Isolate every provider operation in its own subprocess

A per-operation subprocess would make DNS and third-party library hangs killable at the OS boundary.

Rejected for now: it adds serialization, startup, cleanup, and platform complexity to every provider operation. Use it only if the bounded event-loop resolver cannot close actual HTTP resolution without private-library hooks.

### C. Split or defer the remaining findings

Move lifecycle and envelope work into follow-up PRs while leaving #106 open or merging a smaller subset.

Rejected: splitting does not reduce the cross-layer dependencies, and merging would publish guarantees the code does not meet. #101 and #107 would then build on an unstable vocabulary and failure model.

## Lifecycle Architecture

### Process-tree ownership

Replace direct-`PythonShell` lifecycle state with a process-tree record whose identity survives direct-parent exit. The record contains the root PID/process-group identity, platform strategy, termination state, and liveness check.

- POSIX launches each bridge in its own process group.
- Timeout, abort, and shutdown signal the group, not only the direct child.
- Direct-parent exit cleans stream/listener state but does not cancel escalation or deregister a live group.
- After the grace period, a live group receives SIGKILL.
- A lightweight unref'd liveness check removes the record only after the group is gone.
- Shutdown retries termination for every still-live record.
- Windows retains the `taskkill /T /F` strategy with direct-child fallback. It remains a Linux-host-unverified boundary and must not be described as host-tested.

### Managed-Python validation

`getManagedPythonPath` will perform filesystem resolution and executable-permission checks only. It will not run `python --version` synchronously.

The first real bridge invocation is the execution check and already runs through the lifecycle owner. Spawn failures retain the existing actionable environment guidance without creating a second unbounded process path.

### DNS and provider-operation bounds

Preflight-only DNS protection is insufficient because AnyIO/httpx resolves hostnames again.

Install a bounded resolver on the one-shot Python bridge event loop before dispatch:

- preserve the event loop `getaddrinfo` signature;
- execute `socket.getaddrinfo` through the existing daemon-thread boundary;
- apply the configured DNS/preflight deadline;
- return normal address tuples to AnyIO/httpx so actual HTTP clients cannot fall back to the joined default executor;
- restore the loop method during shutdown/tests;
- translate timeout and resolver failures at the adapter boundary into the stable source vocabulary.

The numeric-address TCP preflight remains useful but is no longer the only DNS safety mechanism.

If an integration test demonstrates that httpx bypasses the bounded loop resolver in the installed dependency versions, stop and reconsider alternative B rather than adding another site patch.

## Source Routing and Initialization

`python_bridge.py::main` must parse the requested operation and `book_details.source` before constructing EAPI state.

- LibGen and Anna source downloads route through `SourceRouter` without Z-Library credentials.
- Z-Library downloads initialize EAPI only after routing selects Z-Library.
- Operations that inherently require Z-Library keep the current credential requirement.
- Missing Anna download credentials become `configuration_error`, not dependency-health evidence.

This preserves the repository contract that LibGen needs no account and Z-Library credentials are not required for server startup or LibGen operations.

## Error-Envelope Architecture

### Canonical Python envelope

`SourceError` and `AllSourcesFailedError` remain the canonical Python vocabulary. Every failure exported from a source operation normalizes to:

```json
{
  "operation": "download",
  "provider": "libgen",
  "host": "libgen.li",
  "reason": "connect_timeout",
  "detail": "...",
  "failures": []
}
```

Single-source failures may use the top-level provider/host/reason fields. Aggregate failures carry ordered child failures. Configuration errors use `configuration_error` and are permanent caller errors.

### Adapter and aggregate rules

- LibGen resolution accumulates `SourceError` objects, never display strings.
- `_serves_bytes` returns semantic success/non-byte results but lets transport exceptions propagate for normal classification.
- `SourceRouter` preserves child failures from nested aggregates.
- Anna configuration failures are typed before the router catches them.

### Node normalization

Extract one bridge-envelope parser/normalizer used by both exported Node wrappers. A wrapper may add a human-facing message, but it must preserve the normalized error object directly rather than requiring consumers to traverse arbitrary `cause` chains.

- retry classification reads normalized `reason`/`failures`;
- the circuit breaker excludes aborts and permanent configuration-only failures;
- search and download handlers use the same normalized details helper;
- `wrapResult` emits the unchanged envelope as `structuredContent`;
- no error or diagnostic writes to stdout.

## Handling the Existing Local Draft

The current uncommitted 14-file batch is evidence, not an implementation baseline to accept wholesale.

- retain the RED tests and correct adapter/envelope changes where they fit this design;
- replace the process-tree registry fix because it still loses descendant-only survivors;
- extend the DNS test through a real local httpx resolution path and interpreter exit;
- extend the LibGen test so `_serves_bytes` transport failures retain their true reason;
- add route-before-auth and secondary-wrapper envelope tests.

No existing draft change is published until it satisfies the design and the full verification matrix.

## Test Contract

Each behavior must be observed RED before implementation and GREEN afterward.

Required mutations/scenarios:

1. direct Python parent exits after SIGTERM while a redirected descendant survives;
2. shutdown still owns and kills that descendant group;
3. malformed/wrapper Python executable cannot block pre-run validation indefinitely;
4. actual local httpx hostname resolution stalls, returns within the provider deadline, and the one-shot interpreter exits without joining a resolver worker;
5. credential-free LibGen download reaches `SourceRouter` without EAPI initialization;
6. missing Anna key is `configuration_error` and does not consume retry or breaker budget;
7. LibGen preflight, key resolution, and CDN byte-probe DNS/TLS/timeout failures retain mirror host and reason;
8. search and download aggregates survive Python serialization, both Node wrappers, handlers, and MCP `structuredContent` unchanged;
9. stdio purity remains intact.

After focused RED-GREEN cycles, run on the exact committed and rebased tree:

```bash
npm run build
node --experimental-vm-modules node_modules/jest/bin/jest.js
uv run pytest -m "not slow and not integration and not performance" --benchmark-disable -rs
npx eslint src/
npx prettier --check src/
```

Then push normally, reply to each open finding with a fenced review verdict and commit, resolve the matching thread, audit thread-to-verdict pairing, and request one final `@codex review` pass.

## Completion and Stop Conditions

The correction is complete only when:

- every production Python execution path uses the lifecycle owner;
- no provider HTTP resolution uses the event loop's joined default executor;
- source selection precedes unrelated authentication;
- all exported source failures use the canonical envelope through MCP output;
- the required focused mutations and full matrix pass;
- all current review threads have technically supported dispositions.

Stop and return to design if:

- installed httpx/AnyIO bypasses the bounded loop resolver;
- Windows requires a native job-object dependency to meet the contract;
- the next Codex pass reports another instance of either structural class.

## Residual Boundaries

- Windows process-tree behavior requires Windows-host corroboration.
- Unit tests mock third-party provider responses by repository policy; live drift remains covered by `npm run doctor` and the upstream workflow.
- No history rewrite, merge, or auto-merge is part of this design correction.
