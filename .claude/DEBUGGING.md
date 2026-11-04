# Z-Library MCP - Comprehensive Debugging Guide

Server-side TypeScript diagnostics must use the shared stderr logger from
`src/lib/logger.ts`; adjust its relative import for the module. stdout carries
MCP JSON-RPC only. Shell snippets below are standalone terminal commands, not
output to add to the running server. At debug level, queries can enter client
logs; follow `SECURITY.md` before collecting or sharing them.

## Quick Diagnostics

### Health Check Script

```bash
#!/bin/bash
# Run this first when debugging issues

echo "🔍 Z-Library MCP Health Check"
echo "=============================="

# Check Node.js
echo "Node.js: $(node --version)"

# Check Python
echo "Python: $(python3 --version)"

# Check venv (UV-managed .venv/)
if [ -d ".venv" ]; then
    echo "✅ Virtual environment exists"
    echo "Python in venv: $(.venv/bin/python --version)"
else
    echo "❌ Virtual environment missing - run 'uv sync'"
fi

# Check environment variables
if [ -n "$ZLIBRARY_EMAIL" ]; then
    echo "✅ ZLIBRARY_EMAIL is set"
else
    echo "❌ ZLIBRARY_EMAIL is not set"
fi

if [ -n "$ZLIBRARY_PASSWORD" ]; then
    echo "✅ ZLIBRARY_PASSWORD is set"
else
    echo "❌ ZLIBRARY_PASSWORD is not set"
fi

# Check build
if [ -d "dist" ]; then
    echo "✅ TypeScript build exists"
else
    echo "❌ TypeScript build missing - run 'npm run build'"
fi

# Test Python bridge
echo "Testing Python bridge..."
python3 -c "import lib.python_bridge; print('✅ Python bridge imports successfully')" 2>/dev/null || echo "❌ Python bridge import failed"

# Check Z-Library connectivity
echo "Testing Z-Library connectivity..."
curl -s -o /dev/null -w "HTTP %{http_code}" https://singlelogin.me || echo " - Failed"
```

## Common Issues & Solutions

### Issue: "Cannot find module" Error

**Symptoms:**

```
Error: Cannot find module '../dist/lib/venv-manager.js'
```

**Diagnosis:**

```bash
# Check if TypeScript is built
ls -la dist/

# Rebuild if missing
npm run build

# Check for compilation errors
npm run build 2>&1 | grep -i error
```

**Solution:**

```bash
# Clean and rebuild
rm -rf dist/
npm run build

# If errors persist, check TypeScript config
cat tsconfig.json | grep -A5 "compilerOptions"
```

### Issue: Python Bridge Connection Fails

**Symptoms:**

```
Error: Python shell process exited with code 1
```

**Diagnosis:**

```bash
# Test Python directly
python3 lib/python_bridge.py test_connection

# Check Python dependencies
uv pip list | grep -E "zlibrary|httpx|beautifulsoup4"

# Verify the UV-managed environment
.venv/bin/python --version
```

**Solution:**

```bash
# Recreate venv with UV
rm -rf .venv/
bash setup-uv.sh
# Or: uv sync
```

### Issue: Z-Library Authentication Fails

**Symptoms:**

```
Error: Invalid credentials or login failed
```

**Diagnosis:**

```python
# Test authentication manually
import asyncio
from zlibrary.eapi import EAPIClient

async def test_auth():
    client = EAPIClient("z-library.sk")
    try:
        result = await client.login("your_email", "your_password")
        if result.get("success") == 1:
            print("✅ Authentication successful")
        else:
            print(f"❌ Authentication failed: {result}")
        await client.close()
    except Exception as e:
        print(f"❌ Authentication failed: {e}")

asyncio.run(test_auth())
```

**Solution:**

1. Verify credentials are correct
2. Check if account is blocked/limited
3. Try different domain:

```bash
export ZLIBRARY_MIRROR="https://1lib.fr"
# or
export ZLIBRARY_MIRROR="https://singlelogin.me"
```

### Issue: Domain Not Accessible

**Symptoms:**

```
Error: ECONNREFUSED or timeout errors
```

**Diagnosis:**

```bash
# Test domain connectivity
for domain in singlelogin.me 1lib.fr z-lib.org; do
    echo "Testing $domain..."
    curl -I -m 5 "https://$domain" 2>/dev/null | head -1
done

# Check DNS resolution
nslookup singlelogin.me

# Test with VPN/Tor if blocked
curl --socks5 127.0.0.1:9050 https://singlelogin.me
```

**Solution:**

```typescript
// Implement domain rotation in code
const domains = [
  'https://singlelogin.me',
  'https://1lib.fr',
  'https://z-lib.org'
];

async function tryDomains(operation) {
  for (const domain of domains) {
    try {
      process.env.ZLIBRARY_MIRROR = domain;
      return await operation();
    } catch (error) {
      logger.debug(`Domain ${domain} failed, trying next...`);
    }
  }
  throw new Error('All domains failed');
}
```

### Issue: Test Suite Warnings

**Symptoms:**

```
console.error
  Warning: Failed to read or validate venv config
  Cannot read properties of undefined (reading 'trim')
```

**Root Cause Analysis:**

```typescript
// The issue is in venv-manager.ts:255
// venvPath is undefined when reading config

// Debug with:
logger.debug('Venv config state', {
  hasConfig: Boolean(configContent),
  hasVenvPath: Boolean(venvPath)
});
```

**Solution:**

```typescript
// Add null check before trim
const venvPath = configContent?.trim();
if (!venvPath) {
  throw new Error('Empty or invalid venv config');
}
```

```python
# Test document processing directly

# Test with a known good file
result = process_document('test_files/sample.pdf', 'markdown')
print(f"Processed: {len(result)} characters")

# Check file format support
import PyPDF2
import ebooklib

print(f"PyPDF2 version: {PyPDF2.__version__}")
print(f"ebooklib available: {ebooklib is not None}")
```

**Solution:**

```python
# Add better error handling
def process_document(file_path, output_format='markdown'):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext not in ['.pdf', '.epub', '.txt']:
        raise ValueError(f"Unsupported format: {file_ext}")

    # Add null checks throughout processing
    try:
        if file_ext == '.pdf':
            text = extract_pdf_text(file_path)
        elif file_ext == '.epub':
            text = extract_epub_text(file_path)
        else:
            text = extract_txt_text(file_path)
    except Exception as e:
        logger.error(f"Extraction failed for {file_path}: {e}")
        raise

    if not text:
        raise ValueError("No text extracted from document")

    return text
```

## Performance Debugging

### Slow Search Response

**Diagnosis:**

```bash
# Profile search performance
time node -e "
const api = require('./dist/lib/zlibrary-api.js');
api.searchBooks({ query: 'test' }).then((result) => console.error(result));
"

# Check network latency
ping -c 5 singlelogin.me

# Monitor memory usage
node --expose-gc --trace-gc dist/index.js
```

**Optimization:**

```typescript
// Add timing logs
const start = Date.now();
const result = await searchBooks(params);
logger.debug(`Search took ${Date.now() - start}ms`);

// Implement caching
const cache = new Map();
const cacheKey = JSON.stringify(params);

if (cache.has(cacheKey)) {
  return cache.get(cacheKey);
}

const result = await searchBooks(params);
cache.set(cacheKey, result);
```

### Memory Leaks

**Detection:**

```bash
# Monitor memory over time
node --inspect dist/index.js

# In Chrome DevTools:
# 1. Open chrome://inspect
# 2. Take heap snapshots
# 3. Compare snapshots over time
```

**Common Causes:**

```typescript
// Event listener leaks
emitter.on('event', handler); // Accumulates if not removed

// Solution: Remove listeners
emitter.removeListener('event', handler);

// Or use once
emitter.once('event', handler);

// Large cache without limits
const cache = new Map(); // Can grow indefinitely

// Solution: Use LRU cache
import LRU from 'lru-cache';
const cache = new LRU({ max: 500 });
```

## Advanced Debugging Techniques

### Enable Debug Logging

```typescript
// Add to .env
LOG_LEVEL=debug
DEBUG=zlibrary:*

// In code
const debug = require('debug')('zlibrary:search');

debug('Search params: %O', params);
debug('Search results: %d books found', results.length);
```

### Network Request Debugging

Keep TLS certificate verification enabled. Check the system clock, the server's
certificate chain and the local trusted CA configuration when a TLS error occurs.
For an intercepting proxy, use the operator-approved CA trust configuration;
do not disable certificate verification to make a request succeed.

Treat request captures as sensitive: they can contain account credentials, session
cookies and queries. Follow `SECURITY.md` before sharing diagnostic output.

### Python Bridge Debugging

```python
# Add to python_bridge.py
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('python_bridge.log'),
        logging.StreamHandler(sys.stderr)
    ]
)

logger = logging.getLogger(__name__)

def debug_wrapper(func):
    def wrapper(*args, **kwargs):
        logger.debug(f"Calling {func.__name__} with args={args}, kwargs={kwargs}")
        try:
            result = func(*args, **kwargs)
            logger.debug(f"Result: {result}")
            return result
        except Exception as e:
            logger.exception(f"Error in {func.__name__}: {e}")
            raise
    return wrapper

@debug_wrapper
def search_books(query, **kwargs):
    # Implementation
    pass
```

### Debugging with VS Code

```json
// .vscode/launch.json
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "node",
      "request": "launch",
      "name": "Debug Z-Library MCP",
      "skipFiles": ["<node_internals>/**"],
      "program": "${workspaceFolder}/dist/index.js",
      "envFile": "${workspaceFolder}/.env",
      "outputCapture": "std",
      "console": "integratedTerminal"
    },
    {
      "type": "node",
      "request": "launch",
      "name": "Debug Tests",
      "program": "${workspaceFolder}/node_modules/jest/bin/jest.js",
      "args": [
        "--runInBand",
        "--no-coverage",
        "--detectOpenHandles"
      ],
      "console": "integratedTerminal",
      "internalConsoleOptions": "neverOpen"
    }
  ]
}
```

## Logging Analysis

### Log Aggregation Script

```bash
#!/bin/bash
# analyze_logs.sh - Aggregate and analyze logs

echo "Error Summary:"
grep -i error logs/*.log | cut -d: -f4- | sort | uniq -c | sort -rn | head -10

echo -e "\nSlowest Operations:"
grep "duration" logs/*.log | jq -r '.duration' | sort -rn | head -10

echo -e "\nFailed Domains:"
grep "domain.*failed" logs/*.log | cut -d'"' -f4 | sort | uniq -c

echo -e "\nAuthentication Issues:"
grep -i "auth\|login" logs/*.log | grep -i "fail\|error" | tail -5

echo -e "\nMemory Usage:"
grep "memory" logs/*.log | tail -5
```

### Real-time Log Monitoring

```bash
# Watch errors in real-time
tail -f logs/*.log | grep --line-buffered -i error

# Monitor specific operations
tail -f logs/*.log | jq 'select(.module == "search")'

# Watch performance metrics
tail -f logs/*.log | jq 'select(.duration > 1000)'
```

## Emergency Recovery

### Targeted Build Recovery

For a missing or stale TypeScript build, rebuild from the checkout:

```bash
npm run build
```

If this fails, inspect the reported error and repair the specific dependency or
configuration. Do not use a blanket reset that removes downloads, acquisition
records, logs or the Python environment. Downloads can live outside this checkout
under the configured output root; see the README's download-location section.
A rebuild does not establish provider availability or repair acquired files.

### Session Recovery

```python
# Recover from interrupted session
import pickle
import os

class SessionRecovery:
    def __init__(self, session_file='session.pkl'):
        self.session_file = session_file

    def save_state(self, state):
        with open(self.session_file, 'wb') as f:
            pickle.dump(state, f)

    def load_state(self):
        if os.path.exists(self.session_file):
            with open(self.session_file, 'rb') as f:
                return pickle.load(f)
        return None

    def clear_state(self):
        if os.path.exists(self.session_file):
            os.remove(self.session_file)

# Usage
recovery = SessionRecovery()

# Save progress
state = {
    'processed': processed_books,
    'queue': download_queue,
    'errors': error_list
}
recovery.save_state(state)

# Recover after crash
state = recovery.load_state()
if state:
    print(f"Recovered {len(state['processed'])} processed items")
```

---

_Keep this guide updated with new issues and solutions discovered during development._
