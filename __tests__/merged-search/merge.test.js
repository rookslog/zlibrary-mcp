import { expect, test } from '@jest/globals';
import { mergeRows, normalizeText } from '../../dist/lib/merge.js';
const md5 = 'abcdef0123456789abcdef0123456789';
const copy = (source, id, extra = {}) => ({
  source, id, ref: `r-${source}-${id}`, rank: 1,
  metadata: { title: 'Solaris', author: 'Stanisław Lem', format: 'pdf', size_mb: 3, language: 'Polish' },
  ...extra,
});
const merge = (copies, request = {}, options = {}) => mergeRows(copies, { query: 'Solaris', ...request }, options);

test('F20 mixed-case MD5 groups copies and preserves both source-bound refs without leaking identity', () => {
  const a = copy('libgen', 'a', { md5 });
  const b = copy('annas', 'b', { md5: md5.toUpperCase() });
  const rows = merge([a, b], {}, { routeOrder: ['libgen', 'annas'] });
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ ref: a.ref, source: 'libgen', other_copies: [{ ref: b.ref, source: 'annas' }], route_basis: 'operator_order' });
  expect(rows[0]).not.toHaveProperty('md5');
  expect(rows[0]).not.toHaveProperty('id');
});
test('F21 no MD5 stays separate and likely editions get adjacency without hiding rows', () => {
  const rows = merge([copy('libgen', 'a', { md5 }), copy('zlibrary', 'b')]);
  expect(rows).toHaveLength(2);
  expect(rows[0].likely_group).toBeDefined();
  expect(rows[0].likely_group).toBe(rows[1].likely_group);
});
test('F22 same format and known size yield a hint only; known pages and MD5 conflicts prevent it', () => {
  const a = copy('libgen', 'a', { md5 });
  const b = copy('zlibrary', 'b');
  const rows = merge([a, b]);
  expect(rows[1].likely_same_file_as).toBe(rows[0].ref);
  for (const changed of [
    { metadata: { ...b.metadata, size_mb: undefined } },
    { metadata: { ...b.metadata, format: 'epub' } },
    { md5: '0'.repeat(32) },
  ]) expect(merge([a, { ...b, ...changed }]).every(r => !r.likely_same_file_as)).toBe(true);
  expect(merge([{ ...a, metadata: { ...a.metadata, pages: 10 } }, { ...b, metadata: { ...b.metadata, pages: 11 } }]).every(r => !r.likely_same_file_as)).toBe(true);
});
test('F23 Z-Library MD5 joins through the same identity path; invalid MD5s never merge', () => {
  expect(merge([copy('libgen', 'a', { md5 }), copy('zlibrary', 'b', { md5 })])).toHaveLength(1);
  for (const invalid of ['', 'opaque', 'x'.repeat(32), 'a'.repeat(31)])
    expect(merge([copy('libgen', 'a', { md5: invalid }), copy('zlibrary', 'b', { md5: invalid })])).toHaveLength(2);
});
test('F24 differing known metadata is listed while the recommended metadata stays its own', () => {
  const a = copy('libgen', 'a', { md5 });
  const b = copy('annas', 'b', { md5, metadata: { ...a.metadata, language: 'French', publisher: 'Other' } });
  const [row] = merge([a, b], {}, { routeOrder: ['libgen'] });
  expect(row.differs).toEqual(['language']);
  expect(row.language).toBe('Polish');
  expect(row).not.toHaveProperty('publisher');
});
test('F25 any passing copy admits a file, all copies remain reachable, and only passing copies recommend', () => {
  const a = copy('libgen', 'a', { md5, filterStatus: 'pass' });
  const b = copy('annas', 'b', { md5, filterStatus: 'unknown', metadata: { title: 'Solaris' } });
  const before = JSON.stringify([a, b]);
  const [row] = merge([a, b], {}, { filtersActive: true, routeOrder: ['annas', 'libgen'] });
  expect(row.ref).toBe(a.ref);
  expect(row.other_copies).toEqual([{ ref: b.ref, source: 'annas' }]);
  expect(JSON.stringify([a, b])).toBe(before);
  expect(merge([b], {}, { filtersActive: true })).toEqual([]);
  expect(merge([copy('annas', 'f', { filterStatus: 'fail' })], {}, { filtersActive: true })).toEqual([]);
});
test('F26 different or missing authors never get likely groups; subtitles share an exact key', () => {
  const a = copy('libgen', 'a');
  for (const author of ['Other', undefined, '']) {
    const rows = merge([a, copy('zlibrary', 'b', { metadata: { title: 'Solaris', author } })]);
    expect(rows.every(r => !r.likely_group)).toBe(true);
  }
  const rows = merge([a, copy('zlibrary', 'b', { metadata: { title: 'Solaris — A novel', author: 'Stanisław Lem' } })]);
  expect(rows[0].likely_group).toBe(rows[1].likely_group);
});
test('F28 match class outranks source rank and checks author separately', () => {
  const rows = merge([
    copy('libgen', 'terms', { rank: 1, metadata: { title: 'On Solaris', author: 'Stanisław Lem' } }),
    copy('zlibrary', 'exact', { rank: 9, metadata: { title: 'Solaris: A novel', author: 'Stanisław Lem' } }),
    copy('annas', 'other', { metadata: { title: 'Solaris', author: 'Other' } }),
  ], { author: 'Lem' });
  expect(rows.map(r => [r.ref, r.match])).toEqual([
    ['r-zlibrary-exact', 'exact_title'], ['r-libgen-terms', 'all_terms'], ['r-annas-other', 'source_only'],
  ]);
});
test('F29 repetition adds nothing; best pre-filter source rank wins', () => {
  const repeated = ['libgen', 'annas', 'zlibrary'].map(s => copy(s, s, { md5, rank: 3 }));
  const rare = copy('libgen', 'rare', { rank: 2, metadata: { title: 'Solaris', author: 'Another' } });
  expect(merge([...repeated, rare])[0].ref).toBe(rare.ref);
  expect(merge([{ ...repeated[0], rank: 1 }, rare, ...repeated.slice(1)])[0].other_copies).toHaveLength(2);
});
test('F30 format preference only reorders a likely group at its best member position; filters exclude PDF', () => {
  const epub = copy('libgen', 'epub', { rank: 1, metadata: { title: 'Solaris', author: 'Lem', format: 'epub' } });
  const other = copy('libgen', 'other', { rank: 2, metadata: { title: 'Solaris', author: 'Other' } });
  const pdf = copy('annas', 'pdf', { rank: 9, metadata: { ...epub.metadata, format: 'pdf' } });
  expect(merge([other, epub, pdf], {}, { formatPreference: ['pdf'] }).map(r => r.ref)).toEqual([pdf.ref, epub.ref, other.ref]);
  expect(merge([{ ...pdf, filterStatus: 'fail' }, { ...epub, filterStatus: 'pass' }], {}, { filtersActive: true, formatPreference: ['pdf'] }).map(r => r.ref)).toEqual([epub.ref]);
});
test('F32 stable hashes make row order and route ties independent of arrival and repetition', () => {
  const inputs = [copy('libgen', 'a'), copy('annas', 'b', { metadata: { title: 'Solaris', author: 'Other' } }), copy('zlibrary', 'c', { md5 }), copy('annas', 'd', { md5 })];
  const before = JSON.stringify(inputs);
  const expected = merge(inputs);
  expect(expected.find(r => r.other_copies).route_basis).toBe('stable_tie');
  for (const order of [inputs.toReversed(), [inputs[2], inputs[0], inputs[3], inputs[1]]]) expect(merge(order)).toEqual(expected);
  expect(JSON.stringify(inputs)).toBe(before);
});
test('F33 NFC case folding and punctuation retain accents, so unaccented queries are source_only', () => {
  expect(normalizeText('  STRAẞE—Σς: Cafe\u0301! ')).toBe('strasse σσ café');
  expect(merge([copy('libgen', 'a', { metadata: { title: 'Être et temps', author: 'Heidegger' } })], { query: 'Etre et temps' })[0].match).toBe('source_only');
});
test('route choice respects usability before operator preference and exposes unavailable alternatives', () => {
  const a = copy('libgen', 'a', { md5 });
  for (const extra of [{ downloadAvailable: false }, { quotaRemaining: 0 }]) {
    const b = copy('annas', 'b', { md5, ...extra });
    const [row] = merge([a, b], {}, { routeOrder: ['annas', 'libgen'] });
    expect(row.ref).toBe(a.ref);
    expect(row.route_basis).toBe('only_usable');
  }
  const [row] = merge([a, copy('annas', 'b', { md5, downloadAvailable: false })]);
  expect(row.other_copies[0].download_available).toBe(false);
});
