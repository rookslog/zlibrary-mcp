import './helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';

// Keep API, owner, retry and breaker real. Only interpreter/process I/O is fake.
// In particular a local receipt must not heal an outage or bypass open admission.
let api, errors, runner, breaker, ownerRun, saved;
let loginFailure, operationFailure, receipt, demands, transfers;
const envelope = (data) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(data) }] }),
];
const outage = () =>
  envelope({ error: 'offline outage', type: 'HTTPStatusError', status_code: 503 });
const acquire = () =>
  api.invokeAcquisitionBridge('acquire_book', {
    book_details: { source: 'zlibrary', id: '101', book_hash: 'hash101' },
    output_dir: '/offline',
  });
const health = () => ({ state: breaker.getState(), failures: breaker.getFailureCount() });

beforeEach(async () => {
  saved = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'memory',
    ZLIBRARY_EMAIL: 'offline@example.invalid',
    ZLIBRARY_PASSWORD: 'offline-password-canary',
    RETRY_MAX_RETRIES: '0',
    RETRY_INITIAL_DELAY: '1',
    CIRCUIT_BREAKER_THRESHOLD: '3',
    CIRCUIT_BREAKER_TIMEOUT: '60000',
    LOG_LEVEL: 'error',
  });
  jest.resetModules();
  loginFailure = undefined;
  operationFailure = false;
  receipt = false;
  demands = transfers = 0;
  errors = await import('../lib/errors.js');
  const { CircuitBreaker } = await import('../lib/circuit-breaker.js');
  const execute = CircuitBreaker.prototype.execute;
  jest.spyOn(CircuitBreaker.prototype, 'execute').mockImplementation(function (operation) {
    breaker = this;
    return execute.call(this, operation);
  });
  const { EapiSessionOwner } = await import('../lib/eapi-session.js');
  ownerRun = jest.spyOn(EapiSessionOwner.prototype, 'run');
  runner = jest.fn(async (_script, _options, options) => {
    const input = options.sessionInput;
    if (input?.action === 'login') {
      if (loginFailure instanceof Error) throw loginFailure;
      if (loginFailure) return loginFailure;
      options.onSession({
        identity: input.identity,
        generation: input.generation,
        domain: 'https://offline.invalid',
        remix_userid: 'offline-user',
        remix_userkey: 'offline-key-canary',
      });
      return envelope({ success: 1 });
    }
    if (input?.action === 'validate') {
      if (loginFailure) return loginFailure;
      return envelope({ success: 1 });
    }
    if (options.onSessionNeeded) {
      if (receipt) return envelope({ transfer_skipped: true, path: '/offline/book.pdf' });
      return new Promise((resolve, reject) => {
        options.onSessionNeeded({
          grant: () => {
            transfers++;
            if (operationFailure instanceof Error) {
              reject(operationFailure);
              return;
            }
            resolve(operationFailure ? outage() : envelope({ transfer_skipped: false }));
          },
          deny: reject,
        });
      });
    }
    return operationFailure ? outage() : envelope({ daily_limit: 10 });
  });
  // Count owner demands, not the private child request that asks for admission.
  const run = ownerRun.getMockImplementation();
  ownerRun.mockImplementation(function (...args) {
    demands++;
    return run.apply(this, args);
  });
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/offline/python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  api = await import('../lib/zlibrary-api.js');
});
afterEach(() => {
  jest.restoreAllMocks();
  process.env = saved;
});

async function openEapi() {
  operationFailure = true;
  for (let i = 0; i < 3; i++)
    await expect(api.getDownloadLimits()).rejects.toThrow('offline outage');
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  runner.mockClear();
  demands = transfers = 0;
}

test.each(['HTTP 503', 'transport TIMEOUT'])(
  'one lazy acquisition with login %s counts once and never retries its control child',
  async (kind) => {
    process.env.RETRY_MAX_RETRIES = '3';
    loginFailure =
      kind === 'HTTP 503'
        ? envelope({
            error: 'offline login outage',
            type: 'EAPIAuthenticationError',
            login_failed: { status_code: 503, token: 'offline login outage' },
          })
        : new errors.BridgeTimeoutError('offline login timeout', { reason: 'timeout' });
    const failure = await acquire().catch((error) => error);
    expect(failure).toBeInstanceOf(errors.ZLibraryError);
    expect(failure.context.details.request_issued).toBe(false);
    expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
    expect(runner.mock.calls.map((call) => call[2].sessionInput?.action)).toEqual([
      undefined,
      'login',
    ]);
    expect(demands).toBe(1);
    expect(transfers).toBe(0);
  },
);

test('open EAPI serves a memory receipt without demanding a session or changing breaker health', async () => {
  await openEapi();
  receipt = true;
  const before = health();
  await expect(acquire()).resolves.toEqual({ transfer_skipped: true, path: '/offline/book.pdf' });
  expect(runner).toHaveBeenCalledTimes(1);
  expect(demands).toBe(0);
  expect(transfers).toBe(0);
  expect(health()).toEqual(before);
});

test('an expired OPEN window is not probed or healed by a memory receipt', async () => {
  await openEapi();
  receipt = true;
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  await expect(acquire()).resolves.toMatchObject({ transfer_skipped: true });
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  expect(demands).toBe(0);
  expect(transfers).toBe(0);
});

test('open EAPI admits the lazy child but refuses a non-hit before owner demand or transfer', async () => {
  await openEapi();
  await expect(acquire()).rejects.toMatchObject({
    context: { details: { reason: 'breaker_open', request_issued: false } },
  });
  expect(runner).toHaveBeenCalledTimes(1);
  expect(runner.mock.lastCall[2].sessionInput).toBeUndefined();
  expect(demands).toBe(0);
  expect(transfers).toBe(0);
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
});

test('a lazy handoff refusal retains the breaker retry hint in the download envelope', async () => {
  process.env.CIRCUIT_BREAKER_THRESHOLD = '1';
  jest.resetModules();
  api = await import('../lib/zlibrary-api.js');
  const { errorEnvelope } = await import('../lib/tool-errors.js');
  jest.spyOn(Date, 'now').mockReturnValue(1700000000000);
  loginFailure = outage();
  await expect(acquire()).rejects.toThrow('offline outage');
  runner.mockClear();
  demands = transfers = 0;

  const failure = await acquire().catch((error) => error);
  expect(errorEnvelope(failure, 'download', 'zlibrary').error).toMatchObject({
    code: 'breaker_open',
    retry: 'safe',
    retry_after_s: 60,
    detail: { reason: 'breaker_open', dependency: 'eapi', request_issued: false },
  });
  expect(runner).toHaveBeenCalledTimes(1);
  expect(demands).toBe(0);
  expect(transfers).toBe(0);
});

test('receipt hits interleaved with provider failures do not prevent EAPI opening', async () => {
  operationFailure = true;
  for (let i = 1; i <= 3; i++) {
    await expect(api.getDownloadLimits()).rejects.toThrow('offline outage');
    receipt = true;
    await expect(acquire()).resolves.toMatchObject({ transfer_skipped: true });
    expect(breaker.getFailureCount()).toBe(i);
  }
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  expect(transfers).toBe(0);
});

test('a lazy transfer outage counts once after its successful control login', async () => {
  operationFailure = true;
  await expect(acquire()).rejects.toThrow('offline outage');
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
  expect(transfers).toBe(1);
  expect(runner).toHaveBeenCalledTimes(2);
});

test('shared login denial retains its original context and each lazy waiter gets a private copy', async () => {
  loginFailure = new errors.BridgeTimeoutError('offline login timeout', { reason: 'timeout' });
  const failures = await Promise.all([acquire().catch((e) => e), acquire().catch((e) => e)]);
  expect(failures[0]).toBeInstanceOf(errors.BridgeTimeoutError);
  expect(failures[1]).toBeInstanceOf(errors.BridgeTimeoutError);
  expect(failures[0]).not.toBe(failures[1]);
  expect(failures[0].context).not.toBe(failures[1].context);
  expect(failures[0].context.details).not.toBe(failures[1].context.details);
  expect(failures.map((e) => e.context.details.request_issued)).toEqual([false, false]);
  expect(failures.map((e) => typeof e.stack)).toEqual(['string', 'string']);
  expect(loginFailure.context).toEqual({ reason: 'timeout' });
  expect(runner.mock.calls.filter((call) => call[2].sessionInput?.action === 'login')).toHaveLength(
    1,
  );
});

test('a stale-session validation outage counts once without granting or replaying the transfer', async () => {
  await expect(api.getDownloadLimits()).resolves.toMatchObject({ daily_limit: 10 });
  process.env.ZLIBRARY_SESSION_IDLE_MS = '1';
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 10);
  loginFailure = outage();
  runner.mockClear();
  await expect(acquire()).rejects.toThrow('offline outage');
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
  expect(runner.mock.calls.map((call) => call[2].sessionInput?.action)).toEqual([
    undefined,
    'validate',
  ]);
  expect(transfers).toBe(0);
});

test.each([401, 403, 404])(
  'lazy login HTTP %i and its cooldown do not poison EAPI',
  async (status) => {
    loginFailure = envelope({
      error: 'offline login refused',
      type: 'EAPIAuthenticationError',
      login_failed: { status_code: status, token: 'offline login refused' },
    });
    await expect(acquire()).rejects.toMatchObject({ context: { reason: 'login_failed' } });
    await expect(acquire()).rejects.toMatchObject({ context: { reason: 'auth_backoff' } });
    expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
    expect(transfers).toBe(0);
    expect(
      runner.mock.calls.filter((call) => call[2].sessionInput?.action === 'login'),
    ).toHaveLength(1);
  },
);

test.each([
  ['timeout', 1],
  ['aborted', 0],
])(
  'lazy transfer %s has one logical health verdict and is not replayed',
  async (reason, failures) => {
    operationFailure = new errors.BridgeTimeoutError('offline transfer stopped', { reason });
    process.env.RETRY_MAX_RETRIES = '3';
    await expect(acquire()).rejects.toMatchObject({
      code: 'TIMEOUT',
      context: { reason, details: { request_issued: true } },
    });
    expect(health()).toEqual({ state: 'CLOSED', failures });
    expect(transfers).toBe(1);
    expect(runner.mock.calls.map((call) => call[2].sessionInput?.action)).toEqual([
      undefined,
      'login',
    ]);
  },
);
