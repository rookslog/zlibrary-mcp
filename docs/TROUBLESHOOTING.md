# Troubleshooting Guide

Common issues and solutions for the Z-Library MCP server.

> **Note**: The project uses [UV](https://docs.astral.sh/uv/) with a
> project-local `.venv/`. There is **no** cache venv at `~/.cache/zlibrary-mcp/` and no
> `.venv_config` file — if a guide mentions those, it predates the UV migration.

---

## Server exits immediately / won't start

### Symptoms

The MCP client reports the server disconnected, or `node dist/index.js` exits at once.

### Checks, in order

1. **Build output missing or stale**:
   ```bash
   npm run build      # compiles TypeScript and validates Python files exist
   ```
2. **Python venv missing** (fresh clone or after cleaning):
   ```bash
   bash setup-uv.sh --no-dev   # creates .venv/ with the end-user core
   ```
3. **Node version**: Node 22+ is required (see `engines` in `package.json` / `.nvmrc`).
4. **Z-Library credentials**: missing `ZLIBRARY_EMAIL` / `ZLIBRARY_PASSWORD`
   do not stop startup. The server warns on stderr and keeps LibGen available
   without an account. Set both variables in the environment or the client's
   `.mcp.json` `env` block only when using Z-Library operations.

A healthy start prints (to **stderr**):
```
Book acquisition MCP server is running via Stdio.
```

---

## ImportError: cannot import name ... from 'zlibrary'

### Symptom

MCP tools fail with:
```
ImportError: cannot import name 'Extension' from 'zlibrary' (unknown location)
```

### Cause

The project-local `.venv/` is missing, stale, or was created before the vendored
`./zlibrary` fork changed. The fork is installed editable, so the venv must be in sync
with the working tree.

### Solution

```bash
uv sync --no-dev
.venv/bin/python -c "from zlibrary import Extension; print('OK')"
```

If you **moved the project directory**, `.venv/` moves with it (it is project-local),
but run `uv sync --no-dev` once from the new location to be safe, and update any
absolute paths in your clients' `.mcp.json`.

### Which UV sync command should I use?

| Need | Command |
|---|---|
| Search, metadata, and downloads | `uv sync --no-dev` |
| Contributor test and lint tools | `bash setup-uv.sh` |

Text processing is removed from 2.0, not an optional install tier. See
[migration](text-processing-migration.md) to keep an isolated 1.4.x installation.
The retained test suite collects using only core dependencies and the dev group.

---

## Python bridge script not found at: .../lib/python_bridge.py

### Symptom

```
Python bridge script not found at: /path/to/dist/lib/python_bridge.py
This usually indicates a build or installation issue.
```

### Solution

Python sources stay in `lib/` (they are not copied into `dist/`); the built JS resolves
them relative to the project root. Verify and rebuild:

```bash
npm run validate   # checks all Python bridge files exist
npm run build
```

See `docs/adr/ADR-004-Python-Bridge-Path-Resolution.md` for how resolution works.

---

## Network / upstream failures (searches fail, downloads fail)

### First step: run the doctor

```bash
npm run doctor
```

It probes the live endpoints the server actually uses (Z-Library EAPI, Anna's Archive,
LibGen) and reports OK/WARN/FAIL per surface. Required failures point at real breakage;
optional failures usually mean a secondary source is unreachable.

### Common causes

- **Anti-bot wall on a Z-Library domain** (`redirect_refused` or `diamwall_blocked` in the health check, or
  the doctor reporting "DiamWall anti-bot wall"): some Z-Library domains (notably
  `z-library.sk` and `1lib.sk` since 2026-07) block programmatic `/eapi` access with
  the DiamWall wall — a 307 self-redirect before the wall's 513/517 "Access Denied".
  Credential-bearing requests now stop at that 307 with `Redirect refused` rather
  than follow it. The server probes its candidate list (`z-library.ec` first)
  with an unauthenticated one-row `POST /eapi/book/search`. If none passes, it makes
  one initial login attempt to surface the error. A redirect refusal, connection
  failure/timeout, wall or non-JSON login reply can advance the walk, but each
  subsequent host must pass the probe and is attempted at most once. Read/write
  timeouts do not start this walk. Auth rejection,
  other HTTP errors and an explicit pin stop it. Domain discovery also probes
  advertised candidates. If every built-in candidate is walled, independently
  verify a working domain before pinning it:
  ```bash
  export ZLIBRARY_EAPI_DOMAIN=<working-domain>
  ```
  A pinned domain is used exactly as given — no probing, no silent switching — so
  unset it again to return to automatic fallback.
- **`Redirect refused (HTTP NNN)`**: the configured endpoint answered with a
  redirect. This is an upstream failure, not evidence of a bad password/key.
  Requests carrying credentials do not follow Location, even to the same host.
  An unpinned login can use another configured host only under the probe rule above;
  a pinned login stays on that host. For a wall's 307, try an independently verified
  working domain or another source rather than retrying the same wall repeatedly.
  If the service has moved to an unlisted domain, verify that domain independently
  before setting `ZLIBRARY_EAPI_DOMAIN`, or use a release with an updated candidate
  list. Do not treat the hostname in an error as proof that it is trusted.
  For a file-download refusal, link resolution may already have consumed quota;
  a manual retry may spend more. Wait for a usable direct link or choose another
  configured source. Anna's member page GETs are the bounded exception: at most
  five same-origin hops; its account POST and key-bearing API calls follow none.
- **Region blocking**: Z-Library domains are blocked on some networks. Domain discovery
  ("hydra mode") normally finds a working mirror at runtime; you can pin one explicitly:
  ```bash
  ZLIBRARY_EAPI_DOMAIN=<working-domain> npm run doctor
  ```
  The same variable works for the server and the integration tests.
- **Bad credentials**: login failures surface as auth errors, not network errors —
  re-check `ZLIBRARY_EMAIL` / `ZLIBRARY_PASSWORD`.
- **Genuine upstream drift**: if the doctor reports a contract change (JSON shape
  changed), that is a bug — please open an issue and include the doctor output.

---

## MCP tools work locally but fail in Claude Code / other clients

### Causes

1. Relative path to `dist/index.js` in `.mcp.json` — must be absolute
2. Credentials missing from the client's `env` block
3. A modified build printing to stdout (see next section)

### Solution

```json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/absolute/path/to/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your-email@example.com",
        "ZLIBRARY_PASSWORD": "your-password"
      }
    }
  }
}
```

Use `node`, not `uv`, as the command — the Node entry point locates the UV venv itself.
Restart the client after changes.

---

## Strict clients disconnect (developers)

Under the stdio transport, **stdout carries JSON-RPC and nothing else**. A single
`console.log` anywhere in `src/` corrupts the protocol stream and strict clients drop
the connection. Use the `logger` from `src/lib/logger.ts` (writes to stderr).
`__tests__/stdio-purity.test.js` enforces this at build time — run `npm test` before
debugging the client side.

---

## Retained parser fixtures (developers)

This checkout keeps two HTML fixtures: `test_files/annas/search_results.html`
for Anna's metadata parsing and `test_files/libgen/mixed_book_article_results.html`
for LibGen book/article parsing and the offline upstream-probe regression.
Retired processing PDFs, extracted text, annotations and guides are absent.
The acquisition tests do not require their LFS objects or processing engines.

Historical processing test instructions belong to the separate preserved text
installation; see [the migration guide](text-processing-migration.md#preserved-implementation-and-unresolved-work).

### Which tests need what

| Subset | Command | Needs |
|---|---|---|
| Fast (what CI gates PRs on) | `uv run pytest -m "not slow and not integration and not performance"` | nothing extra |
| Preserved processing corpus | Use the isolated preserved text installation | Separate processing environment |
| Live integration | `uv run pytest -m integration` | `ZLIBRARY_EMAIL`/`ZLIBRARY_PASSWORD` (auto-**skips** without them) |
| E2E | `npm run test:e2e` | Docker |

---

## Quick diagnostic commands

Run from the repository or installed-package root after initializing `.venv/`.
The import check uses that existing environment without synchronizing it and
adds the bridge directory for its script-style imports.

```bash
npm run validate                  # Python bridge files present?
uv run --no-sync python -c "import sys; sys.path.insert(0, 'lib'); import python_bridge; print('bridge imports OK')"
npm run doctor                    # live upstream contract check
npm test                          # Jest (Node side, includes stdio purity)
uv run pytest -m "not slow and not integration and not performance"  # fast Python suite
```

---

## Getting Help

1. Run `npm run doctor` and `npm run validate` and capture the output.
2. Check existing issues: https://github.com/rookslog/zlibrary-mcp/issues
3. Open a bug report — the issue template asks for the doctor output and your
   OS/client; scrub any credentials from pasted stderr logs first.

---

**Last verified**: 2026-07-24
