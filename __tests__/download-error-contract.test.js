import { afterEach, beforeEach, describe, expect, jest, test } from '@jest/globals';

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
const syntheticProvenance = {
  source: 'zlibrary', route: 'eapi', mirror: null, host: 'synthetic-transfer-host',
};
const envelope = (result) => [JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(result) }] })];

async function downloadError() {
  const operation = api.downloadBookToFile(args);
  await expect(operation).rejects.toBeInstanceOf(Error);
  return operation.catch((error) => error);
}

describe('download error contract', () => {
  test.each(['timeout', 'aborted'])('preserves the fatal bridge %s error', async (reason) => {
    const context = { reason, label: 'synthetic download', timeoutMs: 42, stderr: 'synthetic diagnostic' };
    const original = new errors.BridgeTimeoutError(`synthetic ${reason}`, context);
    const cause = new Error('synthetic root cause');
    original.cause = cause;
    original.diagnostic = { childPid: 123 };
    const stack = original.stack;
    runPythonBridge.mockRejectedValue(original);

    const caught = await downloadError();

    expect(caught).toBeInstanceOf(errors.BridgeTimeoutError);
    expect(caught.name).toBe('BridgeTimeoutError');
    expect(caught.code).toBe('TIMEOUT');
    expect(caught.fatal).toBe(true);
    expect(caught.retryable).toBe(false);
    expect(caught.context).toBe(context);
    expect(caught.cause).toBe(cause);
    expect(caught.diagnostic).toBe(original.diagnostic);
    expect(caught.stack).toBe(stack);
    expect(caught.message).toBe(`Failed to download book: synthetic ${reason}`);
  });

  test.each([false, true])('preserves ordinary PythonBridgeError retryable=%s', async (retryable) => {
    const context = { details: { source: 'synthetic-provider', reason: 'connect_timeout' } };
    const original = new errors.PythonBridgeError('synthetic bridge failure', context, retryable);
    runPythonBridge.mockRejectedValue(original);

    const caught = await downloadError();

    expect(caught).toBeInstanceOf(errors.PythonBridgeError);
    expect(caught.name).toBe('PythonBridgeError');
    expect(caught.code).toBe('PYTHON_ERROR');
    expect(caught.fatal).toBe(false);
    expect(caught.retryable).toBe(retryable);
    expect(caught.context).toBe(context);
    expect(caught.message).toBe('Failed to download book: synthetic bridge failure');
  });

  test('preserves another typed error subclass', async () => {
    const context = { reason: 'synthetic rejection' };
    runPythonBridge.mockRejectedValue(new errors.AuthenticationError('synthetic auth failure', context));

    const caught = await downloadError();

    expect(caught).toBeInstanceOf(errors.AuthenticationError);
    expect(caught.name).toBe('AuthenticationError');
    expect(caught.code).toBe('AUTH_ERROR');
    expect(caught.fatal).toBe(true);
    expect(caught.retryable).toBe(false);
    expect(caught.context).toBe(context);
  });

  test('keeps normalized provider source/reason diagnostics', async () => {
    const details = { source: 'synthetic-provider', reason: 'dns_failure', bytes_received: 17 };
    const original = Object.assign(new Error('synthetic child failure'), {
      stderr: JSON.stringify({ error: 'synthetic provider failure', type: 'ProviderError', details }),
    });
    runPythonBridge.mockRejectedValue(original);

    const caught = await downloadError();

    expect(caught).toBeInstanceOf(errors.PythonBridgeError);
    expect(caught.context.details).toEqual(details);
    expect(caught.context.originalError).toBe(original);
    expect(caught.context.stderr).toBe(original.stderr);
    expect(caught.message).toBe('Failed to download book: download_book failed: synthetic provider failure');
  });

  test('keeps generic validation failure message and cause', async () => {
    runPythonBridge.mockResolvedValue(envelope({}));

    const caught = await downloadError();

    expect(caught.constructor).toBe(Error);
    expect(caught.message).toBe('Failed to download book: Invalid response from Python bridge: Missing original file_path.');
    expect(caught.cause).toBeInstanceOf(Error);
    expect(caught.cause.message).toBe('Invalid response from Python bridge: Missing original file_path.');
  });

  test.each([
    { file_path: '/synthetic/book.epub', provenance: syntheticProvenance },
    { file_path: '/synthetic/book.epub', provenance: syntheticProvenance, processed_file_path: null, processing_error: 'synthetic optional processing failure' },
  ])('retains the successful or partial-processing result %#', async (result) => {
    runPythonBridge.mockResolvedValue(envelope(result));
    await expect(api.downloadBookToFile(args)).resolves.toEqual(result);
  });
});
