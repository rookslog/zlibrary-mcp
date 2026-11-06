# Acquisition MCP API

The 2.0 server exposes exactly five tools using SDK 1.32.2 and protocol
2025-11-25. Every result is a JSON object in `structuredContent` and a text
block containing the identical JSON. Every tool declares success and error
output schemas. Arguments are strict: unknown or removed fields return a typed
`invalid_argument` before provider contact. See [migration](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md).

## search

Catalogue metadata, not the text inside a book. Give `query`, `author`, or both.

| Argument | Contract |
|---|---|
| `query` | Nonblank string, 1–500 characters |
| `author` | Nonblank string, 1–200 characters; author-only native queries are quoted |
| `sources` | Set of 1–3 distinct names: `libgen`, `zlibrary`, `annas`. Order has no effect. Omitted: use the resolved default set reported by `get_limits.search.default_sources` |
| `exact` | Boolean, default false. Local normalized phrase check on every source, separately on title for `query` and author for `author`; also sent to Z-Library |
| `year_from`, `year_to` | Inclusive integer year range |
| `languages`, `formats` | String arrays; OR within a list, AND across fields; checked locally on every source |
| `limit` | Integer 1–50, default 10; applied after grouping and ordering |
| `cursor` | Retained continuation, exclusive of every other argument |

Returns `rows`, `sources`, `all_answered`, `ordering: "fit-v1"`, `note`, and
optional `next_cursor`. Rows carry `ref`, `source`, `match`, and known `title`,
`author`, `year`, `language`, `format`, `size_mb`, `pages`, `publisher`.
Unknown scalars are omitted. Raw identifiers, hashes and download URLs belong
in `get_book`, not search rows.

A row's `ref` selects its recommended source copy. `other_copies` contains
selectable `{ref, source}` entries for actual copies in this search's frozen
window. `route_basis` explains the recommendation: `only_usable`,
`operator_order`, or `stable_tie`. `download_available: false` marks a copy
locally unavailable for transfer. `differs` lists fields with real metadata
disagreements. By contrast, `also_available_on` is an upstream availability
hint, not another selectable copy or proof of a current route.

### Source lines and incomplete answers

Every registered source has a line, including sources outside the requested
set. Intent is resolved once, before local admission checks. An explicit
`sources` set or operator list stays in intent even if credentials are missing
or a breaker is open. Without either, install-disabled sources are excluded;
Anna's stays outside this unset default until its route is on. A key alone does
not turn that route on. Explicitly naming Anna's or putting it in the operator
list can still request its search; this does not promise reachability.

| State | Required fields | Meaning |
|---|---|---|
| `rows` | `rows` ≥ 1, `ms` | Answered with accepted records |
| `empty` | `rows: 0`, `ms` | Answered with no accepted records |
| `late` | `ms` | Terminal outcome of waiting for a dispatched source job; it says nothing about whether a network request was issued |
| `failed` | `code`, `reason`, `ms` | Dispatched job failed |
| `not_asked` | `reason` | Outside intent, or refused before dispatch; may include `retry_after_s` |

State names are closed; source names and `reason` strings are open. Common
reasons include `not_requested`, `not_in_default_order`, `credentials_missing`,
`route_off`, `breaker_open`, and `auth_backoff`. Clients must accept new reasons.
`rows` counts accepted source records before grouping, not public file rows.
An answered source with zero accepted records may contribute a failing copy to
a group whose other copy passes. Late, failed and unasked sources contribute
no copies. `all_answered` is true exactly when every source in intent reached
`rows` or `empty`; sources outside intent do not make it false.

| No public rows | Result |
|---|---|
| Every source in intent answered empty | Success with `rows: []`, `all_answered: true` |
| One source in intent failed or was refused | That source's own error |
| Some empty answers, others late, failed or refused | `partial_failure` |
| None answered, at least two in intent | `all_sources_failed` |
| Caller cancelled | `cancelled` |

Error `attempts` contains every registered source's line. `note` describes rows
shown, accepted records, incomplete sources and the bounded-window caveat.
It is for people; clients must not parse it as a protocol field.

### Grouping and ordering

Only equal valid lowercase 32-hex catalogue MD5 values group copies into one
file row. This is a catalogue claim, not verification of downloaded bytes.
Missing MD5 never implies equality; Z-Library's opaque selection hash is a
different identifier. Only filter-passing copies can be recommended and feed
match/rank. A group with no passing copy is not a row. Other copies stay
selectable, with `filter: "pass" | "fail" | "unknown"` when filters are active;
failing values or `unknown` field names explain their status.

`fit-v1` orders by `match` (`exact_title`, `all_terms`, `source_only`), then best
accepted source rank, then a stable hash. Source identity, arrival time and
repeated copies do not improve fit. Related normalized title/author stems may
get a shared `likely_group`; `BOOK_FORMAT_PREFERENCE` orders formats only within
such a group. `likely_same_file_as` is a labelled heuristic when MD5 is missing,
using title/author, format, size and compatible page counts. Neither relation
merges rows or establishes an edition's identity. A copy recommendation uses
local transfer usability, then the operator's written route order if any,
then a stable tie. No default source preference is installed.

### The wait

When at least two source jobs are dispatched, one monotonic deadline starts
before local planning. `BOOK_SOURCE_SEARCH_WAIT_MS` defaults to 12000 ms. A
result must be observed strictly before the deadline; equality is late. If all
jobs settle earlier, the reply returns earlier. A sole dispatched job uses its
ordinary bridge deadline instead. Explicit shorter caller budgets still apply.

The reply freezes once. Late output cannot change rows, refs or cursors.
The wait cut signals cancellation with `search_cut`; it does not count as a
provider failure, an authentication backoff or a successful half-open probe.
A queued waiter withdraws; an issued process remains owned through TERM, grace
and KILL. The reply does not wait for or claim physical cleanup, release a
source lock, or start collecting late results in the background.

### Filters

Every active predicate, including `exact`, is checked locally on every source.
Text comparisons normalize Unicode, case, punctuation and whitespace; accents
remain significant. With `exact`, the requested title phrase must occur at word
boundaries in the title, and the requested author phrase in the author field.
Missing checked metadata is unknown and excluded unless another predicate
already proves a mismatch. No title text is used to infer a year.

Answered source lines carry `filtering.applied`, `native_sent` when applicable,
counts and scope. Native Z-Library
sending is evidence of transmission, separate from the local check. Counts
obey `examined = matched + rejected + unknown`; they describe each bounded
window, not a catalogue total. Every source supplies at most 50 candidate
records per window before local filtering. No extra page fills a short result.

Languages recognize supported names/codes case-insensitively and deduplicate
aliases (`fr` → native `french`, `en` plus `english` → one `english`). Semicolon
row languages use membership. Unknown requested names stay literal and are
reported in `unrecognized.languages`; `indigenous` is literal-only. Spaced
native names normalize to existing native spellings. `nb`/`nn` requests map to
broad Norwegian; named Bokmål/Nynorsk filters preserve their own code varieties.
Moldavian aliases match Romanian. Formats trim, case-fold and strip one leading
dot without family aliases (`azw3` differs from `mobi`). Empty/blank lists and
`exact:false` add no constraint; a dot-only format is blank.

Years used for filtering are positive integers of at least four digits, digit
strings in that domain, or a leading four-digit year with a recognized
English/French/Italian/Spanish/German/Portuguese month, season or quarter,
optionally a day 1–31 (`2014 Juin`, `2001 Spring`, `2001 Q4`). Ranges, two-year
strings, circa/unlisted suffixes, zero and sub-1000 values remain unknown.
Readable dates appear as parsed public years. Existing numeric conversion on
non-year legacy detail/history paths remains unchanged.

## get_book

Requires `ref` from a current search/history row or any `other_copies` entry. Returns the row fields plus
known `isbn`, `series`, `edition`, `translator`, `categories`, `rating`,
`description`, `identifiers`, `url`, and `evidence`. Description is capped at
1,000 characters and marked `description_truncated` when cut.
`evidence.detail` and `evidence.search` name the origin of metadata fields.
A field upstream leaves empty is omitted, never returned empty. `categories` is a
list of `{name}`, and `rating` is `{value}` plus `count` when upstream sends one.
Z-Library's API exposes no terms, booklists or IPFS ids, so `get_book` has none.
Z-Library uses one detail request. Other sources expose their held row without
promising additional detail-page metadata. No direct ID input is accepted.

`other_copies` includes each copy's metadata, identifiers and remembered search
filter evidence. `disagreements` exposes actual differing values, and
`likely_editions` lists up to ten related file rows. The selected copy's detail
is fresh where its source supports detail calls; its filters are rechecked
against those fields. Sibling metadata remains the frozen search evidence.
Optional relationship indexes retain 128 window contexts and may expire before
the ref; their absence does not invalidate the selected ref or its own detail.

## download

| Argument | Contract |
|---|---|
| `ref` | Required current row ref |
| `output_dir` | Optional directory, defaulting to the output root. Relative paths resolve under that root in every mode. Absolute paths must remain inside it unless the operator sets `ZLIBRARY_UNRESTRICTED_OUTPUT=1`. Any `..` component, leading `~`, NUL byte or (on Windows) drive-relative (`C:foo`) or root-relative (`\foo`) path is refused before provider contact in both modes. |
| `filename` | Optional basename; sanitized and extension forced to acquired format |

Source transfers recover within the same call and deadline. Progressing drops
can resume repeatedly; `BOOK_SOURCE_DOWNLOAD_RESUME_ATTEMPTS` (default 2) limits
cumulative recovery requests gaining less than 64 KiB beyond the furthest
offset reached, and `0` disables recovery. Resending a prefix or advancing by
one-byte slices spends that budget. Every admitted retry waits 250 ms.
Range replies of 500/502/503/504 retain the staged prefix when budget remains.
LibGen alone gets one delayed second mirror resolution pass if no mirror yielded
a candidate and the first pass did not stop on budget. Resuming Anna's transfer
reuses its resolved keyed URL without a second keyed resolution or quota spend.

Known selection size extends the 1500s base source-chain allowance using a
40 KiB/s budgeting floor. The default ceiling is 3600s and the effective ceiling
is at least the configured base. Configure bytes/second with
`BOOK_SOURCE_DOWNLOAD_FLOOR_SPEED` and seconds with
`BOOK_SOURCE_DOWNLOAD_TIMEOUT_CEILING`. Unknown sizes retain the base; there is
no extra size probe. Resolution, retries, validation and delays share that
allowance. The Node long timer defaults to 3900000 ms and shorter owner or
caller deadlines still win. Client tool-call deadlines are separate; see
[timeout configuration](RETRY_CONFIGURATION.md#multi-source-timeout-configuration).

Returns `path`, `bytes`, `source`, `reused`, `transfer_skipped`, `verified: "sha256"`,
`record`, and optional `quota_estimate`.
`source` is always the selected copy's source. A failure may include
`error.detail.other_copies` with alternative refs and their per-copy filter
evidence. The server never substitutes one, retries on another source, or
treats matching catalogue MD5 as receipt identity. The caller chooses any new
copy explicitly. Existing receipt identity and refusal rules apply unchanged.

`transfer_skipped` means this call read the receipt and hashed the current
published bytes without contacting a provider. `reused` means a transfer occurred
but an identical existing file was adopted; quota may have been counted.
Different bytes are not overwritten. An occupied predicted destination without
a matching verified receipt, or a modified receipt artifact, is refused with
`output_refused` before intent, authentication or transfer; the file and prior
receipt stay unchanged. Move or rename that file or choose another `output_dir`.
A different filename/directory creates a new destination. `record` points to
`<path>.acq.json`; see the [receipt v1 contract](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/receipt-format.md)
and its schema for downstream readers. One exception: while the source's breaker is open, a receipt
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

Optional `sources` (the same distinct source-set contract as search, default
all registered sources) and `mode` (`local`, default, or `live`). Local mode
makes no network or account call. It returns `sources` and a `search` policy:
`default_sources`, optional operator `route_order`, `wait_ms`,
`ordering: "fit-v1"`, and optional `format_preference`.

Each source has `configured`, `search_available`, `download_available`,
`operations`, `limit.kind` (`daily`, `none`, `unknown`, `unparsed`), and
`filters` reporting all five predicates as `"local"`. `default_search` describes
membership in the resolved intent, before admission; `off_reason` explains
exclusion. `breaker` is `closed`, `open` or `half_open`; `admission` is `open`
or `backoff`, with `retry_after_s` where applicable. These are read-only local
snapshots: inspecting them never resets health, claims a probe or logs in.
`recent` counts `searches`, `late`, `failed` over the last 20 dispatched source
jobs in this process. The denominator is jobs, not network requests; refusals
before dispatch consume no slot. Restart clears these counters.

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
when available. Use `per_day` as the quota signal: `premium` can stay true after a
plan lapses even when the daily limit returns to the free level. `remaining` is
computed only from a known cap
and known spend; a cap-only profile omits `remaining` and reports
`limit.kind: "unparsed"`.
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

Search retains 128 whole searches in a process-local LRU, shared across clients,
with no time-based expiry. A search's cursors and every ref on its pages live
and evict together. Each search holds at most 20 windows (initial plus 19
refills), each bounded to 50 candidate records per source; this is a window
bound, not a metadata-byte bound. At the cap no further cursor is advertised;
the source's `more` evidence and bounded note do not become completeness claims.
Narrow or restart the search for deeper results.

A cursor binds normalized arguments, source intent, limit and retained position.
Replays re-slice the same snapshot and issue no request. Once a sole dispatched
source's retained tail is exhausted, the first continuation issues one upstream
refill, single-flight per position, and memoizes the resulting window. Concurrent
callers and lost-reply replays see the same page. Each caller can cancel its own
subscription without cancelling peers. Last-subscriber withdrawal aborts that
waiter; the position stays fenced until its owner settles. Failed/abandoned
refills can be explicitly retried after that drain, never overlapped.

History retains its separate 5,000-entry handle LRU with 5,000 recent expired
handle tombstones. Its immutable cursor re-queries the same upstream position:
**pages re-queried upstream are not snapshots**. Search and history cursors
cannot be interchanged. Unknown, evicted and other-process handles return
`ref_unavailable`; wrong-kind arguments return `invalid_argument`.
Reconnect preserves process handles; restart does not. Search again and select
the edition after unavailability. Refs are not authorization tokens.

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
| `all_sources_failed` | At least two sources in intent, none answered; jobs may be late, failed or refused |
| `partial_failure` | Some empty answers and some late, failed or refused sources, with no rows |
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
