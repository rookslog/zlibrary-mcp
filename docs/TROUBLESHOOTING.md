# Troubleshooting Guide

Common issues and solutions for the Z-Library MCP server.

> **Note**: Since v2.0.0 the project uses [UV](https://docs.astral.sh/uv/) with a
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
4. **Credentials**: `ZLIBRARY_EMAIL` / `ZLIBRARY_PASSWORD` must be set in the
   environment or the client's `.mcp.json` `env` block.

A healthy start prints (to **stderr**):
```
Z-Library MCP server (ESM/TS) is running via Stdio...
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

Bare `uv sync` installs UV's default development group in this project; it is not the
lightweight end-user environment. Choose the tier explicitly:

| Need | Command |
|---|---|
| Search, metadata, and downloads | `uv sync --no-dev` |
| PDF/EPUB extraction and footnote detection | `uv sync --no-dev --extra rag` |
| Complete scholarly analysis and OCR libraries | `uv sync --no-dev --extra scholar` |
| Contributor test and lint tools | `uv sync --group dev` |

The optional extras remain opt-in even when the contributor development group is
installed. Contributors running the complete Python suite should use
`uv sync --group dev --all-extras`.

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

- **Anti-bot wall on a Z-Library domain** (`diamwall_blocked` in the health check, or
  the doctor reporting "DiamWall anti-bot wall"): some Z-Library domains (notably
  `z-library.sk` and `1lib.sk` since 2026-07) block programmatic `/eapi` access with
  the DiamWall wall — a 307 self-redirect, then 513/517 "Access Denied". The server
  handles this automatically: it probes its candidate list (`z-library.ec` first)
  with an unauthenticated `GET /eapi/info/domains` and logs in on the first healthy
  domain, and hydra-mode discovery skips advertised domains that fail the same
  probe. If every built-in candidate is walled on your network, pin a domain you
  know works:
  ```bash
  export ZLIBRARY_EAPI_DOMAIN=<working-domain>
  ```
  A pinned domain is used exactly as given — no probing, no silent switching — so
  unset it again to return to automatic fallback.
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

## Test suite fails on a fresh clone (PyMuPDF errors, tiny PDF files)

### Symptom

Many Python tests fail with PDF parse errors; files under `test_files/` are ~130 bytes.

### Cause

The test corpus is stored in **Git LFS**. Without LFS you have pointer files, not PDFs.

### Solution

```bash
sudo apt-get install -y git-lfs   # or brew install git-lfs
git lfs pull                      # from the repo root
file test_files/*.pdf             # should say "PDF document", not "ASCII text"
```

> Do not run `git lfs install` inside this repo — it refuses to overwrite the repo's
> existing `pre-push` hook. `git lfs pull` alone is enough for fetching objects.

### Which tests need what

| Subset | Command | Needs |
|---|---|---|
| Fast (what CI gates PRs on) | `uv run pytest -m "not slow and not integration and not performance"` | nothing extra |
| Full corpus | `uv run pytest` | LFS objects |
| Live integration | `uv run pytest -m integration` | `ZLIBRARY_EMAIL`/`ZLIBRARY_PASSWORD` (auto-**skips** without them) |
| E2E | `npm run test:e2e` | Docker |

---

## Quick diagnostic commands

```bash
npm run validate                  # Python bridge files present?
uv run python -c "import lib.python_bridge; print('bridge imports OK')"
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
