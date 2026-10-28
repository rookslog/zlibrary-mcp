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
cookies/storage are never exported. See the
[operator instructions](../README.md#optional-visible-anna-browser).

For older text workflows, see [migration](text-processing-migration.md). A future
text add-on is not shipped by this change.
