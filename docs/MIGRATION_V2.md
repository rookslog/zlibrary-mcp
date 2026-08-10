# Migration Guide: v1.x → v2.0.0 (UV)

**Version**: 2.0.0 (Breaking Change)
**Migration Type**: Major architectural simplification
**Effort**: 5-10 minutes
**Risk**: LOW (all features preserved)

---

## What Changed in v2.0.0

### Summary

**v2.0.0 migrates from custom cache venv to UV-based dependency management.**

| Aspect | v1.x | v2.0.0 |
|--------|------|--------|
| **Python Manager** | Custom cache venv | UV (industry standard) |
| **venv Location** | ~/.cache/zlibrary-mcp/ | ./.venv/ (project-local) |
| **Setup Command** | bash setup_venv.sh | bash setup-uv.sh OR uv sync |
| **Code Complexity** | 406 lines | 92 lines (77% reduction) |
| **Test Complexity** | 833 lines | 85 lines (90% reduction) |
| **Reproducibility** | requirements.txt | uv.lock (lockfile) |
| **Portability** | ⚠️ Stale on move | ✅ Moves with project |

### Why This Change?

**Problems with v1.x**:
- Cache venv became stale when project moved
- 406 lines of custom venv management code
- Non-standard pattern (hard for contributors)
- Hit multiple bugs (path resolution, stale installs)

**Benefits of v2.0.0**:
- ✅ 77% code reduction (406 → 92 lines)
- ✅ 90% test reduction (833 → 85 lines)
- ✅ Follows 2025 best practices (official MCP recommendation)
- ✅ 10-100x faster installation (UV is Rust-based)
- ✅ Reproducible builds (uv.lock)
- ✅ Portable (venv moves with project)
- ✅ No stale venv issues

---

## Prerequisites

### Install UV

**First time only**:

```bash
# macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or via pip:
pip install uv

# Or via homebrew:
brew install uv

# Verify:
uv --version
```

---

## Migration Steps

### Step 1: Update Code

```bash
cd /path/to/zlibrary-mcp

# Pull latest v2.0.0
git pull origin master
# Or: git checkout v2.0.0
```

### Step 2: Clean Old venv (Optional)

```bash
# Remove old cache venv (optional cleanup)
rm -rf ~/.cache/zlibrary-mcp/

# Remove old project venv if exists
rm -rf venv/
```

### Step 3: Setup with UV

```bash
# Run the new UV-based setup
bash setup-uv.sh

# This will:
# - Create .venv/ in project directory
# - Install all dependencies from pyproject.toml
# - Install vendored zlibrary as editable
# - Generate uv.lock for reproducibility
```

**Or manually**:
```bash
uv sync
```

### Step 4: Build

```bash
# Install Node dependencies (if not done)
npm install

# Build TypeScript
npm run build
```

### Step 5: Test (Optional)

```bash
# Verify everything works
npm test

# Should show: 93/93 tests passing
```

### Step 6: Update MCP Configuration

Your `.mcp.json` **should not need changes**:

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

The path to `dist/index.js` remains the same.

### Step 7: Restart MCP Client

```bash
# Restart Claude Code or your MCP client
# The server will now use .venv/ instead of cache venv
```

---

## Verification

### Check UV Setup

```bash
# Verify .venv exists
ls -la .venv/bin/python

# Verify zlibrary installed
.venv/bin/python -c "from zlibrary import Extension; print('✅ Working')"

# Check uv.lock was generated
ls -la uv.lock
```

### Test MCP Server

```bash
# Start server directly
node dist/index.js

# Should show:
# [venv-manager] Using Python: Python 3.x.x from .venv
# Z-Library MCP server (ESM/TS) is running via Stdio...
```

### Test MCP Tools

From your MCP client (e.g., Claude Code):
- Try searching: "Search Z-Library for 'test query'"
- Should work without ImportError
- All 11 tools should function

---

## Troubleshooting

### "UV not found"

```bash
# Install UV:
curl -LsSf https://astral.sh/uv/install.sh | sh

# Add to PATH (if needed):
export PATH="$HOME/.local/bin:$PATH"

# Verify:
uv --version
```

### "Python virtual environment not found"

```bash
# Run UV sync to create .venv:
uv sync

# Or use setup script:
bash setup-uv.sh
```

### ".venv corrupted"

```bash
# Recreate venv:
rm -rf .venv
uv sync
```

### "ImportError: cannot import name from 'zlibrary'"

```bash
# Reinstall vendored zlibrary:
uv sync --reinstall

# Verify:
.venv/bin/python -c "from zlibrary import Extension; print('OK')"
```

---

## What's Different?

### File Changes

**Added**:
- `pyproject.toml` - UV dependency configuration
- `uv.lock` - Lockfile for reproducible builds
- `.venv/` - Project-local virtual environment (gitignored)
- `setup-uv.sh` - New UV-based setup script

**Removed** (deprecated):
- `~/.cache/zlibrary-mcp/` - No more cache venv
- `~/.cache/zlibrary-mcp/.venv_config` - No more config file
- Old `setup_venv.sh` behavior (replaced by setup-uv.sh)

**Updated**:
- `src/lib/venv-manager.ts` - Simplified from 406 → 92 lines
- `__tests__/venv-manager.test.js` - Simplified from 833 → 85 lines

### Behavior Changes

**Startup**:
- v1.x: Checked cache venv, ran pip install if needed
- v2.0: Expects .venv to exist (user runs `uv sync` first)

**Error Messages**:
- v1.x: "Virtual environment configuration is missing"
- v2.0: "Python virtual environment not found. Please run: uv sync"

**Venv Location**:
- v1.x: Shared cache at `~/.cache/zlibrary-mcp/`
- v2.0: Project-local at `./.venv/`

---

## Benefits You'll Experience

### Immediate

- ⚡ **Faster Setup**: UV is 10-100x faster than pip
- ✅ **Simpler**: One command (`uv sync`) vs multi-step script
- 🔒 **Reproducible**: uv.lock ensures exact dependencies
- 📦 **Portable**: .venv moves with project (no more stale cache!)

### Long-Term

- 🛠️ **Easier Maintenance**: 77% less code to maintain
- 📚 **Standard Pattern**: Follows 2025 best practices
- 🐛 **Fewer Bugs**: Standard tooling, less custom code
- 🤝 **Contributor Friendly**: UV is well-documented

---

## Rollback (If Needed)

If you encounter issues:

```bash
# Option 1: Use previous version
git checkout v1.9.9  # Last v1.x version

# Option 2: Report issue
# GitHub: https://github.com/rookslog/zlibrary-mcp/issues
```

---

## FAQ

### Q: Do I need to uninstall the old cache venv?

**A**: Not required, but recommended for cleanup:
```bash
rm -rf ~/.cache/zlibrary-mcp/
```

### Q: Can I still use setup_venv.sh?

**A**: No, v2.0.0 removes cache venv support. Use `bash setup-uv.sh` or `uv sync`.

### Q: What if I don't want to install UV?

**A**: v2.0.0 requires UV. If you prefer not to use UV, stay on v1.9.9.

### Q: Will my .mcp.json configuration break?

**A**: No, the path to `dist/index.js` remains the same. Just restart your MCP client.

### Q: Is uv.lock committed to git?

**A**: Yes! Commit `uv.lock` for reproducible builds (like package-lock.json for Node.js).

### Q: Can I use a different Python version?

**A**: Yes, UV uses system Python by default. Ensure Python 3.9+ is available.

---

## Support

**Documentation**:
- [UV Documentation](https://docs.astral.sh/uv/)
- [UV Migration Plan](UV_MIGRATION_PLAN.md)
- [Architecture Analysis](ARCHITECTURE_ANALYSIS.md)

**Issues**: https://github.com/rookslog/zlibrary-mcp/issues

---

**Migration Complete!** Enjoy the simplified, faster, and more reliable v2.0.0!
