/**
 * MARC-8 mnemonics, as library cataloguing software writes them in some
 * catalogue records ("M{uml}uller-Brockmann"). Mirrors decode_marc_mnemonics
 * in lib/filename_utils.py. A combining mnemonic precedes the letter it
 * modifies, as in MARC; spacing ones stand for a letter. Unknown or
 * ill-placed mnemonics are left as is.
 */
const COMBINING: Record<string, string> = {
  uml: '̈',
  acute: '́',
  grave: '̀',
  circ: '̂',
  cedil: '̧',
  tilde: '̃',
  ring: '̊',
  caron: '̌',
  macr: '̄',
  breve: '̆',
};
const SPACING: Record<string, string> = {
  szlig: 'ß',
  eth: 'ð',
  thorn: 'þ',
  THORN: 'Þ',
  aelig: 'æ',
  AElig: 'Æ',
  oslash: 'ø',
  Oslash: 'Ø',
  oelig: 'œ',
  OElig: 'Œ',
  lstrok: 'ł',
  Lstrok: 'Ł',
  dstrok: 'đ',
  Dstrok: 'Đ',
};
const COMBINING_RUN = new RegExp(
  `((?:\\{(?:${Object.keys(COMBINING).join('|')})\\})+)(\\p{L})`,
  'gu',
);
const SPACING_ONE = new RegExp(`\\{(${Object.keys(SPACING).join('|')})\\}`, 'g');

export function decodeMarcMnemonics<T>(value: T): T {
  if (typeof value !== 'string' || !value.includes('{')) return value;
  return value
    .replace(COMBINING_RUN, (_, run: string, letter: string) =>
      (letter + [...run.matchAll(/\{(\w+)\}/g)].map((m) => COMBINING[m[1]!]).join('')).normalize(
        'NFC',
      ),
    )
    .replace(SPACING_ONE, (_, name: string) => SPACING[name]!) as T;
}
