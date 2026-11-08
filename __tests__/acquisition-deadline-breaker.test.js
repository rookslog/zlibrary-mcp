import './helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';

// Keep API, retry and breaker real; substitute only interpreter/process I/O.
let api, runner, saved, breaker;
const reply = (data) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(data) }] }),
];
const failure = (reason, issued) =>
  Object.assign(new Error('fixture child exit'), {
    stderr: JSON.stringify({
      error: 'fixture refusal',
      type: 'AcquisitionError',
      details: { reason, request_issued: issued },
    }),
  });

beforeEach(async () => {
  saved = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'per-call',
    RETRY_MAX_RETRIES: '3',
    RETRY_INITIAL_DELAY: '1',
    CIRCUIT_BREAKER_THRESHOLD: '2',
    LOG_LEVEL: 'error',
  });
  jest.resetModules();
  runner = jest.fn();
  const { CircuitBreaker } = await import('../lib/circuit-breaker.js');
  const execute = CircuitBreaker.prototype.execute;
  jest.spyOn(CircuitBreaker.prototype, 'execute').mockImplementation(function (...args) {
    breaker = this;
    return execute.apply(this, args);
  });
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/fixture/python',
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

const acquire = (source) =>
  api.invokeAcquisitionBridge('acquire_book', {
    book_details: { source, id: '17', book_hash: 'fixture', md5: 'a'.repeat(32) },
    output_dir: '/fixture',
  });

test.each(
  ['zlibrary', 'libgen', 'annas'].flatMap((source) =>
    [false, true].map((issued) => [source, issued]),
  ),
)(
  '%s budget refusal issued=%s neither retries nor counts against its actual API breaker',
  async (source, issued) => {
    runner.mockRejectedValue(failure('walk_budget_exhausted', issued));
    for (let i = 0; i < 4; i++) {
      await expect(acquire(source)).rejects.toMatchObject({
        context: { details: { reason: 'walk_budget_exhausted', request_issued: issued } },
      });
    }
    expect(runner).toHaveBeenCalledTimes(4); // no replay, even with three retries configured
    expect(breaker.getState()).toBe('CLOSED');
    expect(breaker.getFailureCount()).toBe(0);
    runner.mockResolvedValue(reply({ transfer_skipped: false, path: '/fixture/book.pdf' }));
    await expect(acquire(source)).resolves.toMatchObject({ path: '/fixture/book.pdf' });
    expect(runner).toHaveBeenCalledTimes(5);
  },
);

test('control: real provider failures still count and open the same API breaker', async () => {
  process.env.RETRY_MAX_RETRIES = '0';
  runner.mockRejectedValue(failure('read_timeout', true));
  for (let i = 0; i < 2; i++) await expect(acquire('libgen')).rejects.toThrow('fixture refusal');
  expect(breaker.getState()).toBe('OPEN');
  expect(breaker.getFailureCount()).toBe(2);
});
