# Acquisition MCP API

The 2.0 server exposes exactly five tools using SDK 1.32.1 and protocol
2025-11-25. Every result is a JSON object in `structuredContent` and a text
block containing the identical JSON. Every tool declares success and error
output schemas. Arguments are strict: unknown or removed fields return a typed
`invalid_argument` before provider contact. See [migration](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md).

## search

Catalogue metadata, not the text inside a book. Give `query`, `author`, or both.

| Argument | Contract |
|---|---|
| `query` | Nonblank string, 1–500 characters |
| `author` | Nonblank string, 1–200 characters; author-only exact names are quoted |
| `sources` | Ordered, distinct array of 1–3 of `libgen`, `zlibrary`, `annas`; optional only with operator defaults |
| `exact` | Boolean, default false; applies to the whole combined query, not just the title |
| `year_from`, `year_to` | Inclusive integer year range |
| `languages`, `formats` | String arrays; unsupported source filters are listed in attempts' `ignored` |
| `limit` | Integer 1–50, default 10 |
| `cursor` | Continuation, exclusive of every other argument |

Returns `rows`, `routing`, optional `next_cursor` and a steering `note`.
Rows carry `ref`, `title`, `author`, `year`, `language`, `format`, `size_mb`,
`pages`, `publisher`, `source`, `also_available_on` when known. Unknown scalars
are omitted, not empty strings. Raw IDs, hashes and download URLs are not
search row fields; `get_book` exposes identifiers when needed.

`routing` reports `requested`, `served_by`, `fell_back`, and ordered `attempts`.
Outcomes are `rows`, `empty`, `skipped`, `failed`; failures preserve typed codes
and sub-reasons. Routing stops at the first source with rows. A continuation
uses only that source: it never restarts fallback. All-empty is a successful
empty page: `served_by` is omitted and `fell_back` is false, including an
exhausted continuation. `requested` and `attempts` still describe the walk.
Empty plus failure is `partial_failure`; all attempted sources
failing is `all_sources_failed`. An explicit unconfigured source fails locally.

## get_book

Requires `ref` from a current search/history row. Returns the row fields plus
known `isbn`, `series`, `edition`, `translator`, `categories`, `rating`,
`description`, `identifiers`, `url`, and `evidence`. Description is capped at
1,000 characters and marked `description_truncated` when cut.
`evidence.detail` and `evidence.search` name the origin of metadata fields.
A field upstream leaves empty is omitted, never returned empty. `categories` is a
list of `{name}`, and `rating` is `{value}` plus `count` when upstream sends one.
Z-Library's API exposes no terms, booklists or IPFS ids, so `get_book` has none.
Z-Library uses one detail request. Other sources expose their held row without
promising additional detail-page metadata. No direct ID input is accepted.

## download

| Argument | Contract |
|---|---|
| `ref` | Required current row ref |
| `output_dir` | Optional directory, defaulting to the output root. Relative paths resolve under that root in every mode. Absolute paths must remain inside it unless the operator sets `ZLIBRARY_UNRESTRICTED_OUTPUT=1`. Any `..` component, leading `~`, NUL byte or (on Windows) drive-relative (`C:foo`) or root-relative (`\foo`) path is refused before provider contact in both modes. |
| `filename` | Optional basename; sanitized and extension forced to acquired format |

Returns `path`, `bytes`, `reused`, `transfer_skipped`, `verified: "sha256"`,
`record`, and optional `quota_estimate`.
`transfer_skipped` means this call read the receipt and hashed the current
published bytes without contacting a provider. `reused` means a transfer occurred
but an identical existing file was adopted; quota may have been counted.
Different bytes are not overwritten. An occupied predicted destination without
a matching verified receipt, or a modified receipt artifact, is refused with
`output_refused` before intent, authentication or transfer; the file and prior
receipt stay unchanged. Move or rename that file or choose another `output_dir`.
A different filename/directory creates a new destination. `record` points to
`<path>.acq.json`. One exception: while the source's breaker is open, a receipt
hit is served without writing any file. If that receipt still needs repair (for
example, it still says `intent`, or only the alias at the requested name exists),
`record` points to the receipt file that exists, and the next ordinary call
repairs it.

Record version 1 includes `state` (`intent` or `complete`), file path, bytes,
SHA-256, known MD5, source, nested provenance, edition metadata (explicit nulls
for unknown fields), identifiers and request context. Intent is persisted
before authentication/transfer. When the acquired format corrects the filename,
the initial destination lease remains held and a non-waiting canonical-target
lease covers publication and all receipt finalization. Canonical hashed intent
is written before alias intent and publication. Alias recovery also leases the
canonical target before verifying/finalizing it and rechecks its identity.
Compatible complete canonical receipts and provenance remain authoritative and
unchanged; verified foreign identities cannot adopt even identical bytes.
Complete receipts are re-read and re-hashed per call, not trusted from a process
cache. Unreadable receipts (including permission/I/O errors), symlink sidecars
and nonregular sidecars fail closed with `output_refused`, preserving provenance.
Missing, malformed-JSON and nonobject receipts retain the absent-receipt policy.

In memory mode, fresh Z-Library acquisitions join the same session owner and
login/cooldown coordination as reads, only after held-lease receipt admission and
durable intent. Receipt hits do not enter authentication, even with missing
credentials, cooldown or a pending login. A granted transfer is at-most-once:
authentication rejection cannot automatically refresh and replay it. Explicit
`per-call` mode keeps per-call authentication; LibGen/Anna's paths are unchanged.

Per-destination leases coalesce independent processes on local filesystems.
POSIX uses O_EXCL creation and `flock`, reclaiming stale holders only under an
inode-checked lease and unlinking while held. Live holders are not stolen because
a transfer is slow. Windows uses native nonblocking byte-range leases on
persistent files, released on close or process death without stale-PID checks or
unlink; a remaining file is not a held lease. Unsupported locking returns
`output_refused`; there is no silent uncoalesced fallback. Network-filesystem lock
semantics are not established here.
The output root is `ZLIBRARY_OUTPUT_ROOT` (absolute, with home expansion), or
`${XDG_DATA_HOME:-~/.local/share}/zlibrary-mcp/downloads` on Linux/macOS and
`%LOCALAPPDATA%\zlibrary-mcp\downloads` on Windows. A relative configured root
fails startup. `ZLIBRARY_UNRESTRICTED_OUTPUT` accepts only `1`, `0` or unset;
`1` lifts containment only and logs a startup warning on stderr. Refusals return
`output_refused` with `request_issued: false` before provider contact. Checks
include existing symlinks but do not defend against a local operator racing to
replace directories. On POSIX, newly created directory levels are 0700; existing
directory permissions are preserved. New artifacts and receipts are 0600;
artifacts already present keep their mode on reuse; a receipt the server repairs
is rewritten as 0600.
On Windows, chmod only toggles the read-only attribute, not access-control permissions.

Quota estimates exist only after an observed daily limit. Each issued fresh
transfer decrements the process estimate; skipped transfers do not. Estimates
have `source`, `remaining`, `as_of` (observation time), `basis` (`observed` or
`decremented`). Other processes can change the account count. Unknown outcomes
and potentially quota-spending cancellation invalidate estimates until another
live observation. Acquisition is never automatically retried.
Corrected-target contention refuses without waiting, potentially after a
quota-counting transfer: `request_issued: true` maps to `retry: "may_spend_quota"`
and invalidates the estimate. A refusal before transfer does not spend quota.

## get_limits

Optional `sources` (same array contract as search; default all) and `mode`
(`local`, default, or `live`). Local mode makes no network call. It returns
optional `default_order` and a `sources` object. Each source has `configured`,
`search_available`, `download_available`, `operations`, and `limit.kind`:
`daily`, `none`, `unknown`, or `unparsed`.

`operations` lists what the source supports, independent of configuration:
`search`, `details`, `download` for all three sources, plus `history` for
Z-Library. An unconfigured Anna's source still lists `download`; use
`search_available` and `download_available` to decide which calls are available.
Anna's search requires a usable HTTP(S) base URL (default
`https://annas-archive.gl`): it must parse with a valid nonzero port and contain
no userinfo, query, fragment, whitespace or control characters. A usable base
permits search without a key, including on an untrusted host. Download and
`configured` additionally require `ANNAS_SECRET_KEY` and a trusted host
(`annas-archive.gl`, `.pk`, or `.gd`). An unusable base makes both availability
fields false; the note distinguishes this from a base not trusted for the key.
This local report makes no provider request.

Live Z-Library mode adds `per_day`, `used_today`, `remaining`, `premium`, `as_of`
when available. `remaining` is computed only from a known cap and known spend;
a cap-only profile omits `remaining` and reports `limit.kind: "unparsed"`.
A failed lookup stays in that source's `error` entry while other
sources' reports remain usable. LibGen and Anna's local capability reports cost
no account calls even in live mode. Availability is not a reachability check.

## get_history

Optional `source` (default and only supported source `zlibrary`), `limit`
(1–50, default 10), or `cursor` alone. Returns `rows` and optional `next_cursor`.
Rows have a ref only if upstream supplied sufficient download identity.
`downloaded_at` appears only when supplied upstream. Ordering is provider order,
not a promise of newest-first. Account history does not describe files on disk.

## Refs and cursors

Refs are opaque kind/64-bit-process-nonce/monotonic-counter handles. Their
length is not fixed; retaining full nonce strength and counter non-reuse takes
precedence over an eight-character example. Two processes drawing the same
nonce is a 2^-64 per-pair aliasing bound, not an absolute no-alias claim.

This process shares a 5,000-entry LRU across clients and holds 5,000 recent
expired-handle tombstones. Unknown, expired and other-process refs all use
`ref_unavailable`, with the reason and kind in `detail`. Wrong-kind input is
`invalid_argument`. Reconnect keeps handles; restart does not. Search again and
choose the edition again after unavailability. Refs are not authorization tokens.

Cursors bind operation, resolved source order, normalized arguments, limit and
position. They are immutable continuations: repeating one re-queries the same
Z-Library page or re-slices the same held LibGen/Anna's page. A lost reply does
not advance an iterator. Pages re-queried upstream are not snapshots.

## Errors

```json
{"error":{"code":"ref_unavailable","operation":"download","message":"Reference unavailable (expired).","retry":"no","recovery":"Search again and choose the edition again; use its new ref.","attempts":[],"detail":{"kind":"row","reason":"expired"}}}
```

Tool errors have `isError: true`; they are not JSON-RPC protocol errors.
`source` and `retry_after_s` are omitted when unknown. Messages are under 200
characters without raw upstream stacks or credential-bearing URLs. `attempts`
is always an array; `detail.reason` retains sub-reasons. Independent tests
validate the closed envelope even though the SDK does not validate error outputs.

Authentication and EAPI failures can include `detail.diagnostics`, a closed
record shared with diagnostic logs:

| Field | Values |
|---|---|
| `failure_kind` | `auth_rejected`, `transport`, `wall`, or `ambiguous_status` |
| `http_status` | Integer 100–599; omitted when unknown |
| `attempt` | One-based bridge/retry attempt, capped at 65535 |
| `refresh_count` | Owner-controlled authentication refresh count for the failed invocation (0 or 1 under the current owner), capped at 65535 |
| `request_id` | Internally generated UUID correlating one logical call across its bridge invocations |

A generic HTTP 400 and malformed non-HTTP responses are not authentication
rejections. A 400 does not invalidate a warm session or trigger login/download
replay. Local acquisition refusals before EAPI contact keep their existing
error details without EAPI diagnostics. Authentication cooldown refusals can
carry the prior failed invocation's diagnostic with a new caller correlation id. A circuit-breaker
OPEN refusal carries its dependency (`eapi`, `source_walk`, or `local`) in
`error.detail.dependency` and has no request diagnostic.

Diagnostics contain no account identifiers, credentials, cookies, tokens,
response bodies, book details, or URLs. Malformed diagnostic records are
discarded whole. The limits are 512 UTF-8 bytes and one line per diagnostic
record, 2048 UTF-8 bytes and one line per EAPI bridge failure envelope, and
8192 UTF-8 bytes / 32 lines of retained subprocess stderr. Oversized stderr
records are discarded, not truncated into apparently valid records. Legacy
unstructured logs from `zlibrary` (including all descendant loggers,
notably `zlibrary.sources`) and `lib.enhanced_metadata` (including descendants)
are suppressed by the bridge's logger and installed file/stderr/root handler
filters. Those namespaces emit only valid allowlisted diagnostics; unrelated
logger namespaces are not filtered. Search-argument file logging is suppressed,
and the Node logger uses allowlisted EAPI diagnostics. Direct Python library
consumers do not install the bridge's filters.

| Code | Meaning |
|---|---|
| `invalid_argument` | Invalid/removed tool or argument, bounds or wrong handle kind |
| `sources_required` | No source order was supplied/configured; choices are in `detail.available` |
| `ref_unavailable` | Expired, other-process or unknown row/cursor |
| `unsupported_operation` | Source does not support the operation |
| `credentials_missing` | Required account or key not configured |
| `auth_failed` | Account/key rejection |
| `quota_exhausted` | Anna's keyed-download router reports quota metadata with zero remaining. Z-Library EAPI download daily-limit failures currently surface as `provider_error` or `internal`, not this code |
| `not_found` | Maps an explicit structured `not_found` reason; no current source adapter emits it. Z-Library EAPI download absence currently surfaces as `provider_error` or `internal`; empty search is not absence |
| `provider_unreachable` | DNS, connect or TLS failure |
| `provider_timeout` | Provider operation exceeded its budget |
| `provider_error` | Bad status/body, block page, HTML instead of file |
| `integrity_mismatch` | Download digest differs |
| `breaker_open` | Dependency/destination paused or authentication backoff |
| `all_sources_failed` | All attempted sources failed |
| `partial_failure` | Some empty answers and some failed sources |
| `output_refused` | Output root or destination lock admission failed |
| `cancelled` | Client cancelled; no automatic renewed intent |
| `outcome_unknown` | Download may have been issued but outcome is uncertain |
| `internal` | Other internal failure |

`retry: safe` means a repeat cannot spend acquisition quota under the observed
pre-request boundary, a matching receipt, or the quota-free LibGen download route. `may_spend_quota` means a request
may have counted and another transfer can count again; check disk receipts first.
`no` means repeating unchanged arguments/configuration will not help. Cancellation
on quota-counting or unknown download routes is `safe` only when no request was
issued; otherwise it is `may_spend_quota`. Retryable LibGen download failures are
`safe` even after issuance. This is a quota statement, not permission to ignore
rate limits; cancellation still requires fresh intent and no download auto-retries.
