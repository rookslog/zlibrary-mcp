import { describe, test, expect } from '@jest/globals';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { classifyFailure, ERROR_CODES } from '../dist/lib/tool-errors.js';
import { isTransientDependencyError, isRetryableError } from '../dist/lib/retry-manager.js';

describe('local acquisition budget attribution', () => {
  test.each([false, true])(
    'budget exhaustion preserves issuance %s without replay or breaker failure',
    (issued) => {
      const error = {
        context: {
          pythonErrorType: 'AcquisitionError',
          details: {
            reason: 'walk_budget_exhausted',
            request_issued: issued,
          },
        },
      };
      const failure = classifyFailure(error, 'download', 'zlibrary');
      expect(failure.code).toBe(issued ? 'outcome_unknown' : 'provider_timeout');
      expect(failure.info.retry).toBe(issued ? 'no' : 'safe');
      expect(failure.info.detail).toEqual({
        reason: 'walk_budget_exhausted',
        request_issued: issued,
      });
      expect(isTransientDependencyError(error)).toBe(false);
      expect(isRetryableError(error)).toBe(false);
      expect(ERROR_CODES).toHaveLength(19);
    },
  );
});

// Real Node kill timer, Python process and OS lease, with a logical Python clock
// for contention/expiry. A larger owner leaves startup headroom on loaded hosts.
const { runPythonBridge } = await import('../dist/lib/python-runner.js');
test.each([
  ['late', false, 0],
  ['early', true, 1],
])(
  'real Node owner receives attributed %s contention result before killing Python',
  async (scenario, issued, transfers) => {
    const directory = mkdtempSync(join(tmpdir(), 'i280-deadline-'));
    try {
      const lines = await runPythonBridge(
        'acquisition_deadline_worker.py',
        {
          pythonPath: `${process.cwd()}/.venv/bin/python`,
          scriptPath: `${process.cwd()}/__tests__/python`,
          args: [scenario],
          env: {
            ...process.env,
            ZLIBRARY_OUTPUT_ROOT: directory,
            // The worker creates its own TemporaryDirectory beneath this root.
            TMPDIR: directory,
            TEMP: directory,
            PYTHON_BRIDGE_LONG_TIMEOUT: '40000',
          },
        },
        { timeoutMs: 40000, label: 'i280 synthetic contention' },
      );
      expect(JSON.parse(lines.at(-1))).toEqual({
        reason: 'walk_budget_exhausted',
        request_issued: issued,
        transfers,
      });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  },
  45000,
);

test('EAPI projection preserves a local budget refusal without inventing auth diagnostics', async () => {
  const { safeEapiFailureEnvelope } = await import('../dist/lib/auth-diagnostics.js');
  expect(
    safeEapiFailureEnvelope({
      error: 'untrusted path and provider prose',
      type: 'AcquisitionError',
      details: { reason: 'walk_budget_exhausted', request_issued: true },
    }),
  ).toEqual({
    error: 'Acquisition deadline exhausted',
    type: 'AcquisitionError',
    details: { reason: 'walk_budget_exhausted', request_issued: true },
  });
});

test.each(['zlibrary', 'annas', 'libgen'])(
  'issued %s budget refusal never advertises a safe replay',
  async (source) => {
    const { errorEnvelope } = await import('../dist/lib/tool-errors.js');
    const result = errorEnvelope(
      {
        context: {
          details: {
            reason: 'walk_budget_exhausted',
            request_issued: true,
          },
        },
      },
      'download',
      source,
    );
    expect(result.error.code).toBe('outcome_unknown');
    expect(result.error.retry).toBe('no');
    expect(result.error.recovery).toContain('Check the destination');
    expect(result.error.recovery).not.toContain('Call download again');
  },
);
