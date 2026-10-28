# Python dependencies

The 2.0 server installs acquisition dependencies only. Text processing, OCR,
and their optional extras have been removed, not hidden behind a flag.

| Installation | Command | Includes |
|---|---|---|
| End user | `uv sync --no-dev` | Search, metadata, original-file download |
| Contributor | `bash setup-uv.sh` | Core plus test/lint tools in the dev group |
| Deployment | `bash setup-uv.sh --deploy` | Same core as end users |
| Visible Anna browser | `uv sync --extra annas-browser` | Core, dev tools, optional Playwright Python package |

Docker uses `uv sync --frozen --no-dev`. Contributor and CI suites collect without
processing libraries. `package = false` remains required for packed npm installs.

For visible Anna search/download resolution without dev tools, use `uv sync --no-dev --extra
annas-browser`. Google Chrome and a visible graphical session are separate
requirements. If Chrome is absent, `uv run --no-sync playwright install chrome`
installs it explicitly; `--no-sync` preserves the selected dependency tier.
On Linux this is a system package installation and can require root privileges.
An existing Google Chrome installation is selected by `channel="chrome"`, so it
does not need that installer command.
The base install never requires Playwright or Chrome. Set
`ANNAS_BROWSER_ENABLED=true` to allow search to start visible Chrome after an
HTTP browser check, and to resolve downloads when no key is configured or the
trusted fast endpoint rejects it. Healthy HTTP search and successful keyed
downloads start no browser. File bytes use the existing HTTP transfer; Chrome's
cookies/storage stay in Chrome by default. The optional search-session reuse
below exports earned cookies only to an in-memory HTTP client. See the
[operator instructions](../README.md#optional-visible-anna-browser).

For older text workflows, see [migration](text-processing-migration.md). A future
text add-on is not shipped by this change.

## Optional virtual display and search-session reuse

Both options require `ANNAS_BROWSER_ENABLED=true` and default off.
`ANNAS_BROWSER_DISPLAY=virtual` starts an owned Xvfb child on a free display,
with GLX disabled, and closes it after Chrome. Install Xvfb separately; set
`ANNAS_XVFB_PATH` to its executable or put `Xvfb` on `PATH`. A missing binary
returns `BrowserUnavailableError`; it never substitutes headless Chrome.
Unset or `desktop` keeps the existing visible persistent-profile launch.

`ANNAS_SESSION_REUSE=true` uses an ephemeral browser context instead of the
persistent profile. After settled search results, it keeps that context's
Anna cookies and exact user agent only in memory. The next search tries HTTP
with them first. Only HTTP 200 with settled results is accepted; rejection,
a challenge, partial HTML or an error discards the session and permits one
browser fallback. Redirects outside the original scheme, host or port are
refused before sending the session. Cancellation clears retained clearance.
`ANNAS_SESSION_TTL` is the lifetime in seconds (default 900); HTTP success
does not extend it. No storage-state file or cookie log is written.

The session is local to one adapter/bridge process. The current one-process-
per-operation MCP bridge cannot reuse it across separate tool calls; a
long-lived Python caller can. Browser downloads retain their existing route.
Both search methods hold the same lock, daily counter and 20-second pacing.
A rejected HTTP attempt and its browser fallback each count as a page fetch.
If pacing cannot fit the remaining deadline, the existing refusal applies.

Internal transparency hook: `AnnasBrowserSession.search_html` returns a string
with a `method` attribute (`browser` or `session`) and logs only that method.
`AnnasArchiveAdapter.last_fetch_method` exposes the latest successful search
transport (`http`, `browser` or `session`). Public tool/reply schemas are unchanged.
