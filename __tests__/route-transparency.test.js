import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';

const book = { md5: 'a'.repeat(32), title: 'Fixture', source: 'annas_archive' };
const download = { path: '/fixture.pdf', bytes: 8, reused: false, transfer_skipped: false, verified: 'sha256', record: '/fixture.pdf.acq.json' };

test('search retains actual bridge routes including empty sources, details and held pages make no requests', async () => {
  const tools = new AcquisitionTools(async (_op, args) => args.source === 'libgen'
    ? { books: [], via: [{ source: 'libgen', route: 'html', ms: 3 }] }
    : { books: [book, { ...book, md5: 'b'.repeat(32) }], via: [{ source: 'annas', route: 'html', ms: 7 }] }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('search', { query: 'fixture', sources: ['libgen', 'annas'], limit: 1 });
  expect(result.via).toEqual([{ source: 'libgen', route: 'html', ms: 3 }, { source: 'annas', route: 'html', ms: 7 }]);
  expect((await tools.call('get_book', { ref: result.rows[0].ref })).via).toEqual([]);
  expect((await tools.call('search', { cursor: result.next_cursor })).via).toEqual([]);
});

test('Z-Library detail uses the API route and keeps trace out of metadata evidence', async () => {
  const tools = new AcquisitionTools(async op => op === 'search'
    ? { books: [{ id: '42', hash: 'abc' }], via: [{ source: 'zlibrary', route: 'api', ms: 4 }] }
    : { description: 'Details', via: [{ source: 'zlibrary', route: 'api', ms: 9 }] }, { ZLIBRARY_EMAIL: 'fixture', ZLIBRARY_PASSWORD: 'fixture' });
  const ref = (await tools.call('search', { query: 'fixture', sources: ['zlibrary'] })).rows[0].ref;
  const result = await tools.call('get_book', { ref });
  expect(result.via).toEqual([{ source: 'zlibrary', route: 'api', ms: 9 }]);
  expect(result.evidence.detail).toEqual(['description']);
});

test('download preserves actual browser route and queue duration instead of configured API preference', async () => {
  const via = [{ source: 'annas', route: 'browser', ms: 900, queued_ms: 300 }];
  const tools = new AcquisitionTools(async op => op === 'search_multi_source' ? { books: [book], via: [] } : { ...download, via }, { ANNAS_SECRET_KEY: 'fixture', ANNAS_BROWSER_ENABLED: 'true' });
  const ref = (await tools.call('search', { query: 'fixture', sources: ['annas'] })).rows[0].ref;
  expect((await tools.call('download', { ref })).via).toEqual(via);
});

test('get_limits exposes routes and reads browser budget locally only when enabled', async () => {
  const tools = new AcquisitionTools(async op => {
    if (op !== 'get_annas_browser_limits') throw new Error('unexpected network operation');
    return { enabled: true, used_today: 12, daily_cap: 50, min_interval_s: 20, next_slot_in_s: 6, one_at_a_time: true };
  }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('get_limits');
  expect(result.routes.libgen).toEqual({ routes: ['html'], account_required: false });
  expect(result.routes.zlibrary).toEqual({ routes: ['api'], account_required: true });
  expect(result.routes.annas.routes).toEqual(['html', 'api', 'browser']);
  expect(result.routes.annas.browser).toMatchObject({ enabled: true, used_today: 12, daily_cap: 50, next_slot_in_s: 6, one_at_a_time: true });
  const off = await new AcquisitionTools(async () => { throw new Error('must stay local'); }, {}).call('get_limits', { sources: ['annas'] });
  expect(off.routes.annas.browser).toEqual({ enabled: false });
});

test('a failed source route remains visible if a later source answers', async () => {
  const tools = new AcquisitionTools(async (_op, args) => {
    if (args.source === 'annas') throw { context: { details: { reason: 'read_timeout', via: [{ source: 'annas', route: 'browser', ms: 900, queued_ms: 400 }] } } };
    return { books: [{ ...book, source: 'libgen' }], via: [{ source: 'libgen', route: 'html', ms: 4 }] };
  }, {});
  const result = await tools.call('search', { query: 'fixture', sources: ['annas', 'libgen'] });
  expect(result.via.map(v => v.route)).toEqual(['browser', 'html']);
  expect(result.routing.attempts[0]).toMatchObject({ source: 'annas', route: 'browser', outcome: 'failed' });
});

test('an unreadable local browser bridge preserves every source capability', async () => {
  const tools = new AcquisitionTools(async () => { throw new Error('local bridge unavailable'); }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('get_limits');
  expect(result.error).toBeUndefined();
  expect(result.sources.libgen.download_available).toBe(true);
  expect(result.sources.annas.search_available).toBe(true);
  expect(result.routes.annas.browser).toMatchObject({ enabled: true, used_today: null, next_slot_in_s: null, counter_status: 'unavailable' });
});

test('EAPI sanitization retains bounded route facts through a successful fallback', async () => {
  const { safeEapiFailureEnvelope } = await import('../dist/lib/auth-diagnostics.js');
  const safe = safeEapiFailureEnvelope({
    type: 'ReadTimeout', error: 'untrusted prose',
    diagnostics: { failure_kind: 'transport', attempt: 1, refresh_count: 0, request_id: '00000000-0000-4000-8000-000000000001' },
    details: { via: [{ source: 'zlibrary', route: 'api', ms: 9, payload: 'must drop' }] },
  });
  const tools = new AcquisitionTools(async op => {
    if (op === 'search') throw { context: { details: safe.details, diagnostics: safe.diagnostics } };
    return { books: [{ ...book, source: 'libgen' }], via: [{ source: 'libgen', route: 'html', ms: 4 }] };
  }, { ZLIBRARY_EMAIL: 'fixture', ZLIBRARY_PASSWORD: 'fixture' });
  const result = await tools.call('search', { query: 'fixture', sources: ['zlibrary', 'libgen'] });
  expect(result.via).toEqual([{ source: 'zlibrary', route: 'api', ms: 9 }, { source: 'libgen', route: 'html', ms: 4 }]);
  const poisoned = safeEapiFailureEnvelope({ type: 'ReadTimeout', diagnostics: safe.diagnostics, details: { via: [{ source: 'zlibrary', route: 'https://untrusted.invalid', ms: 9 }] } });
  expect(poisoned.details.via).toBeUndefined();
});
