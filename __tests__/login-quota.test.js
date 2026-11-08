import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
let login, metadata, AcquisitionTools;
beforeEach(async () => {
  jest.resetModules();
  login = { configured: true, origin: 'saved', identity: 'a@example.test', generation: 1 };
  metadata = jest.fn(async () => login);
  jest.unstable_mockModule('../dist/lib/login.js', () => ({ readLoginMetadata: metadata }));
  ({ AcquisitionTools } = await import('../dist/lib/acquisition-tools.js'));
});
afterEach(() => jest.restoreAllMocks());
const book = {
  id: '42',
  hash: 'synthetic',
  title: 'Fixture',
  extension: 'pdf',
  md5: 'a'.repeat(32),
};
const transfer = {
  path: '/synthetic/book.pdf',
  bytes: 7,
  reused: false,
  transfer_skipped: false,
  verified: 'sha256',
  record: '/synthetic/book.pdf.acq.json',
};
function fixture(remaining = 0) {
  const invoke = jest.fn(async (operation) => {
    if (operation === 'get_download_limits')
      return { daily_limit: 10, daily_remaining: remaining, downloads_today: 10 - remaining };
    if (operation === 'search') return { books: [book] };
    if (operation === 'acquire_book') return transfer;
    throw Error('Unexpected operation');
  });
  return { invoke, tools: new AcquisitionTools(invoke, {}) };
}
test.each([0, 7])(
  'switching saved accounts discards the prior %s quota estimate',
  async (remaining) => {
    const { tools } = fixture(remaining);
    await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
    const a = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
    expect(a.rows[0].download_available).toBe(remaining === 0 ? false : undefined);
    login = { ...login, identity: 'b@example.test', generation: 2 };
    const b = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
    expect(b.rows[0].download_available).toBeUndefined();
    const result = await tools.call('download', { ref: b.rows[0].ref });
    expect(result.error).toBeUndefined();
    expect(result.quota_estimate).toBeUndefined();
  },
);
test('same account renewal retains its observed quota while an origin switch discards it', async () => {
  const { tools } = fixture(7);
  await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  login = { ...login, generation: 2 };
  let page = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect((await tools.call('download', { ref: page.rows[0].ref })).quota_estimate.remaining).toBe(
    6,
  );
  login = { ...login, origin: 'environment' };
  page = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect((await tools.call('download', { ref: page.rows[0].ref })).quota_estimate).toBeUndefined();
});
test('a delayed old-account limits reply cannot repopulate the current account quota', async () => {
  const { tools, invoke } = fixture();
  let release, started;
  const entered = new Promise((resolve) => (started = resolve));
  invoke.mockImplementationOnce(() => {
    started();
    return new Promise((resolve) => (release = resolve));
  });
  const old = tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  await entered;
  login = { ...login, identity: 'b@example.test' };
  await tools.call('get_limits', { mode: 'local' });
  release({ daily_remaining: 0, daily_limit: 10, downloads_today: 10 });
  await old;
  const page = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect(page.rows[0].download_available).toBeUndefined();
});

test('a delayed old-account download estimate cannot attach to the new account', async () => {
  const { tools, invoke } = fixture(7);
  await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  const page = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  let release, started;
  const entered = new Promise((resolve) => (started = resolve));
  invoke.mockImplementationOnce(() => {
    started();
    return new Promise((resolve) => (release = resolve));
  });
  const old = tools.call('download', { ref: page.rows[0].ref });
  await entered;
  login = { ...login, identity: 'b@example.test' };
  await tools.call('get_limits', { mode: 'local' });
  release({
    ...transfer,
    quota_estimate: {
      source: 'zlibrary',
      remaining: 6,
      as_of: '2026-11-08T12:00:00Z',
      basis: 'observed',
    },
  });
  await old;
  const fresh = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect((await tools.call('download', { ref: fresh.rows[0].ref })).quota_estimate).toBeUndefined();
});

test('cancelled metadata does not replace the current account quota state', async () => {
  const { tools } = fixture(7);
  await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  const controller = new AbortController();
  metadata.mockImplementationOnce(async () => {
    controller.abort();
    return { ...login, identity: 'b@example.test' };
  });
  const result = await tools.call('get_limits', { mode: 'local' }, { signal: controller.signal });
  expect(result.error.code).toBe('cancelled');
  const page = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect((await tools.call('download', { ref: page.rows[0].ref })).quota_estimate.remaining).toBe(
    6,
  );
});
