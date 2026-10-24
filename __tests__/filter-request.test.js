import { afterEach, expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { logger } from '../dist/lib/logger.js';

const env = { ZLIBRARY_EMAIL: 'offline@example.invalid', ZLIBRARY_PASSWORD: 'offline' };
afterEach(() => jest.restoreAllMocks());

test('Z-Library receives native language names on first page and continuation without gating rows', async () => {
  const calls = [];
  const tools = new AcquisitionTools(async (operation, args) => {
    calls.push({ operation, args });
    // Deliberate mismatch: the checker must not yet be wired into replies.
    return { books: [{ id: '1', hash: 'abc', title: 'Unfiltered', language: 'English', extension: 'pdf', source: 'zlibrary' }] };
  }, env);
  const first = await tools.call('search', {
    query: 'book', sources: ['zlibrary', 'libgen'], languages: ['fr', 'FRA', 'fre', ' fReNcH '], limit: 1,
  });
  expect(calls[0].args.languages).toEqual(['french', 'french', 'french', 'french']);
  expect(first.rows).toHaveLength(1);
  expect(first.rows[0]).toMatchObject({ title: 'Unfiltered', language: 'English', format: 'pdf' });
  expect(first.routing.attempts).toEqual([{ source: 'zlibrary', outcome: 'rows' }]);
  expect(first.filtering).toBeUndefined();
  await tools.call('search', { cursor: first.next_cursor });
  expect(calls.map(c => c.args.languages)).toEqual([
    ['french', 'french', 'french', 'french'], ['french', 'french', 'french', 'french'],
  ]);
  expect(calls.map(c => [c.operation, c.args.page])).toEqual([['search', 1], ['search', 2]]);
});

test('unknown languages are forwarded literally and reported without changing replies', async () => {
  const warning = jest.spyOn(logger, 'warn').mockImplementation(() => {});
  let sent;
  const tools = new AcquisitionTools(async (_op, args) => {
    sent = args.languages;
    return { books: [] };
  }, env);
  const reply = await tools.call('search', { query: 'book', sources: ['zlibrary'], languages: ['fr', ' français ', 'klingon'] });
  expect(sent).toEqual(['french', ' français ', 'klingon']);
  expect(warning).toHaveBeenCalledWith(expect.stringContaining('unrecognized'), JSON.stringify([' français ', 'klingon']));
  expect(reply.rows).toEqual([]);
  expect(reply.routing.attempts).toEqual([{ source: 'zlibrary', outcome: 'empty' }]);
  expect(reply.filtering).toBeUndefined();
});

test('non-Z search still returns its original unfiltered rows and ignored filters', async () => {
  const warning = jest.spyOn(logger, 'warn').mockImplementation(() => {});
  let sent;
  const tools = new AcquisitionTools(async (_op, args) => {
    sent = args;
    return { books: [{ md5: 'a'.repeat(32), title: 'English PDF', language: 'English', extension: 'pdf', source: 'libgen' }] };
  }, {});
  const reply = await tools.call('search', { query: 'book', sources: ['libgen'], languages: ['fr', 'klingon'], formats: ['epub'] });
  expect(sent).toEqual({ query: 'book', source: 'libgen', count: 50 });
  expect(reply.rows[0]).toMatchObject({ title: 'English PDF', language: 'English', format: 'pdf' });
  expect(reply.routing.attempts).toEqual([{ source: 'libgen', outcome: 'rows', ignored: ['languages', 'formats'] }]);
  expect(warning).not.toHaveBeenCalled();
});

test('blank request languages add no native constraint or diagnostic', async () => {
  const warning = jest.spyOn(logger, 'warn').mockImplementation(() => {});
  const calls = [];
  const tools = new AcquisitionTools(async (_operation, args) => {
    calls.push(args.languages);
    return { books: [] };
  }, env);
  await tools.call('search', { query: 'book', sources: ['zlibrary'], languages: ['  ', '', '\t'] });
  await tools.call('search', { query: 'book', sources: ['zlibrary'], languages: [' ', 'fr', ''] });
  expect(calls).toEqual([[], ['french']]);
  expect(warning).not.toHaveBeenCalled();
});
