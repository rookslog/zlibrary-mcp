# Deployment Guide - Edge Cases and Platform Considerations

This document covers deployment scenarios and platform-specific considerations for the Z-Library MCP server. It opens with the record of how the maintainer's own machines run the server.

---

## Table of Contents

0. [Authoritative deployment on the maintainer's machines](#authoritative-deployment-on-the-maintainers-machines)
1. [Path Resolution Overview](#path-resolution-overview)
2. [Standard Deployment](#standard-deployment)
3. [Edge Cases](#edge-cases)
4. [Platform-Specific Notes](#platform-specific-notes)
5. [Troubleshooting](#troubleshooting)

---

## Authoritative deployment on the maintainer's machines

Recorded 2026-11-23 after the signal "many deployments, none authoritative" ([#430](https://github.com/rookslog/zlibrary-mcp/issues/430)). This section names roles, not hosts: the machines are "the Mac" (workstation) and "the Linux server" (holds the library index and the ChatGPT tunnel). Paths below are relative to each user's home. Host names, endpoint IDs and credential locations stay in the operator's private notes.

### The rule

Each machine has **one install**, at `~/.local/share/zlibrary-mcp/current`, a symlink to `releases/<commit>/`. Each release folder is a clean clone of a commit on GitHub, built in place (`npm ci`, `npm run build`, `uv sync --frozen --no-dev`); it is never edited or rebuilt afterwards. Every stdio client registration starts `node ~/.local/share/zlibrary-mcp/current/dist/index.js` and sets `ZLIBRARY_OUTPUT_ROOT` (see [Download folder](#download-folder)). Linux registrations keep their existing wrapper, which loads credentials from the user's environment file before `exec`.

**Chosen release: `master` at `70eebda`** (merge of #410, 2026-11-08), because it is the newest reviewed revision that can be rebuilt from GitHub. Of the builds the clients ran before, only an August feature-branch checkout (`bfef94b`) is on a GitHub branch. The others came from branches that exist on one machine only.

Node resolves the symlinked entry point to its real path at start-up. A running session therefore keeps the release it started with when `current` moves, so an upgrade affects new sessions only.

### Inventory before and after (2026-11-23)

| Role | Before | After |
| --- | --- | --- |
| Mac, Claude Code (user scope) | global npm install, `1.4.0+2d4d9c0` (built 2026-10-28 from a branch that is not on GitHub) | `current` → `70eebda` |
| Mac, Codex | same global install, off by operator decision since 2026-10-19 | path moved to `current`; still off |
| Mac, Claude Desktop | not registered | unchanged |
| Mac, an April file mirror of a Linux dev checkout | its own `.mcp.json` self-registration | unchanged (archive; starts only if a session is opened inside it) |
| Linux dev checkout of this repository | its own untracked `.mcp.json` runs its own `dist/` | unchanged (development loop) |
| Linux, Claude Code (user scope) | separate clone `1.4.0+2d4d9c0`, same build | `current` → `70eebda` |
| Linux, Codex (user scope) | same clone, off | path moved to `current`; still off |
| Linux, four project `.mcp.json` files | three on the `2d4d9c0` clone, one on an August dev checkout (`bfef94b`) | all on `current` |
| Linux, one project Codex config | August dev checkout | `current` |
| Linux, ChatGPT tunnel (systemd user service, plus a 2-minute child watchdog) | deploy-line build `899debf`, running since 2026-11-06 | **unchanged; open exception, see below** |
| Linux, 6-hourly authenticated live check | starts the tunnel's launcher, so it tests whatever the tunnel serves | unchanged |
| Mac, freshness check on the live check (launchd) | monitor only, starts no server | unchanged |
| Linux, `zlib-pages` / `zlib-map-warm` inbox jobs | post-processing of downloads, start no server | unchanged (`zlib-pages` was already in a failed state; not investigated) |
| Linux, ten older tunnel builds and several feature worktrees | rollback archive and development | unchanged, unreferenced |

The old installs remain on disk and are no longer referenced by any client registration. They cannot be renamed or removed yet: at the time of the change, 14 Mac and 24 Linux sessions were running from them, and each session spawns the Python bridge from its install folder on every call.

### Gate passed before any client was moved

On each machine, a fresh process of the old and the new build was started and given `initialize`, `tools/list` and `get_limits(mode="local")`:

- Both builds expose the same five tools: `download`, `get_book`, `get_history`, `get_limits`, `search`.
- The sources configured in each build matched the old build on the same machine. On the Linux server, Z-Library and libgen were configured and Anna's was not, in both builds. On the Mac, only libgen was configured in either build when started from the agent's shell, and one running Mac session's environment had no `ZLIBRARY_*` variables. Other Mac sessions were not checked.
- A libgen-only `search` through `current` succeeded on each machine. Its Python bridge child ran from `releases/70eebda/`, the resolved path, and not from `current/`.
- `claude mcp list`, from a new process in the home folder and in each of the four project folders, reported the server as `✔ Connected` on the `current` path.

`master` does not stamp a commit into the build. Its server reports a bare `1.4.0`, where the old build reported `1.4.0+2d4d9c0`. The commit is recoverable from each release folder's `git rev-parse HEAD`.

### Z-Library searches return HTTP 400 on both builds (provider side, 2026-11-23)

After the change, every Z-Library-source search on the Linux server failed with `provider_error`, `http_error`, `http_status: 400`. That included the control query "Of Grammatology Derrida" and the live check's own query, "Nicomachean Ethics". The old build (`2d4d9c0`) and the new build (`70eebda`) failed identically:
- with the same environment file;
- with the same account (the credentials match those the tunnel uses, compared by hash only);
- with and without the tunnel's domain pin.

So the deploy did not cause the failures. The server reports the status code but not the response body. An unauthenticated one-row `POST /eapi/book/search`, the request the bridge uses to probe a domain, gets the following replies:
- `z-library.ec`: `HTTP 400 {"success":0,"error":"Access denied"}`, from both machines.
- `z-library.sk` and `1lib.sk`: DiamWall `307` redirects.

`/eapi/info/domains` still answers `200`. The tunnel's 6-hourly live check last passed at 05:20Z the same day. Whether the account, the address or the whole API is refused is unchecked: both machines may share one public address, and the authenticated reply body is not surfaced. Do not retry around it; follow #125 and the source-health checks.

### Download folder

The old build wrote downloads to `<working directory>/downloads/`, so files landed in whatever repository a session was started in. `master` defaults to `$XDG_DATA_HOME/zlibrary-mcp/downloads` and honours `ZLIBRARY_OUTPUT_ROOT`, an absolute path. When that variable is set, the server also refuses a `download` whose `output_dir` lies outside the folder, unless `ZLIBRARY_UNRESTRICTED_OUTPUT=1`.

Every client registration now sets `ZLIBRARY_OUTPUT_ROOT`:
- **Linux server:** `/data/library/originals/zlib`, one of the library index's acquisition roots.
- **Mac:** `~/Downloads/zlib`. The Mac has no library index; move books to the server's library deliberately.
- **ChatGPT tunnel:** keeps its own inbox, which its post-processing jobs watch.

A libgen download through a fresh `current` process on the Mac landed in `~/Downloads/zlib/`, with its `.acq.json` record, and nothing appeared in the working directory. The same path is unchecked on the Linux server, where only the configuration and the wrapper's handling of the variable were checked. Sessions started before the change still run the old build and still write to their working directory until they restart.

### Open exception: the ChatGPT tunnel

The tunnel runs `899debf`. That commit is on a deploy line that exists only in a repository on the Linux server: 64 commits are absent from `master`, and 141 `master` commits are absent from it. The line carries the chat-files tools and the Anna's browser route. `master` has no chat-files code and no `ANNAS_BROWSER_*` setting. Checked with `git grep` on `origin/master`, against the positive control `get_limits`, which matched.

It was not moved, for three reasons:

- Moving it would remove tools that the ChatGPT connector uses.
- A launcher edit only takes effect when the tunnel's child process is next respawned, by a crash or by the watchdog, and nobody would be watching that swap.
- The watchdog finds the child by a substring of its install path. A new path needs a matching watchdog pattern, or the watchdog will restart the tunnel up to three times an hour.

Closing the exception needs a maintainer decision. Either the deploy line is merged into `master` (#430, [#431](https://github.com/rookslog/zlibrary-mcp/pull/431)), or the connector accepts losing those tools. After that decision, point the launcher at `current`, update the watchdog pattern, and restart once while watching.

### Roll back

Each machine keeps backups under `~/.local/share/zlibrary-mcp/backups/<UTC timestamp>/` (`claude.json`, `codex-config.toml`), one folder for the repoint and a later one for the download-folder change. Each Linux project file has a copy beside it for each change, named `<file>.bak-<UTC timestamp>-single-deploy` and `<file>.bak-<UTC timestamp>-output-root`. Those copies can hold credentials, so each project repository's local `.git/info/exclude` ignores them.

- **One client:** copy its backup over the file. For Claude Code user scope, prefer `claude mcp remove zlibrary -s user`, then `claude mcp add-json zlibrary '<old JSON>' -s user`, because running sessions rewrite `~/.claude.json`.
- **The whole release:** point `current` back with `ln -sfn <old install folder> ~/.local/share/zlibrary-mcp/current`. The old installs are intact.

### Retiring the old installs

Remove an old install only when `pgrep -f '<old install path>/dist/index.js'` returns nothing. On the Mac, run `npm rm -g zlibrary-mcp`. On the Linux server, remove the clone. Remove a release folder only once no process runs from it.

### What made the sprawl possible

- **No owner of the running estate.** Lanes and sessions installed what they needed and registered it with whatever client they were in, and no one reconciled the results.
- **No release path from `master` to the machines.** Tags and the npm/GHCR workflows publish packages, but nothing installs a reviewed revision on the machines that use it. `package.json` has said `1.4.0` since the August tag, so builds with different code all reported the same version.
- **Ad-hoc installs per session.** Each built in a working copy or worktree, and project-scope `.mcp.json` files pinned whichever checkout was nearby. The tunnel kept one worktree per build, and one build's stamp recorded `commit: null`.

### Recommended release workflow (for the project lead to set up)

1. **One promotion script.** It takes a commit and runs: clean clone into `releases/<commit>`, `npm ci`, build, `uv sync --frozen --no-dev`, the gate above, a receipt (commit, time, tool list), then `ln -sfn` `current`. It never builds inside a live folder.
2. **Stamp the commit into `serverInfo` on `master`.** The deploy line already does this as `1.4.0+<sha>`. Bump the version at each promotion.
3. **Register clients only on `current`.** No project-scope pins to working copies. A development session in this repository uses its own `dist/` on purpose.
4. **Promote by commit, on both machines at once.** Record the commit on each machine, keep the previous release for rollback, and delete releases only when no process uses them.
5. **Report drift.** Extend the existing live check to report the commit that served it, compared with each machine's `current`.
6. **Bring the tunnel onto the same path** once the deploy line is merged. The full design is in #431.

---

## Path Resolution Overview

The Z-Library MCP server uses a dual-language architecture:
- **TypeScript/Node.js** (compiled to `dist/`) - MCP server
- **Python** (in `lib/` directory) - Z-Library operations

**Path Resolution Strategy**: Python scripts stay in source `lib/` directory, TypeScript references them via relative paths from `dist/`.

**For details**: See [ADR-004](adr/ADR-004-Python-Bridge-Path-Resolution.md)

---

## Standard Deployment

### ✅ Supported Scenarios

#### 1. **Standard npm Install**
```bash
npm install
npm run build
npm start
```

**Path Resolution**: ✅ Works
- `dist/lib/` → `../..` → project root → `lib/python_bridge.py`

---

#### 2. **Development with npm link**
```bash
cd /path/to/zlibrary-mcp
npm link

cd /path/to/your-project
npm link zlibrary-mcp
```

**Path Resolution**: ✅ Works
- Symlinks followed by `path.resolve()`
- Source `lib/` directory accessible

---

#### 3. **Clone and Build**
```bash
git clone https://github.com/rookslog/zlibrary-mcp.git
cd zlibrary-mcp
npm install
npm run build
node dist/index.js
```

**Path Resolution**: ✅ Works
- Standard directory structure maintained
- Build validation ensures files present

---

## Edge Cases

### 1. Docker Containers

#### Scenario
Running MCP server in Docker container with volume mounts.

#### Considerations
```dockerfile
FROM node:18

WORKDIR /app

# Copy package files
COPY package*.json ./
RUN npm install

# Copy source and build
COPY . .
RUN npm run build

# IMPORTANT: Must include lib/ directory
# Path resolution expects: dist/lib/ → ../../lib/
COPY lib/ ./lib/

CMD ["node", "dist/index.js"]
```

**Status**: ✅ **Should Work**

**Requirements**:
- Must copy `lib/` directory to container
- Directory structure must be maintained: `dist/` and `lib/` as siblings
- Build validation will catch missing files

**Dockerfile Example**:
```dockerfile
# ✅ Correct: Maintains structure
COPY dist/ ./dist/
COPY lib/ ./lib/
COPY requirements.txt ./

# ❌ Wrong: Breaks path resolution
COPY dist/ ./
COPY lib/ ./dist/lib/  # Don't do this!
```

---

### 2. Global npm Install

#### Scenario
```bash
npm install -g zlibrary-mcp
zlibrary-mcp
```

**Status**: ⚠️ **Needs Testing**

**Potential Issues**:
- Global install location varies by platform
- Path resolution may need adjustment
- `lib/` directory must be included in npm package

**package.json Consideration**:
```json
{
  "files": [
    "dist/",
    "lib/",
    "requirements.txt"
  ]
}
```

**Testing Needed**: Cross-platform global install verification

---

### 3. Symlinked Directories

#### Scenario
Project or lib/ directory is a symbolic link.

```bash
ln -s /real/path/to/zlibrary-mcp /symlink/zlibrary-mcp
cd /symlink/zlibrary-mcp
npm start
```

**Status**: ✅ **Works**

**Reason**: `path.resolve()` follows symlinks by default in Node.js

**Verified**: ✅ Path resolution handles symlinks correctly

---

### 4. Monorepo/Workspaces

#### Scenario
MCP server as part of larger monorepo (yarn workspaces, pnpm, Lerna, etc.)

```
monorepo/
├── packages/
│   ├── zlibrary-mcp/
│   │   ├── dist/
│   │   ├── lib/
│   │   └── package.json
│   └── other-package/
└── package.json
```

**Status**: ✅ **Should Work**

**Requirements**:
- Maintain relative structure within `packages/zlibrary-mcp/`
- Build process respects directory structure
- Build validation runs per-package

---

### 5. Packaged Binaries (pkg, nexe)

#### Scenario
Creating standalone executables with `pkg` or `nexe`.

**Status**: ⚠️ **Complex**

**Challenges**:
- Python scripts must be bundled or referenced externally
- Path resolution needs adjustment for bundled context
- Virtual filesystem complications

**Recommendations**:
1. **Option A**: Bundle everything
   - Package `lib/` as assets
   - Update path resolution to use pkg assets API

2. **Option B**: External Python scripts
   - Keep `lib/` external
   - Set `ZLIBRARY_LIB_PATH` environment variable
   - Update code to check env var first

**Not Currently Supported**: Requires additional development

---

### 6. AWS Lambda / Serverless

#### Scenario
Deploying as serverless function.

**Status**: ⚠️ **Needs Adaptation**

**Considerations**:
- Lambda has read-only filesystem except `/tmp`
- Python venv needs special handling
- Cold start implications

**Recommendations**:
1. Use Lambda Layer for Python dependencies
2. Include `lib/` in deployment package
3. Adjust venv manager for Lambda environment

**Not Currently Optimized**: Standard deployment preferred

---

## Platform-Specific Notes

### Linux

**Status**: ✅ **Fully Tested**

**Path Resolution**: Works as designed
```typescript
path.resolve(__dirname, '..', '..', 'lib', 'python_bridge.py')
// /home/user/project/lib/python_bridge.py ✅
```

**Python**: Works with system Python or venv

---

### macOS

**Status**: ⚠️ **Needs Testing**

**Expected**: Should work identically to Linux
- POSIX-compliant paths
- `path.resolve()` behavior same as Linux

**Testing TODO**:
- [ ] Standard npm install
- [ ] Development workflow
- [ ] Build validation
- [ ] Integration tests

---

### Windows

**Status**: ⚠️ **Needs Testing**

**Path Resolution**: Should work (Node.js handles path normalization)
```typescript
path.resolve(__dirname, '..', '..', 'lib', 'python_bridge.py')
// C:\Users\user\project\lib\python_bridge.py ✅
```

**Potential Issues**:
- Python venv activation (different script: `venv\Scripts\activate`)
- Line endings (CRLF vs LF in Python scripts)
- Path separator handling (handled by `path` module)

**Testing TODO**:
- [ ] Build on Windows
- [ ] Run tests on Windows
- [ ] Verify venv creation
- [ ] Verify Python script execution

**Recommendations**:
- Use WSL2 for development (Linux environment)
- Test native Windows separately

---

## Troubleshooting

### Issue: "Python bridge script not found"

**Symptom**:
```
Error: Python bridge script not found at: /path/to/dist/lib/python_bridge.py
This usually indicates a build or installation issue.
```

**Causes**:
1. Build didn't complete successfully
2. `lib/` directory not present
3. Directory structure broken

**Solutions**:
1. Run build validation:
   ```bash
   npm run validate
   ```

2. Check directory structure:
   ```bash
   ls -la lib/python_bridge.py  # Should exist
   ls -la dist/lib/              # Should NOT contain .py files
   ```

3. Rebuild:
   ```bash
   npm run build
   ```

---

### Issue: Path Resolution in Custom Deployment

**Symptom**: Script found in development but not in production

**Debug Steps**:
1. Verify directory structure:
   ```bash
   tree -L 2 -d
   # Should show:
   # ├── dist/
   # │   └── lib/
   # └── lib/
   ```

2. Check path resolution at runtime:
   ```javascript
   const path = require('path');
   console.log('__dirname:', __dirname);
   console.log('Resolved path:', path.resolve(__dirname, '..', '..', 'lib', 'python_bridge.py'));
   ```

3. Verify build validation passed:
   ```bash
   npm run validate
   ```

---

### Issue: npm Package Missing lib/

**Symptom**: Works locally but not after npm install from registry

**Solution**: Update `package.json`:
```json
{
  "files": [
    "dist/",
    "lib/",
    "pyproject.toml",
    "setup-uv.sh"
  ]
}
```

**Verify**: Check what will be published:
```bash
npm pack --dry-run
```

---

## Testing Deployment Scenarios

### Quick Validation

```bash
# 1. Build validation
npm run validate

# 2. Integration tests
npm test __tests__/integration/

# 3. Manual verification
node -e "
import('./dist/lib/python-bridge.js').then(async (m) => {
  try {
    await m.callPythonFunction('get_download_limits', {});
    console.log('✅ Path resolution working');
  } catch (e) {
    if (e.message.includes('script not found')) {
      console.error('❌ Path resolution failed');
      process.exit(1);
    } else {
      console.log('✅ Script found (other error expected)');
    }
  }
});
"
```

---

## Future Improvements

### Planned
- [ ] Windows cross-platform testing
- [ ] macOS verification
- [ ] Global install testing
- [ ] Docker deployment example
- [ ] Serverless adapter

### Under Consideration
- [ ] Packaged binary support (pkg/nexe)
- [ ] Lambda Layer example
- [ ] Kubernetes deployment guide

---

## Resources

- **Architecture Decision**: [ADR-004](adr/ADR-004-Python-Bridge-Path-Resolution.md)
- **Build Validation**: `scripts/validate-python-bridge.js`
- **Integration Tests**: `__tests__/integration/`
- **Main Docs**: [AGENTS.md](../AGENTS.md)

---

**Last Updated**: 2025-10-12
**Status**: Standard deployments fully supported, edge cases documented
