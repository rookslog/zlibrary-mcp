# Z-Library MCP Server

[![CI](https://github.com/rookslog/zlibrary-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rookslog/zlibrary-mcp/actions/workflows/ci.yml)
[![npm version](https://img.shields.io/npm/v/zlibrary-mcp)](https://www.npmjs.com/package/zlibrary-mcp)
[![License: MIT](https://img.shields.io/github/license/rookslog/zlibrary-mcp)](LICENSE)

An acquisition server for books and articles: find an edition, inspect its metadata,
and download the original file. Five MCP tools serve Library Genesis, Z-Library and
Anna's Archive. Book contents never enter the tool response; paths refer to the
server's disk. See [VISION.md](VISION.md) for the commitments and non-goals.

**Release status:** This README describes the 2.0 interface, which is not yet published on npm or as a Docker image.
`npm install -g zlibrary-mcp` and the Docker image `ghcr.io/rookslog/zlibrary-mcp:latest`
currently provide 1.4.0, whose tools differ; use the
[v1.4.0 README](https://github.com/rookslog/zlibrary-mcp/blob/v1.4.0/README.md) for that release,
or [install from source](#from-source-for-development) for 2.0.

**2.0 is a breaking interface change.** Saved 1.x tool names are not aliases.
See the complete [migration table](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md) and
[API reference](docs/api.md). The npm package, executable, repository and client
configuration keys keep their existing names; `serverInfo.name` is `book-acquisition`.

## Quick Start

**Release status:** This README describes the 2.0 interface, which is not yet published on npm or as a Docker image.
`npm install -g zlibrary-mcp` and the Docker image `ghcr.io/rookslog/zlibrary-mcp:latest`
currently provide 1.4.0, whose tools differ; use the
[v1.4.0 README](https://github.com/rookslog/zlibrary-mcp/blob/v1.4.0/README.md) for that release,
or [install from source](#from-source-for-development) for 2.0.

**Prerequisites:** Node.js 22+, Python 3.10+, [uv](https://docs.astral.sh/uv/).

```bash
npm install -g zlibrary-mcp
cd "$(npm root -g)/zlibrary-mcp"
bash setup-uv.sh --no-dev
```

Add the server to your MCP client configuration:

```json
{
  "mcpServers": {
    "zlibrary": {
      "command": "zlibrary-mcp"
    }
  }
}
```

No credentials are needed for Library Genesis. The server starts without a
Z-Library account; it warns on stderr and keeps the other sources available.
For Z-Library, add `ZLIBRARY_EMAIL` and `ZLIBRARY_PASSWORD` in the client's `env`.
For Anna's Archive downloads, add `ANNAS_SECRET_KEY`. Its search needs no key, but the
site's browser check often refuses it ([#139](https://github.com/rookslog/zlibrary-mcp/issues/139)).

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `search` | Merged catalogue metadata search; per-source answers, compact file rows and refs |
| `get_book` | Inspect a row ref for edition metadata and field-origin evidence |
| `download` | Acquire a ref to server disk; return a file path and acquisition record path |
| `get_limits` | Local source capabilities by default; live account quota on request |
| `get_history` | Z-Library account history, not a list of local files |

```jsonc
// Local configuration and source capabilities; no network call
{}
// search
{ "query": "Distributed Systems", "sources": ["libgen", "zlibrary"], "limit": 5 }
// get_book: copy the actual ref from a returned row
{ "ref": "<returned ref>" }
// download: use the same ref
{ "ref": "<returned ref>", "filename": "distributed-systems.pdf" }
// get_limits: only request live numbers when they affect a decision
{ "mode": "live", "sources": ["zlibrary"] }
```

`search` requires `query`, `author`, or both. `exact` checks normalized phrases
locally on every source: query in title, author in author, separately. This is
catalogue metadata search, not search inside book text.

`sources` is a set of 1–3 distinct names; order has no effect. Omit it to use
`get_limits.search.default_sources`: usable installed search sources, excluding
Anna's until its route is on. The operator can override this with
`BOOK_SOURCE_DEFAULT_ORDER`. Missing credentials or an open breaker do not
silently remove a source explicitly named by the caller or operator.

The server asks admitted sources together and freezes one merged answer.
`sources` accounts for every registered source and `all_answered` covers the
resolved intent. With multiple dispatched jobs, the default wait is 12 seconds;
unfinished jobs are `late`. One dispatched job keeps its ordinary bridge budget.
`late` says the reply stopped waiting, not that a network request was issued or
physical cleanup finished. Read `note` for incomplete and bounded-window limits.

Rows group only equal catalogue MD5, which is a claim about a file, not checked
bytes. `ref` recommends one source copy; `other_copies` holds actual selectable
alternatives. The server never switches a download to another copy. Ordering
uses request fit and source rank, not source preference or arrival time.
Labelled likely relations are hints, not merged editions.

Year, language, format and `exact` are checked locally, with AND across fields
and OR within lists. Missing checked metadata is excluded and counted. Each
answered source reports examined/matched/rejected/unknown counts from at most
50 candidates; these are not catalogue totals. Z-Library also receives native
filters, independently of the local checks. Language aliases are deduplicated;
formats ignore case and one leading dot. Empty lists and `exact:false` add no
constraint. See [the filter contract](docs/api.md#filters).

Search refs and snapshot cursors share a 128-search process-local LRU. Each
search retains up to 20 windows; one-source refills are single-flight and
memoized. Replaying a cursor is local. Refs and cursors evict together; history
has its own existing handle store and upstream continuation behavior.

LibGen resolves fresh links and tries mirrors by bytes actually served, not just
by obtaining a key. Anna's Archive protects its free download route with browser
verification; this server's shipped download route uses its API key. Keys are
sent only to the trusted hosts in `lib/sources/config.py`. `also_available_on`
is an upstream hint, not a promise that another source still has the file.

## Download output

```json
{
  "path": "/server/downloads/book.pdf",
  "bytes": 5680123,
  "reused": false,
  "transfer_skipped": false,
  "verified": "sha256",
  "record": "/server/downloads/book.pdf.acq.json",
  "source": "libgen"
}
```

The record is a versioned JSON sidecar containing edition fields, identifiers,
provenance and a SHA-256 digest. Unknown edition fields are explicit `null`.
A downstream library ingests the sidecar; the tool does not manage that library.

Before authentication or transfer, `download` takes a per-destination exclusive
lock and reads the receipt from disk. A matching identity and freshly recomputed
SHA-256 skip the transfer without quota. `transfer_skipped: true` means no
transfer; `reused: true` means bytes were fetched and an identical existing file
was adopted, so quota may have been spent. Both cannot be true together.
Unreadable receipts and symlink/nonregular sidecars fail with `output_refused`;
they are not treated as permission to overwrite provenance or start a transfer.

An intent receipt precedes the request. If the acquired format corrects the
filename, the call retains its initial lease and takes a non-waiting lease on the
canonical target through receipt finalization; alias recovery also leases that
target before verifying it. Canonical hashed intent is persisted before the alias
intent and publication. Compatible complete canonical receipts, including their
provenance, remain authoritative. A partial or missing file does not qualify for
reuse. Different filenames/directories are different destinations. Pre-2.0 files
have no receipts. An occupied predicted destination without a matching receipt
is refused before transfer (`output_refused`, `destination_occupied: true`), even
if its bytes match. There is no zero-quota adoption path for old files; see
[existing-download migration](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md#existing-140-downloads).
Different existing bytes and verified foreign identities are never silently overwritten.

Downloads default to `${XDG_DATA_HOME:-~/.local/share}/zlibrary-mcp/downloads`
on Linux/macOS and `%LOCALAPPDATA%\zlibrary-mcp\downloads` on Windows. Set
`ZLIBRARY_OUTPUT_ROOT` to an absolute path (home expansion allowed) to choose
another root; a relative configured root fails startup. Relative `output_dir`
values always resolve under the root. Absolute paths inside it are accepted;
outside paths (including symlinks), any `..` component and a leading `~` are
refused before provider contact (`output_refused`, `request_issued: false`).
Trusted operators can set `ZLIBRARY_UNRESTRICTED_OUTPUT=1` to allow outside
absolute paths; relative paths still resolve under the root, and traversal/home
shorthand, NUL bytes and (on Windows) drive-relative (`C:foo`) or root-relative
(`\foo`) output paths stay refused in both modes. The flag accepts only `1`, `0`
or unset and logs a startup warning when enabled. If the root is unset and
`./downloads` exists,
startup reports the new location on stderr. See [download-location migration](docs/interface-migration.md#download-location).
On POSIX, new directory levels are 0700 without changing existing directories;
new artifacts and receipts are 0600. Artifacts already present keep their mode
on reuse; a receipt the server repairs is rewritten as 0600.
On Windows, chmod only toggles the read-only attribute; these modes do not set
access-control permissions.
`filename` is a sanitized basename; its extension is forced to the acquired
format. The root guard admits arguments; it is not a sandbox against a local
operator swapping directories during a call. On local filesystems, POSIX leases
use exclusive creation and `flock`, with inode-checked unlink while held. Windows
uses native nonblocking byte-range leases on persistent files: close or process
death releases the lease, so a remaining file does not mean it is held.
Unsupported locking fails with `output_refused` instead of starting an
uncoalesced transfer. Network-filesystem locking is not established here.

`quota_estimate`, when present, labels the last live observation and deductions
for issued transfers in this process. Other account clients can change the real
count. An ambiguous download or cancellation invalidates the estimate. Downloads
are not automatically retried: `retry` is `safe`, `may_spend_quota`, or `no`, and
cancelled calls require fresh user intent. See [errors](docs/api.md#errors).
Corrected-target contention refuses without waiting and can occur after a
quota-counting transfer (`request_issued: true`, `retry: "may_spend_quota"`); it
invalidates the estimate and is never automatically retried. Retryable LibGen
download failures are quota-safe (`retry: "safe"`), including after a request;
this does not waive provider rate limits or enable automatic retry.

## Installation

### From Source (for development)

```bash
git clone https://github.com/rookslog/zlibrary-mcp.git
cd zlibrary-mcp
npm install
uv sync
npm run build
```

Point the client's command at `node`, with `args` containing the absolute path
to `dist/index.js`. Python bridge files remain in `lib/`, not `dist/`.
The Python environment uses uv, never a separate system installer. End users
can omit development tools; see [Python dependencies](docs/optional-dependencies.md).
Text processing is not part of 2.0; see [preservation and migration](docs/text-processing-migration.md).
Existing downloaded and extracted files are not deleted.

### Docker (HTTP transport)

```bash
docker run -d --name zlibrary-mcp -p 8000:8000 \
  -v "$PWD/downloads:/app/downloads" \
  ghcr.io/rookslog/zlibrary-mcp:latest
```

Add account variables only for the sources you use. The container wraps stdio
with SuperGateway at `http://localhost:8000/sse`. Remote clients receive paths on
the **server**, not downloadable attachments or local client paths. There is no
file-reading tool in this five-tool interface.
The published Docker image is still 1.4.0 until 2.0 is released.

## Configuration

| Variable | Meaning |
|---|---|
| `ZLIBRARY_EMAIL`, `ZLIBRARY_PASSWORD` | Z-Library account; not required for other sources |
| `ANNAS_SECRET_KEY` | Anna's Archive membership key, required for its downloads only |
| `BOOK_SOURCE_DEFAULT_ORDER` | Optional comma-separated default source set and copy-route tie preference, e.g. `libgen,zlibrary`; no repeats |
| `BOOK_SOURCE_SEARCH_WAIT_MS` | Multi-source comparison wait in milliseconds; default `12000`; positive whole decimal within the Node timer range |
| `BOOK_FORMAT_PREFERENCE` | Optional comma-separated formats, e.g. `epub,pdf`; affects only likely-edition groups |
| `ZLIBRARY_OUTPUT_ROOT` | Absolute output root; defaults to the platform data directory described above |
| `ZLIBRARY_UNRESTRICTED_OUTPUT` | `1` allows outside absolute paths; `0` or unset keeps containment; other values fail startup |
| `ZLIBRARY_EAPI_DOMAIN` | Optional pinned EAPI domain |
| `LOG_LEVEL` | `silent`, `error`, `warn`, `info` (default), `debug`; diagnostics use stderr |
| `BOOK_SOURCE_WALK_BUDGET` | Search or download-resolution walk budget in seconds; default `165` |
| `BOOK_SOURCE_PREFLIGHT_TIMEOUT` | Aggregate DNS + TCP preflight budget in seconds; default `5` |
| `BOOK_SOURCE_DOWNLOAD_TIMEOUT` | Source resolution + transfer chain base in seconds; default `1500` |
| `BOOK_SOURCE_DOWNLOAD_FLOOR_SPEED` | Known-size allowance calculation in bytes/second; default `40960` (40 KiB/s), not a minimum-speed cutoff |
| `BOOK_SOURCE_DOWNLOAD_TIMEOUT_CEILING` | Size-scaled chain ceiling in seconds; default `3600`, effective ceiling at least the configured base |
| `PYTHON_BRIDGE_TIMEOUT` | Ordinary bridge deadline in milliseconds; default `240000` |
| `PYTHON_BRIDGE_LONG_TIMEOUT` | Acquisition deadline in milliseconds; default `3900000` |
| `ZLIBRARY_SESSION_MODE` | `memory` (default) or `per-call`; unknown modes fail at startup |

Search settings are validated at startup; malformed lists, wait or format values
refuse startup. Format entries normalize case and one leading dot; duplicates,
blanks and non-alphanumeric extensions are invalid. An unset format list installs
no preference. An empty source-order setting retains the unset behavior.

Each source walk shares one deadline across its configured mirror attempts;
fallback mirrors remain candidates rather than being capped in number. DNS and
TCP preflight share one aggregate budget. A mirror skipped for lack of walk time
is attributed with `walk_budget_exhausted`, which indicates a local budget limit,
not evidence that the host failed. Increase `BOOK_SOURCE_WALK_BUDGET` if needed;
if it is already clamped to the bridge owner, increase the corresponding owner.
LibGen and Anna's searches each run in their own bridge child; the walk budget
is separate from the multi-source comparison wait. Within each LibGen mirror in a comparison search,
preflight, pacing and search share one 5-second attempt allowance, clamped
by the remaining walk; this does not change download budgets.

For downloads, the resolution walk runs inside the source transfer-chain
allowance. Known size can extend that allowance from the base to the ceiling;
do not add the resolution walk again. Walk and transfer allowances are clamped
below their bridge owner with overhead reserved, and acquisition also reserves
finalization time. When used as source-walk owners, valid positive
`PYTHON_BRIDGE_TIMEOUT` / `PYTHON_BRIDGE_LONG_TIMEOUT` settings below `10000` ms
are rejected before provider contact. Invalid timer values fall back to defaults;
see [timeout configuration](docs/RETRY_CONFIGURATION.md#multi-source-timeout-configuration)
for parsing, clamping and acquisition deadline details.

In memory mode, reads and fresh Z-Library acquisitions share the same in-memory
session owner, cold-login coordination and bounded refresh/backoff. Acquisition
asks for authentication only after the held-lease receipt check and durable intent;
a verified local receipt needs no account request, even during login cooldown or
after restart. Once a transfer receives its session, authentication rejection does
not replay it. Explicit `per-call` mode retains per-call authentication.
Credentials and cookies are never written to acquisition records. Debug logs can include queries; inspect before sharing.
See [retry configuration](docs/RETRY_CONFIGURATION.md) for read-operation retry
and circuit-breaker settings. The acquisition call never automatically retries.

## Development

Use the [documentation index](docs/README.md) for installation, troubleshooting,
tool reference, contributing, architecture and releases.

```bash
npm run build
npm test
uv run pytest -rs
npm run test:rehearsal
npx tsc --noEmit
npx eslint src/
uv run ruff check lib/
bash scripts/validate-readme-tools.sh
```

Jest does not run pytest. Unit and offline rehearsal tests do not establish live
provider health. `npm run doctor` probes upstream services and audits release
records; `npm run audit:release` needs source history and GitHub access.
See [troubleshooting](docs/TROUBLESHOOTING.md), [contributing](CONTRIBUTING.md),
and [release process](docs/RELEASE_PROCESS.md).

## License

[MIT](LICENSE).
