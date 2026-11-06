import { z } from 'zod';
import { mergeRows, type MergeCopy, type MergedRow } from './merge.js';
import { checkMergedRowFilters, type MergedFilterRequest } from './row-filters.js';
import { normalizeLanguages } from './filter-languages.js';
import { ToolFailure, errorEnvelope, type ErrorCode, type Retry } from './tool-errors.js';
import type { SourceCapabilities } from './source-registry.js';
import type { SearchSettings } from './search-settings.js';
import type { SourceLine } from './fan-out.js';

export interface SourceView {
  name: string;
  capabilities: SourceCapabilities;
  defaultSearch: { on: boolean; reason?: string };
}
export interface SearchRequest extends MergedFilterRequest {
  sources?: readonly string[];
  limit?: number;
}
export interface SearchPlan {
  request: Readonly<SearchRequest>;
  sources: readonly SourceView[];
  settings: Readonly<SearchSettings>;
  intent: readonly string[];
  mode: 'named' | 'operator' | 'default';
  excluded: Readonly<Record<string, string>>;
}
export interface CatalogueCopy extends Omit<MergeCopy, 'rank' | 'filterResult'> {
  book?: Readonly<Record<string, unknown>>;
}
export type CollectedSource =
  | {
      source: string;
      state: 'answered';
      ms: number;
      copies: readonly CatalogueCopy[];
      more?: true;
      nativeSent?: Readonly<Record<string, unknown>>;
    }
  | { source: string; state: 'late'; ms: number }
  | { source: string; state: 'failed'; ms: number; failure: ToolFailure }
  | {
      source: string;
      state: 'not_asked';
      reason: string;
      retry_after_s?: number;
      failure?: ToolFailure;
    };
export interface SearchError {
  error: {
    code: ErrorCode;
    operation: string;
    message: string;
    retry: Retry;
    recovery: string;
    source?: string;
    retry_after_s?: number;
    attempts: Record<string, unknown>[];
    detail: Record<string, unknown>;
  };
}
export interface PreparedSearch {
  plan: SearchPlan;
  rows: readonly MergedRow[];
  copies: readonly MergeCopy[];
  sources: Readonly<Record<string, SourceLine>>;
  all_answered: boolean;
  acceptedRecords: number;
  error?: SearchError['error'];
}
export interface SearchPage {
  rows: readonly MergedRow[];
  sources: Readonly<Record<string, SourceLine>>;
  all_answered: boolean;
  ordering: 'fit-v1';
  note: string;
  next_cursor?: string;
}
export type SearchReply = SearchPage | SearchError;
export function immutable<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach((child) => immutable(child));
    Object.freeze(value);
  }
  return value;
}
const invalid = (message: string): never => {
  throw new ToolFailure('invalid_argument', message);
};
const requestSchema = z
  .object({
    query: z.string().trim().min(1).max(500).optional(),
    author: z.string().trim().min(1).max(200).optional(),
    sources: z.array(z.string()).min(1).optional(),
    exact: z.boolean().optional(),
    year_from: z.number().int().optional(),
    year_to: z.number().int().optional(),
    languages: z.array(z.string()).optional(),
    formats: z.array(z.string()).optional(),
    limit: z.number().int().min(1).max(50).optional(),
  })
  .strict();

/** Resolve intent before dynamic admission. All inputs are local plain data. */
export function planSearch(
  request: unknown,
  sources: readonly SourceView[],
  settings: SearchSettings,
): SearchPlan {
  const parsed = requestSchema.safeParse(request);
  if (!parsed.success) return invalid('Invalid search model arguments.');
  const args = parsed.data;
  if (!args.query && !args.author) return invalid('Give query, author, or both.');
  if (args.year_from !== undefined && args.year_to !== undefined && args.year_from > args.year_to)
    return invalid('year_from must not exceed year_to.');
  const registered = sources.map((source) => source.name);
  if (registered.some((name) => !name) || new Set(registered).size !== registered.length)
    return invalid('Registered sources must be nonempty and distinct.');
  const validate = (list: readonly string[] | undefined): void => {
    if (
      list &&
      (!list.length ||
        new Set(list).size !== list.length ||
        list.some((name) => !registered.includes(name)))
    )
      invalid('Choose distinct registered sources.');
  };
  validate(args.sources);
  validate(settings.operatorSources);
  const mode = args.sources ? 'named' : settings.operatorSources ? 'operator' : 'default';
  const list = args.sources ?? settings.operatorSources;
  const intent = sources
    .filter((source) =>
      list
        ? list.includes(source.name)
        : source.capabilities.search_available && source.defaultSearch.on,
    )
    .map((source) => source.name);
  const excluded = Object.fromEntries(
    sources
      .filter((source) => !intent.includes(source.name))
      .map((source) => [
        source.name,
        mode === 'named'
          ? 'not_requested'
          : mode === 'operator'
            ? 'not_in_default_order'
            : (source.capabilities.off_reason ??
              source.defaultSearch.reason ??
              'not_in_default_order'),
      ]),
  );
  return immutable(structuredClone({ request: args, sources, settings, intent, mode, excluded }));
}
function refusal(source: string, reason: string, retry_after_s?: number): ToolFailure {
  const code = ['credentials_missing', 'route_off'].includes(reason)
    ? 'credentials_missing'
    : reason === 'filter_unsupported'
      ? 'unsupported_operation'
      : ['breaker_open', 'auth_backoff'].includes(reason)
        ? 'breaker_open'
        : reason === 'queue_wait'
          ? 'provider_timeout'
          : 'provider_error';
  return new ToolFailure(code, 'Source did not answer.', {
    source,
    retry: ['credentials_missing', 'unsupported_operation'].includes(code) ? 'no' : 'safe',
    ...(retry_after_s !== undefined ? { retry_after_s } : {}),
    detail: { reason },
  });
}
function modelError(
  failure: ToolFailure,
  sources: Readonly<Record<string, SourceLine>>,
  ownSource?: string,
): SearchError['error'] {
  // Aggregate attempts account for every registered source; preserve the
  // selected owner separately from the failure detail copied below.
  const info = { ...failure.info };
  delete info.source;
  const result = errorEnvelope(
    new ToolFailure(failure.code, failure.message, {
      ...info,
      attempts: Object.entries(sources).map(([source, line]) => ({ source, ...line })),
    }),
    'search',
  ).error;
  return {
    ...result,
    ...(ownSource
      ? { source: ownSource, message: `${ownSource}: ${result.message}`.slice(0, 199) }
      : {}),
  };
}

/** Filter each collected window once, then rank only accepted copy evidence. */
export function prepareSearch(
  plan: SearchPlan,
  collected: readonly CollectedSource[],
  options: { cancelled?: boolean } = {},
): PreparedSearch {
  const byName = new Map<string, CollectedSource>();
  for (const result of collected) {
    if (!plan.sources.some((source) => source.name === result.source) || byName.has(result.source))
      return invalid('Unexpected or duplicate source result.');
    byName.set(result.source, result);
  }
  const lines: Record<string, SourceLine> = Object.create(null);
  const copies: MergeCopy[] = [];
  const failures = new Map<string, ToolFailure>();
  const refs = new Set<string>();
  let acceptedRecords = 0;
  const probe = checkMergedRowFilters({}, plan.request);
  const applied = Object.keys(probe.checks);
  const active = applied.length > 0;
  for (const view of plan.sources) {
    const source = view.name;
    const result = byName.get(source);
    if (!plan.intent.includes(source) || !view.capabilities.search_available) {
      if (result && result.state !== 'not_asked')
        return invalid('An unadmitted source supplied a result.');
      const reason =
        plan.excluded[source] ??
        view.capabilities.off_reason ??
        view.defaultSearch.reason ??
        'credentials_missing';
      lines[source] = { state: 'not_asked', reason };
      if (plan.intent.includes(source))
        failures.set(source, result?.failure ?? refusal(source, reason));
      continue;
    }
    if (options.cancelled && !result) {
      lines[source] = { state: 'not_asked', reason: 'caller_cancelled' };
      continue;
    }
    if (!result) return invalid('A dispatched source has no terminal result.');
    if (result.state === 'not_asked') {
      if (
        !result.reason ||
        (result.retry_after_s !== undefined &&
          (!Number.isFinite(result.retry_after_s) || result.retry_after_s < 0))
      )
        return invalid('Invalid source refusal.');
      lines[source] = {
        state: 'not_asked',
        reason: result.reason,
        ...(result.retry_after_s !== undefined ? { retry_after_s: result.retry_after_s } : {}),
      };
      failures.set(source, result.failure ?? refusal(source, result.reason, result.retry_after_s));
      continue;
    }
    if (!Number.isInteger(result.ms) || result.ms < 0)
      return invalid('A terminal source needs elapsed milliseconds.');
    if (result.state === 'late') {
      if ('copies' in result) return invalid('Late sources cannot supply records.');
      lines[source] = { state: 'late', ms: result.ms };
      failures.set(
        source,
        new ToolFailure('provider_timeout', 'Source was still searching at the cut.', {
          source,
          retry: 'safe',
          detail: { reason: 'search_cut' },
        }),
      );
      continue;
    }
    if (result.state === 'failed') {
      if (!(result.failure instanceof ToolFailure) || 'copies' in result)
        return invalid('Invalid source failure.');
      const failure = result.failure;
      failures.set(source, failure);
      lines[source] = {
        state: 'failed',
        code: failure.code,
        reason: failure.info.detail?.reason ?? 'unknown',
        ms: result.ms,
        ...(failure.info.retry_after_s !== undefined
          ? { retry_after_s: failure.info.retry_after_s }
          : {}),
      };
      continue;
    }
    if (result.state !== 'answered' || !Array.isArray(result.copies) || result.copies.length > 50)
      return invalid('Invalid collected source window.');
    let matched = 0,
      rejected = 0,
      unknown = 0;
    result.copies.forEach((copy, index) => {
      if (copy.source !== source || !copy.ref || refs.has(copy.ref))
        return invalid('Copy identity changed source or reused a reference.');
      refs.add(copy.ref);
      const filterResult = checkMergedRowFilters(
        { ...copy.metadata, extension: copy.metadata.format },
        plan.request,
      );
      if (filterResult.status === 'pass') matched++;
      else if (filterResult.status === 'fail') rejected++;
      else unknown++;
      copies.push({
        ...structuredClone(copy),
        rank: index + 1,
        filterResult,
        downloadAvailable: view.capabilities.download_available && copy.downloadAvailable !== false,
      });
    });
    acceptedRecords += matched;
    const native = result.nativeSent
      ? Object.fromEntries(
          Object.entries(result.nativeSent).filter(([key]) =>
            ['year_from', 'year_to', 'languages', 'formats', 'exact'].includes(key),
          ),
        )
      : undefined;
    lines[source] = {
      state: matched ? 'rows' : 'empty',
      rows: matched,
      ms: result.ms,
      ...(result.more ? { more: true as const } : {}),
      ...(active
        ? {
            filtering: {
              ...(native ? { native_sent: native } : {}),
              applied,
              examined: result.copies.length,
              matched,
              rejected,
              unknown,
              unknown_metadata: 'excluded',
              scope: source === 'zlibrary' ? 'page' : 'first_upstream_page',
              ...(normalizeLanguages(plan.request.languages ?? []).unrecognized.length
                ? {
                    unrecognized: {
                      languages: normalizeLanguages(plan.request.languages ?? []).unrecognized,
                    },
                  }
                : {}),
            },
          }
        : {}),
    };
  }
  const rows = mergeRows(copies, plan.request, {
    filtersActive: active,
    routeOrder: plan.settings.operatorSources,
    formatPreference: plan.settings.formatPreference,
  });
  const all_answered = plan.intent.every((source) =>
    ['rows', 'empty'].includes(lines[source]!.state),
  );
  let error: SearchError['error'] | undefined;
  if (options.cancelled)
    error = modelError(new ToolFailure('cancelled', 'Search cancelled.', { retry: 'safe' }), lines);
  else if (!rows.length && !all_answered) {
    const answered = plan.intent.some((source) => ['rows', 'empty'].includes(lines[source]!.state));
    const ownSource = plan.intent.length === 1 ? plan.intent[0] : undefined;
    const failed = [...failures.values()];
    const diagnostics = failed.find((f) => f.info.detail?.diagnostics)?.info.detail.diagnostics;
    const configurationRecovery =
      !answered &&
      failed.length &&
      failed.every(
        (f) =>
          f.code === 'invalid_argument' &&
          f.info.detail?.reason === 'configuration_error' &&
          f.info.detail?.request_issued === false &&
          f.info.recovery === failed[0]!.info.recovery,
      )
        ? failed[0]!.info.recovery
        : undefined;
    const failure = ownSource
      ? failures.get(ownSource)!
      : new ToolFailure(
          answered ? 'partial_failure' : 'all_sources_failed',
          answered
            ? 'Some sources answered empty; others did not answer.'
            : 'No requested source completed the operation.',
          {
            retry: failed.some((f) => f.info.retry === 'safe') ? 'safe' : 'no',
            ...(diagnostics ? { detail: { diagnostics } } : {}),
            ...(configurationRecovery ? { recovery: configurationRecovery } : {}),
          },
        );
    error = modelError(failure, lines, ownSource);
  }
  return immutable({
    plan,
    rows,
    copies,
    sources: lines,
    all_answered,
    acceptedRecords,
    ...(error ? { error } : {}),
  });
}
export function renderSearchPage(
  prepared: PreparedSearch,
  offset = 0,
  limit = prepared.plan.request.limit ?? 10,
  acceptedRecords = prepared.acceptedRecords,
): SearchReply {
  if (
    !Number.isInteger(offset) ||
    offset < 0 ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 50
  )
    return invalid('Invalid retained page position.');
  if (prepared.error) return immutable({ error: prepared.error });
  const rows = prepared.rows.slice(offset, offset + limit);
  const incomplete = Object.entries(prepared.sources)
    .filter(([source, line]) => {
      const view = prepared.plan.sources.find((view) => view.name === source)!;
      return (
        !['rows', 'empty'].includes(line.state) &&
        (prepared.plan.intent.includes(source) ||
          !view.capabilities.search_available ||
          !view.defaultSearch.on)
      );
    })
    .map(([source, line]) => {
      const view = prepared.plan.sources.find((view) => view.name === source)!;
      const reason = !prepared.plan.intent.includes(source)
        ? (view.capabilities.off_reason ?? view.defaultSearch.reason)
        : undefined;
      return `${source}: ${line.state}${line.state === 'not_asked' ? ` (${reason ?? line.reason})` : ''}`;
    });
  const note =
    `${rows.length} file rows shown from ${acceptedRecords} accepted records, grouped by file.` +
    (incomplete.length ? ` ${incomplete.join('; ')}.` : '') +
    ' These are bounded source windows (up to 50 records per source), not everything.';
  return immutable({
    rows,
    sources: prepared.sources,
    all_answered: prepared.all_answered,
    ordering: 'fit-v1',
    note,
  });
}
