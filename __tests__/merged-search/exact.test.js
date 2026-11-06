import { expect, test } from '@jest/globals';
import { checkMergedRowFilters, MERGED_FILTER_CAPABILITIES, checkRowFilters, FILTER_CAPABILITIES } from '../../dist/lib/row-filters.js';

test.each(['libgen', 'annas', 'zlibrary'])('F34 %s exact checks title and author as separate phrases with subtitles', source => {
  expect(MERGED_FILTER_CAPABILITIES[source].exact).toBe('local');
  const result = checkMergedRowFilters({ title: 'Spectres de Marx: L’état de la dette', author: 'Jacques Derrida' }, { query: 'SPECTRES de Marx', author: 'Derrida', exact: true });
  expect(result.status).toBe('pass');
  expect(result.checks.exact.status).toBe('pass');
  const unknown = checkMergedRowFilters({ title: 'Spectres de Marx' }, { query: 'Spectres de Marx', author: 'Derrida', exact: true });
  expect(unknown.status).toBe('unknown');
  expect(unknown.checks.exact.reason).toBe('exact_metadata_missing');
});
test('F34 phrase checks reject noncontiguous terms, cross-field matches and partial words', () => {
  for (const [row, request] of [
    [{ title: 'Spectres and Marx' }, { query: 'Spectres Marx' }],
    [{ title: 'Solaris', author: 'Lem' }, { query: 'Solaris Lem' }],
    [{ title: 'Solaris', author: 'Lemuel' }, { query: 'Solaris', author: 'Lem' }],
  ]) expect(checkMergedRowFilters(row, { ...request, exact: true }).status).toBe('fail');
});
test('F34 missing requested title is unknown; author-only requests need no title; failure beats unknown', () => {
  expect(checkMergedRowFilters({ author: 'Lem' }, { query: 'Solaris', exact: true }).status).toBe('unknown');
  expect(checkMergedRowFilters({ author: 'Stanisław Lem' }, { author: 'Lem', exact: true }).status).toBe('pass');
  expect(checkMergedRowFilters({ title: 'Wrong' }, { query: 'Solaris', author: 'Lem', exact: true }).status).toBe('fail');
  expect(checkMergedRowFilters({ year: 1999 }, { query: 'Solaris', exact: true, year_from: 2000 }).status).toBe('fail');
});
test('F34 normalized exact retains accents, folds case and punctuation, and preserves catalogue filters', () => {
  expect(checkMergedRowFilters({ title: 'Die Straße: Ein Buch', author: 'Cafe\u0301', extension: 'pdf' }, { query: 'DIE STRASSE', author: 'Café', formats: ['pdf'], exact: true })).toMatchObject({ status: 'pass', checks: { exact: { status: 'pass' }, formats: { status: 'pass' } } });
  expect(checkMergedRowFilters({ title: 'Être et temps' }, { query: 'Etre et temps', exact: true }).status).toBe('fail');
});
test('S2 deferred integration keeps legacy predicates and capability reporting unchanged', () => {
  expect(FILTER_CAPABILITIES.libgen.exact).toBe('unsupported');
  expect(FILTER_CAPABILITIES.zlibrary.exact).toBe('native_only');
  expect(checkRowFilters({ title: 'Wrong' }, { query: 'Solaris', exact: true }).checks).toEqual({});
  for (const exact of [undefined, false]) {
    expect(checkMergedRowFilters({}, { query: 'Solaris', exact })).toEqual(checkRowFilters({}, {}));
  }
});
