import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import os from 'node:os';

let saved, api, AcquisitionTools, errorEnvelope, runner, clock, now;
beforeEach(async () => {
  saved = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_EMAIL: 'reader@fixture.invalid',
    ZLIBRARY_PASSWORD: 'synthetic-only',
    ZLIBRARY_SESSION_MODE: 'per-call',
    RETRY_MAX_RETRIES: '0',
    CIRCUIT_BREAKER_THRESHOLD: '1',
    CIRCUIT_BREAKER_TIMEOUT: '12345',
    ZLIBRARY_OUTPUT_ROOT: os.tmpdir(),
    LOG_LEVEL: 'error',
  });
  now = 1000000;
  clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
  jest.resetModules();
  runner = jest.fn(async () => {
    throw new Error('Fixture process boundary');
  });
  jest.unstable_mockModule('../../dist/lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/fixture/python',
  }));
  jest.unstable_mockModule('../../dist/lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  api = await import('../../dist/lib/zlibrary-api.js');
  ({ AcquisitionTools } = await import('../../dist/lib/acquisition-tools.js'));
  ({ errorEnvelope } = await import('../../dist/lib/tool-errors.js'));
});
afterEach(() => {
  clock.mockRestore();
  process.env = saved;
});
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
const publicTools = (env) =>
  new AcquisitionTools(api.invokeAcquisitionBridge, env, {
    localState: () => api.getSearchState(env),
  });
const ownerFields = (error) =>
  Object.fromEntries(
    ['code', 'source', 'operation', 'message', 'retry', 'recovery', 'retry_after_s', 'detail']
      .filter((key) => error[key] !== undefined)
      .map((key) => [key, error[key]]),
  );

test.each([
  ['zlibrary', {}, ['ZLIBRARY_EMAIL', 'ZLIBRARY_PASSWORD']],
  ['annas', { ANNAS_BASE_URL: 'file:///fixture' }, ['ANNAS_BASE_URL']],
])(
  '%s configuration refusal retains the named variables and honest owner message',
  async (source, env, variables) => {
    const reply = await publicTools(env).call('search', { query: 'Fixture', sources: [source] });
    expect(reply.error).toMatchObject({
      code: 'credentials_missing',
      message: `${source}: Source configuration is missing.`,
      retry: 'no',
      detail: { reason: 'configuration_error', variables, request_issued: false },
    });
    expect(reply.error.attempts.find((line) => line.source === source).state).toBe('not_asked');
    expect(runner).not.toHaveBeenCalled();
  },
);

async function openBreaker() {
  runner.mockRejectedValue(
    Object.assign(new Error('Fixture transport'), {
      stderr: JSON.stringify({
        error: 'Fixture outage',
        type: 'HTTPStatusError',
        status_code: 503,
      }),
    }),
  );
  await api.invokeAcquisitionBridge('search', { query: 'Fixture' }).catch(() => {});
}

test('open breaker refusal preserves the complete owner envelope and rounds retry upward', async () => {
  await openBreaker();
  const error = await api
    .invokeAcquisitionBridge('search', { query: 'Fixture' })
    .catch((error) => error);
  const expected = errorEnvelope(error, 'search', 'zlibrary').error;
  expect(expected).toMatchObject({
    retry_after_s: 13,
    detail: { dependency: 'eapi', request_issued: false },
  });
  const reply = await publicTools(process.env).call('search', {
    query: 'Fixture',
    sources: ['zlibrary'],
  });
  expect(ownerFields(reply.error)).toEqual(ownerFields(expected));
  expect(reply.error.attempts.find((line) => line.source === 'zlibrary')).toMatchObject({
    state: 'not_asked',
    retry_after_s: 13,
  });
  expect(runner).toHaveBeenCalledTimes(1);
});

async function backoff() {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.CIRCUIT_BREAKER_THRESHOLD = '100';
  // The fixture login is a function call only; no interpreter or network is used.
  runner.mockRejectedValue(
    Object.assign(new Error('Fixture login failed'), {
      stderr: JSON.stringify({
        type: 'EAPIAuthenticationError',
        error: 'Fixture authentication rejected',
      }),
    }),
  );
  await api.invokeAcquisitionBridge('search', { query: 'Fixture' }).catch(() => {});
  now += 50;
}

test('authentication backoff retains diagnostics and the rounded owner retry', async () => {
  await backoff();
  const error = await api
    .invokeAcquisitionBridge('search', { query: 'Fixture' })
    .catch((error) => error);
  const expected = errorEnvelope(error, 'search', 'zlibrary').error;
  expect(expected).toMatchObject({
    code: 'breaker_open',
    retry_after_s: 30,
    detail: {
      reason: 'auth_backoff',
      request_issued: false,
      diagnostics: { failure_kind: 'auth_rejected' },
    },
  });
  const reply = await publicTools(process.env).call('search', {
    query: 'Fixture',
    sources: ['zlibrary'],
  });
  // A call's diagnostic request ID is new; the retained failure evidence is the same.
  expected.detail.diagnostics.request_id = expect.any(String);
  expect(ownerFields(reply.error)).toEqual(ownerFields(expected));
  expect(runner).toHaveBeenCalledTimes(1);
});

test('shipped server reads its real admission snapshot before dispatch', async () => {
  runner.mockRejectedValue(
    Object.assign(new Error('Fixture outage'), {
      stderr: JSON.stringify({
        error: 'Fixture outage',
        type: 'HTTPStatusError',
        status_code: 503,
      }),
    }),
  );
  const { createServer } = await import('../../dist/index.js');
  const server = createServer();
  try {
    await server.tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
    const reply = await server.tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
    expect(reply.error.attempts.find((line) => line.source === 'zlibrary').state).toBe('not_asked');
    expect(runner).toHaveBeenCalledTimes(1);
  } finally {
    await server.close();
  }
});

test('real authentication admission uses the requested identity for limits and search', async () => {
  await backoff();
  const other = { ...process.env, ZLIBRARY_EMAIL: 'other@fixture.invalid' };
  expect(api.getSearchState(other).zlibrary.admission).toBe('open');
  expect(api.getSearchState(process.env).zlibrary.admission).toBe('backoff');
  const limits = await publicTools(process.env).call('get_limits');
  expect(limits.sources.zlibrary).toMatchObject({ admission: 'backoff', retry_after_s: 30 });
});

test('overlapping login backoff and breaker expose the longer retry in every public projection', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.CIRCUIT_BREAKER_TIMEOUT = '600000';
  jest.resetModules();
  api = await import('../../dist/lib/zlibrary-api.js');
  ({ AcquisitionTools } = await import('../../dist/lib/acquisition-tools.js'));
  runner.mockRejectedValue(
    Object.assign(new Error('Fixture login outage'), {
      stderr: JSON.stringify({
        error: 'Fixture login outage',
        type: 'HTTPStatusError',
        status_code: 503,
      }),
    }),
  );
  await api.invokeAcquisitionBridge('search', { query: 'Fixture' }).catch(() => {});
  now += 50;
  expect(api.getSearchState(process.env).zlibrary).toMatchObject({
    admission: 'backoff',
    breaker: 'open',
  });
  const tools = publicTools(process.env);
  const limits = await tools.call('get_limits');
  expect(limits.sources.zlibrary.retry_after_s).toBe(600);
  const reply = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect(reply.error).toMatchObject({
    code: 'breaker_open',
    retry: 'safe',
    retry_after_s: 600,
    detail: { reason: 'auth_backoff', request_issued: false },
  });
  expect(reply.error.attempts.find((line) => line.source === 'zlibrary')).toMatchObject({
    state: 'not_asked',
    reason: 'auth_backoff',
    retry_after_s: 600,
  });
  expect(runner).toHaveBeenCalledTimes(1);
});
