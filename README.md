# Z-Library MCP Server

[![CI](https://github.com/rookslog/zlibrary-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rookslog/zlibrary-mcp/actions/workflows/ci.yml)
[![npm version](https://img.shields.io/npm/v/zlibrary-mcp)](https://www.npmjs.com/package/zlibrary-mcp)
[![License: MIT](https://img.shields.io/github/license/rookslog/zlibrary-mcp)](LICENSE)

An acquisition server for books and articles: find an edition, inspect its metadata,
and download the original file. Five MCP tools serve Library Genesis, Z-Library and
Anna's Archive. Book contents never enter the tool response; paths refer to the
server's disk. See [VISION.md](VISION.md) for the commitments and non-goals.

**2.0 is a breaking interface change.** Saved 1.x tool names are not aliases.
See the complete [migration table](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/interface-migration.md) and
[API reference](docs/api.md). The npm package, executable, repository and client
configuration keys keep their existing names; `serverInfo.name` is `book-acquisition`.

## Quick Start

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
For Anna's Archive, `ANNAS_SECRET_KEY` enables fast downloads. The optional
visible-browser route below can resolve downloads without that key.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `search` | Catalogue metadata search with an explicit ordered source list; compact rows and refs |
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

`search` requires `query`, `author`, or both. With both, `exact` applies to the
whole combined query; author-only exact search retains quoted-author matching.
This is catalogue metadata search, **not search inside book text**.

`sources` names 1–3 distinct sources in order. The server stops at the first
source with rows and reports each attempt in `routing`. There is no hidden
preferred source. Omitting `sources` requires an operator-configured default;
otherwise `sources_required` includes the choices and their availability.

Refs are opaque, in-memory handles shared by this process's clients. The most
recent 5,000 row/cursor entries are held; reconnecting to the same process keeps
them, restarting does not. On `ref_unavailable`, search and choose the edition
again. Do not construct refs or pass book records back as tool arguments.

`limit` defaults to 10, maximum 50. Continue with **`cursor` alone**, with no
other arguments. Cursors bind the operation, source, filters and page size;
replaying one repeats the same position, including after a lost reply. Z-Library
pages are re-queried, not frozen: upstream changes can duplicate or omit rows.
Other sources expose one upstream page, sliced locally, with a stated row cap.

## Sources

| Source | Configuration | Search | Download | Limit |
|---|---|---|---|---|
| `libgen` | None | Yes | Yes | No daily limit |
| `zlibrary` | Account email and password | With account | With account | Provider daily quota |
| `annas` | Key required only for download | Without key | With membership API key | Membership-dependent |

`get_limits` defaults to `mode: "local"`: no network, no account call. It reports
configuration, search/download availability and supported operations for each
source. Supported operations stay the same when configuration changes; use the
availability fields to plan calls. Anna's search requires a usable HTTP(S)
`ANNAS_BASE_URL` (default `https://annas-archive.gl`); download and `configured`
additionally require a trusted host and `ANNAS_SECRET_KEY`.
`mode: "live"` adds Z-Library account numbers; remaining quota is omitted when
spend is unknown. A source failure remains
in that source's `error` entry without discarding other reports. `none`, `daily`,
`unknown` and `unparsed` are distinct limit kinds. Availability is local
configuration, not a reachability probe. Only Z-Library supports account history.

LibGen resolves fresh links and tries mirrors by bytes actually served, not just
by obtaining a key. Anna's Archive protects its free download route with browser
verification; downloads prefer its API key, with an optional visible-browser
fallback. Keys are sent only to the trusted hosts in `lib/sources/config.py`. `also_available_on`
is an upstream hint, not a promise that another source still has the file.

### Optional visible Anna browser

Anna's may answer ordinary HTTP searches with a DDoS-Guard browser check.
With `ANNAS_BROWSER_ENABLED=true`, search can then start Google Chrome on the
server's visible display, using its own dedicated profile. Install the optional
Python package with `uv sync --no-dev --extra annas-browser`; install Google
Chrome separately if needed (`uv run --no-sync playwright install chrome`).
On Linux that command installs system Chrome through the package manager and
can require root privileges. If Chrome is already installed, the route selects
it with `channel="chrome"`; skip the installer.
This option defaults off and adds no browser dependency to the base install.
Without this option, downloads still require the membership API key.

For search, Chrome starts only when the HTTP body is shaped like a browser check; a
`server: ddos-guard` header on an ordinary error does not start it. Chrome waits
for automatic verification, reads settled results, and closes before releasing the shared
profile lock. It never clicks a human check. If a person is needed, search
returns the existing `browser_check` failure and closes Chrome. The operator
must arrange a separate manual session using this dedicated profile, close it,
and retry after the backoff. A person's normal browser profile is never imported.

| Setting | Default / effect |
|---|---|
| `ANNAS_BROWSER_PROFILE_DIR` | `~/.cache/zlibrary-mcp/annas-browser-profile`; persistent clearance/profile |
| `ANNAS_BROWSER_MIN_INTERVAL` | 20 seconds between scheduled browser visits |
| `ANNAS_BROWSER_DAILY_LIMIT` | 30 scheduled visits per rolling 24-hour window; counts requests, not books |
| `ANNAS_BROWSER_BACKOFF` | 300 seconds after an uncleared check or refusal |
| `ANNAS_BROWSER_NAV_TIMEOUT` | 60 seconds for initial navigation |
| `ANNAS_BROWSER_SEARCH_TIMEOUT` | 120 seconds for opted-in search's HTTP/browser work and cleanup |
| `ANNAS_BROWSER_SETTLE` | Up to 90 seconds for automatic verification after navigation |
| `ANNAS_BROWSER_MAX_SERVERS` | 3 slow-partner candidates per download, in site order; unused by search |

Usage and lock files live beside the profile. Profiles with the same parent
share one budget; changing parent directories creates separate state. Counter
errors refuse the visit. Pacing counts scheduled visits; Chrome's automatic
challenge redirects and background requests are not separately counted.
If a required pacing/backoff wait cannot fit the remaining operation clock, the
call refuses immediately, names a retry time, and spends no daily slot.

`BOOK_SOURCE_TOTAL_TIMEOUT` keeps the HTTP phase at 45 seconds by default.
With the browser flag on, `ANNAS_BROWSER_SEARCH_TIMEOUT` gives that HTTP phase
and any browser fallback one shared 120-second allowance. L6's seven successful
capture upper bounds ranged from 21.350 to 84.883 seconds; they include pacing
and capture, so they do not measure an exact timeout failure rate.
The existing shared `BOOK_SOURCE_WALK_BUDGET` and enclosing bridge clock can
shorten this allowance. Downloads retain their existing timeout settings.

The search allowance starts after reachability preflight/client setup and covers
awaited HTTP, browser startup, lock and pacing waits, navigation, settling and
cleanup. Up to two seconds (at most a quarter of a short clock) are reserved
inside it for page/context/driver closes; slow closes trigger forced termination
before unlocking. On Linux the fallback pins owned process identities; without
pidfd support it can terminate only the owned driver. Synchronous HTML parsing,
local file operations and operating-system scheduling are not a hard real-time
guarantee. Real-Chrome forced cleanup remains unmeasured by the offline tests.
This option supplies no display service or headless qualification.

For downloads, the same flag allows Chrome to resolve a file URL when no key is
configured, or the trusted fast-download endpoint explicitly rejects the key
with HTTP 401/403. Other keyed errors and zero membership quota retain their
existing behavior. An untrusted base with a configured key remains a configuration
error; the browser does not hide that error.

Chrome reads the selected book page, follows its slow-partner links in site order
and waits for a visible file link without clicking a manual check. It closes
before handing the URL to the existing HTTP transfer, which receives no browser
cookies or storage, rejects HTML payloads, checks the requested MD5 and recovers
partial transfers. A failed file transfer can try the next partner, reopening
only that partner page with the same profile.

A first successful resolution spends two scheduled browser visits (book plus
partner); each additional partner spends one, up to `ANNAS_BROWSER_MAX_SERVERS`.
Search and download share the same counter, spacing and backoff. Membership quota
is omitted for browser results because page visits are not membership downloads;
the final allowed visit can still deliver its resolved file. `auto` stays
LibGen-first without a key, adding Anna's as a fallback only when this flag is on.
`get_limits` reports local eligibility, not a Chrome/display or live-site check.

Download resolution spends the router's shared walk clock; transfer time is
excluded from that resolution clock but remains inside the existing whole-download
and acquisition deadlines. The same bounded cleanup runs before each URL is
returned. Current live selectors, countdowns and cookie-free URLs across other
file hosts still require a live check; fixture tests do not establish them.

## Download output

```json
{
  "path": "/server/downloads/book.pdf",
  "bytes": 5680123,
  "reused": false,
  "transfer_skipped": false,
  "verified": "sha256",
  "record": "/server/downloads/book.pdf.acq.json"
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

The default directory is `ZLIBRARY_OUTPUT_ROOT`, otherwise `./downloads`.
`output_dir` must resolve inside the root when configured (including symlinks).
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

## Configuration

| Variable | Meaning |
|---|---|
| `ZLIBRARY_EMAIL`, `ZLIBRARY_PASSWORD` | Z-Library account; not required for other sources |
| `ANNAS_SECRET_KEY` | Anna's membership key for fast downloads; optional with the enabled visible-browser route |
| `BOOK_SOURCE_DEFAULT_ORDER` | Optional comma-separated order, e.g. `libgen,zlibrary`; no repeats |
| `ZLIBRARY_OUTPUT_ROOT` | Optional allowed output root and default download directory |
| `ZLIBRARY_EAPI_DOMAIN` | Optional pinned EAPI domain |
| `LOG_LEVEL` | `silent`, `error`, `warn`, `info` (default), `debug`; diagnostics use stderr |
| `PYTHON_BRIDGE_TIMEOUT` | Ordinary bridge deadline in milliseconds |
| `PYTHON_BRIDGE_LONG_TIMEOUT` | Acquisition deadline in milliseconds |
| `ZLIBRARY_SESSION_MODE` | `memory` (default) or `per-call`; unknown modes fail at startup |

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
