import { jest, beforeEach, afterEach, expect, test } from '@jest/globals';

// Per-source breakers (ADR-011): one 2.0 bridge call contacts one source, so an
// Anna's outage must not refuse LibGen, nor the reverse. Real API, retry,
// breaker and 2.0 tools; only interpreter lookup and the process boundary are fake.
let api, tools, runner, saved, down;
const envelope = (data) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(data) }] }),
];
const outage = (provider) =>
  Object.assign(new Error('bridge exit'), {
    stderr: JSON.stringify({
      error: 'fixture failure',
      type: 'SourceError',
      details: { provider, reason: 'read_timeout', operation: 'search' },
    }),
  });
const sourceOf = (call) => {
  const args = JSON.parse(call[1].args[1]);
  return args.book_details?.source ?? args.source;
};
const callsTo = (source) => runner.mock.calls.filter((call) => sourceOf(call) === source);

async function load(env = {}) {
  saved = { ...process.env };
  Object.assign(process.env, {
    RETRY_MAX_RETRIES: '0',
    RETRY_INITIAL_DELAY: '1',
    CIRCUIT_BREAKER_THRESHOLD: '5',
    CIRCUIT_BREAKER_TIMEOUT: '60000',
    LOG_LEVEL: 'error',
    ...env,
  });
  delete process.env.ZLIBRARY_EMAIL;
  delete process.env.ZLIBRARY_PASSWORD;
  jest.resetModules();
  down = new Set();
  runner = jest.fn(async (_script, options) => {
    const [fn, raw] = options.args;
    const args = JSON.parse(raw);
    const source = String(args.book_details?.source ?? args.source ?? 'auto');
    if (down.has(source)) throw outage(source);
    if (fn === 'search_multi_source')
      return envelope({
        books: [{ source, md5: 'a'.repeat(32), title: 'Healthy', extension: 'pdf' }],
      });
    if (fn === 'acquire_book')
      return envelope({
        path: '/out/book.pdf',
        bytes: 3,
        reused: false,
        transfer_skipped: false,
        verified: 'sha256',
        record: '/out/book.pdf.acq.json',
      });
    throw new Error(`unexpected ${fn}`);
  });
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => '/fake/python',
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 2400000,
  }));
  api = await import('../lib/zlibrary-api.js');
  const { AcquisitionTools } = await import('../lib/acquisition-tools.js');
  tools = new AcquisitionTools(api.invokeAcquisitionBridge, process.env);
}
afterEach(() => {
  process.env = saved;
});

async function fiveOutages(source) {
  down.add(source);
  for (let i = 0; i < 5; i++) {
    const reply = await tools.call('search', { query: 'x', sources: [source] });
    expect(reply.error.code).toBe('provider_timeout');
  }
  const refused = await tools.call('search', { query: 'x', sources: [source] });
  expect(refused.error).toMatchObject({ code: 'breaker_open', source });
  expect(callsTo(source)).toHaveLength(5);
}

test.each([
  ['annas', 'libgen'],
  ['libgen', 'annas'],
])('five %s outages leave a %s search admitted', async (failed, healthy) => {
  await load();
  await fiveOutages(failed);
  const reply = await tools.call('search', { query: 'x', sources: [healthy] });
  expect(reply.error).toBeUndefined();
  expect(reply.rows).toHaveLength(1);
  expect(reply.routing).toMatchObject({ served_by: healthy, fell_back: false });
  expect(callsTo(healthy)).toHaveLength(1);
});

test('after an Anna outage an ordered annas,libgen search reaches LibGen', async () => {
  await load();
  await fiveOutages('annas');
  const reply = await tools.call('search', { query: 'x', sources: ['annas', 'libgen'] });
  expect(reply.error).toBeUndefined();
  expect(reply.routing).toMatchObject({
    served_by: 'libgen',
    fell_back: true,
    attempts: [
      { source: 'annas', outcome: 'failed', code: 'breaker_open' },
      { source: 'libgen', outcome: 'rows' },
    ],
  });
  expect(callsTo('annas')).toHaveLength(5);
  expect(callsTo('libgen')).toHaveLength(1);
});

test('annas-first searches keep reaching LibGen while each Anna probe re-opens', async () => {
  await load({ CIRCUIT_BREAKER_TIMEOUT: '50' });
  await fiveOutages('annas');
  for (let round = 1; round <= 3; round++) {
    await new Promise((resolve) => setTimeout(resolve, 80));
    const reply = await tools.call('search', { query: 'x', sources: ['annas', 'libgen'] });
    expect(reply.routing).toMatchObject({
      served_by: 'libgen',
      attempts: [
        { source: 'annas', outcome: 'failed', code: 'provider_timeout' },
        { source: 'libgen', outcome: 'rows' },
      ],
    });
    expect(callsTo('annas')).toHaveLength(5 + round);
    expect(callsTo('libgen')).toHaveLength(round);
  }
});

test('an Anna outage leaves a LibGen download on its ordinary child', async () => {
  await load();
  await fiveOutages('annas');
  const found = await tools.call('search', { query: 'x', sources: ['libgen'] });
  const reply = await tools.call('download', { ref: found.rows[0].ref });
  expect(reply).toMatchObject({ path: '/out/book.pdf', transfer_skipped: false });
  const acquisitions = runner.mock.calls.filter((call) => call[1].args[0] === 'acquire_book');
  expect(acquisitions.map((call) => JSON.parse(call[1].args[1]).receipt_only)).toEqual([undefined]);
});

test('the annas_archive spelling shares the Anna scope', async () => {
  await load();
  await fiveOutages('annas');
  await expect(
    api.invokeAcquisitionBridge('acquire_book', {
      book_details: { source: 'annas_archive', md5: 'a'.repeat(32) },
      output_dir: '/out',
    }),
  ).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
  expect(
    callsTo('annas_archive').filter((call) => !JSON.parse(call[1].args[1]).receipt_only),
  ).toHaveLength(0);
});

test('an internal auto walk has its own scope, apart from both named sources', async () => {
  await load();
  down.add('auto');
  for (let i = 0; i < 5; i++)
    await expect(api.searchMultiSource({ query: 'x' })).rejects.toThrow(/fixture failure/);
  await expect(api.searchMultiSource({ query: 'x' })).rejects.toThrow(/OPEN/);
  for (const source of ['libgen', 'annas'])
    await expect(api.searchMultiSource({ query: 'x', source })).resolves.toMatchObject({
      books: [{ source }],
    });
});

test('a raw search spelling the Python router sends to LibGen counts on the LibGen breaker', async () => {
  // Gate A, A1: lib/sources/router.py routes every spelling but `auto` and
  // `annas` to LibGen, so Anna's breaker must not decide those calls.
  await load();
  await fiveOutages('annas');
  for (const source of ['annas_archive', 'Annas', 'libgen']) {
    const before = runner.mock.calls.length;
    await expect(api.searchMultiSource({ query: 'x', source })).resolves.toBeDefined();
    expect(runner.mock.calls.length).toBe(before + 1);
  }
});

test('raw null search failures open LibGen and leave the auto scope admitted', async () => {
  await load();
  // Python routes an explicit null to LibGen; omission retains its auto default.
  runner.mockImplementation(async (_script, options) => {
    const args = JSON.parse(options.args[1]);
    if (args.source === null) throw outage('libgen');
    return envelope({ books: [] });
  });
  for (let i = 0; i < 5; i++)
    await expect(
      api.invokeAcquisitionBridge('search_multi_source', { query: 'x', source: null }),
    ).rejects.toThrow(/fixture failure/);
  await expect(api.searchMultiSource({ query: 'x', source: 'libgen' })).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  await expect(api.searchMultiSource({ query: 'x' })).resolves.toEqual({ books: [] });
  await expect(api.searchMultiSource({ query: 'x', source: 'auto' })).resolves.toEqual({
    books: [],
  });
  await expect(api.searchMultiSource({ query: 'x', source: 'annas' })).resolves.toEqual({
    books: [],
  });
});

test.each(['per-call', 'memory', 'unused-invalid-mode'])(
  'local browser budget ignores auth mode %s while the auto provider breaker is open',
  async (mode) => {
    await load({ ZLIBRARY_SESSION_MODE: 'per-call' });
    down.add('auto');
    for (let i = 0; i < 5; i++)
      await api
        .invokeAcquisitionBridge('search_multi_source', { query: 'x', source: 'auto' })
        .catch(() => {});
    await expect(
      api.invokeAcquisitionBridge('search_multi_source', { query: 'x', source: 'auto' }),
    ).rejects.toMatchObject({ code: 'CIRCUIT_BREAKER_OPEN' });
    down.delete('auto');
    process.env.ZLIBRARY_SESSION_MODE = mode;
    runner.mockImplementationOnce(async () =>
      envelope({ enabled: true, used_today: 12, daily_cap: 50 }),
    );
    expect(await api.invokeAcquisitionBridge('get_annas_browser_limits')).toMatchObject({
      used_today: 12,
      daily_cap: 50,
    });
  },
);
