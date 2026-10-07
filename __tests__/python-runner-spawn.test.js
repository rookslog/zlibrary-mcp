import { jest, describe, beforeEach, afterEach, expect, test } from '@jest/globals';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { spawnSync, execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

jest.setTimeout(30000);

const RUNNER_URL = pathToFileURL(path.resolve('dist/lib/python-runner.js')).href;
const PYTHON = ['python3', 'python'].find((candidate) => {
  try {
    execFileSync(candidate, ['-c', 'pass'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
});

const mockGetManagedPythonPath = jest.fn();
const mockRunPythonBridge = jest.fn();

function runNode(script, timeout = 5000) {
  return spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    encoding: 'utf8',
    timeout,
  });
}

describe('Python bridge spawn and signal failures', () => {
  test('a missing interpreter rejects without crashing the hosting process', () => {
    const script = [
      `import { runPythonBridge, liveChildCount } from ${JSON.stringify(RUNNER_URL)};`,
      'const uncaughtBefore = process.listenerCount("uncaughtException");',
      'runPythonBridge("missing.py", { mode: "text", pythonPath: "/nonexistent/python", scriptPath: process.cwd() })',
      '  .then(() => console.log("UNEXPECTED_RESOLVE"))',
      '  .catch((error) => console.log("SPAWN_REJECTION", JSON.stringify({ name: error.name, code: error.code })));',
      'setTimeout(() => console.log("SERVER_SURVIVED", process.listenerCount("uncaughtException") === uncaughtBefore && liveChildCount() === 0), 500);',
    ].join('\n');

    const result = runNode(script);

    expect(result.error).toBeUndefined();
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('SPAWN_REJECTION {"name":"BridgeSpawnError","code":"ENOENT"}');
    expect(result.stdout).toContain('SERVER_SURVIVED true');
  });

  test('a signal death reports its signal and releases the process-tree record', () => {
    if (!PYTHON) return;

    const scriptDir = fs.mkdtempSync(path.join(os.tmpdir(), 'zlib-bridge-signal-'));
    try {
      fs.writeFileSync(
        path.join(scriptDir, 'kill.py'),
        'import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n',
      );
      const script = [
        `import { runPythonBridge, liveChildCount } from ${JSON.stringify(RUNNER_URL)};`,
        `const error = await runPythonBridge("kill.py", { mode: "text", pythonPath: ${JSON.stringify(PYTHON)}, scriptPath: ${JSON.stringify(scriptDir)} }, { timeoutMs: 5000 }).then(() => null, (value) => value);`,
        'for (let attempt = 0; attempt < 20 && liveChildCount() !== 0; attempt++) {',
        '  await new Promise((resolve) => setTimeout(resolve, 50));',
        '}',
        'console.log("SIGNAL_REJECTION", JSON.stringify({ name: error?.name, code: error?.code, signal: error?.signal, message: error?.message, liveChildren: liveChildCount() }));',
      ].join('\n');
      const result = runNode(script, 8000);

      expect(result.error).toBeUndefined();
      expect(result.status).toBe(0);
      expect(result.stdout).toContain('"name":"BridgeKilledError"');
      expect(result.stdout).toContain('"signal":"SIGKILL"');
      expect(result.stdout).toMatch(/out of memory|OOM/i);
      expect(result.stdout).toContain('"liveChildren":0');
    } finally {
      fs.rmSync(scriptDir, { recursive: true, force: true });
    }
  });
});

describe('spawn errors and circuit-breaker counting', () => {
  let previousThreshold;
  let previousRetries;

  beforeEach(() => {
    previousThreshold = process.env.CIRCUIT_BREAKER_THRESHOLD;
    previousRetries = process.env.RETRY_MAX_RETRIES;
    process.env.CIRCUIT_BREAKER_THRESHOLD = '1';
    process.env.RETRY_MAX_RETRIES = '0';
  });

  afterEach(() => {
    if (previousThreshold === undefined) delete process.env.CIRCUIT_BREAKER_THRESHOLD;
    else process.env.CIRCUIT_BREAKER_THRESHOLD = previousThreshold;
    if (previousRetries === undefined) delete process.env.RETRY_MAX_RETRIES;
    else process.env.RETRY_MAX_RETRIES = previousRetries;
  });

  async function loadApi() {
    jest.resetModules();
    jest.clearAllMocks();
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      getManagedPythonPath: mockGetManagedPythonPath,
    }));
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge: mockRunPythonBridge,
      killAllPythonChildren: jest.fn(),
      installExitHooks: jest.fn(),
      liveChildCount: jest.fn(() => 0),
      DEFAULT_BRIDGE_TIMEOUT_MS: 240000,
      LONG_BRIDGE_TIMEOUT_MS: 2400000,
    }));

    const [{ searchByTerm }, { BridgeSpawnError }] = await Promise.all([
      import('../lib/zlibrary-api.js'),
      import('../lib/errors.js'),
    ]);
    mockGetManagedPythonPath.mockResolvedValue('/fake/python');
    return { searchByTerm, BridgeSpawnError };
  }

  test('ENOENT does not open the breaker, so the next call reaches the runner', async () => {
    const { searchByTerm, BridgeSpawnError } = await loadApi();
    mockRunPythonBridge.mockRejectedValue(new BridgeSpawnError('python was not found', 'ENOENT'));

    await expect(searchByTerm({ term: 'book' })).rejects.toMatchObject({ code: 'ENOENT' });
    await expect(searchByTerm({ term: 'book' })).rejects.toMatchObject({ code: 'ENOENT' });

    expect(mockRunPythonBridge).toHaveBeenCalledTimes(2);
  });

  test('other errno values continue to count toward the breaker', async () => {
    const { searchByTerm, BridgeSpawnError } = await loadApi();
    mockRunPythonBridge.mockRejectedValue(new BridgeSpawnError('process limit reached', 'EAGAIN'));

    await expect(searchByTerm({ term: 'book' })).rejects.toMatchObject({ code: 'EAGAIN' });
    await expect(searchByTerm({ term: 'book' })).rejects.toThrow('Circuit breaker is OPEN');

    expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
  });
});
