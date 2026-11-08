import { jest, test, expect, beforeEach, afterEach } from '@jest/globals';

const metadata = jest.fn();
const run = jest.fn();
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
let original, api, AcquisitionTools;

beforeEach(async () => {
  original = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_EMAIL: 'environment@example.test',
    ZLIBRARY_PASSWORD: 'fake-environment-password',
    ZLIBRARY_SESSION_MODE: 'memory',
    RETRY_MAX_RETRIES: '0',
  });
  jest.resetModules();
  metadata.mockReset().mockRejectedValue(new Error('fake metadata deadline'));
  run.mockReset();
  jest.unstable_mockModule('../lib/login.js', () => ({ readLoginMetadata: metadata }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: run,
    LONG_BRIDGE_TIMEOUT_MS: 3900000,
  }));
  api = await import('../lib/zlibrary-api.js');
  ({ AcquisitionTools } = await import('../lib/acquisition-tools.js'));
});
afterEach(() => {
  process.env = original;
});

test('metadata refusal preserves receipt reuse and marks the child to forbid password login', async () => {
  const receipt = {
    path: '/synthetic/book.pdf',
    transfer_skipped: true,
    reused: false,
    bytes: 100,
    verified: 'sha256',
    record: '/synthetic/book.pdf.acq.json',
  };
  let child;
  run.mockImplementation(async (_script, options, control) => {
    child = { env: options.env, control };
    return envelope(receipt);
  });
  await expect(
    api.invokeAcquisitionBridge('acquire_book', {
      book_details: { source: 'zlibrary', id: '123', hash: 'fake' },
    }),
  ).resolves.toEqual(receipt);
  expect(metadata).toHaveBeenCalled();
  expect(child.control.sessionInput).toBeUndefined();
  expect(child.control.onSessionNeeded).toBeUndefined();
  expect(child.env.ZLIBRARY_LOGIN_METADATA_REFUSED).toBe('1');
});

test('failed metadata reports known environment configuration and a local unchecked state', async () => {
  const invoke = jest.fn();
  const tools = new AcquisitionTools(invoke);
  const result = await tools.call('get_limits', { mode: 'local', sources: ['zlibrary'] });
  expect(result.sources.zlibrary.configured).toBe(true);
  expect(result.sources.zlibrary.note).toMatch(/not checked/i);
  expect(result.sources.zlibrary.error).toMatchObject({
    code: 'internal',
    detail: { reason: 'login_metadata_unavailable', request_issued: false },
  });
  expect(invoke).not.toHaveBeenCalled();
});

test('failed metadata refuses account work without claiming missing environment variables', async () => {
  const invoke = jest.fn();
  const result = await new AcquisitionTools(invoke).call('get_history', {});
  expect(result.error).toMatchObject({
    code: 'internal',
    detail: { reason: 'login_metadata_unavailable', request_issued: false },
  });
  expect(result.error.detail.variables).toBeUndefined();
  expect(result.error.detail.diagnostics).toBeUndefined();
  expect(invoke).not.toHaveBeenCalled();
});

test('saved identity is removed from bridge diagnostics without an environment email', async () => {
  delete process.env.ZLIBRARY_EMAIL;
  delete process.env.ZLIBRARY_PASSWORD;
  metadata.mockResolvedValue({
    configured: true,
    identity: 'saved-private@example.test',
    origin: 'saved',
    backend: 'file',
    expiry: 'unknown',
    generation: 1,
    notice: '',
  });
  run.mockRejectedValue(new Error('fake failure for saved-private@example.test'));
  let failure;
  try {
    await api.searchBooks({ query: 'fake' });
  } catch (error) {
    failure = error;
  }
  expect(failure).toBeDefined();
  expect(String(failure) + JSON.stringify(failure)).not.toContain('saved-private@example.test');
});

test('local login refusal preserves fixed advice without a provider attempt', async () => {
  const { safeEapiFailureEnvelope } = await import('../lib/auth-diagnostics.js');
  expect(
    safeEapiFailureEnvelope({
      type: 'StoreError',
      error: 'untrusted private text',
      details: { reason: 'login_state_changed', request_issued: false },
    }),
  ).toEqual({
    type: 'StoreError',
    error: 'Saved login changed; run zlibrary login and retry',
    details: { reason: 'login_state_changed', request_issued: false },
  });
});

test.each(['StoreError', 'ValueError'])(
  'public account-state failure keeps login advice and zero request evidence for %s',
  async (pythonErrorType) => {
    const { errorEnvelope } = await import('../lib/tool-errors.js');
    const reply = errorEnvelope(
      {
        context: {
          pythonErrorType,
          details: { reason: 'login_state_changed', request_issued: false },
        },
      },
      'get_history',
      'zlibrary',
    );
    expect(reply.error.code).toBe('internal');
    expect(reply.error.message).toBe('zlibrary: Saved login changed before the provider request.');
    expect(reply.error.recovery).toMatch(/zlibrary login.*retry/i);
    expect(reply.error.detail).toMatchObject({
      reason: 'login_state_changed',
      request_issued: false,
    });
    expect(reply.error.detail.diagnostics).toBeUndefined();
  },
);
