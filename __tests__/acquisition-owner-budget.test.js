import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

let directory, saved;
const listeners = new Map(
  ['exit', 'SIGINT', 'SIGTERM'].map((event) => [event, new Set(process.rawListeners(event))]),
);
beforeEach(() => {
  saved = { ...process.env };
  directory = mkdtempSync(join(tmpdir(), 'i280-owner-'));
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'per-call',
    PYTHON_BRIDGE_LONG_TIMEOUT: '2400000',
    // A caller/environment cannot make the child ignore the effective owner.
    PYTHON_BRIDGE_CALL_TIMEOUT_MS: '999999',
    RETRY_MAX_RETRIES: '0',
  });
  jest.resetModules();
});
afterEach(() => {
  process.env = saved;
  rmSync(directory, { recursive: true, force: true });
  for (const [event, previous] of listeners)
    for (const listener of process.rawListeners(event))
      if (!previous.has(listener)) process.removeListener(event, listener);
});

// Catches forwarding the environment's 2400s budget instead of CallOptions.timeoutMs.
test.each([
  [1500, 1.3115, 1.1680546875],
  [4000, 3.499, 3.116296875],
])(
  'effective %sms call owner bounds the child and finalization reserve',
  async (timeoutMs, budget, slice) => {
    jest.unstable_unmockModule('../lib/python-runner.js');
    const runner = await import('../lib/python-runner.js');
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      getManagedPythonPath: async () => join(process.cwd(), '.venv/bin/python'),
    }));
    // Only redirect the provider entry point; keep API, spawn, timer and acquisition real.
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      LONG_BRIDGE_TIMEOUT_MS: runner.LONG_BRIDGE_TIMEOUT_MS,
      runPythonBridge: (_script, options, runOptions) =>
        runner.runPythonBridge(
          'acquisition_owner_budget_worker.py',
          { ...options, scriptPath: join(process.cwd(), '__tests__/python') },
          runOptions,
        ),
    }));
    const api = await import('../lib/zlibrary-api.js');
    const result = await api.invokeAcquisitionBridge(
      'acquire_book',
      {
        book_details: { source: 'libgen', md5: 'a'.repeat(32), extension: 'pdf' },
        output_dir: directory,
        filename: 'chosen.pdf',
      },
      { timeoutMs },
    );
    expect(result.budget).toBeCloseTo(budget, 8);
    expect(result.transfer_slice).toBeCloseTo(slice, 8);
  },
  10000,
);
