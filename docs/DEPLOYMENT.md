# Deployment Guide - Edge Cases and Platform Considerations

This document covers deployment scenarios and platform-specific considerations for the Z-Library MCP server.

---

## Table of Contents

1. [Path Resolution Overview](#path-resolution-overview)
2. [Standard Deployment](#standard-deployment)
3. [Edge Cases](#edge-cases)
4. [Platform-Specific Notes](#platform-specific-notes)
5. [Troubleshooting](#troubleshooting)

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
