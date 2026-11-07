import './helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';

let api, runner, CircuitBreaker, errorEnvelope;
const saved = { ...process.env };
beforeEach(async () => {
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'per-call',
    RETRY_MAX_RETRIES: '0',
    CIRCUIT_BREAKER_THRESHOLD: '1',
  });
  jest.resetModules();
  runner = jest.fn().mockRejectedValue(
    Object.assign(new Error('offline failure'), {
      stderr: JSON.stringify({
        error: 'offline failure',
        type: 'HTTPStatusError',
        status_code: 503,
      }),
    }),
  );
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/fake/python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  ({ CircuitBreaker } = await import('../lib/circuit-breaker.js'));
  ({ errorEnvelope } = await import('../lib/tool-errors.js'));
  api = await import('../lib/zlibrary-api.js');
});
afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
  process.env = { ...saved };
});

test.each(['eapi', 'source_walk', 'local'])(
  'OPEN and held HALF_OPEN errors carry typed %s dependency through public detail',
  async (dependency) => {
    jest.useFakeTimers();
    const breaker = new CircuitBreaker({ dependency, threshold: 1, timeout: 60000 });
    await expect(
      breaker.execute(async () => {
        throw new Error('offline');
      }),
    ).rejects.toThrow('offline');
    const check = (failure) => {
      expect(failure.dependency).toBe(dependency);
      expect(errorEnvelope(failure, 'search').error).toMatchObject({
        code: 'breaker_open',
        retry: 'safe',
        detail: { dependency, request_issued: false },
      });
    };
    check(await breaker.execute(async () => {}).catch((error) => error));
    await jest.advanceTimersByTimeAsync(60001);
    let release;
    const probe = breaker.execute(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    check(await breaker.execute(async () => {}).catch((error) => error));
    release();
    await probe;
  },
);

test.each([
  ['eapi', () => api.getDownloadLimits()],
  ['source_walk', () => api.searchMultiSource({ query: 'offline', source: 'libgen' })],
])(
  'API breaker wires %s dependency without dispatching a refused call',
  async (dependency, call) => {
    await expect(call()).rejects.toBeInstanceOf(Error);
    const failure = await call().catch((error) => error);
    expect(failure.dependency).toBe(dependency);
    expect(errorEnvelope(failure, 'search').error.detail.dependency).toBe(dependency);
    expect(runner).toHaveBeenCalledTimes(1);
  },
);

test('EAPI breaker refusal has no request diagnostic', async () => {
  await expect(api.getDownloadLimits()).rejects.toBeInstanceOf(Error);
  const failure = await api.getDownloadLimits().catch((error) => error);
  expect(failure.code).toBe('CIRCUIT_BREAKER_OPEN');
  expect(errorEnvelope(failure, 'get_limits', 'zlibrary').error.detail).not.toHaveProperty(
    'diagnostics',
  );
  expect(runner).toHaveBeenCalledTimes(1);
});

test('a held HALF_OPEN hint stays positive when the clock passes its deadline before timer dispatch', async () => {
  jest.useFakeTimers();
  const breaker = new CircuitBreaker({ dependency: 'eapi', threshold: 1, timeout: 60000 });
  await expect(
    breaker.execute(async () => {
      throw new Error('offline');
    }),
  ).rejects.toThrow();
  await jest.advanceTimersByTimeAsync(60001);
  let release;
  const probe = breaker.execute((admission) => {
    admission.holdFor(240000);
    return new Promise((resolve) => {
      release = resolve;
    });
  });
  // Move the wall clock only: the exclusive-hold timer has not been dispatched.
  jest.setSystemTime(Date.now() + 240001);
  const refusedOperation = jest.fn(async () => 'must not execute');
  try {
    const failure = await breaker.execute(refusedOperation).catch((error) => error);
    const reply = errorEnvelope(failure, 'search', 'zlibrary');
    expect(reply.error.code).toBe('breaker_open');
    expect(reply.error.retry_after_s).toBeGreaterThanOrEqual(1);
    expect(refusedOperation).not.toHaveBeenCalled();
  } finally {
    release();
    await probe;
  }
});
