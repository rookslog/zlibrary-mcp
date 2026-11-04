> 2.0 acquisition exception: `download` calls the guarded `acquire_book` bridge
> without automatic retries. Disk receipts are checked before authentication;
> retry hints are `safe`, `may_spend_quota`, or `no`. Cancellation does not renew
> intent. The retry settings below still apply to read operations and internal
> legacy façades. See [API errors](api.md#errors) and [migration](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md).

# Retry Logic and Circuit Breaker Configuration

## Overview

The Z-Library MCP server implements comprehensive retry logic with exponential backoff and circuit breaker patterns to handle transient failures in Z-Library's unreliable infrastructure.

## Features

- **Exponential Backoff**: Automatically retries failed operations with increasing delays
- **Circuit Breaker**: Prevents cascading failures by opening circuit after repeated failures
- **Smart Error Classification**: Distinguishes between retryable and fatal errors
- **Configurable Parameters**: All retry behavior can be customized via environment variables

## Environment Variables

### Retry Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RETRY_MAX_RETRIES` | `3` | Maximum number of retry attempts |
| `RETRY_INITIAL_DELAY` | `1000` | Initial retry delay in milliseconds |
| `RETRY_MAX_DELAY` | `30000` | Maximum retry delay in milliseconds (30 seconds) |
| `RETRY_FACTOR` | `2` | Exponential backoff multiplier |

### Circuit Breaker Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CIRCUIT_BREAKER_THRESHOLD` | `5` | Number of failed logical dependency calls before opening circuit |
| `CIRCUIT_BREAKER_TIMEOUT` | `60000` | Time in ms before attempting to close circuit (1 minute) |

### Multi-Source Timeout Configuration

Nothing in the multi-source path may run unbounded. Two layers cooperate, and
the outer Node budget must remain larger than the worst-case inner provider walk.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOK_SOURCE_CONNECT_TIMEOUT` | `10` | TCP/TLS connect budget per phase, in seconds |
| `BOOK_SOURCE_READ_TIMEOUT` | `30` | Response-read budget per phase, in seconds |
| `BOOK_SOURCE_TOTAL_TIMEOUT` | `45` | Wall-clock budget for one provider operation, in seconds |
| `BOOK_SOURCE_DOWNLOAD_TIMEOUT` | `1500` | One wall-clock budget for the complete source candidate walk (resolution plus full-transfer attempts), in seconds |
| `BOOK_SOURCE_DOWNLOAD_RESUME_ATTEMPTS` | `2` | Maximum extra requests for recovery within one source transfer; `0` disables recovery, values above `10` are capped at `10`, and empty, invalid or negative values use `2`. Recovery uses the same download deadline. |
| `BOOK_SOURCE_PREFLIGHT` | `true` | Probe DNS and TCP before a provider request |
| `BOOK_SOURCE_PREFLIGHT_TIMEOUT` | `5` | Budget per DNS or TCP probe phase, in seconds |
| `PYTHON_BRIDGE_TIMEOUT` | `240000` | Ordinary Python bridge wall-clock budget, in milliseconds |
| `PYTHON_BRIDGE_LONG_TIMEOUT` | `2400000` | Acquisition bridge budget, in milliseconds |
| `PYTHON_BRIDGE_KILL_GRACE` | `3000` | Grace between process-tree termination and forced kill, in milliseconds |

The Node runner falls back to defaults for malformed, non-positive, non-finite,
and oversized timer values. Node timers accept at most `2,147,483,647`
milliseconds; that exact value is valid. The Python acquisition path parses
`PYTHON_BRIDGE_LONG_TIMEOUT` the same way, with the same trimming and the same
fallback, so a malformed value never refuses downloads.

Preflight is skipped when `HTTP_PROXY` or `HTTPS_PROXY` covers the target, with
`NO_PROXY` honored. A direct DNS/TCP socket cannot represent a proxied request;
letting that probe veto the real client would create a false outage.

The preflight budget covers DNS and the TCP connect together. At the defaults, one
provider attempt can cost at most `5s + 45s = 50s`. Those numbers size an
**attempt**. They do not size a walk, and the difference is what #152 was.

A walk costs providers × mirrors, and both factors are operator input:
`LIBGEN_MIRROR` set to something outside `("li", "vg", "la")` adds a fourth
mirror, and `BOOK_SOURCE_FALLBACK_ENABLED` decides whether `auto` adds Anna's.
Any product written down here goes stale the moment either moves — this
document said `4 × 55s = 220s` for months while the real figure under a custom
mirror was 275s, against a 240-second bridge budget. Node killed the subprocess
and the operator saw a generic timeout instead of the attributed per-mirror
failures the error taxonomy exists to produce.

**The walk is bounded by a wall clock instead.** `SourceRouter` creates one
`WalkDeadline` per search or download and passes it to every adapter; each
attempt draws `min(its own budget, what the walk has left)` from it. So:

| Setting | Default | Bounds |
|---|---|---|
| `BOOK_SOURCE_WALK_BUDGET` | `165` (seconds) | one whole walk, across every provider and mirror |
| `BOOK_SOURCE_TOTAL_TIMEOUT` | `45` (seconds) | one attempt, clamped by whatever the walk has left |

The worst case is now `BOOK_SOURCE_WALK_BUDGET`, independent of how many
mirrors or providers exist. Mirrors are **not** capped: every configured
fallback stays a candidate, so `li → vg → la` failover survives a custom
`LIBGEN_MIRROR`, and a mirror that fails its preflight in 5s leaves room for
the next rather than consuming one of a fixed three. A mirror the deadline
never reaches is reported with reason `walk_budget_exhausted`, naming the
mirrors skipped — deliberately distinct from a timeout, which is a verdict
about a host. Raise `BOOK_SOURCE_WALK_BUDGET` to fix it, unless the walk is
already clamped to the owning bridge timer; then raise `PYTHON_BRIDGE_TIMEOUT`
(or `PYTHON_BRIDGE_LONG_TIMEOUT` for downloads) instead.

**Allocation-helper scope: provider walks, not whole tools or acquisitions.**
`worst_case_search_seconds()` allocates one provider search walk plus bridge
overhead; `worst_case_download_seconds()` allocates one provider download chain
plus overhead. Tests compare these allocations strictly below the owning Node
budgets parsed from `src/lib/python-runner.ts`. Raising a default Python budget
past its owner fails the guard; environment overrides are clamped at admission.

Source configuration uses `PYTHON_BRIDGE_TIMEOUT` for searches and
`PYTHON_BRIDGE_LONG_TIMEOUT` for downloads. Overhead is
`min(30s, owning timer / 8)`; the walk is clamped below the remainder by 1 ms.
Source walks require an environment owner of at least **10000 ms** (search
or download, respectively). Smaller owners raise a configuration error before
provider contact, including when preflight is disabled. This supported minimum
avoids the observed millisecond-scale startup margins at 1500–2000 ms owners;
it is not a measured cold-start guarantee on every host. Increase the owner
on slow hosts rather than relying on a tiny allowance.
A 15s source call therefore has 1.875s overhead and at most 13.124s of walk,
not a fixed 30s admission charge. Adapters refuse a slice too short for useful
work with `walk_budget_exhausted` and provider/mirror attribution. A timer
with no positive remainder after overhead and the 1ms margin is a configuration
error before provider contact. Overhead is an allocation, not a measured
cold-start guarantee. Per-call runner timeout overrides are outside this
environment-based provider-walk admission contract; acquisition's outer deadline
below uses the effective call owner instead.

After a destination lease, both the source transfer chain and its resolution
walk draw from `min(configured, acquisition remainder - finalization reserve)`
(with a 1 ms reply margin). Resolution also rechecks this outer remainder after
each yielded transfer; pausing its own clock cannot refund acquisition time.
Range recovery remains inside that same transfer-chain deadline.

The 2.0 public `search` tries each requested source in its own bridge child:
LibGen and Anna's use explicit-source `search_multi_source`; Z-Library uses
separate EAPI calls. An L/A child defaults to a 165s walk + 30s overhead,
leaving 45s under its 240s owner. The public tool does not combine those
children into one Python `auto` walk or claim a whole-tool 240s deadline.

The provider download chain has a separate 1500s default because a large book
can exceed a per-attempt search budget. This timeout wraps resolution **and**
transfer; do not add the 165s resolution walk again. With 30s overhead its
allocation is 1530s, leaving 870s under the default 2400s long owner. Download
configuration also clamps the chain timeout to that owner's remainder.
Text processing does not ship in 2.0; there is no OCR allocation here.

Acquisition has a separate **outer deadline** derived from the effective Node
call timer, normally `PYTHON_BRIDGE_LONG_TIMEOUT`. The runner passes its validated
per-spawn timeout through the internal `PYTHON_BRIDGE_CALL_TIMEOUT_MS` environment
value, overriding any inherited value; direct Python calls fall back to
`PYTHON_BRIDGE_LONG_TIMEOUT` when it is absent. A malformed internal effective
value falls back to the resolved ordinary `PYTHON_BRIDGE_TIMEOUT`, matching
Node's per-call parser. The acquisition budget subtracts
`min(30s, owning timer / 8)` and a 1ms margin from that owner, including when
`CallOptions.timeoutMs` shortens it. One clock spans lease waiting, receipt verification,
transfer and finalization; the provider chain remains an inner bound. Transfer
may spend only the remaining time minus a reserved finalization slice of
`min(135s, initial acquisition budget / 8)`. The outer transfer watchdog allows
`min(100ms, reserve / 8)` from that reserve for cancellation cleanup, so the
inner source chain can retain its mirror attribution while closing streams.

Deadline checks apply before the commit point, including staging and hashing
an existing destination to decide whether it can be reused. The commit point is
final publication (or adoption of a verified identical destination), not private
staging. After that point there is **no cooperative deadline refusal**:
post-publication hashing, receipt writing and the reply finish under the Node
effective call timer's hard stop. This avoids reporting a local timeout after
committing an artifact, without promising unlimited post-commit execution.

On download, `walk_budget_exhausted` maps to `outcome_unknown` when a request was
issued, and to `provider_timeout` before issuance. Local budget exhaustion is
not evidence of a provider failure and does not trip its circuit breaker.

Source downloads have a separate 1,500-second default because a valid large
book can exceed the 45-second search and URL-resolution budget. The 2,400-second
long bridge default provides headroom over the source candidate-walk budget for
receipt checks, authentication and publication/finalization. The 1,500-second
source download budget covers resolution and full-transfer attempts together.
If provider budgets are overridden, keep
`PYTHON_BRIDGE_LONG_TIMEOUT` larger than the inner download budget after
converting milliseconds to seconds, with headroom for the surrounding work.

LibGen download resolution inspects at most the first 2 KiB of a candidate
response. Some CDNs ignore `Range` and answer `200`; the probe streams and closes
that response after the inspection window instead of buffering the complete book
inside the 45-second resolution budget.

On POSIX, bridge timeout, MCP abort, and server shutdown first send `SIGTERM` so
Python can cancel the active coroutine and remove partial `.download` artifacts.
After `PYTHON_BRIDGE_KILL_GRACE`, any live process group receives `SIGKILL`; the
server rejects new bridge work during shutdown and does not re-raise its shutdown
signal until owned groups are observed gone. A second shutdown signal skips the
remaining grace period. Native Windows hard ownership remains tracked by #116.

## How It Works

### Exponential Backoff

When an operation fails, the system will:

1. Check if the error is retryable (network errors, timeouts, server errors)
2. Wait for the calculated delay: `min(initialDelay * (factor ^ attempt), maxDelay)`
3. Retry the operation
4. Repeat until success or max retries exhausted

**Example delay progression** (defaults):
- Attempt 1: 1000ms (1 second)
- Attempt 2: 2000ms (2 seconds)
- Attempt 3: 4000ms (4 seconds)
- No fourth delay: the default is three retries (four total attempts).

### Circuit Breaker

The circuit breaker protects against cascading failures:

1. **CLOSED** (normal): All operations pass through
2. **OPEN** (failing): After threshold failures, circuit opens and rejects all operations
3. **HALF_OPEN** (testing): After timeout, admits exactly one probe. Other callers
   are refused with the ordinary `breaker_open` error while the probe holds the
   circuit. A memory-mode Z-Library download settles its probe as a success at the
   session grant, when the owner hands its transfer a valid session; the transfer
   then counts like any CLOSED call. Every probe, including an ordinary call
   with retries, holds the circuit for at most one `PYTHON_BRIDGE_TIMEOUT` window
   (240 seconds by default); after that further callers are admitted, and the
   probe still delivers its own verdict. Retrying does not renew the hold.
4. **Probe verdict**: success closes the circuit; a counted failure re-opens it for
   a fresh timeout; a probe that says nothing about health (cancelled, a
   non-transient error, a verified receipt) leaves it OPEN with the timeout spent,
   so the next caller probes.

A verified receipt hit never changes breaker state on any download path. When the
breaker refuses a LibGen, Anna's Archive or per-call Z-Library download, a
receipt-only bridge child still serves an intact receipt; anything else returns the
`breaker_open` refusal without an intent or provider contact. Concurrent memory-mode
Z-Library downloads sharing one failed owner login count that failure once.

Breakers count **logical dependency calls, not retry attempts** (#263). Ordinary
breaker admission wraps the complete Node retry sequence: a call that exhausts
four attempts adds one failure, and a call that recovers on a retry adds none.
The default threshold remains five failed logical calls; with three retries this
allows up to twenty failed attempts before opening. Non-transient final errors
and caller cancellation, including cancellation during backoff, add no failure.
A HALF_OPEN probe delivers one final verdict across retries, but its exclusive
hold ends at the ordinary bridge window even if retries or a long download are
still running. Already-aborted calls are rejected before breaker admission.

An OPEN refusal exposes the remaining breaker cooldown as `retry_after_s`.
During an exclusive HALF_OPEN hold, that hint instead uses the hold's remaining
time, rounded up to seconds; it stays positive while the hold is live. This
hint also survives the error wrap at a lazy Z-Library session handoff. A session
grant or an earlier probe verdict can release the hold before the hint expires.

An OPEN-breaker refusal never starts or extends the session owner's login
cooldown (#261). A login that actually runs and fails still uses that policy;
breaker recovery can proceed without an extra cooldown caused by refused logins.

## Error Classification

### Retryable Errors

These errors trigger retry logic:
- Network errors (`NETWORK_ERROR`, `TIMEOUT`, `ECONNREFUSED`, `ETIMEDOUT`)
- Server errors (`SERVER_ERROR`, `5xx` status codes)
- Domain errors (`DOMAIN_ERROR`)
- Typed Python read/write failures and timeouts (not message keywords), HTTP 408/429/5xx,
  and bridge spawn resource pressure (`EAGAIN`, `EMFILE`, `ENFILE`)

### Non-Retryable Errors

These errors fail immediately without retry:
- Authentication errors (`AUTH_ERROR`, `FORBIDDEN`)
- Validation errors (`INVALID_INPUT`, `VALIDATION_ERROR`)
- Fatal errors (marked with `fatal: true`)
- Parse errors (malformed responses)
- Permanent provider state (`configuration_error`, `quota_exhausted`), including
  rejected Anna fast-download credentials and exhausted Anna quota

Under the [ratified #192 rule](https://github.com/rookslog/zlibrary-mcp/issues/192),
**any envelope carrying `details` gets no Node retry**, including null, unknown,
read-timeout, integrity, and mixed permanent/transient details. Python owns its
provider walk. Transient aggregate leaves still count as dependency-health evidence.

Breakers are independent, one per provider: EAPI, Library Genesis, and Anna's Archive
([ADR-011](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/adr/ADR-011-Z-Library-Source-Adapter.md)). Each 2.0 search or download
bridge call names one source, so an Anna's outage never refuses LibGen, nor the
reverse; an ordered `["annas","libgen"]` search still reaches LibGen while Anna's is
open. A bridge-level `auto` walk, which no 2.0 tool sends, covers both providers in
one Python call and so keeps a fourth breaker of its own. Only positively classified
transient health failures count:
transport/timeouts, HTTP 408/429/5xx, integrity failures, and spawn resource pressure.
HTTP source details need a numeric status or the adapter's exact `HTTP NNN` token;
unknown statuses/reasons, TLS/config/auth/caller/parse errors, missing interpreter,
other permanent spawn errors and signal death neither count nor retry. A full
bridge deadline counts in its scope but never retries; caller abort does neither.
Bare Python `ConnectError` is ambiguous (transport versus TLS/config), so it does
not count or retry without a more specific reason. Auth refresh/cooldown remains
owned by the in-memory session policy. Its intentional auth-refresh retry runs
outside both Node retry multiplication and circuit-breaker accounting.

### Composed Node waiting budgets (defaults)

Let B be 240 s for ordinary work, 2,400 s for acquisition. In default
memory-session mode, EAPI has at most B for acquisition plus B for the operation
(including validation, retries and one auth refresh): 480 s ordinary / 4,800 s long.
Other calls (also EAPI in `per-call` mode) allow at most `4B + 7 s` across four
attempts and 1/2/4 s backoff: 967 s ordinary / 9,607 s long. Details-bearing and
full-deadline failures stop after one B, not four. These are configured waiting
ceilings, not real-time guarantees for filesystem work, event-loop stalls or child
teardown (up to 3 s kill grace). Custom retry/budget settings change these sums;
MCP cancellation interrupts runner work and backoff rather than waiting them out.

## Usage Examples

### Default Configuration

```typescript
// Uses default retry settings from environment
const books = await searchBooks({ query: 'python' });
```

### Custom Retry Configuration

Set environment variables before starting the server:

```bash
# More aggressive retry strategy
export RETRY_MAX_RETRIES=5
export RETRY_INITIAL_DELAY=500
export RETRY_MAX_DELAY=60000
export RETRY_FACTOR=3

# More sensitive circuit breaker
export CIRCUIT_BREAKER_THRESHOLD=3
export CIRCUIT_BREAKER_TIMEOUT=30000

npm start
```

### Monitoring Retry Behavior

The system logs all retry attempts:

```
Attempt 1 failed, retrying in 1000ms... {
  error: 'Network timeout',
  attempt: 1,
  maxRetries: 3,
  delay: 1000
}
```

Circuit breaker state changes are also logged:

```
Circuit breaker state transition: CLOSED -> OPEN
Circuit breaker opened due to repeated failures {
  failureCount: 5,
  threshold: 5,
  lastFailureTime: '2025-09-30T12:00:00.000Z'
}
```

## Implementation Details

### Core Components

1. **retry-manager.ts**: Exponential backoff implementation
2. **circuit-breaker.ts**: Circuit breaker pattern
3. **errors.ts**: Custom error classes with retry metadata
4. **zlibrary-api.ts**: Integration point for all API calls

### Code Example

```typescript
import { withRetry, isRetryableError, isTransientDependencyError } from './retry-manager';
import { CircuitBreaker } from './circuit-breaker';

// Create circuit breaker
const breaker = new CircuitBreaker({
  threshold: 5,
  timeout: 60000,
  isFailure: isTransientDependencyError
});

// Wrap operation with retry logic
const result = await breaker.execute(() =>
  withRetry(
    async () => {
      // Your operation here
      return await fetchData();
    },
    {
      maxRetries: 3,
      initialDelay: 1000,
      shouldRetry: isRetryableError
    }
  )
);
```

### Custom Retry Logic

You can override the retry behavior for specific operations:

```typescript
import { withRetry } from './retry-manager';

const result = await withRetry(
  () => criticalOperation(),
  {
    maxRetries: 5,
    initialDelay: 500,
    maxDelay: 10000,
    factor: 2,
    shouldRetry: (error) => {
      // Custom retry logic
      return error.code === 'CUSTOM_RETRYABLE';
    },
    onRetry: (attempt, error, delay) => {
      // Custom logging or metrics
      console.log(`Retry ${attempt}: ${error.message}`);
    }
  }
);
```

## Performance Considerations

### Default Settings Rationale

- **3 retries**: Four total attempts, with 7 s default backoff (plus operation time)
- **1s initial delay**: Avoids overwhelming failing services
- **30s max delay**: Prevents indefinite waits
- **2x factor**: Reasonable exponential growth

### Tuning Recommendations

**For development/testing**:
```bash
export RETRY_MAX_RETRIES=1
export RETRY_INITIAL_DELAY=100
```

**For production with unstable network**:
```bash
export RETRY_MAX_RETRIES=5
export RETRY_INITIAL_DELAY=2000
export RETRY_MAX_DELAY=60000
```

**For rate-limited APIs**:
```bash
export RETRY_INITIAL_DELAY=5000
export RETRY_MAX_DELAY=120000
export RETRY_FACTOR=3
```

## Troubleshooting

### Circuit Keeps Opening

If circuit breaker keeps opening:
1. Check Z-Library service availability
2. Increase `CIRCUIT_BREAKER_THRESHOLD`
3. Verify network connectivity
4. Review error logs for root cause

### Too Many Retries

If operations retry excessively:
1. Reduce `RETRY_MAX_RETRIES`
2. Check error classification (ensure fatal errors don't retry)
3. Review `shouldRetry` predicate logic

### Slow Response Times

If responses are too slow:
1. Reduce `RETRY_INITIAL_DELAY`
2. Reduce `RETRY_MAX_DELAY`
3. Consider reducing `RETRY_MAX_RETRIES`

## Related Documentation

- [PATTERNS.md](https://github.com/rookslog/zlibrary-mcp/blob/master/.claude/PATTERNS.md) - Code patterns and best practices
- [ISSUES.md](https://github.com/rookslog/zlibrary-mcp/blob/master/ISSUES.md) - Known issues including ISSUE-005 (resolved)
- [ADR-002](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/adr/ADR-002-Download-Workflow-Redesign.md) - Download workflow architecture

## Testing

Comprehensive unit tests cover all retry scenarios:
- `__tests__/retry-manager.test.js`: Retry logic tests (96.96% coverage)
- `__tests__/circuit-breaker.test.js`: Circuit breaker tests (100% coverage)

Run tests:
```bash
npm test -- retry-manager.test.js
npm test -- circuit-breaker.test.js
```

## Version History

- **v1.0.0** (2025-09-30): Initial implementation resolving ISSUE-005
  - Exponential backoff with configurable parameters
  - Circuit breaker pattern
  - Smart error classification
  - Comprehensive test coverage
