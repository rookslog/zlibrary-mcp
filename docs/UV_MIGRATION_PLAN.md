# UV Migration Plan - v2.0.0

**Target**: Migrate from custom cache venv to UV-based dependency management
**Rationale**: Simplify from 406 lines → ~20 lines, follow 2025 best practices
**Version**: 2.0.0 (breaking change)
**Effort Estimate**: 6-8 hours
**Risk**: LOW (with proper testing)

---

## Table of Contents

1. [Why UV Migration](#why-uv-migration)
2. [Current vs Future State](#current-vs-future-state)
3. [Migration Steps](#migration-steps)
4. [Testing Strategy](#testing-strategy)
5. [Rollback Plan](#rollback-plan)
6. [Timeline](#timeline)

---

## Why UV Migration

### Evidence from Analysis

**Current Issues**:
- 406 lines of custom venv management
- Cache venv becomes stale when project moves
- Non-standard pattern (hard for contributors)
- Already hit 2 bugs in short period

**UV Benefits**:
- Official 2025 MCP recommendation
- 10-100x faster than pip
- Automatic lockfile (uv.lock) for reproducibility
- Auto venv creation (.venv/)
- Simple: ~20 lines vs 406 lines

### Success Metrics

From successful test report:
- ✅ All 11 MCP tools working after cache venv fix
- ✅ Philosophy research validated
- ✅ Production-ready quality

**UV will make this MORE reliable**, not less.

---

## Current vs Future State

### Current (v1.x)

**Dependency Management**:
```bash
# Setup:
bash setup_venv.sh  # Creates cache venv

# venv-manager.ts: 406 lines
# - Creates ~/.cache/zlibrary-mcp/zlibrary-mcp-venv/
# - Writes config to ~/.cache/zlibrary-mcp/.venv_config
# - Programmatically runs pip install
```

**Issues**:
- Cache venv location-dependent (stale on move)
- Complex error handling
- Not standard pattern

---

### Future (v2.0.0 with UV)

**Dependency Management**:
```bash
# Setup:
uv sync  # That's it!

# venv-manager.ts: ~20 lines
# - Checks for .venv/bin/python
# - No cache, no config file, no pip orchestration
```

**Benefits**:
- Project-local .venv (moves with project)
- Standard 2025 pattern
- Much simpler code
- Automatic lockfile

---

## Dependencies Inventory

### Current Python Dependencies

From cache venv analysis:

**Core (zlibrary-mcp specific)**:
```
aiofiles==24.1.0
beautifulsoup4==4.14.2
lxml==6.0.2
httpx==0.28.1
PyMuPDF==1.26.4
EbookLib==0.19
```

**Vendored**:
```
zlibrary==1.0.2  (editable from ./zlibrary)
```

**Optional (OCR - not currently used)**:
```
pytesseract  (not installed)
pdf2image    (not installed)
Pillow==11.3.0  (installed but not used yet)
```

**Transitive** (auto-installed by above):
- All other packages in list

---

## Migration Steps

### Phase 1: Create pyproject.toml

**File**: `pyproject.toml` (root level)

```toml
[project]
name = "zlibrary-mcp"
version = "2.0.0"
description = "Z-Library MCP server for AI assistants"
readme = "README.md"
requires-python = ">=3.9"
authors = [
    { name = "rookslog", email = "rookslog@users.noreply.github.com" }
]
keywords = ["mcp", "zlibrary", "ai", "roocode", "cline"]
license = { text = "MIT" }

# Core dependencies for zlibrary-mcp functionality
dependencies = [
    "aiofiles>=24.1.0",
    "beautifulsoup4>=4.14.0",
    "lxml>=6.0.0",
    "httpx>=0.28.0",
    "pymupdf>=1.26.0",
    "ebooklib>=0.19",
]

[project.optional-dependencies]
# OCR capabilities (optional)
ocr = [
    "pytesseract>=0.3.10",
    "pdf2image>=1.16.0",
    "pillow>=11.0.0",
]

# Development dependencies
dev = [
    "pytest>=8.4.0",
    "pytest-asyncio>=1.2.0",
    "pytest-mock>=3.15.0",
    "pytest-benchmark>=5.1.0",
]

# Vendored zlibrary package (editable install from ./zlibrary)
[tool.uv.sources]
zlibrary = { path = "./zlibrary", editable = true }

# Note: zlibrary is in dependencies implicitly via editable source

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
dev-dependencies = [
    "pytest>=8.4.0",
    "pytest-asyncio>=1.2.0",
    "pytest-mock>=3.15.0",
    "pytest-benchmark>=5.1.0",
]
```

**Key Points**:
- `[tool.uv.sources]` handles vendored zlibrary
- UV will install it as editable automatically
- Optional OCR dependencies separate
- Dev dependencies for testing

---

### Phase 2: Simplify venv-manager.ts

**From**: 406 lines
**To**: ~20 lines

```typescript
/**
 * Simplified venv-manager for UV
 *
 * UV automatically creates and manages .venv/ in the project directory.
 * This module just provides the path to UV's Python executable.
 */

import * as path from 'path';
import { existsSync } from 'fs';
import { fileURLToPath } from 'url';
import { execSync } from 'child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Get path to UV-managed Python executable
 *
 * UV creates .venv/ in project root. This function returns the Python path.
 *
 * @returns {Promise<string>} Path to Python executable in .venv
 * @throws {Error} If .venv not found (user needs to run: uv sync)
 */
export async function getManagedPythonPath(): Promise<string> {
  const projectRoot = path.resolve(__dirname, '..', '..');
  const uvVenvPython = path.join(projectRoot, '.venv', 'bin', 'python');

  if (!existsSync(uvVenvPython)) {
    throw new Error(
      'Python virtual environment not found.\n\n' +
      'UV has not initialized the environment. Please run:\n' +
      '  uv sync\n\n' +
      'This will create .venv/ and install all dependencies.\n' +
      'First time? Install UV: https://docs.astral.sh/uv/getting-started/installation/'
    );
  }

  // Verify Python is executable
  try {
    execSync(`"${uvVenvPython}" --version`, { stdio: 'pipe' });
  } catch (error) {
    throw new Error(
      `Python at ${uvVenvPython} is not executable.\n` +
      `Try running: uv sync`
    );
  }

  return uvVenvPython;
}

// That's the entire venv-manager for UV!
// Remove all cache management, pip orchestration, config files, etc.
```

**Deleted**:
- Cache directory management (~80 lines)
- Config file read/write (~60 lines)
- Programmatic pip install (~100 lines)
- Venv creation logic (~80 lines)
- Complex error handling (~50 lines)
- Dependency injection for testing (~36 lines)

**Result**: 96% code reduction

---

### Phase 3: Update Setup Scripts

**New**: `setup-uv.sh` (replaces setup_venv.sh)

```bash
#!/bin/bash
# UV-based setup script
# Simpler and faster than previous approach

set -e

echo "🚀 Z-Library MCP - UV Setup"
echo "==========================="
echo ""

# Check if UV is installed
if ! command -v uv &> /dev/null; then
    echo "❌ UV not found"
    echo ""
    echo "Please install UV first:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  # Or: pip install uv"
    echo ""
    echo "Then run this script again."
    exit 1
fi

echo "✅ UV found: $(uv --version)"
echo ""

# Initialize UV project (creates .venv and installs deps)
echo "📦 Installing dependencies with UV..."
uv sync

echo ""
echo "✅ Dependencies installed"
echo ""

# Verify zlibrary import
echo "🔍 Verifying zlibrary installation..."
if .venv/bin/python -c "from zlibrary import AsyncZlib; print('✅ zlibrary ready')" 2>&1; then
    echo ""
    echo "🎉 Setup complete!"
    echo ""
    echo "Next steps:"
    echo "  1. npm install"
    echo "  2. npm run build"
    echo "  3. Configure in your MCP client"
else
    echo ""
    echo "❌ zlibrary import failed"
    echo "   Check the error messages above"
    exit 1
fi
```

---

### Phase 4: Update package.json

```json
{
  "scripts": {
    "build": "tsc",
    "postbuild": "node scripts/validate-python-bridge.js",
    "validate": "node scripts/validate-python-bridge.js",
    "setup": "bash setup-uv.sh",
    "start": "node dist/index.js",
    "test": "node --experimental-vm-modules node_modules/jest/bin/jest.js --coverage",
    "prepublishOnly": "npm run build"
  }
}
```

**Changes**:
- Add "setup": "bash setup-uv.sh"
- Consider: "preinstall": "command -v uv || echo 'Install UV first'"

---

### Phase 5: Update Documentation

**README.md**:
```markdown
## Prerequisites

- Node.js 18+
- [UV](https://docs.astral.sh/uv/) (Python package manager)

## Quick Start

1. **Install UV** (one-time):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone and setup**:
   ```bash
   git clone https://github.com/rookslog/zlibrary-mcp.git
   cd zlibrary-mcp
   ```

3. **Install dependencies**:
   ```bash
   uv sync        # Python dependencies
   npm install    # Node.js dependencies
   npm run build  # Build TypeScript
   ```

4. **Configure** in your MCP client...
```

---

## Testing Strategy

### Pre-Migration Testing

**Current v1.x**:
```bash
# 1. Verify current state
npm test  # Should show: 100/100 tests passing

# 2. Test actual MCP tools (in philosophy workspace)
# All 11 tools should work

# 3. Document baseline
git tag v1.0.0-pre-uv-migration
```

---

### Migration Testing

**Step-by-Step Validation**:

```bash
# 1. Create feature branch
git checkout -b feature/uv-migration

# 2. Add pyproject.toml
# ... create file ...

# 3. Initialize UV
uv sync

# 4. Verify Python imports
.venv/bin/python -c "from zlibrary import AsyncZlib; print('OK')"

# 5. Update venv-manager.ts
# ... simplify to 20 lines ...

# 6. Rebuild
npm run build

# 7. Run tests
npm test  # Should still show: 100/100

# 8. Test MCP server directly
node dist/index.js  # Should start without errors

# 9. Test via MCP (in test workspace)
# Create test .mcp.json, try tools

# 10. Test cross-platform (if possible)
# Windows, macOS verification
```

---

### Post-Migration Validation

**Checklist**:
- [ ] All 100 tests pass
- [ ] Build validation passes
- [ ] Integration tests pass
- [ ] MCP server starts successfully
- [ ] All 11 tools work via MCP
- [ ] uv.lock file created
- [ ] .venv/ contains correct packages
- [ ] Documentation updated

---

## Backwards Compatibility

### Breaking Changes

**v2.0.0 is a BREAKING CHANGE**:

1. **Removed**: Cache venv at ~/.cache/zlibrary-mcp/
2. **Removed**: .venv_config file
3. **Removed**: setup_venv.sh (replaced with setup-uv.sh)
4. **Changed**: Requires UV installation
5. **Changed**: Uses .venv/ instead of cache venv

### Migration Guide for Users

```markdown
## Migrating from v1.x to v2.0

### Prerequisites

Install UV:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Migration Steps

1. **Pull latest code**:
   ```bash
   git pull origin master
   ```

2. **Clean old venv** (optional):
   ```bash
   rm -rf ~/.cache/zlibrary-mcp/  # Old cache venv
   ```

3. **Setup with UV**:
   ```bash
   uv sync           # Creates .venv/
   npm install       # Node.js deps
   npm run build     # Build TypeScript
   ```

4. **Test**:
   ```bash
   npm test          # Should pass
   npm run validate  # Should pass
   ```

5. **Update MCP config** (if needed):
   - Path to dist/index.js should be same
   - Just restart MCP client

### Troubleshooting

**If UV not found**:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Or: pip install uv
```

**If .venv creation fails**:
```bash
rm -rf .venv
uv sync --reinstall
```
```

---

## Migration Implementation

### Step 1: Create pyproject.toml

**Dependencies** (from current cache venv):
```toml
dependencies = [
    "aiofiles>=24.1.0",
    "beautifulsoup4>=4.14.0",
    "lxml>=6.0.0",
    "httpx>=0.28.0",
    "pymupdf>=1.26.0",
    "ebooklib>=0.19",
]
```

**Vendored zlibrary**:
```toml
[tool.uv.sources]
zlibrary = { path = "./zlibrary", editable = true }
```

**Optional deps**:
```toml
[project.optional-dependencies]
ocr = [
    "pytesseract>=0.3.10",
    "pdf2image>=1.16.0",
    "pillow>=11.0.0",
]
```

---

### Step 2: Simplify venv-manager.ts

**Current**: 406 lines of cache management

**New** (~20 lines):
```typescript
export async function getManagedPythonPath(): Promise<string> {
  const projectRoot = path.resolve(__dirname, '..', '..');
  const uvVenvPython = path.join(projectRoot, '.venv', 'bin', 'python');

  if (!existsSync(uvVenvPython)) {
    throw new Error('Run: uv sync');
  }

  return uvVenvPython;
}
```

**Delete**:
- All cache directory logic
- All config file management
- All programmatic pip installation
- All complex error handling for venv creation

---

### Step 3: Update Scripts

**Create**: `setup-uv.sh`
**Update**: `package.json` scripts
**Remove**: `setup_venv.sh` (or mark deprecated)

**New npm scripts**:
```json
"scripts": {
  "setup": "bash setup-uv.sh",
  "build": "tsc",
  "postbuild": "node scripts/validate-python-bridge.js",
  ...
}
```

---

### Step 4: Update Build Validation

**Modify**: `scripts/validate-python-bridge.js`

Change venv check from cache to .venv:
```javascript
// Old:
const cacheVenv = path.join(os.homedir(), '.cache', 'zlibrary-mcp');

// New:
const projectVenv = path.join(projectRoot, '.venv');
```

---

### Step 5: Update Documentation

**Files to Update**:
1. `README.md` - Installation instructions
2. `CLAUDE.md` - Developer guide
3. `docs/DEPLOYMENT.md` - Add UV section
4. `docs/TROUBLESHOOTING.md` - UV-specific issues

**New Sections**:
- UV installation guide
- uv sync usage
- uv.lock file explanation
- Migration from v1.x

---

## Testing Strategy

### Unit Tests

**Current tests should pass** with minimal changes:

```javascript
// __tests__/venv-manager.test.js
// Update to test new simplified version
// Much simpler tests (no cache logic to test)
```

**Expected**: Easier to test, fewer test cases needed

---

### Integration Tests

**Existing**:
- `__tests__/integration/python-bridge-integration.test.js`
- Should work without changes (just checks Python execution)

**New**:
- Test UV venv detection
- Test missing .venv error message

---

### Manual MCP Testing

**Test in actual workspace**:
1. Create test .mcp.json (gitignored)
2. Restart MCP client
3. Run all 11 tools
4. Verify against successful test report baseline

---

## Rollback Plan

### If Migration Fails

**Option 1**: Git Rollback
```bash
git checkout master  # Back to v1.x
git branch -D feature/uv-migration
```

**Option 2**: Tag-Based Rollback
```bash
git tag v1.9.9-stable  # Before migration
# ... attempt migration ...
git reset --hard v1.9.9-stable  # If fails
```

**Option 3**: Parallel Branch
```bash
# Keep master on v1.x
# Merge UV migration only after full validation
```

---

## Risk Mitigation

### Known Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| UV not available on user system | MEDIUM | HIGH | Clear install instructions, check in setup |
| Breaking existing workflows | HIGH | MEDIUM | Version 2.0.0, migration guide |
| Platform incompatibility | LOW | HIGH | Test on Linux, macOS, Windows |
| Vendored zlibrary issues | LOW | HIGH | Test editable install thoroughly |

---

## Timeline

### Conservative Estimate

**Phase 1: Planning & Prep** (1-2 hours)
- Create pyproject.toml
- Document dependencies
- Plan testing

**Phase 2: Implementation** (2-3 hours)
- Simplify venv-manager.ts
- Update scripts
- Update docs

**Phase 3: Testing** (2-3 hours)
- Run all tests
- MCP integration testing
- Cross-platform verification (if possible)

**Phase 4: Documentation** (1 hour)
- Migration guide
- Update all docs
- Create release notes

**Total**: 6-9 hours

---

## Success Criteria

### Must Have

- ✅ All 100 tests passing
- ✅ Build validation passes
- ✅ All 11 MCP tools work via actual MCP calls
- ✅ uv.lock file generated
- ✅ Code reduced from 406 → ~20 lines
- ✅ Migration guide complete

### Nice to Have

- ✅ Cross-platform tested (Windows, macOS)
- ✅ Performance benchmarks (UV vs current)
- ✅ User feedback from migration
- ✅ ADR updated with UV decision

---

## Benefits Summary

### Immediate Benefits

| Metric | v1.x (Current) | v2.0 (UV) | Improvement |
|--------|----------------|-----------|-------------|
| venv-manager LOC | 406 | ~20 | ✅ 95% reduction |
| Setup complexity | High | Low | ✅ Simplified |
| Stale venv risk | HIGH | NONE | ✅ Eliminated |
| Installation speed | Slow (pip) | Fast (uv) | ✅ 10-100x faster |
| Reproducibility | requirements.txt | uv.lock | ✅ Better |
| Standard pattern | ❌ Custom | ✅ 2025 best practice | ✅ Aligned |

### Long-Term Benefits

1. **Maintainability**: Much less code to maintain
2. **Reliability**: Standard tool, less custom code = fewer bugs
3. **Performance**: Faster installs, better UX
4. **Modern**: Following 2025 best practices
5. **Reproducible**: uv.lock ensures consistency

---

## Recommendation

### Should We Migrate?

**Evidence-Based Answer**: ✅ **YES**

**Reasoning**:
1. Current approach has fundamental issues (proven by 2 bugs)
2. UV is official MCP recommendation for 2025
3. Massive simplification (406 → 20 lines)
4. All tests currently passing (good migration baseline)
5. Low risk with proper testing

### When?

**Recommended Timeline**:

1. **Now**: Create feature branch and implement
2. **This Week**: Complete testing and validation
3. **Next Week**: Merge and release v2.0.0
4. **Ongoing**: Monitor for issues, iterate

### How?

**Process**:
1. Create `feature/uv-migration` branch
2. Implement all phases systematically
3. Test thoroughly (unit + integration + MCP)
4. Document migration guide
5. Release as v2.0.0 with breaking change notice

---

## Next Actions

### Immediate

1. **Review this plan** - Is approach sound?
2. **Decide timing** - Now or later?
3. **Get buy-in** - Any concerns?

### If Approved

1. Create feature branch
2. Implement Phase 1 (pyproject.toml)
3. Test UV sync
4. Proceed systematically through phases

---

**Prepared by**: Claude Code (Sequential Analysis)
**Analysis Depth**: 12-step systematic planning
**Confidence**: HIGH (based on research, evidence, and testing baseline)
