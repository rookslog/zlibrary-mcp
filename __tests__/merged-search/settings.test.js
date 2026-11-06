import { expect, test } from '@jest/globals';
import { parseSearchSettings } from '../../dist/lib/search-settings.js';

test('S8a unset settings retain optional intent and format inputs with a 12-second wait', () => {
  expect(parseSearchSettings({})).toEqual({ waitMs: 12000 });
  expect(parseSearchSettings({ BOOK_SOURCE_DEFAULT_ORDER: '' })).toEqual({ waitMs: 12000 });
});
test('S8a parses typed operator intent and format order into immutable snapshots', () => {
  const env = { BOOK_SOURCE_DEFAULT_ORDER: 'zlibrary, libgen', BOOK_SOURCE_SEARCH_WAIT_MS: ' 12001 ', BOOK_FORMAT_PREFERENCE: ' .PDF, EPUB ' };
  const result = parseSearchSettings(env);
  expect(result).toEqual({ operatorSources: ['zlibrary', 'libgen'], waitMs: 12001, formatPreference: ['pdf', 'epub'] });
  env.BOOK_FORMAT_PREFERENCE = 'mobi'; expect(result.formatPreference).toEqual(['pdf', 'epub']);
  expect(() => result.operatorSources.push('annas')).toThrow();
  expect(Object.isFrozen(result)).toBe(true);
});
test.each(['0', '-1', '1.5', '1e3', '12ms', '', ' ', '2147483648'])('S8a rejects malformed timer input %p only when the pure parser is called', raw => {
  expect(() => parseSearchSettings({ BOOK_SOURCE_SEARCH_WAIT_MS: raw })).toThrow(/BOOK_SOURCE_SEARCH_WAIT_MS/);
});
test('S8a accepts the timer boundaries without Node overflow', () => {
  expect(parseSearchSettings({ BOOK_SOURCE_SEARCH_WAIT_MS: '1' }).waitMs).toBe(1);
  expect(parseSearchSettings({ BOOK_SOURCE_SEARCH_WAIT_MS: '2147483647' }).waitMs).toBe(2147483647);
});
test.each(['pdf,pdf', '.PDF,pdf', '', ' ', 'pdf,,epub', '..pdf', 'pdf/epub'])('S8a rejects ambiguous format preference %p', raw => {
  expect(() => parseSearchSettings({ BOOK_FORMAT_PREFERENCE: raw })).toThrow(/BOOK_FORMAT_PREFERENCE/);
});
test.each(['libgen,libgen', 'held', 'LIBGEN', 'libgen,', ' '])('S8a validates explicit operator intent %p against registered names', raw => {
  expect(() => parseSearchSettings({ BOOK_SOURCE_DEFAULT_ORDER: raw })).toThrow(/BOOK_SOURCE_DEFAULT_ORDER/);
});
test('S8a an injected source list supports a fourth source without public registration', () => {
  expect(parseSearchSettings({ BOOK_SOURCE_DEFAULT_ORDER: 'held,libgen' }, ['libgen', 'held']).operatorSources).toEqual(['held', 'libgen']);
});
