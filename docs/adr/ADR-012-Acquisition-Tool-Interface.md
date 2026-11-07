# ADR-012: Five-tool acquisition interface

**Status:** Accepted for the authorized 2.0 build; release acceptance remains with the lead/operator.
**Date:** 2026-10-19
**Decision owner:** operator through ZLIB-OPUS-LEAD's IFACE-BUILD-sol brief.

## Decision

Replace the old tool registrations with `search`, `get_book`, `download`,
`get_limits`, `get_history`. Search/history mint process-local refs;
get_book/download accept only those refs. `search` asks a set of sources at once:
the caller's, or the default set `get_limits` reports. No source is preferred
unless the operator configures one. Local limits make no network call. Acquisition
writes
intent and completion receipts, checks current SHA-256 bytes, and coalesces
processes with per-destination exclusive locks.

This **supersedes only ADR-011's compatibility-façade interval**: no old tool
name is an alias during 2.0. It does not claim completion of ADR-011's adapter
rebuild; the existing Python source fork remains behind the unified tool layer.
The capability block moves to get_limits rather than every search reply.

## Why now and alternatives

The authorized design chooses 2.0 as the breaking interface release. Compact
refs replace repeated full-record inputs, asking a reported set of sources
and accounting for each prevents silent provider selection, and durable receipt
checks avoid quota-spending repeats.
Keeping aliases would preserve the ambiguous surface this release removes.
Combining this with the adapter rewrite was deferred by the design; durable
cross-session selection, want-list workers, file reading and a CLI remain out
of this build.

## Consequences and uncertainty

Tool calls and results break; [migration](../interface-migration.md) lists each
old name and field. All removed names return a typed error, not a silent alias.
Refs are not authorization tokens and expire under shared LRU pressure. The
64-bit nonce yields a probabilistic aliasing bound, not an absolute claim.

The design's handle-length examples are incompatible with its entropy and
unbounded-counter requirements; full nonce/counter semantics win. An extra
pre-publication digest checkpoint closes the intent/publication crash window.
The initial destination lease stays held while a corrected-format canonical
target is leased without waiting, through publication and receipt finalization.
Recovery also leases that target before verification and identity checks.
Canonical hashed intent precedes alias intent and publication; compatible
complete canonical receipts and provenance remain authoritative and unchanged.
Foreign verified identities cannot adopt identical bytes.

Waiting for a secondary lease is rejected because opposing format corrections
can deadlock. A directory guard only after transfer misses initial admission and
recovery. Non-waiting secondary contention can instead refuse after a
quota-counting transfer: `request_issued: true` maps to `retry: "may_spend_quota"`,
invalidates the estimate, and never triggers an automatic retry.

POSIX retains O_EXCL creation, `flock` and inode-checked unlink while held.
Windows uses persistent native nonblocking byte-range lease files: close or
process death releases the lease without unlink or POSIX PID checks, and a
remaining file is not a held lease. Unsupported locking fails closed instead of
downgrading idempotence. Network filesystems and hostile concurrent directory
swaps remain outside this guarantee. Native Windows qualification requires the
focused hosted CI job; semantic-emulator results alone do not establish it.
These build choices are explicit in the migration note and lane handoff.

Receipt permission/I/O read failures fail closed with `output_refused`, rather
than erasing provenance and starting a provider request. Static symlink/nonregular
sidecars are refused before reading external data; missing/malformed/nonobject
receipts retain the absent policy. Retryable LibGen download failures are quota-safe
even after issuance; quota-counting/unknown sources remain conservative. No retry
hint authorizes automatic transfer replay or removes provider rate limits.

Memory-mode Z-Library acquisitions now use the existing owner only after durable
intent under the held leases. A bearer-free child sends one bounded `lazy-v2`
application-terminal request on fd4: `ZLAD`, version byte 2, uint16 big-endian
length, then exact ASCII `{"type":"session_needed","terminal":true}`. It does not
wait for fd4 OS EOF before one asynchronous operation grant plus EOF on stdin
(the separate stdin v1 envelope is unchanged). Same-chunk and pregrant extra bytes
invalidate before grant; postgrant extras abort/fail without replay and may already
have issued quota. Logical readiness is not success: actual child close and physical
private-channel drain still gate completion. Corrupt private streams never adopt
buffered fd3 metadata; valid provider-error recovery remains adoptable. Parent-known
handoff issuance cannot be erased by a child error envelope. Auth-only login/validate children remain under the existing
singleflight owner. Its explicit at-most-once mode retains pregrant refresh but
never reacquires/replays a granted transfer. The raw child outcome is decoded
independently of owner completion, avoiding a promise cycle. Parent denial and
cancellation close pipes and reap private children; generation/account fences and
redaction cover delivered and recovered keys. Receipt hits bypass the owner,
including cooldown/missing credentials/pending login. Per-call mode is unchanged.

The demand body is capped at 128 bytes with its exact canonical length enforced;
grant and legacy terminal update remain capped at 8192 bytes;
fd3 remains zero-or-one terminal update, never a request/response multiplex. The
private descriptors are non-inheritable. Node overlapped stdin plus asyncio
`connect_read_pipe` provides cancellable I/O on supported POSIX/Windows event loops
without executor readers or a Python3.12-only primitive; Python>=3.10 remains.
Snapshot-only reuse, eager authentication, released preflight/restart and child-local
login fallback were rejected because they lose receipt-first ordering or shared
coordination. Native Windows with Python3.10/3.13 actual-pipe CI remains a lead-owned runtime
gate; local POSIX and primary documentation are not native qualification.

Offline tests address protocol shape, replay, collisions, actual two-process
coalescing, altered bytes and process-crash recovery. They do not establish live
provider metadata, network/other filesystem behavior, or the design's separate
agent-evaluation bar. Those are integration/release boundaries, not inferred
from unit-test success.

## Amendment: merged search (Refs #377, 2026-11-06)

This supersedes ordered routing in the search contract. The five tool names,
process-local refs and receipts are unchanged. `sources` becomes a set, the
`routing` block becomes per-source `sources` lines plus `all_answered`, and
`sources_required` is removed because a search with no sources now has a meaning.
Ordered first-answer routing is removed rather than kept as a mode: a caller
who wants it searches one source at a time. Rows are files: `ref` names the
recommended copy and `other_copies` the rest. `get_book` and `download` take
those refs as before, and `download` remains bound to the copy it was given.
When two or more source jobs are dispatched, the reply waits at most
`BOOK_SOURCE_SEARCH_WAIT_MS` and marks unfinished jobs `late`. `exact` becomes
a local per-field check on every source.

Search cursors retain snapshots, including single-flight memoized refills.
Their whole-search LRU is separate from account history; history still
re-queries upstream pages. The chosen bounds are 128 searches and 20 windows
per search, with 128 optional relationship contexts. One caller's cancellation
withdraws only its subscription; last withdrawal aborts with owner cleanup
remaining fenced. See [the API reference](../api.md) for lifetime, admission,
filter evidence and the no-row truth table, and ADR-011's merged-search amendment.
