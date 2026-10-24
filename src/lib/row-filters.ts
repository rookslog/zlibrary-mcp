import { normalizeLanguages } from './filter-languages.js';

export type FilterStatus = 'pass' | 'fail' | 'unknown';
type FilterKey = 'year_from' | 'year_to' | 'languages' | 'formats';
export interface FilterCheck {
  status: FilterStatus;
  reason: string;
}
export interface RowFilterRequest {
  year_from?: number;
  year_to?: number;
  languages?: readonly string[];
  formats?: readonly string[];
}
export interface CatalogueFields {
  year?: unknown;
  language?: unknown;
  extension?: unknown;
}
export interface RowFilterResult {
  status: FilterStatus;
  year: number | null;
  checks: Partial<Record<FilterKey, FilterCheck>>;
  unrecognized: { languages: string[] };
}

// Accept listed date words in English, French, Italian, Spanish, German and Portuguese; other suffixes stay unknown.
const dateWords = new Set(
  [
    'january february march april may june july august september october november december',
    'janvier février mars avril mai juin juillet août septembre octobre novembre décembre',
    'gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre',
    'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre',
    'januar februar märz april mai juni juli august september oktober november dezember',
    'janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro',
    'spring summer autumn fall winter printemps été automne hiver primavera estate autunno inverno',
    'verano otoño invierno frühling sommer herbst outono verão',
    'q1 q2 q3 q4 quarter trimestre quartal',
  ].flatMap((words) => words.split(' ')),
);

function parseYear(raw: unknown): { year: number | null; reason: string } {
  let value: number;
  if (typeof raw === 'number') value = raw;
  else if (typeof raw === 'string') {
    const text = raw.normalize('NFC').trim();
    if (/^[0-9]+$/u.test(text)) value = Number(text);
    else {
      const date = /^([0-9]{4})\s+([\p{L}0-9]+)(?:\s+([0-9]{1,2}))?$/u.exec(text);
      if (
        !date ||
        !dateWords.has(date[2]!.toLowerCase()) ||
        (date[3] !== undefined && (Number(date[3]) < 1 || Number(date[3]) > 31))
      )
        return { year: null, reason: 'year_missing_or_ambiguous' };
      value = Number(date[1]);
    }
  } else return { year: null, reason: 'year_missing_or_ambiguous' };
  if (!Number.isSafeInteger(value) || value <= 0)
    return { year: null, reason: 'year_missing_or_ambiguous' };
  // Years below 1000 stay unknown until the allowed year domain is defined.
  if (value < 1000) return { year: null, reason: 'year_domain_unsettled' };
  return { year: value, reason: 'year_readable' };
}

const normalizeFormat = (value: string): string => value.normalize('NFC').trim().toLowerCase().replace(/^\./u, '');
const languageKey = (value: string): string =>
  normalizeLanguages([value]).values[0]!.normalize('NFC').trim().toLowerCase();

/** Evaluate catalogue metadata only; callers decide what to do with unknown rows.
 * Empty lists and blank entries add no check. Known failure outranks unknown in the AND result.
 * Unfamiliar language strings match literally, with case/whitespace normalized.
 */
export function checkRowFilters(
  row: CatalogueFields,
  request: RowFilterRequest,
): RowFilterResult {
  for (const bound of [request.year_from, request.year_to])
    if (bound !== undefined && !Number.isInteger(bound))
      throw new RangeError('Year bounds must be integers.');
  if (
    request.year_from !== undefined &&
    request.year_to !== undefined &&
    request.year_from > request.year_to
  )
    throw new RangeError('year_from must not exceed year_to.');

  const checks: RowFilterResult['checks'] = {};
  const parsed = parseYear(row.year);
  if (request.year_from !== undefined)
    checks.year_from =
      parsed.year === null
        ? { status: 'unknown', reason: parsed.reason }
        : parsed.year >= request.year_from
          ? { status: 'pass', reason: 'year_at_or_above_minimum' }
          : { status: 'fail', reason: 'year_below_minimum' };
  if (request.year_to !== undefined)
    checks.year_to =
      parsed.year === null
        ? { status: 'unknown', reason: parsed.reason }
        : parsed.year <= request.year_to
          ? { status: 'pass', reason: 'year_at_or_below_maximum' }
          : { status: 'fail', reason: 'year_above_maximum' };

  const languages = normalizeLanguages(request.languages ?? []);
  if (languages.values.length) {
    const components =
      typeof row.language === 'string'
        ? row.language.split(';').map((value) => value.trim()).filter(Boolean)
        : [];
    checks.languages = !components.length
      ? { status: 'unknown', reason: 'language_missing_or_invalid' }
      : components.some((component) => languages.values.some((wanted) => languageKey(component) === languageKey(wanted)))
        ? { status: 'pass', reason: 'language_matches' }
        : { status: 'fail', reason: 'language_mismatch' };
  }
  const formats = (request.formats ?? []).filter((value) => value.trim().length > 0).map(normalizeFormat);
  if (formats.length) {
    const format = typeof row.extension === 'string' ? normalizeFormat(row.extension) : '';
    checks.formats = !format
      ? { status: 'unknown', reason: 'format_missing_or_invalid' }
      : formats.includes(format)
        ? { status: 'pass', reason: 'format_matches' }
        : { status: 'fail', reason: 'format_mismatch' };
  }
  const statuses = Object.values(checks).map((check) => check.status);
  return {
    status: statuses.includes('fail') ? 'fail' : statuses.includes('unknown') ? 'unknown' : 'pass',
    year: parsed.year,
    checks,
    unrecognized: { languages: languages.unrecognized },
  };
}
