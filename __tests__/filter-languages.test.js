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

test('unrecognized values are preserved and reported; aliases neither deduplicate nor mutate input', () => {
  const input = Object.freeze(['fr', 'FRA', ' français ', 'klingon', '', 'fr-CA']);
  expect(normalizeLanguages(input)).toEqual({
    values: ['french', 'french', ' français ', 'klingon', 'fr-CA'],
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
      expect(result.unrecognized).toEqual([]);
      expect(result.values).toEqual([row[0]]);
      expect(native.has(result.values[0])).toBe(true);
    }
  }
  for (const value of native) {
    expect(normalizeLanguages([value.toUpperCase()])).toEqual({ values: [value], unrecognized: [] });
  }
});

test('names without an enum counterpart remain unrecognized and are sent literally', () => {
  expect(normalizeLanguages(['Traditional chinese', ' TRADITIONAL CHINESE '])).toEqual({
    values: ['Traditional chinese', ' TRADITIONAL CHINESE '],
    unrecognized: ['Traditional chinese', ' TRADITIONAL CHINESE '],
  });
});

test('blank language entries add no constraint or unrecognized value', () => {
  expect(normalizeLanguages(['', '  ', '\t\n'])).toEqual({ values: [], unrecognized: [] });
  expect(normalizeLanguages([' ', 'fr', ''])).toEqual({ values: ['french'], unrecognized: [] });
});
