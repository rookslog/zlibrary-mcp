import { expect, test } from '@jest/globals';
import { checkRowFilters } from '../dist/lib/row-filters.js';

// Read listed date words; treat ranges, double years and unlisted suffixes as unknown.
test.each([
  [2000, 2000], [2010, 2010], ['2001', 2001], [' 2001 ', 2001],
  ['2014 Juin', 2014], ['2001 Gennaio 29', 2001], ['1968 Dezembro', 1968],
  ['1975 Januar 01', 1975], ['2001 October', 2001], ['1974 Enero', 1974],
  ['2001 Spring', 2001], ['2001 été', 2001], ['2001 Q4', 2001],
])('readable catalogue year %s yields %s', (raw, year) => {
  const result = checkRowFilters({ year: raw }, { year_from: year, year_to: year });
  expect(result.year).toBe(year);
  expect(result.status).toBe('pass');
  expect(result.checks).toMatchObject({ year_from: { status: 'pass' }, year_to: { status: 'pass' } });
});

test.each([
  '1974 Enero–1974 Marzo', '1974 Enero―1974 Marzo', '1974-1974', '1988;2003',
  '2001 circa', 'c. 2001', '2001 UnlistedWord', '2001 January extra',
  '2001 January 00', '2001 January 32', '2001 January 2.5',
  0, '', '0', '0000 January', null, undefined, -2001, 2001.5, NaN, Infinity,
  true, [], {}, '2001.0', '2e3', '2001/2002', '2001-10-01',
])('ambiguous/missing catalogue year %s stays unknown', (year) => {
  const result = checkRowFilters({ year, title: 'Published 2001' }, { year_from: 1900, year_to: 2020 });
  expect(result.year).toBeNull();
  expect(result.status).toBe('unknown');
  expect(result.checks.year_from.status).toBe('unknown');
  expect(result.checks.year_to.status).toBe('unknown');
  expect(result.checks.year_from.reason.length).toBeGreaterThan(0);
});

test.each([2, 101, '2', '0101', '0101 January'])('unsettled sub-four-digit year %s has an explicit reason', year => {
  expect(checkRowFilters({ year }, { year_from: 1 }).checks.year_from)
    .toEqual({ status: 'unknown', reason: 'year_domain_unsettled' });
});

test('year bounds are independently inclusive, with known mismatch dominating unknown metadata', () => {
  const result = checkRowFilters({ year: 1999 }, { year_from: 2000, year_to: 2010, languages: ['fr'] });
  expect(result.status).toBe('fail');
  expect(result.checks).toEqual({
    year_from: { status: 'fail', reason: 'year_below_minimum' },
    year_to: { status: 'pass', reason: 'year_at_or_below_maximum' },
    languages: { status: 'unknown', reason: 'language_missing_or_invalid' },
  });
  expect(checkRowFilters({ year: 2011 }, { year_to: 2010 }).checks.year_to.status).toBe('fail');
});

test.each([
  ['English;French', ['fr'], 'pass'], ['English;French', ['en'], 'pass'],
  [' english ; FRE ; ', ['fra'], 'pass'], ['Ger', ['deu'], 'pass'],
  ['Traditional chinese', ['TRADITIONAL CHINESE'], 'pass'],
  ['English;French', ['de', 'es'], 'fail'], ['English', ['de', 'en'], 'pass'],
  [' Klingon ', ['klingon'], 'pass'], ['français', ['FRANÇAIS'], 'pass'],
  ['français', ['fr'], 'fail'], ['', ['fr'], 'unknown'],
  [' ; ; ', ['fr'], 'unknown'], [null, ['fr'], 'unknown'],
  [17, ['fr'], 'unknown'], [['French'], ['fr'], 'unknown'],
])('catalogue language %s versus %s => %s', (language, languages, status) => {
  const result = checkRowFilters({ language }, { languages });
  expect(result.checks.languages.status).toBe(status);
  expect(result.checks.languages.reason.length).toBeGreaterThan(0);
});

test.each([
  ['EPUB', ['.epub'], 'pass'], [' .PDF ', ['epub', 'PDF'], 'pass'],
  ['azw3', ['mobi', 'azw'], 'fail'], ['..pdf', ['pdf'], 'fail'],
  ['.', ['pdf'], 'unknown'], ['', ['epub'], 'unknown'],
  [null, ['epub'], 'unknown'], [false, ['epub'], 'unknown'],
])('catalogue format %s versus %s => %s', (extension, formats, status) => {
  const result = checkRowFilters({ extension }, { formats });
  expect(result.checks.formats.status).toBe(status);
  expect(result.checks.formats.reason.length).toBeGreaterThan(0);
});

test('empty lists add no predicates; the pure checker preserves its inputs and returns literal diagnostics', () => {
  const row = Object.freeze({ year: '2001 Gennaio 29', language: 'English;French', extension: '.EPUB' });
  const request = Object.freeze({ languages: Object.freeze(['fr', 'klingon']), formats: Object.freeze(['epub']) });
  expect(checkRowFilters(row, request)).toMatchObject({
    status: 'pass', year: 2001, unrecognized: { languages: ['klingon'] },
  });
  expect(row.year).toBe('2001 Gennaio 29');
  expect(request.languages).toEqual(['fr', 'klingon']);
  expect(checkRowFilters({}, { languages: [], formats: [] })).toEqual({
    status: 'pass', year: null, checks: {}, unrecognized: { languages: [] },
  });
});

test('AND across filters preserves a real failure beside unknown and passing checks', () => {
  const result = checkRowFilters({ language: 'French', extension: 'pdf' }, {
    year_from: 2000, languages: ['fr'], formats: ['epub'],
  });
  expect(result.status).toBe('fail');
  expect(result.checks).toMatchObject({
    year_from: { status: 'unknown' }, languages: { status: 'pass' }, formats: { status: 'fail' },
  });
});

test.each([{ year_from: 2010, year_to: 2000 }, { year_from: NaN }, { year_to: 1.5 }])(
  'invalid internal year requests %s are rejected instead of silently passing', request => {
    expect(() => checkRowFilters({ year: 2001 }, request)).toThrow(RangeError);
  },
);

test.each([
  { languages: ['  '] }, { languages: [''] }, { formats: [''] },
  { formats: [' \t '] }, { languages: ['', '  '], formats: [''] },
])('blank entries are no constraint even with missing catalogue fields: %s', request => {
  expect(checkRowFilters({}, request)).toEqual({
    status: 'pass', year: null, checks: {}, unrecognized: { languages: [] },
  });
});

test('blank entries leave real language and format constraints intact', () => {
  const request = { languages: [' ', 'fr'], formats: ['', ' .PDF '] };
  expect(checkRowFilters({ language: 'French', extension: 'pdf' }, request).status).toBe('pass');
  expect(checkRowFilters({ language: 'German', extension: 'pdf' }, request).status).toBe('fail');
  expect(checkRowFilters({ language: 'French', extension: 'epub' }, request).status).toBe('fail');
  expect(checkRowFilters({}, request).checks).toMatchObject({
    languages: { status: 'unknown' }, formats: { status: 'unknown' },
  });
});

test.each([
  [{ year: '2001 fe\u0301vrier', language: 'franc\u0327ais', extension: '.E\u0301PUB' }, { year_from: 2001, languages: ['français'], formats: ['épub'] }],
  [{ year: '2001 février', language: 'français', extension: '.ÉPUB' }, { year_from: 2001, languages: ['franc\u0327ais'], formats: ['e\u0301pub'] }],
])('NFC-equivalent row and request text matches: %s', (row, request) => {
  expect(checkRowFilters(row, request)).toMatchObject({
    status: 'pass', year: 2001,
    checks: { year_from: { status: 'pass' }, languages: { status: 'pass' }, formats: { status: 'pass' } },
  });
});
