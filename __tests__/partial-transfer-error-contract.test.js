import './helpers/environment-login.js';
import { afterEach, beforeEach, describe, expect, jest, test } from '@jest/globals';

// These fixtures describe a single per-call subprocess, not private acquisition.
let previousSessionMode;
beforeEach(() => {
  previousSessionMode = process.env.ZLIBRARY_SESSION_MODE;
  process.env.ZLIBRARY_SESSION_MODE = 'per-call';
});
afterEach(() => {
  if (previousSessionMode === undefined) delete process.env.ZLIBRARY_SESSION_MODE;
  else process.env.ZLIBRARY_SESSION_MODE = previousSessionMode;
});

const runPythonBridge = jest.fn();
let api;
let errors;
let previousRetries;
let consoleSpy;

beforeEach(async () => {
  previousRetries = process.env.RETRY_MAX_RETRIES;
  process.env.RETRY_MAX_RETRIES = '0';
  consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  jest.resetModules();
  runPythonBridge.mockReset();
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/synthetic/nonexistent-python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  errors = await import('../lib/errors.js');
  api = await import('../lib/zlibrary-api.js');
});

afterEach(() => {
  if (previousRetries === undefined) delete process.env.RETRY_MAX_RETRIES;
  else process.env.RETRY_MAX_RETRIES = previousRetries;
  consoleSpy.mockRestore();
});

const args = { bookDetails: { id: 'synthetic-book' } };

async function downloadError() {
  const operation = api.downloadBookToFile(args);
  await expect(operation).rejects.toBeInstanceOf(Error);
  return operation.catch((error) => error);
}

describe('partial source transfer diagnosis', () => {
  test('preserves bridge byte/resume detail and maps the reason into the existing per-mirror envelope', async () => {
    const details = {
      operation: 'download',
      failures: [
        {
          provider: 'libgen',
          host: 'cdn.example',
          reason: 'partial_transfer',
          detail: 'partial transfer: bytes_received=8000 resumes_tried=2; peer closed SECRET',
        },
      ],
    };
    runPythonBridge.mockRejectedValue(
      Object.assign(new Error('synthetic child failure'), {
        stderr: JSON.stringify({
          error: 'source transfer failed',
          type: 'AllSourcesFailedError',
          details,
        }),
      }),
    );
    const caught = await downloadError();
    expect(caught).toBeInstanceOf(errors.PythonBridgeError);
    expect(caught.context.details).toEqual(details);
    const { errorEnvelope } = await import('../dist/lib/tool-errors.js');
    const { isTransientDependencyError } = await import('../dist/lib/retry-manager.js');
    const publicError = errorEnvelope(caught, 'download', 'libgen').error;
    expect(publicError.code).toBe('provider_error');
    expect(publicError.detail.reason).toBe('partial_transfer');
    expect(publicError.attempts).toEqual([
      { source: 'libgen', outcome: 'failed', code: 'provider_error', reason: 'partial_transfer' },
    ]);
    expect(isTransientDependencyError(caught)).toBe(true);
    expect(JSON.stringify(publicError)).not.toMatch(
      /SECRET|cdn\.example|bytes_received|resumes_tried/,
    );
  });

  test('a resume HTTP 503 remains transient for breaker accounting', async () => {
    runPythonBridge.mockRejectedValue(
      Object.assign(new Error('synthetic child failure'), {
        stderr: JSON.stringify({
          error: 'source transfer failed',
          type: 'AllSourcesFailedError',
          details: {
            operation: 'download',
            failures: [
              {
                provider: 'libgen',
                host: 'cdn.example',
                reason: 'http_error',
                detail: 'HTTP 503',
              },
            ],
          },
        }),
      }),
    );
    const caught = await downloadError();
    const { isTransientDependencyError } = await import('../dist/lib/retry-manager.js');
    expect(isTransientDependencyError(caught)).toBe(true);
  });
});
