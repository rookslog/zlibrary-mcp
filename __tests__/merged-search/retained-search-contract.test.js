import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { ToolFailure } from '../../dist/lib/tool-errors.js';

beforeEach(() => jest.useFakeTimers({ now: 1000000 }));
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});
const env = { ZLIBRARY_EMAIL: 'fixture.invalid', ZLIBRARY_PASSWORD: 'synthetic-only' };
const book = (id, title, extra = {}) => ({
  id,
  title,
  author: 'Lem',
  extension: 'pdf',
  hash: 'fixture',
  ...extra,
});
const fullPage = (query, page) =>
  Array.from({ length: 50 }, (_, index) => book(`${query}-${page}-${index}`, query));

test('public copy relationships survive seven other searches of twenty windows', async () => {
  const tools = new AcquisitionTools(async (operation, args) => {
    if (args.query === 'Solaris')
      return { books: [book(operation, 'Solaris', { md5: 'a'.repeat(32) })] };
    return { books: fullPage(args.query, args.page) };
  }, env);
  const paired = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  expect(paired.rows).toHaveLength(1);
  const copies = [paired.rows[0], ...paired.rows[0].other_copies];
  expect(copies.map((copy) => copy.source).sort()).toEqual(['libgen', 'zlibrary']);
  const selected = copies.find((copy) => copy.source === 'libgen').ref;
  const sibling = copies.find((copy) => copy.source === 'zlibrary').ref;
  for (let search = 0; search < 7; search++) {
    let page = await tools.call('search', {
      query: `deep-${search}`,
      sources: ['zlibrary'],
      limit: 50,
    });
    for (let window = 2; window <= 20; window++) {
      expect(page.next_cursor).toBeDefined();
      page = await tools.call('search', { cursor: page.next_cursor });
    }
    expect(page.next_cursor).toBeUndefined();
  }
  const detail = await tools.call('get_book', { ref: selected });
  expect(detail).toMatchObject({ title: 'Solaris', source: 'libgen' });
  expect(detail.other_copies).toEqual([
    expect.objectContaining({ ref: sibling, source: 'zlibrary' }),
  ]);
}, 30000);

test('public cursor cancellation exposes only source accounting keys for a retained refusal', async () => {
  let release;
  const refusal = new ToolFailure('breaker_open', 'search did not complete (breaker_open).', {
    source: 'libgen',
    retry: 'safe',
    retry_after_s: 7,
    detail: { reason: 'breaker_open', dependency: 'source_walk', request_issued: false },
  });
  const tools = new AcquisitionTools(
    async (_operation, args) => {
      if (args.page === 1) return { books: fullPage('Solaris', 1) };
      return new Promise((resolve) => {
        release = resolve;
      });
    },
    env,
    {
      localState: () => ({
        libgen: { breaker: 'open', breakerBlocked: true, breakerRetryAfterS: 7, refusal },
      }),
    },
  );
  const first = await tools.call('search', {
    query: 'Solaris',
    sources: ['libgen', 'zlibrary'],
    limit: 50,
  });
  expect(first.sources.libgen).toEqual({
    state: 'not_asked',
    reason: 'breaker_open',
    retry_after_s: 7,
  });
  const controller = new AbortController();
  const pending = tools.call(
    'search',
    { cursor: first.next_cursor },
    { signal: controller.signal },
  );
  await jest.advanceTimersByTimeAsync(0);
  controller.abort();
  try {
    const reply = await pending;
    expect(reply.error.code).toBe('cancelled');
    const line = reply.error.attempts.find((attempt) => attempt.source === 'libgen');
    expect(line).toMatchObject({
      source: 'libgen',
      state: 'not_asked',
      reason: 'breaker_open',
      retry_after_s: 7,
    });
    expect(Object.keys(line).sort()).toEqual(['reason', 'retry_after_s', 'source', 'state']);
  } finally {
    release({ books: fullPage('Solaris', 2) });
    await jest.advanceTimersByTimeAsync(0);
  }
});
