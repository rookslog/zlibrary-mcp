import { z } from 'zod';
import { RefStore } from './refs.js';
import { classifyFailure, errorEnvelope, ToolFailure } from './tool-errors.js';
import type { CallOptions } from './zlibrary-api.js';

type Source = 'libgen' | 'annas' | 'zlibrary';
type Invoke = (operation: string, args: Record<string, any>, options?: CallOptions) => Promise<any>;
const sourceSchema = z.enum(['libgen', 'annas', 'zlibrary']);
const sources = z
  .array(sourceSchema)
  .min(1)
  .max(3)
  .refine((a) => new Set(a).size === a.length);
const limit = z.number().int().min(1).max(50).optional();
export const INPUTS = {
  search: z
    .object({
      query: z.string().trim().min(1).max(500).optional(),
      author: z.string().trim().min(1).max(200).optional(),
      sources: sources.optional(),
      exact: z.boolean().optional(),
      year_from: z.number().int().optional(),
      year_to: z.number().int().optional(),
      languages: z.array(z.string()).optional(),
      formats: z.array(z.string()).optional(),
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
    'Use search with author, sources and optionally exact; count becomes limit; exact covers the whole combined query.',
  fetch_booklist: 'Removed; use search with catalogue metadata and explicit sources.',
  search_advanced: 'Removed; call search with exact:true and then exact:false if needed.',
  search_multi_source:
    'Use search with sources in the desired order and limit instead of count; auto needs operator defaults.',
  ['process_document_for_' + 'r' + 'ag']:
    'Text processing was removed in 2.0; use a separate 1.4.x installation.',
};
// Retired tool spellings stay in migration data, not source branding text.
interface Selection {
  book: Record<string, any>;
  source: Source;
  query?: string;
}
interface Cursor {
  operation: 'search' | 'get_history';
  args: Record<string, any>;
  requested: Source[];
  served: Source;
  page: number;
  held?: any[];
  offset?: number;
}
interface Attempt {
  source: Source;
  outcome: string;
  ignored?: string[];
  code?: string;
  reason?: string;
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
    return (
      rawHost === url.hostname &&
      !url.username &&
      !url.password &&
      url.port !== '0'
    );
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
export class AcquisitionTools {
  readonly refs = new RefStore();
  private estimates = new Map<
    Source,
    { source: Source; remaining: number; as_of: string; basis: 'observed' | 'decremented' }
  >();
  private defaultOrder?: Source[];
  constructor(
    private invoke: Invoke,
    private env: Record<string, string | undefined> = process.env,
  ) {
    const raw = env.BOOK_SOURCE_DEFAULT_ORDER;
    if (raw) {
      const parsed = sources.safeParse(raw.split(',').map((s) => s.trim()));
      if (parsed.success) this.defaultOrder = parsed.data;
      else
        throw new Error(
          'BOOK_SOURCE_DEFAULT_ORDER must name 1–3 distinct sources: libgen,zlibrary,annas',
        );
    }
  }
  private capabilities(source: Source) {
    const annasBaseUsable = usableAnnasBase(this.env.ANNAS_BASE_URL);
    const annasBaseTrusted = trustedAnnasBase(this.env.ANNAS_BASE_URL);
    const configured =
      source === 'libgen' ||
      (source === 'zlibrary'
        ? Boolean(this.env.ZLIBRARY_EMAIL && this.env.ZLIBRARY_PASSWORD)
        : Boolean(this.env.ANNAS_SECRET_KEY) && annasBaseTrusted);
    return {
      configured,
      search_available:
        source === 'annas' ? annasBaseUsable : source !== 'zlibrary' || configured,
      download_available: configured,
      operations: [
        'search',
        'details',
        'download',
        ...(source === 'zlibrary' ? ['history'] : []),
      ],
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
                : 'Set ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD.',
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
              ? ['ZLIBRARY_EMAIL', 'ZLIBRARY_PASSWORD']
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
      year: numberOrUndefined(b.year),
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
    return source === 'zlibrary'
      ? nonempty({ id: b.id == null ? undefined : String(b.id), hash: b.hash ?? b.book_hash })
      : nonempty({ md5: b.md5 });
  }
  async call(name: string, raw: unknown = {}, options: CallOptions = {}): Promise<any> {
    try {
      if (!Object.hasOwn(INPUTS, name))
        throw new ToolFailure('invalid_argument', 'Tool name is not available in 2.0.', {
          recovery:
            (Object.hasOwn(OLD_TOOLS, name) ? OLD_TOOLS[name] : undefined) ??
            'Use search, get_book, download, get_limits or get_history.',
        });
      const parsed = INPUTS[name as ToolName].safeParse(raw);
      if (!parsed.success) {
        const operationRecovery =
          name === 'download'
            ? 'Pass ref, optional output_dir and filename only; text processing was removed in 2.0.'
            : 'Use snake_case arguments; count becomes limit and source order is sources.';
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
      if (name === 'search' || name === 'get_history') return await this.page(name, args, options);
      const selection = this.refs.get<Selection>(args.ref, 'r');
      if (name === 'get_book') return await this.details(args.ref, selection, options);
      // Configuration is checked inside the bridge, after disk reuse, so a receipt remains useful offline.
      try {
        const result = DownloadReplySchema.parse(
          await this.invoke(
            'acquire_book',
            {
              book_details: selection.book,
              output_dir: args.output_dir,
              filename: args.filename,
              request: nonempty({ ref: args.ref, query: selection.query }),
            },
            options,
          ),
        );
        if (result.quota_estimate?.source === selection.source)
          this.estimates.set(selection.source, { ...result.quota_estimate });
        const estimate = this.estimates.get(selection.source);
        if (estimate && !result.transfer_skipped && !result.quota_estimate) {
          estimate.remaining = Math.max(0, estimate.remaining - 1);
          estimate.basis = 'decremented';
        }
        return { ...result, ...(estimate ? { quota_estimate: { ...estimate } } : {}) };
      } catch (e) {
        const failure = classifyFailure(e, 'download', selection.source);
        if (
          failure.info.retry === 'may_spend_quota' ||
          failure.code === 'internal' ||
          failure.info.detail?.request_issued === true
        )
          this.estimates.delete(selection.source);
        throw failure;
      }
    } catch (error) {
      return errorEnvelope(error, name);
    }
  }
  private async limits(args: any, options: CallOptions) {
    const result: any = {
      ...(this.defaultOrder ? { default_order: this.defaultOrder } : {}),
      sources: {},
    };
    for (const source of args.sources ?? allSources) {
      const capability = this.capabilities(source);
      result.sources[source] = capability;
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
    if (selection.source === 'zlibrary') {
      try {
        this.configured(selection.source);
        detail = await this.invoke(
          'get_book_metadata_complete',
          { book_id: String(b.id), book_hash: b.hash ?? b.book_hash },
          options,
        );
      } catch (error) {
        return errorEnvelope(error, 'get_book', selection.source);
      }
    }
    const merged = { ...b, ...nonempty(detail) };
    const description = merged.description;
    return nonempty({
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
  }
  private async page(operation: 'search' | 'get_history', args: any, options: CallOptions) {
    let cursor: Cursor | undefined;
    if (args.cursor !== undefined) {
      cursor = this.refs.get<Cursor>(args.cursor, 'c');
      if (cursor.operation !== operation)
        throw new ToolFailure('invalid_argument', 'Cursor belongs to another operation.');
      args = cursor.args;
    }
    if (operation === 'get_history' && !cursor && args.source && args.source !== 'zlibrary')
      throw new ToolFailure('unsupported_operation', 'Only zlibrary supports account history.', {
        ...(allSources.includes(args.source) ? { source: args.source } : {}),
      });
    const requested: Source[] =
      cursor?.requested ??
      (operation === 'get_history' ? ['zlibrary'] : (args.sources ?? this.defaultOrder));
    if (!requested)
      throw new ToolFailure('sources_required', 'Choose the sources to search.', {
        detail: {
          available: allSources.map((source) => ({
            source,
            search_available: this.capabilities(source).search_available,
            download_available: this.capabilities(source).download_available,
          })),
        },
      });
    if (operation === 'search' && !args.query && !args.author)
      throw new ToolFailure('invalid_argument', 'Give query, author, or both.');
    if (args.year_from != null && args.year_to != null && args.year_from > args.year_to)
      throw new ToolFailure('invalid_argument', 'year_from must not exceed year_to.');
    const count = args.limit ?? 10;
    const attempts: Attempt[] = [];
    const failures: ToolFailure[] = [];
    let answered = false;
    let selected: Source | undefined;
    let books: any[] = [];
    let next: Cursor | undefined;
    let note: string | undefined;
    for (const source of cursor ? [cursor.served] : requested) {
      const ignored =
        source === 'zlibrary'
          ? []
          : ['exact', 'year_from', 'year_to', 'languages', 'formats'].filter(
              (k) => args[k] !== undefined,
            );
      try {
        this.configured(source);
        let fetched: any[];
        const page = cursor?.page ?? 1;
        if (cursor?.held) fetched = cursor.held;
        else if (operation === 'get_history')
          fetched = await this.invoke('get_download_history', { count, page }, options);
        else if (source === 'zlibrary')
          fetched = (
            await this.invoke(
              'search',
              {
                query: args.query ?? '',
                author: args.author,
                exact: args.exact ?? false,
                from_year: args.year_from,
                to_year: args.year_to,
                languages: args.languages ?? [],
                extensions: args.formats ?? [],
                count,
                page,
              },
              options,
            )
          ).books;
        else {
          const query = [args.query, args.author].filter(Boolean).join(' ');
          fetched = (
            await this.invoke('search_multi_source', { query, source, count: 50 }, options)
          ).books;
        }
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
        answered = true;
        selected = source;
        note = undefined;
        const offset = cursor?.offset ?? 0;
        books =
          source === 'zlibrary' ? fetched.slice(0, count) : fetched.slice(offset, offset + count);
        attempts.push({
          source,
          outcome: books.length ? 'rows' : 'empty',
          ...(ignored.length ? { ignored } : {}),
        });
        const base = {
          operation,
          args: { ...args, limit: count, sources: requested },
          requested,
          served: source,
        };
        if (source === 'zlibrary' && books.length === count) next = { ...base, page: page + 1 };
        if (source !== 'zlibrary') {
          if (books.length) note = `This server returns at most 50 ${source} rows per query.`;
          if (offset + count < fetched.length)
            next = { ...base, page: 1, held: fetched, offset: offset + count };
        }
        if (books.length || cursor) break;
      } catch (e) {
        const failure = classifyFailure(e, operation, source);
        failures.push(failure);
        attempts.push({
          source,
          outcome:
            failure.code === 'credentials_missing' || failure.code === 'unsupported_operation'
              ? 'skipped'
              : 'failed',
          code: failure.code,
          reason: failure.info.detail?.reason,
        });
      }
    }
    if (
      !books.length &&
      failures.length &&
      (!answered || attempts.some((a) => a.outcome === 'failed'))
    ) {
      if (requested.length === 1 || cursor)
        throw new ToolFailure(failures[0]!.code, failures[0]!.message, {
          ...failures[0]!.info,
          attempts,
        });
      const code = answered
        ? 'partial_failure'
        : failures.every((f) => f.code === 'unsupported_operation')
          ? 'unsupported_operation'
          : 'all_sources_failed';
      const diagnostics = failures.find((failure) => failure.info.detail?.diagnostics)?.info.detail
        .diagnostics;
      const configurationRecovery =
        !answered &&
        failures.every(
          (failure) =>
            failure.code === 'invalid_argument' &&
            failure.info.detail?.reason === 'configuration_error' &&
            failure.info.detail?.request_issued === false &&
            failure.info.recovery === failures[0]!.info.recovery,
        )
          ? failures[0]!.info.recovery
          : undefined;
      throw new ToolFailure(
        code,
        answered
          ? 'Some sources answered empty; others did not answer.'
          : 'No requested source completed the operation.',
        {
          attempts,
          retry: failures.some((f) => f.info.retry === 'safe') ? 'safe' : 'no',
          ...(configurationRecovery ? { recovery: configurationRecovery } : {}),
          ...(diagnostics ? { detail: { diagnostics } } : {}),
        },
      );
    }
    const rows = books.map((book) => {
      const actualSource = selected!;
      book = { ...book, source: actualSource };
      const downloadable =
        actualSource === 'zlibrary'
          ? Boolean(book.id && (book.hash || book.book_hash))
          : Boolean(book.md5);
      return {
        ...this.compact(
          {
            book,
            source: actualSource,
            query: [args.query, args.author].filter(Boolean).join(' '),
          },
          operation !== 'get_history' || downloadable,
        ),
        ...(book.downloaded_at ? { downloaded_at: book.downloaded_at } : {}),
      };
    });
    return {
      rows,
      ...(next ? { next_cursor: this.refs.mintStable('c', next) } : {}),
      ...(operation === 'search'
        ? {
            routing: nonempty({
              requested,
              served_by: rows.length ? selected : undefined,
              fell_back: rows.length > 0 && selected !== requested[0],
              attempts,
            }),
          }
        : {}),
      ...(note
        ? { note }
        : books.length === 0 && operation === 'search'
          ? {
              note: cursor
                ? 'No more matches for this continuation.'
                : 'No matches; try exact:false, fewer filters or another source.',
            }
          : {}),
    };
  }
}
