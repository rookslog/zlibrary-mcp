import './helpers/environment-login.js';
import { afterEach, expect, jest, test } from '@jest/globals';

// Breaker accounting (#267, #272, #273). Keep API, owner, retry and breaker real;
// only interpreter/process I/O is fake, as in lazy-dependency-breaker.test.js.
let api, errors, runner, breaker, saved;
let loginFailure, operationFailure, receipt, demands, transfers, gates, transferGate;
let holds, validateFailure, foreign;
const refused = { context: { details: { reason: 'breaker_open', request_issued: false } } };
const tick = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
/** Hold the next login child until released, so concurrent callers all join it. */
const holdLogin = () => {
  let release;
  gates.push(new Promise((resolve) => (release = resolve)));
  return release;
};
/** Hold every child of one kind (validate, acquire, search) until released. */
const hold = (kind) => {
  let release;
  holds[kind] = new Promise((resolve) => (release = resolve));
  return () => {
    delete holds[kind];
    release();
  };
};
const foreignRefusal = () =>
  Object.assign(new Error('bridge exit'), {
    stderr: JSON.stringify({
      error: 'Destination has a verified receipt or intent for another selection',
      type: 'AcquisitionError',
      details: { reason: 'output_refused', request_issued: false },
    }),
  });
const envelope = (data) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(data) }] }),
];
const outage = () =>
  envelope({ error: 'offline outage', type: 'HTTPStatusError', status_code: 503 });
const sourceOutage = () =>
  Object.assign(new Error('bridge exit'), {
    stderr: JSON.stringify({
      error: 'offline source outage',
      type: 'SourceError',
      details: { provider: 'libgen', reason: 'read_timeout' },
    }),
  });
const acquire = (source = 'zlibrary', options = {}) =>
  api.invokeAcquisitionBridge(
    'acquire_book',
    {
      book_details: { source, id: '101', book_hash: 'hash101', md5: 'a'.repeat(32) },
      output_dir: '/offline',
    },
    options,
  );
const health = () => ({ state: breaker.getState(), failures: breaker.getFailureCount() });
const bridgeArgs = (call) => JSON.parse(call[1].args[1]);
const children = () =>
  runner.mock.calls
    .filter((call) => call[1].args[0] === 'acquire_book' && !call[2].sessionInput)
    .map((call) => ({ receipt_only: bridgeArgs(call).receipt_only === true }));

async function load(env = {}) {
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
    ...env,
  });
  jest.resetModules();
  loginFailure = undefined;
  operationFailure = false;
  receipt = false;
  demands = transfers = 0;
  gates = [];
  transferGate = undefined;
  holds = {};
  validateFailure = undefined;
  foreign = false;
  errors = await import('../lib/errors.js');
  const { CircuitBreaker } = await import('../lib/circuit-breaker.js');
  const execute = CircuitBreaker.prototype.execute;
  jest.spyOn(CircuitBreaker.prototype, 'execute').mockImplementation(function (...args) {
    breaker = this;
    return execute.apply(this, args);
  });
  const { EapiSessionOwner } = await import('../lib/eapi-session.js');
  const ownerRun = jest.spyOn(EapiSessionOwner.prototype, 'run');
  const run = ownerRun.getMockImplementation();
  ownerRun.mockImplementation(function (...args) {
    demands++;
    return run.apply(this, args);
  });
  runner = jest.fn(async (_script, shell, options) => {
    const input = options.sessionInput;
    if (input?.action === 'login') {
      if (gates.length) await gates.shift();
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
      if (holds.validate) await holds.validate;
      return validateFailure ?? envelope({ success: 1 });
    }
    const [name, raw] = shell.args;
    if (name === 'acquire_book') {
      // Python reads the receipt first in every mode; a hit never transfers.
      if (receipt) return envelope({ transfer_skipped: true, path: '/offline/book.pdf' });
      if (JSON.parse(raw).receipt_only) {
        if (foreign) throw foreignRefusal();
        return envelope({ receipt_hit: false });
      }
    }
    if (options.onSessionNeeded) {
      return new Promise((resolve, reject) => {
        options.onSessionNeeded({
          grant: () => {
            transfers++;
            const settle = () =>
              resolve(operationFailure ? outage() : envelope({ transfer_skipped: false }));
            if (transferGate) void transferGate.then(settle);
            else settle();
          },
          deny: reject,
        });
      });
    }
    if (name === 'acquire_book') {
      transfers++;
      if (holds.acquire) await holds.acquire;
    }
    if (name === 'search_multi_source' && holds.search) await holds.search;
    if (name === 'search_multi_source' && operationFailure) throw sourceOutage();
    if (operationFailure) return outage();
    return envelope(name === 'acquire_book' ? { transfer_skipped: false } : { daily_limit: 10 });
  });
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/offline/python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  api = await import('../lib/zlibrary-api.js');
}
afterEach(() => {
  jest.restoreAllMocks();
  if (saved) process.env = saved;
  saved = undefined;
});

async function open(scope) {
  operationFailure = true;
  for (let i = 0; i < 3; i++)
    await expect(
      scope === 'eapi'
        ? api.getDownloadLimits()
        : api.searchMultiSource({ query: 'x', source: scope }),
    ).rejects.toThrow(/offline/);
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  operationFailure = false;
  runner.mockClear();
  demands = transfers = 0;
}

// ---- #267: receipt hits on non-lazy acquisition paths -----------------------

const nonLazy = [
  ['libgen', 'libgen', {}],
  ['annas', 'annas', {}],
  ['zlibrary', 'eapi', { ZLIBRARY_SESSION_MODE: 'per-call' }],
];

test.each(nonLazy)(
  'open breaker serves a verified %s receipt from a receipt-only child without changing health',
  async (source, scope, env) => {
    await load(env);
    await open(scope);
    receipt = true;
    await expect(acquire(source)).resolves.toEqual({
      transfer_skipped: true,
      path: '/offline/book.pdf',
    });
    expect(children()).toEqual([{ receipt_only: true }]);
    expect(transfers).toBe(0);
    expect(demands).toBe(0);
    expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  },
);

test.each(nonLazy)(
  'open breaker still refuses a %s non-hit before any transfer child',
  async (source, scope, env) => {
    await load(env);
    await open(scope);
    await expect(acquire(source)).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN', ...refused });
    expect(children()).toEqual([{ receipt_only: true }]);
    expect(transfers).toBe(0);
    expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  },
);

test('a receipt-only child that fails is still the breaker refusal, never its own error', async () => {
  await load();
  await open('libgen');
  runner.mockImplementationOnce(async () => {
    throw new errors.BridgeTimeoutError('offline receipt check stopped', { reason: 'timeout' });
  });
  await expect(acquire('libgen')).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
});

test('a non-lazy receipt hit neither heals failures nor probes an expired window', async () => {
  await load();
  operationFailure = true;
  for (let i = 1; i <= 2; i++) {
    await expect(api.searchMultiSource({ query: 'x', source: 'libgen' })).rejects.toThrow();
    receipt = true;
    await expect(acquire('libgen')).resolves.toMatchObject({ transfer_skipped: true });
    receipt = false;
    expect(health()).toEqual({ state: 'CLOSED', failures: i });
  }
  await expect(api.searchMultiSource({ query: 'x', source: 'libgen' })).rejects.toThrow();
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  receipt = true;
  await expect(acquire('libgen')).resolves.toMatchObject({ transfer_skipped: true });
  expect(health()).toEqual({ state: 'OPEN', failures: 3 });
  expect(transfers).toBe(0);
});

test('a CLOSED non-lazy acquisition runs one ordinary child and no receipt-only pre-check', async () => {
  await load();
  await expect(acquire('libgen')).resolves.toEqual({ transfer_skipped: false });
  expect(children()).toEqual([{ receipt_only: false }]);
  expect(transfers).toBe(1);
});

// ---- #272: one shared owner login failure counts once ------------------------

test.each([2, 5])(
  '%i concurrent lazy acquisitions sharing one failed login count it once',
  async (count) => {
    await load();
    loginFailure = new errors.BridgeTimeoutError('offline login timeout', { reason: 'timeout' });
    const release = holdLogin();
    const pending = Array.from({ length: count }, () => acquire().catch((error) => error));
    await tick();
    release();
    const failures = await Promise.all(pending);
    expect(failures.every((error) => error instanceof errors.BridgeTimeoutError)).toBe(true);
    expect(failures.map((error) => error.context.details.request_issued)).toEqual(
      Array(count).fill(false),
    );
    expect(runner.mock.calls.filter((call) => call[2].sessionInput?.action === 'login')).toHaveLength(1);
    expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
    expect(transfers).toBe(0);
  },
);

test('a shared login led by a non-lazy call counts once, not again for a lazy waiter', async () => {
  await load();
  loginFailure = new errors.BridgeTimeoutError('offline login timeout', { reason: 'timeout' });
  const release = holdLogin();
  const pending = [api.getDownloadLimits().catch((e) => e), acquire().catch((e) => e)];
  await tick();
  release();
  const [limits, lazy] = await Promise.all(pending);
  expect(limits).toBeInstanceOf(errors.BridgeTimeoutError);
  expect(lazy).toBeInstanceOf(errors.BridgeTimeoutError);
  expect(runner.mock.calls.filter((call) => call[2].sessionInput?.action === 'login')).toHaveLength(1);
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
});

test('a lazy waiter still counts its own transfer outage after a shared login succeeds', async () => {
  await load();
  operationFailure = true;
  const failures = await Promise.all([acquire().catch((e) => e), acquire().catch((e) => e)]);
  expect(failures.map((error) => error.message)).toEqual([
    expect.stringContaining('offline outage'),
    expect.stringContaining('offline outage'),
  ]);
  expect(transfers).toBe(2);
  expect(health()).toEqual({ state: 'CLOSED', failures: 2 });
});

// ---- #273: HALF_OPEN admits exactly one probe --------------------------------

test('HALF_OPEN admits one probe; concurrent callers are refused fast; success closes', async () => {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ threshold: 1, timeout: 10 });
  await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  await new Promise((resolve) => setTimeout(resolve, 20));
  let finish;
  const probe = unit.execute(() => new Promise((resolve) => (finish = resolve)));
  expect(unit.getState()).toBe('HALF_OPEN');
  const second = jest.fn(async () => 'must not run');
  const refusal = await unit.execute(second).catch((error) => error);
  expect(refusal).toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
    context: { details: { reason: 'breaker_open', request_issued: false } },
  });
  expect(second).not.toHaveBeenCalled();
  finish('ok');
  await expect(probe).resolves.toBe('ok');
  expect(unit.getState()).toBe('CLOSED');
  await expect(unit.execute(second)).resolves.toBe('must not run');
});

test('a failed HALF_OPEN probe re-opens for a fresh cooldown', async () => {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ threshold: 1, timeout: 1000 });
  await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  const now = Date.now();
  jest.spyOn(Date, 'now').mockReturnValue(now + 1001);
  let fail;
  const probe = unit.execute(() => new Promise((_resolve, reject) => (fail = reject)));
  await expect(unit.execute(async () => 'refused')).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  fail(new Error('still down'));
  await expect(probe).rejects.toThrow('still down');
  expect(unit.getState()).toBe('OPEN');
  const refusal = await unit.execute(async () => 'refused').catch((error) => error);
  expect(refusal.context.retryAfterMs).toBe(1000);
});

test.each([
  [
    'an uncounted rejection',
    { isFailure: (error) => error.message !== 'aborted' },
    () => Promise.reject(new Error('aborted')),
  ],
  ['a neutral result', { isNeutral: (r) => r === 'receipt' }, async () => 'receipt'],
])('a HALF_OPEN probe with %s frees the slot and leaves the breaker OPEN', async (_kind, options, op) => {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ threshold: 1, timeout: 10, ...options });
  await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  const failures = unit.getFailureCount();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await unit.execute(op).catch(() => {});
  expect(unit.getState()).toBe('OPEN');
  expect(unit.getFailureCount()).toBe(failures);
  await expect(unit.execute(async () => 'next probe')).resolves.toBe('next probe');
  expect(unit.getState()).toBe('CLOSED');
});

test('reset during a HALF_OPEN probe frees the probe slot', async () => {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ threshold: 1, timeout: 10 });
  await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  await new Promise((resolve) => setTimeout(resolve, 20));
  let finish;
  const probe = unit.execute(() => new Promise((resolve) => (finish = resolve)));
  unit.reset();
  await expect(unit.execute(async () => 'admitted')).resolves.toBe('admitted');
  finish('late');
  await expect(probe).resolves.toBe('late');
  expect(unit.getState()).toBe('CLOSED');
});

test('two lazy non-hits after the OPEN window: one probe demand and transfer, the other refused', async () => {
  await load();
  await open('eapi');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  let release;
  transferGate = new Promise((resolve) => (release = resolve));
  const pending = [acquire().catch((e) => e), acquire().catch((e) => e)];
  await tick();
  release();
  const settled = await Promise.all(pending);
  expect(settled.filter((r) => r?.transfer_skipped === false)).toHaveLength(1);
  const refusals = settled.filter((r) => r?.context?.details?.reason === 'breaker_open');
  expect(refusals).toHaveLength(1);
  expect(refusals[0]).toMatchObject(refused);
  expect(demands).toBe(1);
  expect(transfers).toBe(1);
  expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
});

// Round 2 (lead decision): the grant settles the probe, so a transfer failure
// after it counts once as a CLOSED call instead of re-opening.
test('a lazy HALF_OPEN probe whose transfer fails after the grant counts once, CLOSED', async () => {
  await load();
  await open('eapi');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  operationFailure = true;
  await expect(acquire()).rejects.toThrow('offline outage');
  expect(transfers).toBe(1);
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
  operationFailure = false;
  await expect(acquire()).resolves.toEqual({ transfer_skipped: false });
  expect(transfers).toBe(2);
});

// ---- Round 2: bound the HALF_OPEN refusal window ------------------------------

async function brokenUnit(options) {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ timeout: 10, ...options });
  for (let i = 0; i < (options.threshold ?? 1); i++)
    await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  expect(unit.getState()).toBe('OPEN');
  await tick();
  return unit;
}

test('a probe that confirms health closes at once; its later failure counts once as CLOSED', async () => {
  const unit = await brokenUnit({ threshold: 2 });
  let fail;
  const probe = unit.execute(
    (admission) =>
      new Promise((_resolve, reject) => {
        fail = reject;
        admission.confirm();
      }),
  );
  expect(unit.getState()).toBe('CLOSED');
  expect(unit.getFailureCount()).toBe(0);
  await expect(unit.execute(async () => 'admitted')).resolves.toBe('admitted');
  fail(new Error('transfer down'));
  await expect(probe).rejects.toThrow('transfer down');
  expect({ state: unit.getState(), failures: unit.getFailureCount() }).toEqual({
    state: 'CLOSED',
    failures: 1,
  });
});

test('confirm outside a probe changes nothing', async () => {
  const { CircuitBreaker } = await import('../dist/lib/circuit-breaker.js');
  const unit = new CircuitBreaker({ threshold: 3 });
  await expect(unit.execute(async () => Promise.reject(new Error('down')))).rejects.toThrow();
  await expect(
    unit.execute(async (admission) => {
      admission.confirm();
      throw new Error('down again');
    }),
  ).rejects.toThrow('down again');
  expect(unit.getFailureCount()).toBe(2);
});

test('an expired probe hold admits others; the probe still re-opens on failure', async () => {
  const unit = await brokenUnit({ threshold: 1 });
  let fail;
  const probe = unit.execute((admission) => {
    admission.holdFor(30);
    return new Promise((_resolve, reject) => (fail = reject));
  });
  await expect(unit.execute(async () => 'early')).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  await tick(60);
  expect(unit.getState()).toBe('HALF_OPEN');
  await expect(unit.execute(async () => 'after hold')).resolves.toBe('after hold');
  fail(new Error('probe down'));
  await expect(probe).rejects.toThrow('probe down');
  expect(unit.getState()).toBe('OPEN');
});

test('a lazy probe refuses others until its grant, then they are admitted', async () => {
  await load({ ZLIBRARY_SESSION_IDLE_MS: '60000' });
  await open('eapi');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  const releaseValidate = hold('validate');
  let releaseTransfer;
  transferGate = new Promise((resolve) => (releaseTransfer = resolve));
  const probe = acquire().catch((e) => e);
  await tick();
  // Before the grant: the probe is validating, everyone else is refused fast.
  await expect(api.getDownloadLimits()).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
  expect(transfers).toBe(0);
  releaseValidate();
  await tick();
  // At the grant the provider has answered: CLOSED while the transfer still runs.
  expect(transfers).toBe(1);
  expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
  await expect(api.getDownloadLimits()).resolves.toEqual({ daily_limit: 10 });
  operationFailure = true;
  releaseTransfer();
  await expect(probe).resolves.toMatchObject({ message: expect.stringContaining('offline outage') });
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
});

test('a lazy probe that fails before its grant re-opens without a transfer', async () => {
  await load({ ZLIBRARY_SESSION_IDLE_MS: '60000' });
  await open('eapi');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  validateFailure = outage();
  await expect(acquire()).rejects.toThrow('offline outage');
  expect(transfers).toBe(0);
  expect(breaker.getState()).toBe('OPEN');
  await expect(acquire()).rejects.toMatchObject(refused);
});

test('a lazy probe stuck before its grant holds HALF_OPEN only for the normal budget', async () => {
  await load({ ZLIBRARY_SESSION_IDLE_MS: '60000', PYTHON_BRIDGE_TIMEOUT: '50' });
  await open('eapi');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  const releaseValidate = hold('validate');
  const probe = acquire().catch((e) => e);
  await tick();
  expect(demands).toBe(1);
  await expect(acquire()).rejects.toMatchObject(refused);
  expect(demands).toBe(1);
  await tick(60);
  const admitted = acquire().catch((e) => e);
  await tick();
  expect(demands).toBe(2);
  releaseValidate();
  await expect(probe).resolves.toEqual({ transfer_skipped: false });
  await expect(admitted).resolves.toEqual({ transfer_skipped: false });
  expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
});

test('a non-lazy long probe refuses others only until its hold expires', async () => {
  await load({ PYTHON_BRIDGE_TIMEOUT: '50' });
  await open('libgen');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  const release = hold('acquire');
  const probe = acquire('libgen');
  await tick(5);
  await expect(acquire('libgen')).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
  expect(transfers).toBe(1);
  await tick(60);
  const admitted = acquire('libgen');
  await tick(5);
  expect(transfers).toBe(2);
  release();
  await expect(probe).resolves.toEqual({ transfer_skipped: false });
  await expect(admitted).resolves.toEqual({ transfer_skipped: false });
  expect(health()).toEqual({ state: 'CLOSED', failures: 0 });
});

test('a non-lazy long probe that fails after its hold expired still re-opens', async () => {
  await load({ PYTHON_BRIDGE_TIMEOUT: '50' });
  await open('libgen');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  const release = hold('acquire');
  const probe = acquire('libgen').catch((e) => e);
  await tick(60);
  operationFailure = true;
  release();
  await expect(probe).resolves.toMatchObject({ message: expect.stringContaining('offline outage') });
  expect(breaker.getState()).toBe('OPEN');
});

test('a short non-lazy probe releases the one-probe hold after the normal budget', async () => {
  await load({ PYTHON_BRIDGE_TIMEOUT: '50' });
  await open('libgen');
  jest.spyOn(Date, 'now').mockReturnValue(Date.now() + 60001);
  const release = hold('search');
  const probe = api.searchMultiSource({ query: 'x', source: 'libgen' });
  await expect(api.searchMultiSource({ query: 'x', source: 'libgen' })).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  await tick(60);
  const admitted = api.searchMultiSource({ query: 'x', source: 'libgen' });
  release();
  await expect(probe).resolves.toEqual({ daily_limit: 10 });
  await expect(admitted).resolves.toEqual({ daily_limit: 10 });
  expect(breaker.getState()).toBe('CLOSED');
});

// Accepted as is (round 2): the lazy path answers output_refused here, because
// its child reads the destination before admission. Change this deliberately.
test('an open breaker answers a non-lazy foreign destination with breaker_open', async () => {
  await load();
  await open('libgen');
  foreign = true;
  await expect(acquire('libgen')).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
    ...refused,
  });
  expect(children()).toEqual([{ receipt_only: true }]);
  expect(transfers).toBe(0);
});

test('a lazy waiter that takes over an aborted leader login counts its own login once', async () => {
  await load();
  loginFailure = new errors.BridgeTimeoutError('offline login timeout', { reason: 'timeout' });
  const releaseFirst = holdLogin();
  const controller = new AbortController();
  const leader = acquire('zlibrary', { signal: controller.signal }).catch((e) => e);
  await tick();
  const waiter = acquire().catch((e) => e);
  await tick();
  controller.abort();
  await expect(waiter).resolves.toBeInstanceOf(errors.BridgeTimeoutError);
  const logins = runner.mock.calls.filter((call) => call[2].sessionInput?.action === 'login');
  expect(logins).toHaveLength(2);
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
  releaseFirst();
  await expect(leader).resolves.toMatchObject({ context: { reason: 'aborted' } });
  expect(health()).toEqual({ state: 'CLOSED', failures: 1 });
});
