import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
const runPythonBridge = jest.fn();
let api, originalEnv, consoleSpy, now, clockSpy, ownerModule;
const identity = 'memory-reader@example.test';
const token = 'fake-private-cookie-marker';
const session = (generation, key = token, domain = 'memory.example.test') => ({
  identity,
  generation,
  domain,
  remix_userid: '7',
  remix_userkey: key,
});
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
const rejectReply = (generation) =>
  envelope({
    error: 'Authentication rejected: Expired #1',
    type: 'EAPIAuthenticationError',
    auth_rejected: { generation },
  });

beforeEach(async () => {
  originalEnv = { ...process.env };
  process.env.ZLIBRARY_EMAIL = identity;
  process.env.ZLIBRARY_PASSWORD = 'fake-password-marker';
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.RETRY_MAX_RETRIES = '0';
  delete process.env.ZLIBRARY_SESSION_LOGIN_COOLDOWN_MS;
  delete process.env.ZLIBRARY_SESSION_IDLE_MS;
  consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  now = 1000000;
  clockSpy = jest.spyOn(Date, 'now').mockImplementation(() => now);
  jest.resetModules();
  runPythonBridge.mockReset();
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/synthetic/python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  api = await import('../lib/zlibrary-api.js');
  ownerModule = await import('../lib/eapi-session.js');
});
afterEach(() => {
  process.env = originalEnv;
  clockSpy.mockRestore();
  consoleSpy.mockRestore();
});
const call = (options) => api.getDownloadHistory({ count: 1 }, options);
function fakeBackend() {
  const counts = { login: 0, operation: 0, validate: 0 };
  runPythonBridge.mockImplementation(async (_script, options, runOptions) => {
    const input = runOptions.sessionInput;
    expect(options.args.join(' ')).not.toContain(token);
    expect(options.env?.ZLIBRARY_EAPI_SESSION).toBeUndefined();
    if (!input) return envelope([]);
    counts[input.action]++;
    if (input.action === 'login') {
      await new Promise((resolve) => setTimeout(resolve, 10));
      runOptions.onSession(session(input.generation));
      return envelope({ authenticated: true });
    }
    expect(input.remix_userkey).toBe(token);
    return envelope(input.action === 'validate' ? { authenticated: true } : []);
  });
  return counts;
}

test('six cold calls single-flight one login through a private channel', async () => {
  const counts = fakeBackend();
  expect(await Promise.all(Array.from({ length: 6 }, () => call()))).toEqual([
    [],
    [],
    [],
    [],
    [],
    [],
  ]);
  expect(counts).toEqual({ login: 1, operation: 6, validate: 0 });
  expect(JSON.stringify(consoleSpy.mock.calls)).not.toContain(token);
});

test('operation rejection makes Node refresh once on a different domain', async () => {
  const counts = fakeBackend();
  await call();
  now += 180001;
  runPythonBridge.mockImplementation(async (_script, _options, ro) => {
    const input = ro.sessionInput;
    if (input.action === 'login') {
      counts.login++;
      ro.onSession(session(input.generation, 'fresh-cookie', 'new.example.test'));
      return envelope({ authenticated: true });
    }
    if (input.remix_userkey === token) return rejectReply(input.generation);
    expect(input.domain).toBe('new.example.test');
    expect(input.authRetry).toBe(true);
    return envelope([{ id: 'fresh-result' }]);
  });
  await expect(call()).resolves.toEqual([{ id: 'fresh-result' }]);
  expect(counts.login).toBe(2);
});

test('repeated rejection has no third operation or second refresh', async () => {
  fakeBackend();
  await call();
  now += 180001;
  const actions = [];
  runPythonBridge.mockImplementation(async (_script, _options, ro) => {
    const input = ro.sessionInput;
    actions.push(input.action);
    if (input.action === 'login') {
      ro.onSession(session(input.generation, 'fresh-cookie'));
      return envelope({ authenticated: true });
    }
    return rejectReply(input.generation);
  });
  await expect(call()).rejects.toThrow(/rejected/i);
  expect(actions).toEqual(['operation', 'login', 'operation']);
});

test('two owners get one immediate refresh each then bounded ping-pong backoff', async () => {
  const first = new ownerModule.EapiSessionOwner(),
    second = new ownerModule.EapiSessionOwner();
  let current = '',
    logins = 0;
  const backend = (owner) => async (input) => {
    if (input.action === 'login') {
      current = 'cookie-' + ++logins;
      owner.consume(session(input.generation, current), identity, input.generation);
      return { authenticated: true };
    }
    if (input.remix_userkey !== current)
      throw ownerModule.authRejected(input.generation, 'Expired #1');
    return 'ok';
  };
  await first.run(identity, backend(first));
  await second.run(identity, backend(second));
  await expect(first.run(identity, backend(first))).resolves.toBe('ok');
  await expect(second.run(identity, backend(second))).resolves.toBe('ok');
  expect(logins).toBe(4);
  await expect(first.run(identity, backend(first))).rejects.toThrow(/30 s/);
  for (let n = 0; n < 4; n++)
    await expect(first.run(identity, backend(first))).rejects.toMatchObject({
      context: { reason: 'auth_backoff' },
    });
  expect(logins).toBe(4);
  await expect(second.run(identity, backend(second))).resolves.toBe('ok');
  now += 30001;
  await expect(first.run(identity, backend(first))).resolves.toBe('ok');
  await expect(second.run(identity, backend(second))).resolves.toBe('ok');
  await expect(first.run(identity, backend(first))).rejects.toThrow(/60 s/);
  expect(logins).toBe(6);
});

test('stale generation cannot overwrite or clear a newer session', () => {
  const owner = new ownerModule.EapiSessionOwner();
  const generation = owner.generationFor(identity);
  owner.consume(session(generation), identity, generation);
  const latest = owner.generationFor(identity);
  owner.consume(session(generation, 'stale'), identity, generation);
  owner.reject(identity, generation);
  expect(owner.get(identity).remix_userkey).toBe(token);
  expect(owner.generationFor(identity)).toBe(latest);
});

test('idle validation runs after the gap, not on each warm call', async () => {
  const counts = fakeBackend();
  await call();
  await call();
  expect(counts.validate).toBe(0);
  now += 300001;
  await call();
  expect(counts.validate).toBe(1);
  expect(counts.login).toBe(1);
});

test('identity change never sends a prior identity session', async () => {
  fakeBackend();
  await call();
  process.env.ZLIBRARY_EMAIL = 'other@example.test';
  let seenLogin = false;
  runPythonBridge.mockImplementation(async (_script, _opts, ro) => {
    const input = ro.sessionInput;
    if (input.action === 'login') {
      expect(input.remix_userkey).toBeUndefined();
      seenLogin = true;
      ro.onSession({
        ...session(input.generation, 'other-cookie'),
        identity: 'other@example.test',
      });
    } else expect(input.remix_userkey).toBe('other-cookie');
    return envelope([]);
  });
  await call();
  expect(seenLogin).toBe(true);
});

test('per-call mode and non-EAPI work have no session channel', async () => {
  fakeBackend();
  await call();
  runPythonBridge.mockImplementation(async (_s, options, ro) => {
    expect(ro.sessionInput).toBeUndefined();
    expect(options.env.ZLIBRARY_EAPI_SESSION).toBeUndefined();
    return envelope({ books: [], sources_used: [] });
  });
  await api.searchMultiSource({ query: 'offline', source: 'libgen' });
  process.env.ZLIBRARY_SESSION_MODE = 'per-call';
  await call();
  await call();
});

test.each(['', 'off', 'disk', 'false', 'unexpected'])('unknown mode %s fails loudly', (mode) => {
  process.env.ZLIBRARY_SESSION_MODE = mode;
  expect(() => ownerModule.getSessionMode()).toThrow(/ZLIBRARY_SESSION_MODE/);
});

test('queued waiter has its own deadline and never spawns', async () => {
  let release;
  runPythonBridge.mockImplementationOnce(
    (_s, _opts, ro) =>
      new Promise((resolve) => {
        release = () => {
          ro.onSession(session(ro.sessionInput.generation));
          resolve(envelope({ authenticated: true }));
        };
      }),
  );
  const first = call();
  while (!release) await new Promise((resolve) => setTimeout(resolve, 1));
  await expect(call({ timeoutMs: 10 })).rejects.toThrow(/deadline|budget|timeout/i);
  release();
  runPythonBridge.mockResolvedValue(envelope([]));
  await first;
  expect(runPythonBridge).toHaveBeenCalledTimes(2);
});

test('failed acquisition rejects its waiters and can lead again after backoff', async () => {
  process.env.ZLIBRARY_SESSION_LOGIN_COOLDOWN_MS = '1';
  let attempts = 0;
  runPythonBridge.mockImplementation(async (_s, _opts, ro) => {
    if (ro.sessionInput.action === 'login') {
      attempts++;
      if (attempts === 1) {
        now += 2;
        throw new Error('leader died');
      }
      ro.onSession(session(ro.sessionInput.generation));
    }
    return envelope([]);
  });
  const results = await Promise.allSettled([call(), call()]);
  expect(results.map((r) => r.status)).toEqual(['rejected', 'rejected']);
  expect(attempts).toBe(1);
  now += 1;
  await expect(call()).resolves.toEqual([]);
  expect(attempts).toBe(2);
});

test.each([false, true])(
  'configuration_error request_issued=%s controls login cooldown',
  async (issued) => {
    const { PythonBridgeError } = await import('../lib/errors.js');
    const owner = new ownerModule.EapiSessionOwner();
    let attempts = 0;
    const failure = new PythonBridgeError(
      'Missing source configuration',
      {
        pythonErrorType: 'AcquisitionError',
        details: { reason: 'configuration_error', request_issued: issued },
      },
      false,
    );
    const backend = async (input) => {
      if (input.action === 'login') {
        if (++attempts === 1) throw failure;
        owner.consume(session(input.generation), identity, input.generation);
      }
      return 'ok';
    };
    await expect(owner.run(identity, backend)).rejects.toBe(failure);
    if (issued) {
      await expect(owner.run(identity, backend)).rejects.toMatchObject({
        context: { reason: 'auth_backoff', retryAfterMs: 30000 },
      });
      expect(attempts).toBe(1);
      now += 30000;
    }
    await expect(owner.run(identity, backend)).resolves.toBe('ok');
    expect(attempts).toBe(2);
  },
);

test('private marker never appears in malformed stdout errors or logs', async () => {
  fakeBackend();
  await call();
  runPythonBridge.mockResolvedValue(['not JSON ' + token]);
  const caught = await call().catch((error) => error);
  expect(JSON.stringify(caught) + JSON.stringify(consoleSpy.mock.calls)).not.toContain(token);
});

test('a queued deadline cannot release the active leader to a later caller', async () => {
  const owner = new ownerModule.EapiSessionOwner();
  let release,
    operations = 0;
  const backend = async (input) => {
    if (input.action === 'login') {
      await new Promise((resolve) => {
        release = resolve;
      });
      owner.consume(session(input.generation), identity, input.generation);
    } else operations++;
    return 'ok';
  };
  const first = owner.run(identity, backend);
  while (!release) await new Promise((resolve) => setTimeout(resolve, 1));
  await expect(owner.run(identity, backend, { timeoutMs: 10 })).rejects.toThrow(/deadline/i);
  const third = owner.run(identity, backend);
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(operations).toBe(0);
  release();
  await first;
  await third;
  expect(operations).toBe(2);
});

test('timed-out leader releases flight and late acquisition is fenced', async () => {
  process.env.ZLIBRARY_SESSION_LOGIN_COOLDOWN_MS = '1';
  const owner = new ownerModule.EapiSessionOwner();
  let late,
    attempts = 0;
  const backend = async (input) => {
    if (input.action === 'login') {
      attempts++;
      if (attempts === 1) {
        await new Promise((resolve) => {
          late = () => {
            owner.consume(session(input.generation, 'late-cookie'), identity, input.generation);
            resolve();
          };
        });
      } else owner.consume(session(input.generation, 'current-cookie'), identity, input.generation);
    }
    return 'ok';
  };
  const first = owner.run(identity, backend, { timeoutMs: 10 });
  await expect(first).rejects.toThrow(/deadline/i);
  now += 2;
  await expect(owner.run(identity, backend)).resolves.toBe('ok');
  late();
  await new Promise((resolve) => setTimeout(resolve, 1));
  expect(owner.get(identity).remix_userkey).toBe('current-cookie');
  expect(attempts).toBe(2);
});

test('a queued caller aborts without spawning a child', async () => {
  const owner = new ownerModule.EapiSessionOwner();
  let release,
    attempts = 0;
  const backend = async (input) => {
    attempts++;
    if (input.action === 'login') {
      await new Promise((resolve) => {
        release = resolve;
      });
      owner.consume(session(input.generation), identity, input.generation);
    }
    return 'ok';
  };
  const first = owner.run(identity, backend);
  while (!release) await new Promise((resolve) => setTimeout(resolve, 1));
  const controller = new AbortController();
  const queued = owner.run(identity, backend, { signal: controller.signal });
  controller.abort();
  await expect(queued).rejects.toThrow(/aborted/i);
  release();
  await first;
  expect(attempts).toBe(2);
});

test('unset mode defaults to memory', () => {
  delete process.env.ZLIBRARY_SESSION_MODE;
  expect(ownerModule.getSessionMode()).toBe('memory');
});

test.each(['timeout', 'HTTP 513 DiamWall', 'HTTP 503', 'HTML HTTP 200'])(
  'operation uncertainty %s retains the owner session without login',
  async (message) => {
    const counts = fakeBackend();
    await call();
    runPythonBridge.mockRejectedValueOnce(new Error(message));
    await expect(call()).rejects.toThrow(message);
    fakeBackend();
    await call();
    expect(counts.login).toBe(1);
    expect(
      runPythonBridge.mock.calls.filter((args) => args[2].sessionInput.action === 'login'),
    ).toHaveLength(1);
  },
);

test('intentional auth retry bypasses retry multiplication and breaker accounting', async () => {
  process.env.RETRY_MAX_RETRIES = '3';
  process.env.CIRCUIT_BREAKER_THRESHOLD = '1';
  // Re-import so the threshold applies to this server owner.
  jest.resetModules();
  api = await import('../lib/zlibrary-api.js');
  const { PythonBridgeError } = await import('../lib/errors.js');
  fakeBackend();
  await call();
  now += 180001;
  const actions = [];
  runPythonBridge.mockImplementation(async (_script, _options, ro) => {
    const input = ro.sessionInput;
    actions.push(input.action);
    if (input.action === 'login') {
      ro.onSession(session(input.generation, 'new-cookie'));
      return envelope({ authenticated: true });
    }
    if (input.remix_userkey === token) return rejectReply(input.generation);
    throw new PythonBridgeError('network retry must not multiply this operation', {
      pythonErrorType: 'HTTPStatusError',
      httpStatus: 503,
    });
  });
  await expect(call()).rejects.toThrow(/network/);
  expect(actions).toEqual(['operation', 'login', 'operation']);
  const callsBefore = runPythonBridge.mock.calls.length;
  runPythonBridge.mockResolvedValue(envelope([]));
  await expect(call()).resolves.toEqual([]);
  expect(runPythonBridge).toHaveBeenCalledTimes(callsBefore + 1);
});

test.each([
  [JSON.stringify({ unexpected: token })],
  [JSON.stringify({ content: [{ type: 'text', text: 'invalid ' + token }] })],
])('malformed public envelopes cannot echo a known private marker', async (...lines) => {
  fakeBackend();
  await call();
  runPythonBridge.mockResolvedValue(lines);
  const error = await call().catch((error) => error);
  expect(error).toBeInstanceOf(Error);
  expect(
    String(error.stack) + JSON.stringify(error) + JSON.stringify(consoleSpy.mock.calls),
  ).not.toContain(token);
});

test('private business-failure metadata forces validation after normalized results', async () => {
  const counts = fakeBackend();
  await call();
  let operations = 0,
    profiles = 0;
  runPythonBridge.mockImplementation(async (_s, _opts, ro) => {
    const input = ro.sessionInput;
    if (input.action === 'validate') {
      profiles++;
      return envelope({ authenticated: true });
    }
    operations++;
    if (operations === 1) ro.onSession({ ...session(input.generation), validate_next: true });
    return envelope([]);
  });
  await call();
  await call();
  expect(profiles).toBe(1);
  expect(counts.login).toBe(1);
});

test('a rejected recovered-domain update cannot hide the input generation rejection', async () => {
  const counts = fakeBackend();
  await call();
  runPythonBridge.mockImplementation(async (_s, _opts, ro) => {
    const input = ro.sessionInput;
    if (input.action === 'login') {
      counts.login++;
      ro.onSession(session(input.generation, 'new-cookie', 'fresh.example.test'));
      return envelope({ authenticated: true });
    }
    if (input.remix_userkey === token) {
      ro.onSession(session(input.generation, token, 'recovered.example.test'));
      return rejectReply(input.generation);
    }
    expect(input.remix_userkey).toBe('new-cookie');
    return envelope(['fresh']);
  });
  await expect(call()).resolves.toEqual(['fresh']);
  expect(counts.login).toBe(2);
});

test('short password cannot corrupt authentication reason codes or backoff', async () => {
  process.env.ZLIBRARY_PASSWORD = 'a';
  runPythonBridge.mockResolvedValue(
    envelope({
      error: 'Z-Library rejected the login (HTTP 401)',
      login_failed: { status_code: 401, token: 'Invalid credentials' },
    }),
  );
  await expect(call()).rejects.toMatchObject({ context: { reason: 'login_failed' } });
  await expect(call()).rejects.toMatchObject({ context: { reason: 'auth_backoff' } });
});

// Lazy acquisition uses the same owner but never replays a granted transfer.
const lazyCall = (options = {}) => api.invokeAcquisitionBridge('acquire_book', { book_details: { source: 'zlibrary', id: '202', hash: 'hash202' } }, options);
function lazyBackend({ operation, login } = {}) {
  const counts = { login: 0, validate: 0, grants: 0, quota: 0, normal: 0 };
  runPythonBridge.mockImplementation(async (_script, _options, ro) => {
    const input = ro.sessionInput;
    if (input?.action === 'login') {
      counts.login++;
      if (login) return login(input, ro, counts);
      ro.onSession({ ...session(input.generation), identity: input.identity });
      return envelope({ authenticated: true });
    }
    if (input?.action === 'validate') { counts.validate++; return envelope({ authenticated: true }); }
    if (!ro.onSessionNeeded) { counts.normal++; return envelope([]); }
    return new Promise((resolve, reject) => {
      const abort = () => reject(new Error('Synthetic child cancellation'));
      ro.signal?.addEventListener('abort', abort, { once: true });
      const finish = value => { ro.signal?.removeEventListener('abort', abort); resolve(value); };
      ro.onSessionNeeded({
        grant(snapshot) {
          counts.grants++; counts.quota++;
          if (operation) operation(snapshot, ro, counts, finish, reject);
          else finish(envelope({ path: '/synthetic/document', transfer_skipped: false }));
        },
        deny(error) { ro.signal?.removeEventListener('abort', abort); reject(error); },
      });
    });
  });
  return counts;
}

test('lazy postgrant quota-decrement rejection performs one transfer, rejects generation, never refreshes', async () => {
  const counts = lazyBackend({ operation(input, ro, _counts, finish) {
    // A rejected generation must not be rescued by its terminal private metadata.
    ro.onSession(session(input.generation, 'rejected-private-canary'));
    finish(rejectReply(input.generation));
  } });
  await expect(lazyCall()).rejects.toMatchObject({ context: { reason: 'auth_rejected', details: { request_issued: true } } });
  expect(counts).toMatchObject({ login: 1, grants: 1, quota: 1 });
  expect(JSON.stringify(consoleSpy.mock.calls)).not.toContain('rejected-private-canary');
  // A later independent read may authenticate, but the transfer was not replayed.
  await call();
  expect(counts.login).toBe(2); expect(counts.grants).toBe(1);
});

test('lazy recovered domain/validate_next is adopted and validated by later search/read', async () => {
  const counts = lazyBackend({ operation(input, ro, _counts, finish) {
    ro.onSession({ ...session(input.generation, 'recovered-private-canary', 'recovered.example.test'), validate_next: true });
    finish(envelope({ path: '/synthetic/document' }));
  } });
  await lazyCall();
  const seen = [];
  runPythonBridge.mockImplementation(async (_script, _options, ro) => {
    seen.push({ action: ro.sessionInput.action, domain: ro.sessionInput.domain });
    expect(ro.sessionInput.remix_userkey).toBe('recovered-private-canary');
    return envelope(ro.sessionInput.action === 'validate' ? { authenticated: true } : []);
  });
  await api.searchBooks({ query: 'synthetic recovered-domain search' });
  expect(seen).toEqual([{ action: 'validate', domain: 'recovered.example.test' }, { action: 'operation', domain: 'recovered.example.test' }]);
  expect(counts.login).toBe(1);
});

test('lazy raw error before grant survives cancellation of its unfinished owner login', async () => {
  let failChild;
  const entered = new Promise(resolve => {
    runPythonBridge.mockImplementation((_script, _options, ro) => {
      if (ro.sessionInput) { resolve(); return new Promise((_done, reject) => ro.signal.addEventListener('abort', () => reject(new Error('Synthetic cancelled login')), { once: true })); }
      return new Promise((_done, reject) => { failChild = reject; ro.onSessionNeeded({ grant() { throw new Error('No grant expected'); }, deny() {} }); });
    });
  });
  const work = lazyCall(); const outcome = work.catch(error => error);
  await entered; failChild(new Error('Invalid private session request'));
  expect((await outcome).message).toContain('Invalid private session request');
});

test('lazy leader cancellation permits live search waiter takeover, no late login adoption', async () => {
  let entered, late;
  const started = new Promise(resolve => { entered = resolve; });
  const controller = new AbortController();
  const counts = lazyBackend({ login(input, ro, stats) {
    if (stats.login > 1) { ro.onSession({ ...session(input.generation, 'takeover-cookie'), identity: input.identity }); return envelope({ authenticated: true }); }
    late = () => ro.onSession(session(input.generation, 'late-private-canary'));
    entered();
    return new Promise((_resolve, reject) => ro.signal.addEventListener('abort', () => reject(new Error('Synthetic cancelled login')), { once: true }));
  } });
  const leader = lazyCall({ signal: controller.signal }).catch(error => error);
  await started;
  const waiter = call(); controller.abort();
  expect((await leader).context.reason).toBe('aborted');
  await expect(waiter).resolves.toEqual([]); late();
  expect(counts.login).toBe(2); expect(counts.grants).toBe(0);
  await call(); expect(counts.login).toBe(2);
});

test('lazy postgrant cancellation fences late update and does not replay transfer', async () => {
  let granted, late;
  const started = new Promise(resolve => { granted = resolve; });
  const controller = new AbortController();
  const counts = lazyBackend({ operation(input, ro) { late = () => ro.onSession(session(input.generation, 'late-operation-cookie')); granted(); } });
  const outcome = lazyCall({ signal: controller.signal }).catch(error => error);
  await started; controller.abort();
  expect((await outcome).context.reason).toBe('aborted'); late();
  const inputs = [];
  runPythonBridge.mockImplementation(async (_script, _options, ro) => { inputs.push(ro.sessionInput); return envelope([]); });
  await call();
  expect(inputs[0].remix_userkey).toBe(token);
  expect(counts).toMatchObject({ login: 1, grants: 1, quota: 1 });
});

test('stale lazy rejection and account-switch output cannot clear a newer session', async () => {
  let granted, completeOld, oldInput, oldRun;
  const started = new Promise(resolve => { granted = resolve; });
  const counts = lazyBackend({ operation(input, ro, _counts, finish) { oldInput = input; oldRun = ro; completeOld = finish; granted(); } });
  const old = lazyCall().catch(error => error); await started;
  process.env.ZLIBRARY_EMAIL = 'other-account@example.test'; await call();
  process.env.ZLIBRARY_EMAIL = identity; await call();
  const logins = counts.login;
  oldRun.onSession(session(oldInput.generation, 'obsolete-private-canary'));
  completeOld(rejectReply(oldInput.generation));
  expect((await old).context.reason).toBe('auth_rejected');
  await call(); expect(counts.login).toBe(logins); expect(counts.quota).toBe(1);
});

test('lazy waiter cancellation neither aborts shared login leader nor receives a grant', async () => {
  let release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const counts = lazyBackend({ login(input, ro) {
    entered(); return new Promise(resolve => { release = () => { ro.onSession(session(input.generation)); resolve(envelope({ authenticated: true })); }; });
  } });
  const leader = call(); await started;
  const controller = new AbortController();
  const waiter = lazyCall({ signal: controller.signal }).catch(error => error);
  await new Promise(resolve => setTimeout(resolve, 0)); controller.abort();
  expect((await waiter).context.reason).toBe('aborted');
  release(); await expect(leader).resolves.toEqual([]);
  expect(counts).toMatchObject({ login: 1, grants: 0, quota: 0 });
});

test('lazy non-auth uncertainty retains the session without operation replay', async () => {
  const counts = lazyBackend({ operation(_input, _ro, _counts, finish) {
    finish(envelope({ error: 'Synthetic transfer timeout', type: 'RuntimeError', details: { reason: 'read_timeout', request_issued: true } }));
  } });
  await expect(lazyCall()).rejects.toThrow('Synthetic transfer timeout');
  await call(); expect(counts).toMatchObject({ login: 1, grants: 1, quota: 1 });
});

test('typed private corruption cannot adopt a valid update or become an owner/envelope provider error', async () => {
  const counts = lazyBackend({
    operation(input, ro, _counts, _finish, reject) {
      ro.onSession({
        ...session(input.generation, 'corrupted-private-canary', 'corrupted.example.test'),
        validate_next: true,
      });
      reject(
        Object.assign(new Error('Invalid private session request'), {
          privateSessionProtocolCorruption: true,
          stderr: JSON.stringify({
            error: 'corrupted-private-canary',
            type: 'ReadTimeout',
            details: { reason: 'read_timeout', request_issued: false },
          }),
        }),
      );
    },
  });
  const error = await lazyCall().catch((e) => e);
  expect(error.context).toMatchObject({ reason: 'internal', details: { request_issued: true } });
  const { errorEnvelope } = await import('../dist/lib/tool-errors.js');
  expect(errorEnvelope(error, 'download', 'zlibrary').error).toMatchObject({
    code: 'internal',
    retry: 'no',
    detail: { request_issued: true },
  });
  runPythonBridge.mockImplementation(async (_s, _o, ro) => {
    expect(ro.sessionInput.remix_userkey).toBe(token);
    return envelope([]);
  });
  await call();
  expect(counts).toMatchObject({ login: 1, grants: 1, quota: 1 });
  expect(JSON.stringify(error)).not.toContain('corrupted-private-canary');
});

test.each([false, undefined])(
  'parent handoff issuance wins child stderr request_issued=%s; valid recovery still adopts',
  async (issued) => {
    const counts = lazyBackend({
      operation(input, ro, _counts, _finish, reject) {
        ro.onSession(session(input.generation, 'valid-recovery-cookie', 'recovery.example.test'));
        reject(
          Object.assign(new Error('Synthetic provider failure'), {
            stderr: JSON.stringify({
              error: 'Synthetic provider failure',
              type: 'ReadTimeout',
              details: {
                provider: 'zlibrary',
                reason: 'read_timeout',
                ...(issued === undefined ? {} : { request_issued: issued }),
              },
            }),
          }),
        );
      },
    });
    const error = await lazyCall().catch((e) => e);
    expect(error.context.details).toMatchObject({ reason: 'read_timeout', request_issued: true });
    runPythonBridge.mockImplementation(async (_s, _o, ro) => {
      expect(ro.sessionInput.remix_userkey).toBe('valid-recovery-cookie');
      return envelope([]);
    });
    await call();
    expect(counts).toMatchObject({ login: 1, grants: 1, quota: 1 });
  },
);
