import './helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';

// Keep API, retry and breaker real; replace only interpreter/process I/O.
let api, runner, breaker, saved;
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
const outage = () => Object.assign(new Error('offline transport failure'), { code: 'ECONNRESET' });
const search = () => api.searchMultiSource({ query: 'offline', source: 'libgen' });
const health = () => ({ state: breaker.getState(), failures: breaker.getFailureCount() });

beforeEach(async () => {
  saved = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'per-call',
    RETRY_MAX_RETRIES: '3',
    RETRY_INITIAL_DELAY: '1',
    RETRY_MAX_DELAY: '1',
    CIRCUIT_BREAKER_THRESHOLD: '5',
    CIRCUIT_BREAKER_TIMEOUT: '60000',
    PYTHON_BRIDGE_TIMEOUT: '240000',
    LOG_LEVEL: 'error',
  });
  jest.resetModules();
  const { CircuitBreaker } = await import('../lib/circuit-breaker.js');
  const execute = CircuitBreaker.prototype.execute;
  jest.spyOn(CircuitBreaker.prototype, 'execute').mockImplementation(function (...args) {
    breaker = this;
    return execute.apply(this, args);
  });
  jest.spyOn(console, 'error').mockImplementation(() => {});
  runner = jest.fn();
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
  jest.useRealTimers();
  jest.restoreAllMocks();
  process.env = saved;
});

test.each(['hint', 'bounded hold'])(
  'an ordinary retrying HALF_OPEN probe supplies a %s',
  async (property) => {
    jest.useFakeTimers();
    process.env.RETRY_MAX_RETRIES = '0';
    runner.mockRejectedValue(outage());
    for (let i = 0; i < 5; i++) await expect(search()).rejects.toThrow();
    await jest.advanceTimersByTimeAsync(60001);
    process.env.RETRY_MAX_RETRIES = '1';
    let release, started;
    const retryStarted = new Promise((resolve) => {
      started = resolve;
    });
    runner
      .mockReset()
      .mockRejectedValueOnce(outage())
      .mockImplementationOnce(() => {
        started();
        return new Promise((resolve) => {
          release = resolve;
        });
      })
      .mockResolvedValue(envelope({ books: [] }));
    const probe = search();
    // Retry backoff uses node:timers/promises (real); the breaker clock is fake.
    await retryStarted;
    try {
      expect(health()).toEqual({ state: 'HALF_OPEN', failures: 5 });
      if (property === 'hint') {
        const { errorEnvelope } = await import('../lib/tool-errors.js');
        const refused = await search().catch((error) => error);
        expect(errorEnvelope(refused, 'search').error.retry_after_s).toBe(240);
        await jest.advanceTimersByTimeAsync(239999);
        const lastMillisecond = await search().catch((error) => error);
        expect(errorEnvelope(lastMillisecond, 'search').error.retry_after_s).toBe(1);
        expect(runner).toHaveBeenCalledTimes(2);
      } else {
        await jest.advanceTimersByTimeAsync(240000);
        await expect(search()).resolves.toEqual({ books: [] });
        expect(runner).toHaveBeenCalledTimes(3);
      }
    } finally {
      release(envelope({ books: [] }));
      await probe;
    }
    expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
  },
);

test.each([0, 60001])(
  'an already-aborted call cannot take breaker admission at +%i ms',
  async (elapsed) => {
    jest.useFakeTimers();
    process.env.RETRY_MAX_RETRIES = '0';
    runner.mockRejectedValue(outage());
    for (let i = 0; i < 5; i++) await expect(search()).rejects.toThrow();
    await jest.advanceTimersByTimeAsync(elapsed);
    const execute = breaker.execute;
    const admissions = execute.mock.calls.length;
    const controller = new AbortController();
    controller.abort();
    const failure = await api
      .searchMultiSource({ query: 'offline', source: 'libgen' }, { signal: controller.signal })
      .catch((error) => error);
    const { errorEnvelope } = await import('../lib/tool-errors.js');
    const { BridgeTimeoutError } = await import('../lib/errors.js');
    expect(errorEnvelope(failure, 'search', 'libgen').error).toMatchObject({
      code: 'cancelled',
      retry: 'safe',
      detail: { reason: 'aborted' },
    });
    expect(failure).toBeInstanceOf(BridgeTimeoutError);
    expect(execute).toHaveBeenCalledTimes(admissions);
    expect(runner).toHaveBeenCalledTimes(5);
    expect(health()).toEqual({ state: 'OPEN', failures: 5 });
  },
);

test.each(['libgen', 'eapi'])(
  '%s counts four exhausted attempts as one failed call',
  async (scope) => {
    runner.mockImplementation(async () => {
      throw outage();
    });
    const call = scope === 'eapi' ? () => api.getDownloadLimits() : search;
    await expect(call()).rejects.toThrow('offline transport failure');
    expect(runner).toHaveBeenCalledTimes(4);
    expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
    for (let i = 2; i <= 5; i++) {
      await expect(call()).rejects.toThrow('offline transport failure');
      expect(runner).toHaveBeenCalledTimes(i * 4);
      expect(health()).toEqual({ state: i === 5 ? 'OPEN' : 'CLOSED', failures: i });
    }
    await expect(call()).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
    expect(runner).toHaveBeenCalledTimes(20);
  },
);

test('a retry can recover at threshold one without opening the breaker', async () => {
  process.env.CIRCUIT_BREAKER_THRESHOLD = '1';
  jest.resetModules();
  api = await import('../lib/zlibrary-api.js');
  runner.mockRejectedValueOnce(outage()).mockResolvedValue(envelope({ books: [] }));
  await expect(search()).resolves.toEqual({ books: [] });
  // The re-imported breaker is a new class; assert subsequent admission too.
  await expect(search()).resolves.toEqual({ books: [] });
  expect(runner).toHaveBeenCalledTimes(3);
});

test('aborting retry backoff adds no health failure for the cancelled logical call', async () => {
  process.env.RETRY_MAX_RETRIES = '0';
  runner.mockRejectedValue(outage());
  await expect(search()).rejects.toThrow('offline transport failure');
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
  process.env.RETRY_MAX_RETRIES = '3';
  process.env.RETRY_INITIAL_DELAY = '10000';
  let attempted;
  const started = new Promise((resolve) => {
    attempted = resolve;
  });
  runner.mockImplementation(async () => {
    attempted();
    throw outage();
  });
  const controller = new AbortController();
  const pending = api.searchMultiSource(
    { query: 'offline', source: 'libgen' },
    { signal: controller.signal },
  );
  const rejection = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  await started;
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await rejection;
  expect(runner).toHaveBeenCalledTimes(2);
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
});

test('a HALF_OPEN probe can recover on a retry before its logical verdict', async () => {
  process.env.RETRY_MAX_RETRIES = '0';
  runner.mockRejectedValue(outage());
  for (let i = 0; i < 5; i++) await expect(search()).rejects.toThrow('offline transport failure');
  expect(health()).toEqual({ state: 'OPEN', failures: 5 });
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  process.env.RETRY_MAX_RETRIES = '1';
  runner
    .mockReset()
    .mockRejectedValueOnce(outage())
    .mockResolvedValue(envelope({ books: [] }));
  await expect(search()).resolves.toEqual({ books: [] });
  expect(runner).toHaveBeenCalledTimes(2);
  expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
});
