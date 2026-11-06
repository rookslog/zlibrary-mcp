import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { SearchRecords } from '../../dist/lib/search-records.js';
import { planSearch } from '../../dist/lib/search-assembly.js';
import { CircuitBreaker } from '../../dist/lib/circuit-breaker.js';
import { windowFixture } from './window-fixture.js';

beforeEach(() => jest.useFakeTimers({ now: 1000000 }));
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});
const env = { ZLIBRARY_EMAIL: 'fixture.invalid', ZLIBRARY_PASSWORD: 'synthetic-only' };
const row = (id) => ({ id, hash: 'fixture', title: 'Solaris', author: 'Lem', extension: 'pdf' });

test('refill dispatch uses only its original answered source even if a refused source recovers', async () => {
  let blocked = true;
  const calls = [];
  const registry = new SourceRegistry(
    ['libgen', 'zlibrary'].map((name) => ({
      name,
      capabilities: () => ({ configured: true, search_available: true, download_available: true }),
      defaultSearch: () => ({ on: true }),
      md5: () => undefined,
      searchWindow: async (request) => {
        calls.push([name, request.page]);
        return windowFixture(
          [{ id: `${name}-${request.page}`, metadata: { title: 'Solaris' } }],
          { more: true },
          request,
        );
      },
    })),
  );
  const tools = new AcquisitionTools(
    async () => {},
    {},
    {
      registry,
      localState: () =>
        blocked ? { libgen: { admission: 'backoff', admissionRetryAfterS: 1 } } : {},
    },
  );
  const first = await tools.call('search', {
    query: 'Solaris',
    sources: ['libgen', 'zlibrary'],
    limit: 1,
  });
  blocked = false;
  const second = await tools.call('search', { cursor: first.next_cursor });
  expect(calls).toEqual([
    ['zlibrary', 1],
    ['zlibrary', 2],
  ]);
  expect(second.sources.libgen).toMatchObject({ state: 'not_asked', reason: 'auth_backoff' });
});

test('relationship unknown fields retain the original exact title and author request', async () => {
  const tools = new AcquisitionTools(
    async (operation) => ({
      books: [
        operation === 'search'
          ? { id: 'missing', hash: 'fixture', md5: 'a'.repeat(32), extension: 'pdf' }
          : { ...row('known'), md5: 'a'.repeat(32) },
      ],
    }),
    env,
  );
  const search = await tools.call('search', {
    query: 'Solaris',
    author: 'Lem',
    exact: true,
    sources: ['libgen', 'zlibrary'],
  });
  expect(search.rows).toHaveLength(1);
  const detail = await tools.call('get_book', { ref: search.rows[0].ref });
  expect(detail.other_copies[0]).toMatchObject({
    source: 'zlibrary',
    filter: 'unknown',
    unknown: ['title', 'author'],
  });
});

test('fresh selected detail rechecks the remembered format constraint', async () => {
  const tools = new AcquisitionTools(
    async (operation) =>
      operation === 'get_book_metadata_complete'
        ? { extension: 'epub' }
        : { books: [row('chosen')] },
    env,
  );
  const search = await tools.call('search', {
    query: 'Solaris',
    sources: ['zlibrary'],
    formats: ['pdf'],
  });
  const detail = await tools.call('get_book', { ref: search.rows[0].ref });
  expect(detail).toMatchObject({ format: 'epub', filter: 'fail' });
});

const views = [
  {
    name: 'zlibrary',
    capabilities: { configured: true, search_available: true, download_available: true },
    defaultSearch: { on: true },
  },
];
const plan = () =>
  planSearch({ query: 'Solaris', sources: ['zlibrary'], limit: 1 }, views, { waitMs: 100 });
const collected = (id, more = true) => [
  {
    source: 'zlibrary',
    state: 'answered',
    ms: 0,
    more,
    copies: [
      { source: 'zlibrary', id, ref: `input-${id}`, metadata: { title: 'Solaris' }, book: row(id) },
    ],
  },
];

test('seven deep searches do not evict relationships belonging to another retained search', async () => {
  const store = new SearchRecords({ capacity: 128 });
  const first = store.retain(plan(), collected('first', false));
  for (let search = 0; search < 7; search++) {
    let page = store.retain(plan(), collected(`${search}-1`), {
      refill: async (number) => collected(`${search}-${number}`)[0],
    });
    for (let number = 2; number <= 20; number++) page = await store.page(page.next_cursor);
  }
  expect(store.select(first.rows[0].ref).copy.id).toBe('first');
  expect(store.related(first.rows[0].ref).copies[0].id).toBe('first');
});

test('parent eviction releases its relationship index as well as refs', () => {
  const store = new SearchRecords({ capacity: 1 });
  const first = store.retain(plan(), collected('first', false));
  store.retain(plan(), collected('second', false));
  expect(() => store.select(first.rows[0].ref)).toThrow('Reference unavailable (expired).');
  // Index occupancy observes retained object lifetime, not merely invalid handles.
  expect(store.contexts.size).toBe(1);
  expect(
    [...store.contexts.values()].flatMap((context) => context.copies.map((copy) => copy.id)),
  ).toEqual(['second']);
});

test('a real held half-open probe refuses search locally without charging recent health', async () => {
  const breaker = new CircuitBreaker({ threshold: 1, timeout: 100 });
  await breaker
    .execute(async () => {
      throw Error('Fixture outage');
    })
    .catch(() => {});
  await jest.advanceTimersByTimeAsync(101);
  let release;
  const probe = breaker.execute((admission) => {
    admission.holdFor(1000);
    return new Promise((resolve) => (release = resolve));
  });
  try {
    expect(breaker.snapshot()).toMatchObject({ breaker: 'half_open', breakerBlocked: true });
    const tools = new AcquisitionTools(
      async () => {
        throw Error('Must not dispatch');
      },
      env,
      { localState: () => ({ zlibrary: breaker.snapshot() }) },
    );
    const reply = await tools.call('search', { query: 'Solaris', sources: ['zlibrary'] });
    expect(reply.error.attempts.find((line) => line.source === 'zlibrary').state).toBe('not_asked');
    expect((await tools.call('get_limits')).sources.zlibrary.recent).toEqual({
      searches: 0,
      late: 0,
      failed: 0,
    });
  } finally {
    release();
    await probe;
  }
});

test('caller cancellation is absent from recent provider health on first pages', async () => {
  const tools = new AcquisitionTools(async () => new Promise(() => {}), env);
  for (let i = 0; i < 3; i++) {
    const controller = new AbortController();
    const pending = tools.call('search', { query: 'Solaris' }, { signal: controller.signal });
    await jest.advanceTimersByTimeAsync(0);
    controller.abort();
    expect((await pending).error.code).toBe('cancelled');
  }
  const limits = await tools.call('get_limits');
  for (const source of ['libgen', 'zlibrary'])
    expect(limits.sources[source].recent).toEqual({ searches: 0, late: 0, failed: 0 });
});
