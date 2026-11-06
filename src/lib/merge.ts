import { languageKey } from './filter-languages.js';
import { createHash } from 'node:crypto';
import type { FilterStatus, RowFilterResult, FilterCheck } from './row-filters.js';

export interface MergeMetadata {
  title?: string;
  author?: string;
  year?: number;
  language?: string;
  format?: string;
  size_mb?: number;
  pages?: number;
  publisher?: string;
  also_available_on?: unknown;
}
/** Adapter-owned identity and rank never appear on display rows. */
export interface MergeCopy {
  ref: string;
  source: string;
  id: string;
  rank: number;
  md5?: string;
  metadata: MergeMetadata;
  /** Complete predicate evidence from checkMergedRowFilters. */
  filterResult: Pick<RowFilterResult, 'status' | 'checks'>;
  downloadAvailable?: boolean;
  quotaRemaining?: number;
}
export interface MergeRequest {
  query?: string;
  author?: string;
}
export interface MergeOptions {
  /** Optional consistency assertion; activation is derived from predicate evidence. */
  filtersActive?: boolean;
  routeOrder?: readonly string[];
  formatPreference?: readonly string[];
}
export type MatchClass = 'exact_title' | 'all_terms' | 'source_only';
export interface CopyRef {
  ref: string;
  source: string;
  download_available?: false;
  filter?: FilterStatus;
  unknown?: string[];
  year?: number;
  language?: string;
  format?: string;
  title?: string;
  author?: string;
}
export interface MergedRow extends MergeMetadata, CopyRef {
  match: MatchClass;
  other_copies?: CopyRef[];
  route_basis?: 'only_usable' | 'operator_order' | 'stable_tie';
  differs?: string[];
  likely_group?: string;
  likely_same_file_as?: string;
}

// Full default Unicode case folding exceptions to simple lowercase (generated
// from Unicode CaseFolding data via Python str.casefold). Accents are retained.
const caseFolds: Record<string, string> = {
  '\u00b5': '\u03bc',
  '\u00df': 'ss',
  '\u0149': '\u02bcn',
  '\u017f': 's',
  '\u01f0': 'j\u030c',
  '\u0345': '\u03b9',
  '\u0390': '\u03b9\u0308\u0301',
  '\u03b0': '\u03c5\u0308\u0301',
  '\u03c2': '\u03c3',
  '\u03d0': '\u03b2',
  '\u03d1': '\u03b8',
  '\u03d5': '\u03c6',
  '\u03d6': '\u03c0',
  '\u03f0': '\u03ba',
  '\u03f1': '\u03c1',
  '\u03f5': '\u03b5',
  '\u0587': '\u0565\u0582',
  '\u13a0': '\u13a0',
  '\u13a1': '\u13a1',
  '\u13a2': '\u13a2',
  '\u13a3': '\u13a3',
  '\u13a4': '\u13a4',
  '\u13a5': '\u13a5',
  '\u13a6': '\u13a6',
  '\u13a7': '\u13a7',
  '\u13a8': '\u13a8',
  '\u13a9': '\u13a9',
  '\u13aa': '\u13aa',
  '\u13ab': '\u13ab',
  '\u13ac': '\u13ac',
  '\u13ad': '\u13ad',
  '\u13ae': '\u13ae',
  '\u13af': '\u13af',
  '\u13b0': '\u13b0',
  '\u13b1': '\u13b1',
  '\u13b2': '\u13b2',
  '\u13b3': '\u13b3',
  '\u13b4': '\u13b4',
  '\u13b5': '\u13b5',
  '\u13b6': '\u13b6',
  '\u13b7': '\u13b7',
  '\u13b8': '\u13b8',
  '\u13b9': '\u13b9',
  '\u13ba': '\u13ba',
  '\u13bb': '\u13bb',
  '\u13bc': '\u13bc',
  '\u13bd': '\u13bd',
  '\u13be': '\u13be',
  '\u13bf': '\u13bf',
  '\u13c0': '\u13c0',
  '\u13c1': '\u13c1',
  '\u13c2': '\u13c2',
  '\u13c3': '\u13c3',
  '\u13c4': '\u13c4',
  '\u13c5': '\u13c5',
  '\u13c6': '\u13c6',
  '\u13c7': '\u13c7',
  '\u13c8': '\u13c8',
  '\u13c9': '\u13c9',
  '\u13ca': '\u13ca',
  '\u13cb': '\u13cb',
  '\u13cc': '\u13cc',
  '\u13cd': '\u13cd',
  '\u13ce': '\u13ce',
  '\u13cf': '\u13cf',
  '\u13d0': '\u13d0',
  '\u13d1': '\u13d1',
  '\u13d2': '\u13d2',
  '\u13d3': '\u13d3',
  '\u13d4': '\u13d4',
  '\u13d5': '\u13d5',
  '\u13d6': '\u13d6',
  '\u13d7': '\u13d7',
  '\u13d8': '\u13d8',
  '\u13d9': '\u13d9',
  '\u13da': '\u13da',
  '\u13db': '\u13db',
  '\u13dc': '\u13dc',
  '\u13dd': '\u13dd',
  '\u13de': '\u13de',
  '\u13df': '\u13df',
  '\u13e0': '\u13e0',
  '\u13e1': '\u13e1',
  '\u13e2': '\u13e2',
  '\u13e3': '\u13e3',
  '\u13e4': '\u13e4',
  '\u13e5': '\u13e5',
  '\u13e6': '\u13e6',
  '\u13e7': '\u13e7',
  '\u13e8': '\u13e8',
  '\u13e9': '\u13e9',
  '\u13ea': '\u13ea',
  '\u13eb': '\u13eb',
  '\u13ec': '\u13ec',
  '\u13ed': '\u13ed',
  '\u13ee': '\u13ee',
  '\u13ef': '\u13ef',
  '\u13f0': '\u13f0',
  '\u13f1': '\u13f1',
  '\u13f2': '\u13f2',
  '\u13f3': '\u13f3',
  '\u13f4': '\u13f4',
  '\u13f5': '\u13f5',
  '\u13f8': '\u13f0',
  '\u13f9': '\u13f1',
  '\u13fa': '\u13f2',
  '\u13fb': '\u13f3',
  '\u13fc': '\u13f4',
  '\u13fd': '\u13f5',
  '\u1c80': '\u0432',
  '\u1c81': '\u0434',
  '\u1c82': '\u043e',
  '\u1c83': '\u0441',
  '\u1c84': '\u0442',
  '\u1c85': '\u0442',
  '\u1c86': '\u044a',
  '\u1c87': '\u0463',
  '\u1c88': '\ua64b',
  '\u1e96': 'h\u0331',
  '\u1e97': 't\u0308',
  '\u1e98': 'w\u030a',
  '\u1e99': 'y\u030a',
  '\u1e9a': 'a\u02be',
  '\u1e9b': '\u1e61',
  '\u1e9e': 'ss',
  '\u1f50': '\u03c5\u0313',
  '\u1f52': '\u03c5\u0313\u0300',
  '\u1f54': '\u03c5\u0313\u0301',
  '\u1f56': '\u03c5\u0313\u0342',
  '\u1f80': '\u1f00\u03b9',
  '\u1f81': '\u1f01\u03b9',
  '\u1f82': '\u1f02\u03b9',
  '\u1f83': '\u1f03\u03b9',
  '\u1f84': '\u1f04\u03b9',
  '\u1f85': '\u1f05\u03b9',
  '\u1f86': '\u1f06\u03b9',
  '\u1f87': '\u1f07\u03b9',
  '\u1f88': '\u1f00\u03b9',
  '\u1f89': '\u1f01\u03b9',
  '\u1f8a': '\u1f02\u03b9',
  '\u1f8b': '\u1f03\u03b9',
  '\u1f8c': '\u1f04\u03b9',
  '\u1f8d': '\u1f05\u03b9',
  '\u1f8e': '\u1f06\u03b9',
  '\u1f8f': '\u1f07\u03b9',
  '\u1f90': '\u1f20\u03b9',
  '\u1f91': '\u1f21\u03b9',
  '\u1f92': '\u1f22\u03b9',
  '\u1f93': '\u1f23\u03b9',
  '\u1f94': '\u1f24\u03b9',
  '\u1f95': '\u1f25\u03b9',
  '\u1f96': '\u1f26\u03b9',
  '\u1f97': '\u1f27\u03b9',
  '\u1f98': '\u1f20\u03b9',
  '\u1f99': '\u1f21\u03b9',
  '\u1f9a': '\u1f22\u03b9',
  '\u1f9b': '\u1f23\u03b9',
  '\u1f9c': '\u1f24\u03b9',
  '\u1f9d': '\u1f25\u03b9',
  '\u1f9e': '\u1f26\u03b9',
  '\u1f9f': '\u1f27\u03b9',
  '\u1fa0': '\u1f60\u03b9',
  '\u1fa1': '\u1f61\u03b9',
  '\u1fa2': '\u1f62\u03b9',
  '\u1fa3': '\u1f63\u03b9',
  '\u1fa4': '\u1f64\u03b9',
  '\u1fa5': '\u1f65\u03b9',
  '\u1fa6': '\u1f66\u03b9',
  '\u1fa7': '\u1f67\u03b9',
  '\u1fa8': '\u1f60\u03b9',
  '\u1fa9': '\u1f61\u03b9',
  '\u1faa': '\u1f62\u03b9',
  '\u1fab': '\u1f63\u03b9',
  '\u1fac': '\u1f64\u03b9',
  '\u1fad': '\u1f65\u03b9',
  '\u1fae': '\u1f66\u03b9',
  '\u1faf': '\u1f67\u03b9',
  '\u1fb2': '\u1f70\u03b9',
  '\u1fb3': '\u03b1\u03b9',
  '\u1fb4': '\u03ac\u03b9',
  '\u1fb6': '\u03b1\u0342',
  '\u1fb7': '\u03b1\u0342\u03b9',
  '\u1fbc': '\u03b1\u03b9',
  '\u1fbe': '\u03b9',
  '\u1fc2': '\u1f74\u03b9',
  '\u1fc3': '\u03b7\u03b9',
  '\u1fc4': '\u03ae\u03b9',
  '\u1fc6': '\u03b7\u0342',
  '\u1fc7': '\u03b7\u0342\u03b9',
  '\u1fcc': '\u03b7\u03b9',
  '\u1fd2': '\u03b9\u0308\u0300',
  '\u1fd3': '\u03b9\u0308\u0301',
  '\u1fd6': '\u03b9\u0342',
  '\u1fd7': '\u03b9\u0308\u0342',
  '\u1fe2': '\u03c5\u0308\u0300',
  '\u1fe3': '\u03c5\u0308\u0301',
  '\u1fe4': '\u03c1\u0313',
  '\u1fe6': '\u03c5\u0342',
  '\u1fe7': '\u03c5\u0308\u0342',
  '\u1ff2': '\u1f7c\u03b9',
  '\u1ff3': '\u03c9\u03b9',
  '\u1ff4': '\u03ce\u03b9',
  '\u1ff6': '\u03c9\u0342',
  '\u1ff7': '\u03c9\u0342\u03b9',
  '\u1ffc': '\u03c9\u03b9',
  '\uab70': '\u13a0',
  '\uab71': '\u13a1',
  '\uab72': '\u13a2',
  '\uab73': '\u13a3',
  '\uab74': '\u13a4',
  '\uab75': '\u13a5',
  '\uab76': '\u13a6',
  '\uab77': '\u13a7',
  '\uab78': '\u13a8',
  '\uab79': '\u13a9',
  '\uab7a': '\u13aa',
  '\uab7b': '\u13ab',
  '\uab7c': '\u13ac',
  '\uab7d': '\u13ad',
  '\uab7e': '\u13ae',
  '\uab7f': '\u13af',
  '\uab80': '\u13b0',
  '\uab81': '\u13b1',
  '\uab82': '\u13b2',
  '\uab83': '\u13b3',
  '\uab84': '\u13b4',
  '\uab85': '\u13b5',
  '\uab86': '\u13b6',
  '\uab87': '\u13b7',
  '\uab88': '\u13b8',
  '\uab89': '\u13b9',
  '\uab8a': '\u13ba',
  '\uab8b': '\u13bb',
  '\uab8c': '\u13bc',
  '\uab8d': '\u13bd',
  '\uab8e': '\u13be',
  '\uab8f': '\u13bf',
  '\uab90': '\u13c0',
  '\uab91': '\u13c1',
  '\uab92': '\u13c2',
  '\uab93': '\u13c3',
  '\uab94': '\u13c4',
  '\uab95': '\u13c5',
  '\uab96': '\u13c6',
  '\uab97': '\u13c7',
  '\uab98': '\u13c8',
  '\uab99': '\u13c9',
  '\uab9a': '\u13ca',
  '\uab9b': '\u13cb',
  '\uab9c': '\u13cc',
  '\uab9d': '\u13cd',
  '\uab9e': '\u13ce',
  '\uab9f': '\u13cf',
  '\uaba0': '\u13d0',
  '\uaba1': '\u13d1',
  '\uaba2': '\u13d2',
  '\uaba3': '\u13d3',
  '\uaba4': '\u13d4',
  '\uaba5': '\u13d5',
  '\uaba6': '\u13d6',
  '\uaba7': '\u13d7',
  '\uaba8': '\u13d8',
  '\uaba9': '\u13d9',
  '\uabaa': '\u13da',
  '\uabab': '\u13db',
  '\uabac': '\u13dc',
  '\uabad': '\u13dd',
  '\uabae': '\u13de',
  '\uabaf': '\u13df',
  '\uabb0': '\u13e0',
  '\uabb1': '\u13e1',
  '\uabb2': '\u13e2',
  '\uabb3': '\u13e3',
  '\uabb4': '\u13e4',
  '\uabb5': '\u13e5',
  '\uabb6': '\u13e6',
  '\uabb7': '\u13e7',
  '\uabb8': '\u13e8',
  '\uabb9': '\u13e9',
  '\uabba': '\u13ea',
  '\uabbb': '\u13eb',
  '\uabbc': '\u13ec',
  '\uabbd': '\u13ed',
  '\uabbe': '\u13ee',
  '\uabbf': '\u13ef',
  '\ufb00': 'ff',
  '\ufb01': 'fi',
  '\ufb02': 'fl',
  '\ufb03': 'ffi',
  '\ufb04': 'ffl',
  '\ufb05': 'st',
  '\ufb06': 'st',
  '\ufb13': '\u0574\u0576',
  '\ufb14': '\u0574\u0565',
  '\ufb15': '\u0574\u056b',
  '\ufb16': '\u057e\u0576',
  '\ufb17': '\u0574\u056d',
};
export function normalizeText(value: unknown): string {
  if (typeof value !== 'string') return '';
  return [...value.normalize('NFC')]
    .map((char) => caseFolds[char] ?? char.toLowerCase())
    .join('')
    .normalize('NFC')
    .replace(/\p{P}/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim();
}
const titleStem = (title: unknown): string =>
  normalizeText(typeof title === 'string' ? title.split(/[:\p{Pd}]/u)[0] : '');
const hash = (...parts: unknown[]): string =>
  createHash('sha256').update(JSON.stringify(parts)).digest('hex');
const compare = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);
const md5Of = (copy: MergeCopy): string | undefined =>
  typeof copy.md5 === 'string' && /^[a-f0-9]{32}$/iu.test(copy.md5)
    ? copy.md5.toLowerCase()
    : undefined;
const phrase = (field: string, wanted: string): boolean =>
  !wanted || ` ${field} `.includes(` ${wanted} `);
function matchClass(copy: MergeCopy, request: MergeRequest): MatchClass {
  const title = normalizeText(copy.metadata.title);
  const author = normalizeText(copy.metadata.author);
  const query = normalizeText(request.query);
  const wantedAuthor = normalizeText(request.author);
  if (
    query &&
    (title === query || titleStem(copy.metadata.title) === query) &&
    phrase(author, wantedAuthor)
  )
    return 'exact_title';
  const terms = [query, wantedAuthor].filter(Boolean).flatMap((text) => text.split(' '));
  const words = new Set(`${title} ${author}`.split(' '));
  return terms.length && terms.every((term) => words.has(term)) ? 'all_terms' : 'source_only';
}
const classes: Record<MatchClass, number> = { exact_title: 0, all_terms: 1, source_only: 2 };
const fields = [
  'title',
  'author',
  'year',
  'language',
  'format',
  'size_mb',
  'pages',
  'publisher',
  'also_available_on',
] as const;
const known = (value: unknown): boolean => value !== undefined && value !== null && value !== '';
/** Comparison never rewrites the raw metadata selected for display. */
const comparisonValue = (field: (typeof fields)[number], value: unknown): string =>
  JSON.stringify(
    typeof value === 'string'
      ? field === 'language' && value.trim()
        ? languageKey(value)
        : normalizeText(value)
      : value,
  );
const usable = (copy: MergeCopy): boolean =>
  copy.downloadAvailable !== false && copy.quotaRemaining !== 0;
const copyRef = (copy: MergeCopy): CopyRef => ({
  ref: copy.ref,
  source: copy.source,
  ...(!usable(copy) ? { download_available: false as const } : {}),
});
interface Ranked {
  row: MergedRow;
  fit: number;
  rank: number;
  tie: string;
  md5?: string;
}
const compareRanked = (a: Ranked, b: Ranked): number =>
  a.fit - b.fit || a.rank - b.rank || compare(a.tie, b.tie) || compare(a.row.ref, b.row.ref);

/** Validate complete evidence before any group can produce a row. */
function filterActivation(copies: readonly MergeCopy[], asserted?: boolean): boolean {
  const invalid = (): never => {
    throw new TypeError('Every merge copy requires complete filterResult evidence.');
  };
  let active = false;
  for (const copy of copies) {
    const result = copy.filterResult;
    if (
      !result ||
      typeof result !== 'object' ||
      Array.isArray(result) ||
      !result.checks ||
      typeof result.checks !== 'object' ||
      Array.isArray(result.checks)
    )
      invalid();
    const entries = Object.entries(result.checks) as [string, FilterCheck][];
    if (
      entries.some(
        ([key, check]) =>
          !['year_from', 'year_to', 'languages', 'formats', 'exact'].includes(key) ||
          !check ||
          !['pass', 'fail', 'unknown'].includes(check.status) ||
          typeof check.reason !== 'string',
      )
    )
      invalid();
    const derived = entries.some(([, check]) => check.status === 'fail')
      ? 'fail'
      : entries.some(([, check]) => check.status === 'unknown')
        ? 'unknown'
        : 'pass';
    if (result.status !== derived) invalid();
    if (entries.length) active = true;
  }
  if (copies.length && asserted !== undefined && asserted !== active)
    throw new RangeError('filtersActive disagrees with predicate evidence.');
  return active;
}

/** Pure, deterministic merge. S4 owns filtering counts, ref storage and paging.
 * filterResult records every applied predicate; rejected copies remain
 * reachable only when a copy with the same valid MD5 passed.
 */
export function mergeRows(
  copies: readonly MergeCopy[],
  request: MergeRequest,
  options: MergeOptions = {},
): MergedRow[] {
  const filtersActive = filterActivation(copies, options.filtersActive);
  const groups = new Map<string, MergeCopy[]>();
  for (const copy of copies) {
    const md5 = md5Of(copy);
    const identity = md5 ? `md5:${md5}` : JSON.stringify([copy.source, copy.id]);
    const key = md5 ? identity : JSON.stringify([identity, copy.ref]);
    const group = groups.get(key) ?? [];
    group.push(copy);
    groups.set(key, group);
  }
  const filterFields = (copy: MergeCopy, status?: FilterStatus): string[] => {
    const names = new Set<string>();
    for (const [key, check] of Object.entries(copy.filterResult.checks)) {
      if (!check || (status && check.status !== status)) continue;
      if (key === 'exact') {
        for (const [field, wanted] of [
          ['title', request.query],
          ['author', request.author],
        ] as const) {
          if (!normalizeText(wanted)) continue;
          const actual = normalizeText(copy.metadata[field]);
          const outcome = !actual
            ? 'unknown'
            : phrase(actual, normalizeText(wanted))
              ? 'pass'
              : 'fail';
          if (!status || outcome === status) names.add(field);
        }
      } else names.add(key === 'formats' ? 'format' : key === 'languages' ? 'language' : 'year');
    }
    return [...names];
  };
  const alternate = (copy: MergeCopy): CopyRef => {
    const ref = copyRef(copy);
    if (!filtersActive) return ref;
    const status = copy.filterResult.status;
    return {
      ...ref,
      filter: status,
      ...(status === 'fail'
        ? Object.fromEntries(
            filterFields(copy, 'fail').map((field) => [
              field,
              copy.metadata[field as keyof MergeMetadata],
            ]),
          )
        : {}),
      ...(status === 'unknown' ? { unknown: filterFields(copy, 'unknown') } : {}),
    };
  };
  const ranked: Ranked[] = [];
  for (const group of groups.values()) {
    const passing = filtersActive
      ? group.filter((copy) => copy.filterResult.status === 'pass')
      : group;
    if (!passing.length) continue;
    const usableCopies = passing.filter(usable);
    const candidates = usableCopies.length ? usableCopies : passing;
    const md5 = md5Of(group[0]!);
    const routeIndex = (copy: MergeCopy): number => {
      const index = options.routeOrder?.indexOf(copy.source) ?? -1;
      return index < 0 ? Infinity : index;
    };
    const ordered = [...candidates].sort((a, b) => {
      const delta = routeIndex(a) - routeIndex(b);
      return (
        (Number.isNaN(delta) ? 0 : delta) ||
        compare(hash(md5, a.source), hash(md5, b.source)) ||
        compare(a.id, b.id) ||
        compare(a.ref, b.ref)
      );
    });
    const recommended = ordered[0]!;
    // Whitelist display metadata so raw adapter fields cannot escape.
    const metadata = Object.fromEntries(
      fields
        .filter((field) => known(recommended.metadata[field]))
        .map((field) => [field, recommended.metadata[field]]),
    );
    const match = [...passing]
      .map((copy) => matchClass(copy, request))
      .sort((a, b) => classes[a] - classes[b])[0]!;
    const row: MergedRow = { ...metadata, ...copyRef(recommended), match };
    const others = group
      .filter((copy) => copy !== recommended)
      .sort((a, b) => compare(a.source, b.source) || compare(a.id, b.id) || compare(a.ref, b.ref));
    if (others.length) {
      row.other_copies = others.map(alternate);
      row.route_basis =
        usableCopies.length === 1
          ? 'only_usable'
          : ordered.length > 1 && Number.isFinite(routeIndex(recommended))
            ? 'operator_order'
            : 'stable_tie';
      const differs = fields.filter(
        (field) =>
          new Set(
            group
              .map((copy) => copy.metadata[field])
              .filter(known)
              .map((value) => comparisonValue(field, value)),
          ).size > 1,
      );
      if (differs.length) row.differs = differs;
    }
    ranked.push({
      row,
      fit: classes[match],
      rank: Math.min(...passing.map((copy) => copy.rank)),
      tie: hash(normalizeText(request.query), md5 ?? [recommended.source, recommended.id]),
      md5,
    });
  }
  ranked.sort(compareRanked);
  const editions = new Map<string, Ranked[]>();
  for (const item of ranked) {
    const title = titleStem(item.row.title);
    const author = normalizeText(item.row.author);
    const key = title && author ? JSON.stringify([title, author]) : `single:${item.row.ref}`;
    const group = editions.get(key) ?? [];
    group.push(item);
    editions.set(key, group);
  }
  const result: MergedRow[] = [];
  for (const [key, group] of editions) {
    if (group.length > 1) {
      const label = `e${hash(key).slice(0, 16)}`;
      const formatIndex = (item: Ranked): number => {
        const index =
          options.formatPreference?.map(normalizeText).indexOf(normalizeText(item.row.format)) ??
          -1;
        return index < 0 ? Infinity : index;
      };
      group.sort((a, b) => {
        const delta = formatIndex(a) - formatIndex(b);
        return (Number.isNaN(delta) ? 0 : delta) || compareRanked(a, b);
      });
      group.forEach((item) => {
        item.row.likely_group = label;
      });
      for (let i = 1; i < group.length; i++) {
        const item = group[i]!;
        const same = group
          .slice(0, i)
          .find(
            (previous) =>
              (!item.md5 || !previous.md5) &&
              Boolean(normalizeText(item.row.format)) &&
              normalizeText(item.row.format) === normalizeText(previous.row.format) &&
              typeof item.row.size_mb === 'number' &&
              Number.isFinite(item.row.size_mb) &&
              item.row.size_mb > 0 &&
              item.row.size_mb === previous.row.size_mb &&
              (!known(item.row.pages) ||
                !known(previous.row.pages) ||
                item.row.pages === previous.row.pages),
          );
        if (same) item.row.likely_same_file_as = same.row.ref;
      }
    }
    result.push(...group.map((item) => item.row));
  }
  return result;
}
