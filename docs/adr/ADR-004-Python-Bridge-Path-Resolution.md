# ADR-004: Python Bridge Path Resolution Strategy

## Status
**Accepted** - Implemented in v1.0.0

## Context

The Z-Library MCP server uses a dual-language architecture:
- **TypeScript/Node.js** (compiled to `dist/`) - MCP server, tool registration, client communication
- **Python** (`lib/` directory) - Z-Library API interaction, document processing (EPUB, PDF, TXT)

### Problem
When TypeScript is compiled to the `dist/` directory, the compiled JavaScript needs to locate Python scripts at runtime. We needed to determine:

1. Where should Python scripts reside?
2. How should compiled TypeScript reference them?
3. How do we maintain single source of truth?
4. How do we ensure portability across environments?

### The Core Issue (Issue #6)
Original implementation used `path.join(__dirname, 'python_bridge.py')`, assuming Python scripts would be in the same directory as compiled JS (`dist/lib/`). However, Python scripts remained in source `lib/` directory, causing:
- **Runtime Error**: `ImportError: cannot import name 'AsyncZlib' from 'zlibrary'`
- **Root Cause**: Script not found at expected location
- **Impact**: All 11 MCP tools non-functional

## Decision

**Keep Python scripts in source `lib/` directory and reference them using relative path resolution from compiled location.**

### Implementation

```typescript
// In dist/lib/python-bridge.js (at runtime)
const scriptPath = path.resolve(__dirname, '..', '..', 'lib', 'python_bridge.py');
```

**Path Resolution Logic**:
```
Runtime: dist/lib/python-bridge.js
__dirname = /project/dist/lib/
           ↓ ..
         /project/dist/
           ↓ ..
         /project/
           ↓ lib/
         /project/lib/python_bridge.py ✅
```

### Files Updated
- `src/lib/python-bridge.ts` (line 26)
- `src/lib/zlibrary-api.ts` (lines 17-19) - Already implemented correctly
- Tests updated for cross-machine portability

## Alternatives Considered

### Alternative 1: Copy Python Files to dist/ During Build

**Approach**: Modify build process to copy `lib/*.py` to `dist/lib/`

```json
{
  "scripts": {
    "build": "tsc && npm run copy-python",
    "copy-python": "cp lib/*.py dist/lib/"
  }
}
```

**Pros**:
- Simple relative paths: `path.join(__dirname, 'python_bridge.py')`
- Mirrors TypeScript compilation model
- Self-contained dist/ directory

**Cons**:
- ❌ File duplication (source of truth split between `lib/` and `dist/lib/`)
- ❌ Build process complexity (need to maintain file list)
- ❌ Easy to forget new Python files
- ❌ Sync issues if editing during development
- ❌ Larger repository footprint

**Rejected**: Too much complexity and duplication for minimal benefit

---

### Alternative 2: Use Absolute Paths from Environment Variables

**Approach**: Set `ZLIBRARY_PYTHON_PATH` environment variable

```typescript
const scriptPath = process.env.ZLIBRARY_PYTHON_PATH || '/default/path/lib/python_bridge.py';
```

**Pros**:
- Flexibility for different deployment scenarios
- Can override for testing

**Cons**:
- ❌ Less portable (requires configuration)
- ❌ Fragile (typos in env vars cause runtime failures)
- ❌ Not discoverable (hidden configuration requirement)
- ❌ Deployment complexity

**Rejected**: Reduces portability and adds unnecessary configuration burden

---

### Alternative 3: Bundle Python Scripts as Resources

**Approach**: Use bundler to embed Python scripts as strings/data

**Pros**:
- Single executable artifact
- No external file dependencies

**Cons**:
- ❌ Significant build tooling complexity
- ❌ Harder to debug Python scripts
- ❌ Can't hot-reload during development
- ❌ Non-standard approach for Python

**Rejected**: Over-engineering for this use case

## Consequences

### Positive ✅

1. **Single Source of Truth**
   - Python scripts remain in `lib/` directory
   - No duplication or sync issues
   - Edit once, works everywhere

2. **Simple Build Process**
   - No additional build steps required
   - TypeScript compilation handles everything
   - Standard `tsc` command sufficient

3. **Development-Friendly**
   - Edit Python files directly in `lib/`
   - Changes immediately reflected without rebuild
   - Standard Python development workflow

4. **Portability**
   - Works on any platform (Windows/Linux/macOS)
   - No machine-specific paths
   - No environment variable configuration needed

5. **Future-Proof**
   - New Python files automatically work
   - No build process updates needed
   - Scales with project growth

### Negative ⚠️

1. **Directory Structure Dependency**
   - Requires specific directory structure: `dist/` and `lib/` as siblings
   - Breaking this structure breaks path resolution
   - **Mitigation**: Build validation script (`scripts/validate-python-bridge.js`)

2. **Package Distribution Considerations**
   - npm package must include `lib/` directory
   - `package.json` exports must reference both `dist/` and ensure `lib/` is included
   - **Mitigation**: Standard npm package inclusion (non-ignored directories)

3. **Global npm Install Consideration**
   - Path resolution may need adjustment for global installs
   - **Status**: Requires testing (documented in IMPROVEMENT_RECOMMENDATIONS.md Phase 3)

4. **Not Obvious from Code**
   - Path navigation (`.., ..`) not immediately intuitive
   - **Mitigation**: Comprehensive inline comments explaining resolution logic

## Implementation Notes

### Critical Files
All files using this pattern:
- `src/lib/python-bridge.ts` - Direct Python script execution
- `src/lib/zlibrary-api.ts` - PythonShell scriptPath configuration
- `src/lib/venv-manager.ts` - Project root resolution

### Build Validation
Added `scripts/validate-python-bridge.js` to verify all required Python files exist:
- Runs automatically after build (`postbuild` script)
- Can be run manually (`npm run validate`)
- Catches missing files before deployment
- Exit code 1 on failure (CI/CD integration)

### Test Updates
Tests updated to use dynamic path resolution:
- `__tests__/python-bridge.test.js` - `path.resolve(process.cwd(), 'lib', 'python_bridge.py')`
- `__tests__/zlibrary-api.test.js` - `EXPECTED_SCRIPT_PATH = path.resolve(process.cwd(), 'lib')`

## Verification

### Build Time
```bash
npm run build
# Automatically runs postbuild validation
# ✅ BUILD VALIDATION PASSED
# All required files are present and accounted for.
```

### Runtime
```bash
node dist/index.js
# Z-Library MCP server starts successfully
# Python bridge scripts found and executed
```

### Cross-Platform
Tested on:
- ✅ Linux (development environment)
- ⏳ Windows (requires testing)
- ⏳ macOS (requires testing)

## Related

- **Issue #6**: Python Bridge Import Failure
- **Commit**: `f02acd8` (Initial fix)
- **Commit**: `fa67c60` (Path validation)
- **Commit**: `6c74ce1` (Test portability)
- **Documentation**: `IMPROVEMENT_RECOMMENDATIONS.md`
- **Testing**: `TEST_ISSUES.md`

## Future Considerations

### Phase 3 Enhancements (Low Priority)
1. **Path Helper Module** - Centralize all path resolution logic
2. **Global Install Testing** - Verify npm global install behavior
3. **Docker Deployment** - Test in containerized environments
4. **Symbolic Link Handling** - Verify behavior with symlinked directories

### Monitoring
- Track deployment issues related to path resolution
- Monitor for npm package structure complaints
- Watch for global install reports

## References

- [Node.js path.resolve() documentation](https://nodejs.org/api/path.html#pathresolvepaths)
- [TypeScript Module Resolution](https://www.typescriptlang.org/docs/handbook/module-resolution.html)
- [npm package.json files configuration](https://docs.npmjs.com/cli/v9/configuring-npm/package-json#files)

---

**Author**: Claude Code (with rookslog)
**Date**: 2025-10-12
**Supersedes**: None
**Superseded by**: None (current)
