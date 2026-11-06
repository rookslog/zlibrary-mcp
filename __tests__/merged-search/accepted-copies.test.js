import { expect, test } from '@jest/globals';
import { mergeRows } from '../../dist/lib/merge.js';
import { checkMergedRowFilters } from '../../dist/lib/row-filters.js';
const digest = 'a'.repeat(32);
const request = { query: 'Solaris', languages: ['fr'], formats: ['pdf'], year_from: 2000 };
const copy = (id, metadata, extra = {}, filters = request) => ({
  ref: `r-${id}`, source: id, id, rank: 9, md5: digest, metadata,
  filterResult: checkMergedRowFilters({ ...metadata, extension: metadata.format }, filters), ...extra,
});
const passed = { title: 'On Solaris', author: 'Lem', language: 'French', format: 'pdf', year: 2001, publisher: 'A' };

test('M05 alternatives show their own pass/fail/unknown evidence and all real conflicts remain in differs', () => {
  const a = copy('passing', passed);
  const b = copy('failed', { ...passed, language: 'English', format: 'epub', year: 1990, publisher: 'B' });
  const c = copy('unknown', { title: 'Solaris' });
  const d = copy('also-pass', passed);
  const [row] = mergeRows([a, b, c, d], request, { filtersActive: true, routeOrder: ['passing'] });
  expect(row.ref).toBe(a.ref);
  expect(row.other_copies).toEqual([
    { ref: d.ref, source: d.source, filter: 'pass' },
    { ref: b.ref, source: b.source, filter: 'fail', language: 'English', format: 'epub', year: 1990 },
    { ref: c.ref, source: c.source, filter: 'unknown', unknown: ['year', 'language', 'format'] },
  ]);
  expect(row.differs).toEqual(['title', 'year', 'language', 'format', 'publisher']);
  expect(row.language).toBe('French');
  expect(row.format).toBe('pdf');
  expect(mergeRows([b, c], request, { filtersActive: true })).toEqual([]);
  expect(mergeRows([a, b], request)[0].other_copies[0]).not.toHaveProperty('filter');
});

test('M05 unavailable passing copies still recommend before contradictory usable alternatives', () => {
  for (const flags of [{ downloadAvailable: false }, { quotaRemaining: 0 }, {}]) {
    const a = copy('passing', passed, flags);
    const b = copy('failed', { ...passed, language: 'English' });
    const [row] = mergeRows([b, a], request, { filtersActive: true, routeOrder: ['failed'] });
    expect(row.ref).toBe(a.ref);
    expect(row.other_copies[0]).toMatchObject({ ref: b.ref, source: b.source, filter: 'fail', language: 'English' });
  }
});

test('M15 rejected rank/title/source cannot improve a displayed file match or best rank', () => {
  const a = copy('passing', passed);
  const rival = copy('rival', { ...passed, author: 'Other' }, { md5: 'b'.repeat(32), rank: 2 });
  const base = mergeRows([a, rival], request, { filtersActive: true });
  for (const source of ['annas', 'zlibrary', 'libgen']) {
    const b = copy(source, { ...passed, title: 'Solaris', language: 'English' }, { rank: 1 });
    const rows = mergeRows([a, rival, b], request, { filtersActive: true });
    expect(rows.map(row => [row.ref, row.match])).toEqual(base.map(row => [row.ref, row.match]));
    expect(rows[0].ref).toBe(rival.ref);
    expect(rows[1].match).toBe('all_terms');
  }
});

test('M15 author-only and accent-preserving exact evidence remains tied to the accepted copy', () => {
  const filters = { author: 'Lem', exact: true, languages: ['fr'] };
  const a = copy('passing', { ...passed, title: 'Être', author: 'Stanisław Lem' }, {}, filters);
  const b = copy('failed', { ...passed, title: 'Etre', author: 'Other', language: 'English' }, { rank: 1 }, filters);
  const [row] = mergeRows([b, a], filters, { filtersActive: true });
  expect(row.match).toBe('all_terms');
  expect(row.other_copies).toEqual([{ ref: b.ref, source: b.source, filter: 'fail', language: 'English', author: 'Other' }]);
  const exact = { query: 'Être', author: 'Lem', exact: true };
  const good = copy('good', { title: 'Être', author: 'Lem' }, {}, exact);
  const missing = copy('missing', { title: 'Être' }, {}, exact);
  expect(mergeRows([good, missing], exact, { filtersActive: true })[0].other_copies[0]).toMatchObject({ filter: 'unknown', unknown: ['author'] });
});

test('M05 M15 both copies passing an OR filter still disclose their filtered-field disagreements', () => {
  const filters = { query: 'Solaris', languages: ['fr', 'en'], formats: ['pdf', 'epub'] };
  const a = copy('a', { ...passed, title: 'Solaris' }, {}, filters);
  const b = copy('b', { ...passed, title: 'Solaris', language: 'English', format: 'epub' }, {}, filters);
  const [row] = mergeRows([a, b], filters, { filtersActive: true, routeOrder: ['a', 'b'] });
  expect(row.differs).toEqual(['language', 'format']);
  expect(row.other_copies).toEqual([{ ref: b.ref, source: b.source, filter: 'pass' }]);
  expect(row.language).toBe('French');
});
