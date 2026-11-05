import { afterEach, expect, jest, test } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createServer } from '../dist/index.js';

const opened = [];
afterEach(async () => { for (const resource of opened.splice(0)) await resource.close(); });
async function fixture(invoke, env = {}) {
  const server = createServer();
  // Keep real SDK dispatch, routing, refs and filter checking. Replace external transport only.
  server.tools.invoke = invoke;
  server.tools.env = env;
  const client = new Client({ name: 'filters-fixture', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
  opened.push(client, server);
  return { server, client, call: async args => {
    const reply = await client.callTool({ name: 'search', arguments: args });
    expect(JSON.parse(reply.content[0].text)).toEqual(reply.structuredContent);
    const result = reply.structuredContent;
    if (result.filtering) {
      const keys = Object.keys(result.filtering.requested).sort();
      for (const attempt of result.routing.attempts) {
        if (['failed', 'skipped'].includes(attempt.outcome)) {
          expect(attempt.filtering).toBeUndefined();
          continue;
        }
        const f = attempt.filtering;
        const partition = [...f.applied, ...(f.native_only ?? []), ...(attempt.ignored ?? [])];
        expect(partition.sort()).toEqual(keys);
        expect(new Set(partition).size).toBe(keys.length);
        expect(f.examined).toBe(f.matched + f.rejected + f.unknown);
        expect(f.unknown_metadata).toBe(f.applied.length ? 'excluded' : undefined);
      }
    }
    return result;
  } };
}
const row = (title, extension, extra = {}) => ({ title, extension, md5: title.padEnd(32, '0'), ...extra });

test('LibGen formats filter the whole candidate window before limit, with .PDF and unknown counts', async () => {
  const invoke = jest.fn(async () => ({ books: [row('EPUB', 'epub'), row('Missing'), row('Dot PDF', '.PDF'), row('Late PDF', 'pdf')] }));
  const { call } = await fixture(invoke);
  const first = await call({ query: 'book', sources: ['libgen'], formats: [' PDF '], limit: 1 });
  expect(first.rows.map(r => r.title)).toEqual(['Dot PDF']);
  expect(first.filtering).toEqual({ requested: { formats: ['pdf'] } });
  expect(first.routing.attempts).toEqual([{
    source: 'libgen', outcome: 'rows', ignored: [], filtering: {
      applied: ['formats'], examined: 4, matched: 2, rejected: 1, unknown: 1,
      unknown_metadata: 'excluded', scope: 'first_upstream_page',
    },
  }]);
  const next = await call({ cursor: first.next_cursor });
  expect(next.rows.map(r => r.title)).toEqual(['Late PDF']);
  expect(invoke).toHaveBeenCalledTimes(1);
  expect((await call({ query: 'book', sources: ['libgen'], formats: ['mobi'] })).rows).toEqual([]);
});

test.each(['libgen', 'annas'])('%s checks language membership and inclusive readable years with disjoint counts', async source => {
  const { call } = await fixture(async () => ({ books: [
    row('French dated PDF', 'pdf', { language: 'English;French', year: '2014 Juin' }),
    row('Wrong language and missing year', 'pdf', { language: 'German' }),
    row('Double year', 'pdf', { language: 'French', year: '1988;2003' }),
    row('Range', 'pdf', { language: 'French', year: '1974 Enero―1974 Marzo' }),
    row('Zero', 'pdf', { language: 'French', year: 0 }),
    row('Sub-four digits', 'pdf', { language: 'French', year: 101 }),
    row('Above range', 'pdf', { language: 'French', year: 2015 }),
  ] }));
  const reply = await call({ query: 'book', sources: [source], languages: ['fr'], year_from: 2014, year_to: 2014, formats: ['.PDF'] });
  expect(reply.rows.map(r => [r.title, r.year])).toEqual([['French dated PDF', 2014]]);
  expect(reply.routing.attempts[0]).toEqual({ source, outcome: 'rows', ignored: [], filtering: {
    applied: ['year_from', 'year_to', 'languages', 'formats'],
    examined: 7, matched: 1, rejected: 2, unknown: 4, unknown_metadata: 'excluded', scope: 'first_upstream_page',
  } });
});

test('unrecognized language is echoed literally and mismatches lose rows visibly', async () => {
  const { call } = await fixture(async () => ({ books: [row('Literal', 'pdf', { language: 'français' }), row('Alias mismatch', 'pdf', { language: 'French' }), row('Unknown language', 'pdf')] }));
  const reply = await call({ query: 'book', sources: ['libgen'], languages: [' français '] });
  expect(reply.filtering).toEqual({ requested: { languages: [' français '] }, unrecognized: { languages: [' français '] } });
  expect(reply.rows.map(r => r.title)).toEqual(['Literal']);
  expect(reply.routing.attempts[0].filtering).toMatchObject({ examined: 3, matched: 1, rejected: 1, unknown: 1 });
});

const zenv = { ZLIBRARY_EMAIL: 'fixture@example.invalid', ZLIBRARY_PASSWORD: 'fixture-only' };
const zrow = (title, extra = {}) => ({ id: title, hash: 'fixture', title, source: 'zlibrary', ...extra });

test('Z-Library receives translated, deduplicated languages; local check overrides native claims', async () => {
  const calls = [];
  const { call } = await fixture(async (_op, args) => {
    calls.push(args);
    return { books: [zrow('French', { year: 2000, language: 'French', extension: 'epub' }), zrow('Wrong', { year: 2000, language: 'English', extension: 'epub' }), zrow('Missing', { language: 'French', extension: 'epub' })] };
  }, zenv);
  const reply = await call({ query: 'book', sources: ['zlibrary'], languages: ['fr', 'FRA', 'french'], formats: ['.EPUB'], year_from: 2000, limit: 3 });
  expect(calls[0]).toMatchObject({ languages: ['french'], extensions: ['epub'], count: 3, from_year: 2000 });
  expect(reply.rows.map(r => r.title)).toEqual(['French']);
  expect(reply.routing.attempts[0]).toEqual({ source: 'zlibrary', outcome: 'rows', ignored: [], filtering: {
    applied: ['year_from', 'languages', 'formats'],
    native_sent: { year_from: 2000, languages: ['french'], formats: ['epub'] },
    examined: 3, matched: 1, rejected: 1, unknown: 1, unknown_metadata: 'excluded', scope: 'page',
  } });
  expect(reply.next_cursor).toEqual(expect.any(String));
});

test('6.2 fixture 2: locally empty full Z page cannot lend its cursor to a short LibGen winner', async () => {
  const { call } = await fixture(async (_op, args) => ({ books: args.source === 'libgen' ? [row('Winner', 'pdf')] : [zrow('Mismatch1', { extension: 'epub' }), zrow('Mismatch2', { extension: 'epub' })] }), zenv);
  const reply = await call({ query: 'book', sources: ['zlibrary', 'libgen'], formats: ['pdf'], limit: 2 });
  expect(reply.rows.map(r => r.title)).toEqual(['Winner']);
  expect(reply.routing.served_by).toBe('libgen');
  expect(reply.next_cursor).toBeUndefined();
  expect(reply.routing.continuation_source).toBeUndefined();
});

test('6.2 fixtures 3-4: all empty retains Z onward pointer; continuation walks only Z and replays', async () => {
  const calls = [];
  const { call } = await fixture(async (op, args) => {
    calls.push([op, args.source, args.page]);
    return { books: args.source ? [] : [zrow('Mismatch', { extension: 'epub' })] };
  }, zenv);
  const first = await call({ query: 'book', sources: ['zlibrary', 'libgen', 'annas'], formats: ['pdf'], limit: 1 });
  expect(first.rows).toEqual([]);
  expect(first.routing.continuation_source).toBe('zlibrary');
  expect(first.routing.served_by).toBeUndefined();
  expect(first.next_cursor).toEqual(expect.any(String));
  const next = await call({ cursor: first.next_cursor });
  expect(await call({ cursor: first.next_cursor })).toEqual(next);
  expect(calls).toEqual([['search', undefined, 1], ['search_multi_source', 'libgen', undefined], ['search_multi_source', 'annas', undefined], ['search', undefined, 2], ['search', undefined, 2]]);
  expect(next.filtering).toEqual(first.filtering);
});

test('a later held-list winner owns its continuation; refs are minted only for displayed rows', async () => {
  const { server, call } = await fixture(async (_op, args) => ({ books: args.source === 'libgen' ? [row('Rejected', 'epub'), row('One', 'pdf'), row('Two', 'pdf')] : [zrow('Mismatch', { extension: 'epub' })] }), zenv);
  const mint = jest.spyOn(server.tools.refs, 'mintStable');
  const first = await call({ query: 'book', sources: ['zlibrary', 'libgen'], formats: ['pdf'], limit: 1 });
  expect(mint.mock.calls.filter(c => c[0] === 'r').map(c => c[1].book.title)).toEqual(['One']);
  expect(first.routing.continuation_source).toBe('libgen');
  const next = await call({ cursor: first.next_cursor });
  expect(next.rows.map(r => r.title)).toEqual(['Two']);
  expect(next.next_cursor).toBeUndefined();
  expect(next.routing.attempts[0].filtering).toEqual(first.routing.attempts[1].filtering);
});

test('exact preflight skips unsupported sources before configuration or invoke and names recovery', async () => {
  const invoke = jest.fn();
  const { server, call } = await fixture(invoke);
  const configured = jest.spyOn(server.tools, 'configured');
  const reply = await call({ query: 'book', sources: ['libgen', 'annas'], exact: true });
  expect(reply.error).toMatchObject({ code: 'unsupported_operation', attempts: [
    { source: 'libgen', outcome: 'skipped', code: 'unsupported_operation', reason: 'filter_unsupported', unsupported: ['exact'] },
    { source: 'annas', outcome: 'skipped', code: 'unsupported_operation', reason: 'filter_unsupported', unsupported: ['exact'] },
  ] });
  expect(reply.error.recovery).toMatch(/zlibrary.*drop exact/i);
  expect(configured).not.toHaveBeenCalled();
  expect(invoke).not.toHaveBeenCalled();
  for (const a of reply.error.attempts) expect(a.filtering).toBeUndefined();
});

test('exact falls through to Z as native_only, with metadata applied and native transmission separate', async () => {
  const invoke = jest.fn(async () => ({ books: [zrow('French PDF', { language: 'French', extension: 'pdf' })] }));
  const { call } = await fixture(invoke, zenv);
  const reply = await call({ query: 'book', sources: ['libgen', 'zlibrary'], exact: true, languages: ['fr'], formats: ['pdf'] });
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(reply.rows).toHaveLength(1);
  expect(reply.routing.fell_back).toBe(true);
  expect(reply.routing.attempts[0].unsupported).toEqual(['exact']);
  expect(reply.routing.attempts[1]).toMatchObject({ ignored: [], filtering: {
    native_sent: { exact: true, languages: ['french'], formats: ['pdf'] },
    applied: ['languages', 'formats'], native_only: ['exact'],
  } });
  expect(reply.filtering.requested).toEqual({ exact: true, languages: ['fr'], formats: ['pdf'] });
  expect(reply.note).toMatch(/sent to Z-Library, not checked by this server/);
});

test('exact-only declares native_only and counts, without unknown_metadata or applied predicates', async () => {
  const { call } = await fixture(async () => ({ books: [zrow('Book')] }), zenv);
  const reply = await call({ query: 'book', sources: ['zlibrary'], exact: true });
  expect(reply.filtering).toEqual({ requested: { exact: true } });
  expect(reply.routing.attempts[0].filtering).toEqual({
    native_sent: { exact: true }, native_only: ['exact'], applied: [],
    examined: 1, matched: 1, rejected: 0, unknown: 0, scope: 'page',
  });
});

test('exact skip plus missing Z credentials is all_sources_failed, with no counts', async () => {
  const invoke = jest.fn();
  const { call } = await fixture(invoke);
  const reply = await call({ query: 'book', sources: ['libgen', 'zlibrary'], exact: true });
  expect(reply.error.code).toBe('all_sources_failed');
  expect(reply.error.attempts.map(a => a.code)).toEqual(['unsupported_operation', 'credentials_missing']);
  expect(invoke).not.toHaveBeenCalled();
  for (const attempt of reply.error.attempts) expect(attempt.filtering).toBeUndefined();
});

test('exact skip plus Z answered empty remains success, but timeout plus empty remains partial_failure', async () => {
  const { call } = await fixture(async () => ({ books: [] }), zenv);
  const empty = await call({ query: 'book', sources: ['libgen', 'zlibrary'], exact: true });
  expect(empty.rows).toEqual([]);
  expect(empty.routing.attempts.map(a => a.outcome)).toEqual(['skipped', 'empty']);
  const failed = await fixture(async (_op, args) => { if (args.source === 'libgen') throw { context: { reason: 'timeout' } }; return { books: [] }; }, zenv);
  const reply = await failed.call({ query: 'book', sources: ['libgen', 'zlibrary'], formats: ['pdf'] });
  expect(reply.error.code).toBe('partial_failure');
  expect(reply.error.attempts[0].filtering).toBeUndefined();
  expect(reply.error.attempts[1].filtering.examined).toBe(0);
});

test('false exact, empty arrays and blank lists add no filtering contract', async () => {
  const { call } = await fixture(async () => ({ books: [row('Unfiltered')] }));
  const reply = await call({ query: 'book', sources: ['libgen'], exact: false, languages: [' ', ''], formats: [], limit: 1 });
  expect(reply.rows).toHaveLength(1);
  expect(reply.filtering).toBeUndefined();
  expect(reply.routing.attempts[0].filtering).toBeUndefined();
  expect(reply.routing.attempts[0].ignored).toEqual([]);
});

test('local get_limits reports filter capabilities without bridge contact', async () => {
  const invoke = jest.fn();
  const { client } = await fixture(invoke);
  const limits = (await client.callTool({ name: 'get_limits', arguments: {} })).structuredContent;
  for (const source of ['libgen', 'annas', 'zlibrary']) {
    expect(limits.sources[source].filters).toEqual({
      year_from: 'local', year_to: 'local', languages: 'local', formats: 'local',
      exact: source === 'zlibrary' ? 'native_only' : 'unsupported',
    });
  }
  expect(invoke).not.toHaveBeenCalled();
});

test('format grammar strips exactly one leading dot from the original request and row', async () => {
  const { call } = await fixture(async () => ({ books: [row('Two dots', '..PDF'), row('One dot', '.pdf'), row('No dot', 'pdf')] }));
  const reply = await call({ query: 'book', sources: ['libgen'], formats: ['..PDF'] });
  expect(reply.rows.map(r => r.title)).toEqual(['Two dots']);
  expect(reply.routing.attempts[0].filtering).toMatchObject({ examined: 3, matched: 1, rejected: 2, unknown: 0 });
});
