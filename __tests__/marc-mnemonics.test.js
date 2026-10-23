import { expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { decodeMarcMnemonics } from '../dist/lib/marc-mnemonics.js';

// Mirrors __tests__/python/test_marc_mnemonics.py: the same table on the display side.
const env = { ZLIBRARY_EMAIL: 'offline@example.invalid', ZLIBRARY_PASSWORD: 'password' };
const GRID_AUTHOR = 'M{uml}uller-Brockmann, Josef, 1914-';

test.each([
  [GRID_AUTHOR, 'Müller-Brockmann, Josef, 1914-'],
  ['f{uml}ur', 'für'],
  ['Andr{acute}e', 'André'],
  ['tr{grave}es', 'très'],
  ['h{circ}otel', 'hôtel'],
  ['Fran{cedil}cois', 'François'],
  ['Espa{tilde}na', 'España'],
  ['{ring}Angstr{uml}om', 'Ångström'],
  ['{caron}Capek', 'Čapek'],
  ['Stra{szlig}e', 'Straße'],
  ['{eth}a', 'ða'],
  ['{thorn}orn', 'þorn'],
  ['{aelig}on', 'æon'],
  ['{AElig}lfric', 'Ælfric'],
  ['S{oslash}ren', 'Søren'],
  ['{Oslash}stergaard', 'Østergaard'],
  // Unknown or ill-placed (no following letter) stay as is.
  ['{unknown}x', '{unknown}x'],
  ['x{uml}', 'x{uml}'],
  ['{uml} a', '{uml} a'],
])('decodes %s', (raw, decoded) => {
  expect(decodeMarcMnemonics(raw)).toBe(decoded);
});

test('search rows display decoded catalogue strings; the ref keeps the raw record', async () => {
  const book = {
    id: '123484509',
    hash: 'e21523',
    title: 'Raster systeme f{uml}ur die visuelle Gestaltung',
    author: GRID_AUTHOR,
    extension: 'pdf',
  };
  const invoke = jest.fn().mockResolvedValue({ books: [book] });
  const tools = new AcquisitionTools(invoke, env);
  const result = await tools.call('search', { query: 'grid', sources: ['zlibrary'] });
  expect(result.rows[0]).toMatchObject({
    title: 'Raster systeme für die visuelle Gestaltung',
    author: 'Müller-Brockmann, Josef, 1914-',
  });
});
