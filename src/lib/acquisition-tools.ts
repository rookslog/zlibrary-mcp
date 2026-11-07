import { z } from 'zod';
import { filterFields, type MergeCopy } from './merge.js';
import type { MergedFilterRequest } from './row-filters.js';
import { RefStore } from './refs.js';
import { checkRowFilters, checkMergedRowFilters, FILTER_CAPABILITIES } from './row-filters.js';
import { SourceRegistry, type WindowRequest, type SearchWindow } from './source-registry.js';
import { fanOut } from './fan-out.js';
import { SearchRecords, type RetainedSelection } from './search-records.js';
import { planSearch, type SearchPlan, type CollectedSource } from './search-assembly.js';
import { parseSearchSettings, type SearchSettings } from './search-settings.js';
import { RecentSearches, projectSearchLimits, type LocalSearchState } from './search-limits.js';
import { normalizeLanguages } from './filter-languages.js';
import { classifyFailure, errorEnvelope, ToolFailure } from './tool-errors.js';
import type { CallOptions } from './zlibrary-api.js';
import { inspectZlibraryAccounts } from './zlibrary-accounts.js';

import { annasMemberWorker } from './annas-member-worker.js';

type Source = string;
type Invoke = (operation: string, args: Record<string, any>, options?: CallOptions) => Promise<any>;
export function createInputs(names: readonly string[] = ['libgen', 'zlibrary', 'annas']) {
  const sourceSchema = z.enum(names as [string, ...string[]]);
  const sources = z
    .array(sourceSchema)
    .min(1)
    .max(names.length)
    .refine((a) => new Set(a).size === a.length);
  const limit = z.number().int().min(1).max(50).optional();
  return {
    search: z
      .object({
        query: z.string().trim().min(1).max(500).optional(),
        author: z.string().trim().min(1).max(200).optional(),
        sources: sources.optional(),
        exact: z
          .boolean()
          .optional()
          .describe('Local per-field phrase check on every source; also sent to Z-Library.'),
        year_from: z
          .number()
          .int()
          .optional()
          .describe(
            'Inclusive minimum year; checked on every source. Unknown years are excluded and counted.',
          ),
        year_to: z
          .number()
          .int()
          .optional()
          .describe(
            'Inclusive maximum year; checked on every source. Unknown years are excluded and counted.',
          ),
        languages: z
          .array(z.string())
          .optional()
          .describe(
            'OR of names/codes; checked on every source. Unknown metadata is excluded. Empty list adds no constraint.',
          ),
        formats: z
          .array(z.string())
          .optional()
          .describe(
            'OR of extensions, case-insensitive, one leading dot ignored; checked on every source. Empty list adds no constraint.',
          ),
        limit,
        cursor: z.string().optional(),
      })
      .strict(),
    get_book: z.object({ ref: z.string().min(1) }).strict(),
    download: z
      .object({
        ref: z.string().min(1),
        output_dir: z.string().min(1).optional(),
        filename: z.string().min(1).optional(),
      })
      .strict(),
    get_limits: z
      .object({ sources: sources.optional(), mode: z.enum(['local', 'live']).optional() })
      .strict(),
    get_history: z
      .object({ source: sourceSchema.optional(), limit, cursor: z.string().optional() })
      .strict(),
  };
}
export const INPUTS = createInputs();
export type ToolName = keyof typeof INPUTS;
export const OLD_TOOLS: Record<string, string> = {
  search_books:
    'Use search with query and sources:["zlibrary"]; count becomes limit and year/format fields use snake_case.',
  full_text_search:
    'Removed: this was catalogue metadata search, not text search; use search for metadata.',
  get_download_history: 'Use get_history; count becomes limit, and cursor continues the page.',
  get_download_limits: 'Use get_limits; mode:"live" obtains account numbers; local is the default.',
  get_recent_books: 'Removed; use search with a query and explicit sources.',
  download_book_to_file:
    'Search again, choose the edition, then call download with its ref; outputDir becomes output_dir; text processing was removed in 2.0.',
  get_book_metadata:
    'Search again and call get_book with the row ref; bookId, bookHash and include are removed.',
  search_by_term: 'Removed; use search with the term as query and explicit sources.',
  search_by_author:
    'Use search with author, sources and optionally exact; count becomes limit; exact checks title-query and author phrases separately on every source.',
  fetch_booklist: 'Removed; use search with catalogue metadata and explicit sources.',
  search_advanced: 'Removed; call search with exact:true and then exact:false if needed.',
  search_multi_source:
    'Use search with a source set and limit instead of count; omit sources for the reported default set.',
  ['process_document_for_' + 'r' + 'ag']:
    'Text processing was removed in 2.0; use a separate 1.4.x installation.',
};
// Retired tool spellings stay in migration data, not source branding text.
export interface Selection {
  book: Record<string, any>;
  source: Source;
  query?: string;
  search?: RetainedSelection;
}
interface Cursor {
  operation: 'search' | 'get_history';
  args: Record<string, any>;
  requested: Source[];
  served: Source;
  page: number;
  held?: any[];
  filtering?: Record<string, any>;
  offset?: number;
}
const allSources: Source[] = ['libgen', 'zlibrary', 'annas'];
// Match lib/sources/config.py. The cross-language reporting test guards drift.
export const annasTrustedHosts = new Set([
  'annas-archive.gl',
  'annas-archive.pk',
  'annas-archive.gd',
]);
function usableAnnasBase(raw = 'https://annas-archive.gl'): boolean {
  // Reject raw characters that URL() would silently normalize or reinterpret.
  if (
    !/^https?:\/\//i.test(raw) ||
    /[\s\\?#]/u.test(raw) ||
    [...raw].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  )
    return false;
  try {
    const url = new URL(raw);
    // Preserve the literal host rather than URL()'s percent-decoded spelling.
    // Reject userinfo too, including forms Python's hostname would accept.
    const rawHost = raw.split('/')[2]?.replace(/:\d*$/, '').toLowerCase();
    return rawHost === url.hostname && !url.username && !url.password && url.port !== '0';
  } catch {
    return false;
  }
}
function trustedAnnasBase(raw = 'https://annas-archive.gl'): boolean {
  return usableAnnasBase(raw) && annasTrustedHosts.has(new URL(raw).hostname);
}
const sourceName = (value: any): Source =>
  value === 'annas_archive' ? 'annas' : (value ?? 'zlibrary');
const nonempty = (o: Record<string, any>) =>
  Object.fromEntries(
    Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== ''),
  );
const numberOrUndefined = (n: any) =>
  (typeof n === 'number' || typeof n === 'string') && n !== '' && Number.isFinite(Number(n))
    ? Number(n)
    : undefined;
function sizeMB(book: any): number | undefined {
  const raw = book.size_mb ?? book.filesize ?? book.size;
  if (typeof raw === 'number') return book.size_mb != null ? raw : Math.round(raw / 10485.76) / 100;
  const match = String(raw ?? '').match(/^([\d.]+)\s*(KB|MB|GB|B)?$/i);
  if (!match) return undefined;
  return (
    Math.round(
      Number(match[1]) *
        ({ KB: 1 / 1024, MB: 1, GB: 1024, B: 1 / 1048576 }[match[2]?.toUpperCase() ?? 'B'] ?? 1) *
        100,
    ) / 100
  );
}
const DownloadReplySchema = z
  .object({
    path: z.string(),
    bytes: z.number().int().nonnegative(),
    reused: z.boolean(),
    transfer_skipped: z.boolean(),
    account_slot: z.number().int().positive().optional(),
    verified: z.literal('sha256'),
    record: z.string(),
    quota_estimate: z
      .object({
        source: z.enum(['zlibrary', 'annas']),
        remaining: z.number().int().nonnegative(),
        as_of: z.iso.datetime(),
        basis: z.enum(['observed', 'decremented']),
      })
      .optional(),
  })
  .refine((result) => !(result.reused && result.transfer_skipped));
export interface SearchRuntime {
  memberSession?: () => ReturnType<typeof annasMemberWorker.status>;
  registry?: SourceRegistry;
  localState?: () => Readonly<Record<string, LocalSearchState>>;
  now?: () => number;
  nonce?: bigint;
  recordCapacity?: number;
  maxWindows?: number;
  relationshipCapacity?: number;
}
export class AcquisitionTools {
  readonly refs = new RefStore();
  private estimates = new Map<
    Source,
    { source: Source; remaining: number; as_of: string; basis: 'observed' | 'decremented' }
  >();
  private readonly settings: Readonly<SearchSettings>;
  private readonly registry: SourceRegistry;
  private readonly searches: SearchRecords;
  private readonly recent: RecentSearches;
  private readonly inputs: typeof INPUTS;
  private readonly zlibraryAccountConfig: ReturnType<typeof inspectZlibraryAccounts>;
  constructor(
    private invoke: Invoke,
    private env: Record<string, string | undefined> = process.env,
    private readonly runtime: SearchRuntime = {},
  ) {
    this.zlibraryAccountConfig = inspectZlibraryAccounts(env);
    this.registry =
      runtime.registry ??
      new SourceRegistry(
        allSources.map((name) => ({
          name,
          capabilities: () => this.capabilities(name),
          defaultSearch: () => this.defaultSearch(name),
          md5: (book: any) => book.md5,
          searchWindow: (request, signal) => this.sourceWindow(name, request, signal),
        })),
      );
    const names = this.registry.sources.map((spec) => spec.name);
    this.inputs = createInputs(names);
    this.settings = parseSearchSettings(env, names);
    this.searches = new SearchRecords({
      capacity: runtime.recordCapacity ?? 128,
      maxWindows: runtime.maxWindows ?? 20,
      relationshipCapacity: runtime.relationshipCapacity,
      nonce: runtime.nonce,
    });
    this.recent = new RecentSearches(names);
  }
  private defaultSearch(source: Source) {
    if (source === 'annas') return { on: false, reason: 'route_off' };
    const capability = this.capabilities(source);
    return capability.search_available
      ? { on: true }
      : { on: false, reason: 'credentials_missing' };
  }
  private capabilities(source: Source) {
    const annasBaseUsable = usableAnnasBase(this.env.ANNAS_BASE_URL);
    const annasBaseTrusted = trustedAnnasBase(this.env.ANNAS_BASE_URL);
    const configured =
      source === 'libgen' ||
      (source === 'zlibrary'
        ? this.zlibraryAccountConfig.accounts.length > 0
        : Boolean(this.env.ANNAS_SECRET_KEY) && annasBaseTrusted);
    return {
      configured,
      ...(source === 'annas'
        ? {
            member_key_configured: Boolean(this.env.ANNAS_SECRET_KEY),
            member_session: this.env.ANNAS_SECRET_KEY
              ? (this.runtime.memberSession ?? (() => annasMemberWorker.status()))()
              : 'none',
          }
        : {}),
      filters: {
        ...FILTER_CAPABILITIES[source as keyof typeof FILTER_CAPABILITIES],
        exact: 'local',
      },
      search_available: source === 'annas' ? annasBaseUsable : source !== 'zlibrary' || configured,
      download_available: configured,
      operations: ['search', 'details', 'download', ...(source === 'zlibrary' ? ['history'] : [])],
      limit: {
        kind: source === 'libgen' ? 'none' : source === 'zlibrary' ? 'daily' : 'unknown',
      } as Record<string, any>,
      ...(!configured
        ? {
            note:
              source === 'annas'
                ? !annasBaseUsable
                  ? 'Anna’s Archive base is unusable; set ANNAS_BASE_URL to a usable HTTP(S) URL.'
                  : !annasBaseTrusted
                    ? 'Anna’s Archive base is not trusted for the key; search is available. Set ANNAS_BASE_URL to a trusted host and ANNAS_SECRET_KEY for download.'
                    : 'Set ANNAS_SECRET_KEY for download; search needs no key.'
                : 'Set a complete ZLIBRARY_EMAIL/ZLIBRARY_PASSWORD pair or a numbered ZLIBRARY_EMAIL_N/ZLIBRARY_PASSWORD_N pair.',
          }
        : {}),
    };
  }
  private configured(source: Source) {
    const c = this.capabilities(source);
    if (!c.search_available)
      throw new ToolFailure('credentials_missing', 'Source configuration is missing.', {
        source,
        detail: {
          reason: 'configuration_error',
          variables:
            source === 'zlibrary'
              ? ['ZLIBRARY_EMAIL', 'ZLIBRARY_PASSWORD', 'ZLIBRARY_EMAIL_N', 'ZLIBRARY_PASSWORD_N']
              : !usableAnnasBase(this.env.ANNAS_BASE_URL)
                ? ['ANNAS_BASE_URL']
                : [],
        },
      });
  }
  private compact(selection: Selection, mint = true): Record<string, any> {
    const b = selection.book;
    const author =
      b.author ??
      b.authors
        ?.map((a: any) => (typeof a === 'string' ? a : (a.name ?? a.author)))
        .filter(Boolean)
        .join(', ');
    return nonempty({
      ...(mint ? { ref: this.refs.mintStable('r', selection) } : {}),
      title: b.title ?? b.name,
      author,
      year: checkRowFilters(b, {}).year ?? numberOrUndefined(b.year),
      language: b.language,
      format: b.extension ?? b.format,
      size_mb: sizeMB(b),
      pages: numberOrUndefined(b.pages),
      publisher: b.publisher,
      source: selection.source,
      also_available_on: b.also_available_on,
    });
  }
  private identifiers(b: any, source: Source) {
    return nonempty({
      ...(source === 'zlibrary'
        ? { id: b.id == null ? undefined : String(b.id), hash: b.hash ?? b.book_hash }
        : {}),
      md5: b.md5,
      sha256: b.sha256,
    });
  }
  private filterEvidence(copy: MergeCopy, request: MergedFilterRequest) {
    if (!Object.keys(copy.filterResult.checks).length) return {};
    return {
      filter: copy.filterResult.status,
      ...(copy.filterResult.status === 'unknown'
        ? { unknown: filterFields(copy, request, 'unknown') }
        : {}),
    };
  }
  private copyView(copy: MergeCopy, request: MergedFilterRequest) {
    return nonempty({
      ...copy.metadata,
      ref: copy.ref,
      source: copy.source,
      identifiers: this.identifiers((copy as any).book ?? {}, copy.source),
      ...this.filterEvidence(copy, request),
    });
  }
  private relationships(ref: string) {
    if (!this.searches.owns(ref)) return {};
    let context: ReturnType<SearchRecords['related']>;
    try {
      context = this.searches.related(ref);
    } catch (error) {
      if (error instanceof ToolFailure && error.code === 'ref_unavailable') return {};
      throw error;
    }
    if (!context) return {};
    const selected = this.searches.select(ref),
      row = context.rows.find(
        (row) => row.ref === ref || row.other_copies?.some((copy) => copy.ref === ref),
      );
    if (!row) return {};
    const group = new Set([row.ref, ...(row.other_copies ?? []).map((copy) => copy.ref)]);
    const copies = context.copies.filter((copy) => group.has(copy.ref));
    const others = copies
      .filter((copy) => copy.ref !== ref)
      .map((copy) => this.copyView(copy, selected.request));
    const disagreements = (row.differs ?? []).map((field) => ({
      field,
      values: copies.flatMap((copy) => {
        const value = copy.metadata[field as keyof typeof copy.metadata];
        return value === undefined || value === null || value === ''
          ? []
          : [{ source: copy.source, value }];
      }),
    }));
    const editions = row.likely_group
      ? context.rows
          .filter((other) => other.likely_group === row.likely_group && other.ref !== row.ref)
          .slice(0, 10)
          .map((other) =>
            this.copyView(
              context.copies.find((copy) => copy.ref === other.ref)!,
              selected.request,
            ),
          )
      : [];
    return {
      ...(others.length ? { other_copies: others } : {}),
      ...(disagreements.length ? { disagreements } : {}),
      ...(editions.length ? { likely_editions: editions } : {}),
    };
  }
  async call(name: string, raw: unknown = {}, options: CallOptions = {}): Promise<any> {
    try {
      if (!Object.hasOwn(this.inputs, name))
        throw new ToolFailure('invalid_argument', 'Tool name is not available in 2.0.', {
          recovery:
            (Object.hasOwn(OLD_TOOLS, name) ? OLD_TOOLS[name] : undefined) ??
            'Use search, get_book, download, get_limits or get_history.',
        });
      const parsed = this.inputs[name as ToolName].safeParse(raw);
      if (!parsed.success) {
        const operationRecovery =
          name === 'download'
            ? 'Pass ref, optional output_dir and filename only; text processing was removed in 2.0.'
            : 'Use snake_case arguments; count becomes limit and sources is a set.';
        const limitRecovery =
          'Set limit to an integer from 1 to 50 (default 10); use paging with next_cursor passed as cursor for more.';
        const hasLimitIssue = parsed.error.issues.some((i) => i.path[0] === 'limit');
        const hasOtherIssue = parsed.error.issues.some((i) => i.path[0] !== 'limit');
        throw new ToolFailure('invalid_argument', 'Arguments do not match the tool schema.', {
          detail: { reason: 'schema', fields: parsed.error.issues.map((i) => i.path.join('.')) },
          recovery: hasLimitIssue
            ? limitRecovery + (hasOtherIssue ? ` ${operationRecovery}` : '')
            : operationRecovery,
        });
      }
      const args = parsed.data as Record<string, any>;
      if ('cursor' in args && Object.keys(args).length !== 1)
        throw new ToolFailure('invalid_argument', 'A cursor must be passed by itself.', {
          recovery: 'Pass cursor by itself, or start a new search without it.',
        });
      if (options.signal?.aborted)
        throw new ToolFailure('cancelled', 'Call cancelled before any request.', { retry: 'safe' });
      if (name === 'get_limits') return await this.limits(args, options);
      if (name === 'get_history') {
        if (args.cursor && this.searches.owns(args.cursor))
          throw new ToolFailure('invalid_argument', 'Cursor belongs to another operation.');
        return await this.historyPage(args, options);
      }
      if (name === 'search') return await this.searchPage(args, options);
      const selection: Selection = this.searches.owns(args.ref)
        ? this.selection(args.ref)
        : this.refs.get<Selection>(args.ref, 'r');
      if (name === 'get_book') return await this.details(args.ref, selection, options);
      // Configuration is checked inside the bridge, after disk reuse, so a receipt remains useful offline.
      try {
        const result = DownloadReplySchema.parse(
          await this.acquire(
            selection.source,
            {
              book_details: selection.book,
              output_dir: args.output_dir,
              filename: args.filename,
              request: nonempty({ ref: args.ref, query: selection.query }),
            },
            options,
          ),
        );
        const multiAccountZlibrary =
          selection.source === 'zlibrary' && this.zlibraryAccountConfig.accounts.length > 1;
        if (!multiAccountZlibrary && result.quota_estimate?.source === selection.source)
          this.estimates.set(selection.source, { ...result.quota_estimate });
        const estimate = multiAccountZlibrary ? undefined : this.estimates.get(selection.source);
        if (estimate && !result.transfer_skipped && !result.quota_estimate) {
          estimate.remaining = Math.max(0, estimate.remaining - 1);
          estimate.basis = 'decremented';
        }
        return {
          ...result,
          source: selection.source,
          ...(estimate ? { quota_estimate: { ...estimate } } : {}),
        };
      } catch (e) {
        const failure = this.selectionFailure(e, 'download', selection.source);
        if (
          failure.info.retry === 'may_spend_quota' ||
          failure.code === 'internal' ||
          failure.info.detail?.request_issued === true
        )
          this.estimates.delete(selection.source);
        const alternatives = this.relationships(args.ref).other_copies;
        if (alternatives?.length)
          failure.info.detail = { ...failure.info.detail, other_copies: alternatives };
        throw failure;
      }
    } catch (error) {
      return errorEnvelope(error, name);
    }
  }
  private selectionFailure(error: unknown, operation: string, source: Source) {
    const failure = classifyFailure(error, operation, source);
    return new ToolFailure(failure.code, failure.message, { ...failure.info, source });
  }
  private async acquire(source: Source, args: Record<string, any>, options: CallOptions) {
    const spec = this.registry.sources.find((spec) => spec.name === source);
    if (spec?.acquire) return spec.acquire(args, options);
    if (!allSources.includes(source))
      throw new ToolFailure(
        'unsupported_operation',
        'Registered source has no acquisition owner.',
        { source },
      );
    return this.invoke('acquire_book', args, options);
  }
  private async limits(args: any, options: CallOptions) {
    const views = this.views();
    const snapshot = projectSearchLimits(
      views,
      this.settings,
      this.runtime.localState?.(),
      this.recent.snapshot(),
    );
    const result: any = structuredClone(snapshot);
    if (args.sources)
      result.sources = Object.fromEntries(
        Object.entries(result.sources).filter(([name]) => args.sources.includes(name)),
      );
    for (const source of args.sources ?? this.registry.sources.map((spec) => spec.name)) {
      const capability = result.sources[source];
      if (args.mode === 'live' && source === 'zlibrary') {
        try {
          this.configured(source);
          const live = await this.invoke('get_download_limits', {}, options);
          const remaining = numberOrUndefined(live.daily_remaining);
          const perDay = numberOrUndefined(live.daily_limit);
          const used = numberOrUndefined(live.downloads_today);
          const asOf = new Date().toISOString();
          capability.limit = nonempty({
            kind: perDay === undefined || remaining === undefined ? 'unparsed' : 'daily',
            per_day: perDay,
            used_today: used,
            remaining,
            premium: live.is_premium,
            as_of: asOf,
            ...(this.zlibraryAccountConfig.accounts.length > 1
              ? {
                  account_slot: this.zlibraryAccountConfig.accounts[0]?.slot,
                  account_pool_size: this.zlibraryAccountConfig.accounts.length,
                }
              : {}),
          });
          if (remaining !== undefined)
            this.estimates.set(source, { source, remaining, as_of: asOf, basis: 'observed' });
          else this.estimates.delete(source);
        } catch (e) {
          (capability as any).error = errorEnvelope(e, 'get_limits', source).error;
        }
      }
    }
    return result;
  }
  private async details(ref: string, selection: Selection, options: CallOptions) {
    const b = selection.book;
    let detail: any = {};
    const registered = this.registry.sources.find((spec) => spec.name === selection.source);
    if (registered?.details || selection.source === 'zlibrary') {
      try {
        if (registered?.details) detail = await registered.details(b, options);
        else {
          this.configured(selection.source);
          detail = await this.invoke(
            'get_book_metadata_complete',
            { book_id: String(b.id), book_hash: b.hash ?? b.book_hash },
            options,
          );
        }
      } catch (error) {
        return errorEnvelope(
          this.selectionFailure(error, 'get_book', selection.source),
          'get_book',
        );
      }
    }
    const merged = { ...b, ...nonempty(detail) };
    const description = merged.description;
    const result = nonempty({
      ...this.compact({ ...selection, book: merged }, false),
      ref,
      isbn: merged.isbn ?? merged.isbn_13 ?? merged.isbn_10,
      series: merged.series,
      edition: merged.edition,
      translator: merged.translator,
      categories: merged.categories,
      rating: merged.rating,
      description: description?.slice(0, 1000),
      ...(description?.length > 1000 ? { description_truncated: true } : {}),
      identifiers: this.identifiers(b, selection.source),
      url: merged.url ?? merged.href ?? merged.book_url,
      evidence: {
        detail: Object.keys(nonempty(detail)).filter((k) => !['id', 'book_hash'].includes(k)),
        search: Object.keys(nonempty(b)).filter(
          (k) => k !== 'content_type' && (detail[k] == null || detail[k] === ''),
        ),
      },
    });
    const retained = selection.search;
    if (!retained) return result;
    const copy = {
      ...retained.copy,
      metadata: result,
      filterResult: checkMergedRowFilters(
        { ...result, extension: result.format },
        retained.request,
      ),
    };
    return {
      ...result,
      ...this.filterEvidence(copy, retained.request),
      ...this.relationships(ref),
    };
  }
  /** Account history keeps its upstream continuation semantics independently of search. */
  private async historyPage(args: any, options: CallOptions) {
    let cursor: Cursor | undefined;
    if (args.cursor !== undefined) {
      cursor = this.refs.get<Cursor>(args.cursor, 'c');
      if (cursor.operation !== 'get_history')
        throw new ToolFailure('invalid_argument', 'Cursor belongs to another operation.');
      args = cursor.args;
    }
    if (!cursor && args.source && args.source !== 'zlibrary')
      throw new ToolFailure('unsupported_operation', 'Only zlibrary supports account history.', {
        source: args.source,
      });
    const source = 'zlibrary' as const;
    const requested: Source[] = cursor?.requested ?? [source];
    const count = args.limit ?? 10;
    const page = cursor?.page ?? 1;
    let fetched: any[];
    try {
      this.configured(source);
      fetched = await this.invoke('get_download_history', { count, page }, options);
      if (!Array.isArray(fetched))
        throw new ToolFailure('provider_error', 'Provider returned an invalid page.', {
          source,
          retry: 'safe',
          detail: { reason: 'protocol_error' },
        });
      if (fetched.some((book) => book.source && sourceName(book.source) !== source))
        throw new ToolFailure('provider_error', 'Provider changed the selected source.', {
          source,
          retry: 'safe',
          detail: { reason: 'protocol_error' },
        });
    } catch (error) {
      const failure = classifyFailure(error, 'get_history', source);
      throw new ToolFailure(failure.code, failure.message, {
        ...failure.info,
        attempts: [
          {
            source,
            outcome:
              failure.code === 'credentials_missing' || failure.code === 'unsupported_operation'
                ? 'skipped'
                : 'failed',
            code: failure.code,
            reason: failure.info.detail?.reason,
          },
        ],
      });
    }
    const rows = fetched.slice(0, count).map((raw) => {
      const book = { ...raw, source };
      const downloadable = Boolean(book.id && (book.hash || book.book_hash));
      return {
        ...this.compact({ book, source, query: '' }, downloadable),
        ...(book.downloaded_at ? { downloaded_at: book.downloaded_at } : {}),
      };
    });
    const next: Cursor | undefined =
      fetched.length >= count
        ? {
            operation: 'get_history',
            args: { ...args, limit: count, sources: requested },
            requested,
            served: source,
            page: page + 1,
          }
        : undefined;
    return { rows, ...(next ? { next_cursor: this.refs.mintStable('c', next) } : {}) };
  }
  private selection(ref: string): Selection {
    const selection = this.searches.select(ref);
    return {
      source: selection.copy.source,
      book: {
        ...selection.copy.book,
        source: selection.copy.source,
        md5: selection.copy.md5 ?? selection.copy.book?.md5,
      },
      query: [selection.request.query, selection.request.author].filter(Boolean).join(' '),
      search: selection,
    };
  }
  private views() {
    return this.registry.sources.map((spec) => ({
      name: spec.name,
      capabilities: spec.capabilities(this.env),
      defaultSearch: spec.defaultSearch(this.env),
    }));
  }
  private async sourceWindow(
    source: Source,
    request: WindowRequest,
    signal: AbortSignal,
    options: CallOptions = {},
  ): Promise<SearchWindow> {
    const languages = normalizeLanguages(request.languages ?? []).values;
    const formats = [
      ...new Set(
        (request.formats ?? [])
          .map((value) => value.normalize('NFC').trim().toLowerCase().replace(/^\./u, ''))
          .filter(Boolean),
      ),
    ];
    const result =
      source === 'zlibrary'
        ? await this.invoke(
            'search',
            {
              query: request.query ?? '',
              author: request.author,
              exact: request.exact ?? false,
              from_year: request.year_from,
              to_year: request.year_to,
              languages,
              extensions: formats,
              count: 50,
              page: request.page ?? 1,
            },
            { ...options, signal },
          )
        : await this.invoke(
            'search_multi_source',
            {
              query: [request.query, request.author].filter(Boolean).join(' '),
              source,
              count: 50,
              multi_source: request.multiSource,
            },
            { ...options, signal },
          );
    const fetched = result?.books;
    if (
      !Array.isArray(fetched) ||
      fetched.length > 50 ||
      fetched.some((book) => !book || (book.source && sourceName(book.source) !== source))
    )
      throw new ToolFailure('provider_error', 'Provider returned an invalid page.', {
        source,
        retry: 'safe',
        detail: { reason: 'protocol_error' },
      });
    const rows = fetched.map((raw, index) => {
      const book = { ...raw, source };
      const metadata = this.compact({ book, source }, false);
      delete metadata.source;
      return {
        id: String(book.id ?? book.md5 ?? index),
        source,
        book,
        metadata,
        md5: book.md5,
        rank: index + 1,
        filterResult: checkMergedRowFilters({ ...metadata, extension: metadata.format }, request),
        downloadAvailable: this.capabilities(source).download_available,
        quotaRemaining: this.estimates.get(source)?.remaining,
      };
    });
    const nativeSent = nonempty({
      year_from: request.year_from,
      year_to: request.year_to,
      ...(languages.length ? { languages } : {}),
      ...(formats.length ? { formats } : {}),
      ...(request.exact ? { exact: true } : {}),
    });
    return {
      rows,
      accepted: rows.filter((row) => row.filterResult.status === 'pass').length,
      ...(source === 'zlibrary' && fetched.length === 50 ? { more: true as const } : {}),
      ...(source === 'zlibrary' ? { filtering: { native_sent: nativeSent } } : {}),
    };
  }
  private async collect(
    plan: SearchPlan,
    options: CallOptions,
    page = 1,
    startedAt?: number,
    drainOnCancel = false,
  ): Promise<CollectedSource[]> {
    if (!plan.intent.length)
      return plan.sources.map((view) => ({
        source: view.name,
        state: 'not_asked',
        reason: plan.excluded[view.name] ?? 'not_in_default_order',
      }));
    const states = this.runtime.localState?.() ?? {};
    const blocked = Object.fromEntries(
      Object.entries(states).flatMap(([name, state]) =>
        state.admission === 'backoff' ||
        (state.breakerBlocked ?? (state.breaker === 'open' && (state.breakerRetryAfterS ?? 0) > 0))
          ? [
              [
                name,
                {
                  reason: state.admission === 'backoff' ? 'auth_backoff' : 'breaker_open',
                  retry_after_s: Math.ceil(
                    Math.max(state.admissionRetryAfterS ?? 0, state.breakerRetryAfterS ?? 0),
                  ),
                },
              ],
            ]
          : [],
      ),
    );
    const issued: Promise<SearchWindow>[] = [];
    const registry = new SourceRegistry(
      this.registry.sources.map((spec) => {
        const view = plan.sources.find((view) => view.name === spec.name)!;
        return {
          ...spec,
          capabilities: () => view.capabilities,
          defaultSearch: () => view.defaultSearch,
          searchWindow: (request, signal) => {
            const work = this.runtime.registry
              ? spec.searchWindow(request, signal)
              : this.sourceWindow(spec.name, request, signal, options);
            issued.push(work);
            return work;
          },
        };
      }),
    );
    let snapshot: Awaited<ReturnType<typeof fanOut>>;
    try {
      snapshot = await fanOut(
        registry,
        { ...plan.request, page },
        {
          sources: plan.intent,
          env: this.env,
          waitMs: this.settings.waitMs,
          now: this.runtime.now,
          startedAt,
          signal: options.signal,
          blocked,
        },
      );
    } catch (error) {
      if (error instanceof ToolFailure && error.code === 'cancelled')
        for (const line of error.info.attempts ?? []) this.recent.record(line.source, line);
      // A refill's subscribers can leave immediately, but its position stays
      // leased until the actual adapter promises settle. Physical cleanup and
      // source-lock release remain with the adapter/runner, never this cache.
      if (drainOnCancel) await Promise.allSettled(issued);
      throw error;
    }
    return Object.entries(snapshot.sources).map(([source, line]): CollectedSource => {
      this.recent.record(source, line);
      if (line.state === 'rows' || line.state === 'empty') {
        const window = snapshot.windows.get(source)!;
        return {
          source,
          state: 'answered',
          ms: line.ms,
          copies: window.rows.map((row, index) => ({
            ...row,
            source,
            id: String(row.id ?? index),
            ref: `input-${source}-${index}`,
            metadata: (row.metadata ?? {}) as any,
            book: row.book as any,
            md5: this.registry.sources.find((spec) => spec.name === source)!.md5(row.book ?? row),
          })),
          ...(window.more ? { more: true as const } : {}),
          ...(window.filtering?.native_sent
            ? { nativeSent: window.filtering.native_sent as Record<string, unknown> }
            : {}),
        };
      }
      if (line.state === 'failed')
        return { source, state: 'failed', ms: line.ms, failure: snapshot.failures.get(source)! };
      if (line.state === 'late') return { source, state: 'late', ms: line.ms };
      if (line.state === 'not_asked') {
        let failure: ToolFailure | undefined;
        const owner = states[source]?.refusal;
        if (owner) failure = classifyFailure(owner, 'search', source);
        else if (['credentials_missing', 'route_off'].includes(line.reason)) {
          try {
            this.configured(source);
          } catch (error) {
            if (error instanceof ToolFailure)
              failure = new ToolFailure(error.code, error.message, {
                ...error.info,
                detail: { ...error.info.detail, request_issued: false },
              });
            else throw error;
          }
        }
        if (failure && line.retry_after_s !== undefined)
          failure = new ToolFailure(failure.code, failure.message, {
            ...failure.info,
            retry_after_s: Math.max(failure.info.retry_after_s ?? 0, line.retry_after_s),
          });
        return { source, ...line, ...(failure ? { failure } : {}) };
      }
      throw new ToolFailure('internal', 'Invalid terminal source state.');
    });
  }
  private async searchPage(args: any, options: CallOptions) {
    if (args.cursor !== undefined) {
      if (args.cursor.slice(1, 14) === this.refs.nonce) {
        this.refs.get<Cursor>(args.cursor, 'c');
        throw new ToolFailure('invalid_argument', 'Cursor belongs to another operation.');
      }
      return this.searches.page(args.cursor, options);
    }
    const startedAt = this.runtime.now?.() ?? performance.now();
    const plan = planSearch(args, this.views(), this.settings);
    const collected = await this.collect(plan, options, 1, startedAt);
    return this.searches.retain(plan, collected, {
      refill: async (page, signal) => {
        const dispatched = collected.filter((result) => result.state !== 'not_asked');
        const source = dispatched[0]!.source;
        const narrowed = { ...plan, intent: [source] };
        const next = await this.collect(narrowed, { signal }, page, undefined, true);
        return next.find((result) => result.source === source)!;
      },
    });
  }
}
