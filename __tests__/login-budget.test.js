import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

let metadata, AcquisitionTools;
const publicLogin = { configured: false, origin: 'none', identity: '' };
beforeEach(async () => {
  jest.resetModules();
  metadata = jest.fn(async () => publicLogin);
  jest.unstable_mockModule('../dist/lib/login.js', () => ({ readLoginMetadata: metadata }));
  ({ AcquisitionTools } = await import('../dist/lib/acquisition-tools.js'));
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});

test.each([false, true])(
  'spent metadata budget prevents retained refill dispatch, rejected=%s',
  async (rejected) => {
    jest.useFakeTimers();
    const { SourceRegistry } = await import('../dist/lib/source-registry.js');
    const { windowFixture } = await import('./merged-search/window-fixture.js');
    const invoke = jest.fn(async (request) =>
      windowFixture(
        Array.from({ length: request.page === 1 ? 50 : 1 }, (_, i) => ({
          id: String(i),
          metadata: { title: 'Fixture' },
          book: { id: String(i), title: 'Fixture' },
        })),
        request.page === 1 ? { more: true } : {},
      ),
    );
    const registry = new SourceRegistry([
      {
        name: 'libgen',
        capabilities: () => ({
          configured: true,
          search_available: true,
          download_available: true,
        }),
        defaultSearch: () => ({ on: true }),
        md5: () => undefined,
        searchWindow: invoke,
      },
    ]);
    const tools = new AcquisitionTools(
      async () => {
        throw Error('Unexpected bridge');
      },
      {},
      { registry },
    );
    const first = await tools.call('search', { query: 'Fixture', sources: ['libgen'], limit: 50 });
    expect(first.next_cursor).toBeDefined();
    invoke.mockClear();
    metadata.mockImplementation(
      () =>
        new Promise((resolve, reject) =>
          setTimeout(
            () => (rejected ? reject(new Error('synthetic deadline')) : resolve(publicLogin)),
            20,
          ),
        ),
    );
    const reply = tools.call('search', { cursor: first.next_cursor }, { timeoutMs: 20 });
    await jest.advanceTimersByTimeAsync(20);
    const value = await reply;
    expect(invoke).not.toHaveBeenCalled();
    expect(value.error).toMatchObject({
      code: 'provider_timeout',
      retry: 'safe',
      detail: { reason: 'caller_timeout' },
    });
  },
);

test('successful metadata lookup reduces the options passed to an account operation', async () => {
  jest.useFakeTimers();
  metadata.mockImplementation(
    () =>
      new Promise((resolve) =>
        setTimeout(
          () =>
            resolve({
              ...publicLogin,
              configured: true,
              origin: 'environment',
              identity: 'reader@example.test',
            }),
          25,
        ),
      ),
  );
  const invoke = jest.fn(async () => []);
  const pending = new AcquisitionTools(invoke, {
    ZLIBRARY_EMAIL: 'reader@example.test',
    ZLIBRARY_PASSWORD: 'synthetic',
  }).call('get_history', {}, { timeoutMs: 100 });
  await jest.advanceTimersByTimeAsync(25);
  await pending;
  expect(invoke).toHaveBeenCalled();
  expect(invoke.mock.calls[0][2].timeoutMs).toBe(75);
});

test('renewed saved-key ConnectError is counted by the Node dependency policy', async () => {
  const state = await mkdtemp(path.join(os.tmpdir(), 'saved-transport-'));
  try {
    const { stdout } = await promisify(execFile)(
      path.resolve('.venv/bin/python'),
      ['__tests__/fixtures/saved_renewal_transport.py'],
      {
        env: {
          ...process.env,
          XDG_STATE_HOME: state,
          PYTHON_DOTENV_DISABLED: '1',
          PYTHONDONTWRITEBYTECODE: '1',
          PYTHONPATH: [path.resolve('zlibrary/src'), path.resolve('lib'), process.cwd()].join(
            path.delimiter,
          ),
        },
      },
    );
    const value = JSON.parse(stdout);
    const { isTransientDependencyError } = await import('../dist/lib/retry-manager.js');
    expect(value.type).toBe('ConnectError');
    expect(value.diagnostics.failure_kind).toBe('transport');
    expect(value.diagnostics.http_status).toBeUndefined();
    expect(
      isTransientDependencyError({
        context: { pythonErrorType: value.type, diagnostics: value.diagnostics },
      }),
    ).toBe(true);
  } finally {
    await rm(state, { recursive: true, force: true });
  }
});

test('real-clock bridge route receives a safe integer within the caller budget', async () => {
  jest.useRealTimers();
  metadata.mockImplementation(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    return {
      ...publicLogin,
      configured: true,
      origin: 'environment',
      identity: 'reader@example.test',
    };
  });
  const invoke = jest.fn(async () => []);
  const tools = new AcquisitionTools(invoke, {
    ZLIBRARY_EMAIL: 'reader@example.test',
    ZLIBRARY_PASSWORD: 'synthetic',
  });
  for (let i = 0; i < 3; i++) {
    const reply = await tools.call('get_history', {}, { timeoutMs: 1000 });
    expect(reply.error).toBeUndefined();
    const forwarded = invoke.mock.calls.at(-1)[2].timeoutMs;
    expect(Number.isSafeInteger(forwarded)).toBe(true);
    expect(forwarded).toBeGreaterThan(0);
    expect(forwarded).toBeLessThanOrEqual(1000);
  }
});

test.each(['get_history', 'get_limits'])(
  'abort during metadata remains cancelled for %s',
  async (name) => {
    const controller = new AbortController();
    metadata.mockImplementation(async () => {
      controller.abort();
      throw new Error('Synthetic aborted metadata lookup');
    });
    const invoke = jest.fn();
    const tools = new AcquisitionTools(invoke);
    const reply = await tools.call(name, {}, { signal: controller.signal });
    expect(reply.error).toMatchObject({ code: 'cancelled', retry: 'safe' });
    expect(JSON.stringify(reply)).not.toMatch(/permissions|metadata_unavailable/);
    expect(invoke).not.toHaveBeenCalled();
  },
);

test('an abort immediately after successful metadata stops before dispatch', async () => {
  const controller = new AbortController();
  metadata.mockImplementation(async () => {
    controller.abort();
    return {
      ...publicLogin,
      configured: true,
      origin: 'environment',
      identity: 'reader@example.test',
    };
  });
  const invoke = jest.fn(async () => ({ daily_remaining: 7 }));
  const reply = await new AcquisitionTools(invoke).call(
    'get_limits',
    { mode: 'live', sources: ['zlibrary'] },
    { signal: controller.signal },
  );
  expect(reply).toMatchObject({ error: { code: 'cancelled' } });
  expect(invoke).not.toHaveBeenCalled();
});

test('submillisecond remaining budget uses the exhausted-budget reply before dispatch', async () => {
  jest.useFakeTimers();
  metadata.mockResolvedValue({
    ...publicLogin,
    configured: true,
    origin: 'environment',
    identity: 'reader@example.test',
  });
  const invoke = jest.fn(async () => []);
  const reply = await new AcquisitionTools(invoke).call('get_history', {}, { timeoutMs: 0.5 });
  expect(invoke).not.toHaveBeenCalled();
  expect(reply.error).toMatchObject({
    code: 'provider_timeout',
    retry: 'safe',
    detail: { reason: 'caller_timeout', request_issued: false },
  });
});
