import { expect, test } from '@jest/globals';
import { readFileSync } from 'node:fs';
import { normalizeLanguages } from '../dist/lib/filter-languages.js';

test.each([
  ['fr', 'french'], ['fra', 'french'], ['fre', 'french'], [' FrEnCh ', 'french'],
  ['en', 'english'], ['eng', 'english'], ['ENGLISH', 'english'],
  ['de', 'german'], ['deu', 'german'], ['ger', 'german'], ['Ger', 'german'],
  ['it', 'italian'], ['ita', 'italian'], ['es', 'spanish'], ['spa', 'spanish'],
  ['pt', 'portuguese'], ['por', 'portuguese'], ['ru', 'russian'], ['rus', 'russian'],
  ['zh', 'chinese'], ['zho', 'chinese'], ['chi', 'chinese'],
  ['ja', 'japanese'], ['jpn', 'japanese'], ['ko', 'korean'], ['kor', 'korean'],
  ['nl', 'dutch'], ['nld', 'dutch'], ['dut', 'dutch'],
  ['el', 'greek'], ['ell', 'greek'], ['gre', 'greek'],
  ['la', 'latin'], ['lat', 'latin'], ['uk', 'ukrainian'], ['ukr', 'ukrainian'],
  ['ar', 'arabic'], ['ara', 'arabic'], ['he', 'hebrew'], ['heb', 'hebrew'],
  ['AZERBAIJANI', 'azerbaijani'], [' WELSH ', 'welsh'],
  ['MALAGASY', 'malagasy'], ['NORWEGIAN_BOKMAL', 'norwegian_bokmal'],
])('%s becomes the native name %s', (input, expected) => {
  expect(normalizeLanguages([input])).toEqual({ values: [expected], unrecognized: [] });
});

test('unrecognized values are preserved and reported; aliases deduplicate without mutating input', () => {
  const input = Object.freeze(['fr', 'FRA', ' français ', 'klingon', '', 'fr-CA']);
  expect(normalizeLanguages(input)).toEqual({
    values: ['french', ' français ', 'klingon', 'fr-CA'],
    unrecognized: [' français ', 'klingon', 'fr-CA'],
  });
  expect(input[0]).toBe('fr');
  expect(normalizeLanguages([])).toEqual({ values: [], unrecognized: [] });
});

test('object property names cannot accidentally resolve as language aliases', () => {
  expect(normalizeLanguages(['constructor', '__proto__', 'toString'])).toEqual({
    values: ['constructor', '__proto__', 'toString'],
    unrecognized: ['constructor', '__proto__', 'toString'],
  });
});

test('every table target and alias resolves to an exact vendored Language value', () => {
  const source = readFileSync(new URL('../zlibrary/src/zlibrary/const.py', import.meta.url), 'utf8');
  const block = source.split('class Language(Enum):')[1].split(/^class /m)[0];
  const native = new Set([...block.matchAll(/^    [A-Z_]+ = "([^"]+)"/gm)].map(match => match[1]));
  const table = readFileSync(new URL('../src/lib/filter-languages.ts', import.meta.url), 'utf8')
    .split('const aliases')[0];
  const rows = [...table.matchAll(/^  \[([^\n]+)\],$/gm)]
    .map(match => [...match[1].matchAll(/'([^']+)'/g)].map(value => value[1]));
  expect(native.size).toBeGreaterThan(0);
  expect(new Set(rows.map(row => row[0]))).toEqual(native);
  for (const row of rows) {
    for (const input of row) {
      const result = normalizeLanguages([input]);
      expect(result.unrecognized).toEqual(row[0] === 'indigenous' ? [input] : []);
      expect(result.values).toEqual([row[0]]);
      expect(native.has(result.values[0])).toBe(true);
    }
  }
  for (const value of native) {
    expect(normalizeLanguages([value.toUpperCase()])).toEqual({ values: [value === 'indigenous' ? value.toUpperCase() : value], unrecognized: value === 'indigenous' ? [value.toUpperCase()] : [] });
  }
});

test('observed Traditional Chinese spelling maps to the existing native chinese target', () => {
  expect(normalizeLanguages(['Traditional chinese', ' TRADITIONAL CHINESE '])).toEqual({
    values: ['chinese'],
    unrecognized: [],
  });
});

test('blank language entries add no constraint or unrecognized value', () => {
  expect(normalizeLanguages(['', '  ', '\t\n'])).toEqual({ values: [], unrecognized: [] });
  expect(normalizeLanguages([' ', 'fr', ''])).toEqual({ values: ['french'], unrecognized: [] });
});

test.each([
  ['ca','catalan'], ['sl','slovenian'], ['et','estonian'], ['lt','lithuanian'], ['lv','latvian'],
  ['is','icelandic'], ['ga','irish'], ['cy','welsh'], ['eu','basque'], ['nb','norwegian'], ['nn','norwegian'],
])('issue 346: %s normalizes to %s', (input, native) => {
  expect(normalizeLanguages([input])).toEqual({ values: [native], unrecognized: [] });
});
test('equivalent language aliases are sent once', () => {
  expect(normalizeLanguages(['en', 'english', 'ENG', 'fr', 'french'])).toEqual({ values: ['english', 'french'], unrecognized: [] });
});

test('connector whole-table ISO fixture matches names to Anna bracket-extracted codes', async () => {
  const { checkRowFilters } = await import('../dist/lib/row-filters.js');
  const rows = JSON.parse(readFileSync(new URL('./fixtures/filter-language-codes.json', import.meta.url), 'utf8')).rows;
  expect(rows).toHaveLength(186);
  const nativeSource = readFileSync(new URL('../zlibrary/src/zlibrary/const.py', import.meta.url), 'utf8').split('class Language(Enum):')[1].split(/^class /m)[0];
  const native = [...nativeSource.matchAll(/^    [A-Z_]+ = "([^"]+)"/gm)].map(m => m[1]);
  expect(new Set([...rows.map(r => r[0]), 'indigenous'])).toEqual(new Set(native));
  for (const [name, ...codes] of rows) {
    expect(codes.length).toBeGreaterThan(0);
    for (const code of codes) {
      expect(checkRowFilters({ language: code }, { languages: [name] }).status).toBe('pass');
      expect(normalizeLanguages([code]).unrecognized).toEqual([]);
    }
  }
});
test('indigenous stays literal-only and is flagged, never assigned a guessed ISO code', () => {
  expect(normalizeLanguages(['indigenous'])).toEqual({ values: ['indigenous'], unrecognized: ['indigenous'] });
});
test('Traditional chinese is the observed spelling of the chinese alias', async () => {
  const { checkRowFilters } = await import('../dist/lib/row-filters.js');
  expect(normalizeLanguages(['Traditional chinese'])).toEqual({ values: ['chinese'], unrecognized: [] });
  expect(checkRowFilters({ language: 'Traditional chinese' }, { languages: ['zh'] }).status).toBe('pass');
});

test.each([
  ['central_khmer', 'Central khmer'],
  ['luba-katanga', 'Luba katanga'],
])('G2 spaced name %s is recognized as %s and matches spaced catalogue rows', async (native, spaced) => {
  const { checkRowFilters } = await import('../dist/lib/row-filters.js');
  expect(normalizeLanguages([spaced])).toEqual({ values: [native], unrecognized: [] });
  expect(normalizeLanguages([native, spaced])).toEqual({ values: [native], unrecognized: [] });
  expect(checkRowFilters({ language: spaced }, { languages: [native] }).status).toBe('pass');
  expect(checkRowFilters({ language: native }, { languages: [spaced] }).status).toBe('pass');
});

test('G2 every multi-word native name recognizes its spaced spelling and matches spaced rows', async () => {
  const { checkRowFilters } = await import('../dist/lib/row-filters.js');
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/filter-language-codes.json', import.meta.url), 'utf8'));
  const multiWord = fixture.rows.map(r => r[0]).filter(native => /[_-]/.test(native));
  expect(multiWord).toHaveLength(12);
  for (const native of multiWord) {
    const spaced = native.replace(/[_-]/g, ' ');
    expect(normalizeLanguages([spaced])).toEqual({ values: [native], unrecognized: [] });
    expect(checkRowFilters({ language: spaced }, { languages: [native] }).status).toBe('pass');
    expect(checkRowFilters({ language: native }, { languages: [spaced] }).status).toBe('pass');
  }
});
