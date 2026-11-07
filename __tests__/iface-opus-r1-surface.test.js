import './helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

const run = jest.fn();
jest.unstable_mockModule('../lib/venv-manager.js', () => ({
  getManagedPythonPath: async () => '/offline/python',
}));
jest.unstable_mockModule('../lib/python-runner.js', () => ({
  runPythonBridge: run,
  LONG_BRIDGE_TIMEOUT_MS: 2400000,
}));
// This suite tests public reporting; native member-worker pipes have their own tests.
jest.unstable_mockModule('../lib/annas-member-worker.js', () => ({
  annasMemberWorker: {
    status: () => 'none',
    search: async () => JSON.parse(JSON.parse((await run())[0]).content[0].text),
  },
}));
const { createServer } = await import('../dist/index.js');
const encode = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
const row = (source) => ({
  source,
  id: '1',
  hash: 'hash1',
  md5: 'a'.repeat(32),
  title: 'Book',
  extension: 'pdf',
});
let server, client, previous;
beforeEach(async () => {
  previous = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_SESSION_MODE: 'per-call',
    ZLIBRARY_EMAIL: 'offline@example.invalid',
    ZLIBRARY_PASSWORD: 'synthetic-password',
    ANNAS_SECRET_KEY: 'synthetic-key',
    RETRY_MAX_RETRIES: '0',
  });
  run.mockReset();
  server = createServer();
  client = new Client({ name: 'review-regressions', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
});
afterEach(async () => {
  await client.close();
  await server.close();
  process.env = previous;
  jest.restoreAllMocks();
});
const call = async (name, args = {}) =>
  (await client.callTool({ name, arguments: args })).structuredContent;

test.each(['search', 'get_book', 'download', 'get_limits', 'get_history'])(
  'SDK mid-flight %s cancellation reaches invoke and the bridge signal',
  async (name) => {
    run.mockResolvedValue(encode({ books: [row('zlibrary')] }));
    const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
    const args = {
      search: { query: 'Book', sources: ['zlibrary'] },
      get_book: { ref },
      download: { ref },
      get_limits: { mode: 'live', sources: ['zlibrary'] },
      get_history: {},
    }[name];
    let started, release, observed, timer;
    const entered = new Promise((resolve) => {
      started = resolve;
    });
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    const abortObserved = new Promise((resolve) => {
      observed = resolve;
    });
    let bridgeSignal;
    run.mockImplementation(async (_script, _options, options) => {
      bridgeSignal = options.signal;
      if (bridgeSignal)
        bridgeSignal.addEventListener('abort', () => observed(true), { once: true });
      else observed(false);
      started();
      await pending;
      return encode({});
    });
    const invoke = jest.spyOn(server.tools, 'invoke');
    const controller = new AbortController();
    const outcome = client
      .callTool({ name, arguments: args }, undefined, { signal: controller.signal })
      .then(
        () => ({ rejected: false }),
        () => ({ rejected: true }),
      );
    try {
      await entered;
      expect(bridgeSignal?.aborted ?? false).toBe(false);
      controller.abort();
      expect((await outcome).rejected).toBe(true);
      expect(
        await Promise.race([
          abortObserved,
          new Promise((resolve) => {
            timer = setTimeout(() => resolve(false), 1000);
          }),
        ]),
      ).toBe(true);
      expect(invoke).toHaveBeenCalledTimes(1);
      expect(invoke.mock.calls[0][2].signal.aborted).toBe(true);
      expect(bridgeSignal.aborted).toBe(true);
    } finally {
      controller.abort();
      clearTimeout(timer);
      release();
      await outcome;
      await new Promise((resolve) => setImmediate(resolve));
    }
  },
);

test.each(['libgen', 'annas'])(
  '%s note reports its fixed query cap, not the fetched row count',
  async (source) => {
    run.mockResolvedValue(
      encode({ books: [row(source), { ...row(source), md5: 'b'.repeat(32) }] }),
    );
    const result = await call('search', { query: 'Book', sources: [source], limit: 1 });
    expect(result.note).toContain(source);
    expect(result.note).toContain('50');
    expect(result.next_cursor).toBeDefined();
  },
);
test('fallthrough cannot retain an empty non-serving source note', async () => {
  run.mockImplementation(async (_script, options) =>
    encode({ books: options.args[0] === 'search' ? [row('zlibrary')] : [] }),
  );
  const result = await call('search', { query: 'Book', sources: ['libgen', 'zlibrary'] });
  expect(result.routing.served_by).toBe('zlibrary');
  expect(result.note).toBeUndefined();
});
test('exhausted continuation never advises changing exact on its cursor-only request', async () => {
  run
    .mockResolvedValueOnce(encode({ books: [row('zlibrary')] }))
    .mockResolvedValueOnce(encode({ books: [] }));
  const first = await call('search', {
    query: 'Book',
    sources: ['zlibrary'],
    limit: 1,
    exact: true,
  });
  const result = await call('search', { cursor: first.next_cursor });
  expect(result.rows).toEqual([]);
  expect(result.note ?? '').not.toContain('exact:false');
});
test.each(['libgen', 'zlibrary', 'annas'])(
  '%s unknown-outcome recovery agrees with its quota semantics',
  async (source) => {
    run.mockResolvedValue(encode({ books: [row(source)] }));
    const ref = (await call('search', { query: 'Book', sources: [source] })).rows[0].ref;
    jest
      .spyOn(server.tools, 'invoke')
      .mockRejectedValue(
        Object.assign(new Error('synthetic deadline'), { code: 'BRIDGE_TIMEOUT' }),
      );
    const result = await call('download', { ref });
    expect(result.error.code).toBe('outcome_unknown');
    expect(result.error.retry).toBe(source === 'libgen' ? 'safe' : 'may_spend_quota');
    if (source === 'libgen') {
      expect(result.error.recovery).not.toContain('may spend quota');
      expect(result.error.recovery).toContain('quota-free');
    } else expect(result.error.recovery).toContain('may spend quota');
  },
);
