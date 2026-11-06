import { immutable, planSearch, type SourceView } from './search-assembly.js';
import type { SearchSettings } from './search-settings.js';
import { ToolFailure } from './tool-errors.js';

export interface LocalSearchState {
  /** Private owner error; limits project only the fields below. */
  refusal?: unknown;
  breaker?: 'closed' | 'open' | 'half_open';
  breakerBlocked?: boolean;
  breakerRetryAfterS?: number;
  admission?: 'open' | 'backoff';
  admissionRetryAfterS?: number;
}
export interface RecentCount {
  searches: number;
  late: number;
  failed: number;
}
type JobState = 'rows' | 'empty' | 'late' | 'failed';
const invalid = (): never => {
  throw new ToolFailure('invalid_argument', 'Invalid local search state.');
};

/** The denominator is dispatched source jobs, not network requests. A new
 * instance after restart is empty; admission refusals and caller withdrawals
 * do not consume a slot. */
export class RecentSearches {
  private readonly outcomes = new Map<string, JobState[]>();
  constructor(names: readonly string[]) {
    if (new Set(names).size !== names.length || names.some((name) => !name)) invalid();
    names.forEach((name) => this.outcomes.set(name, []));
  }
  record(source: string, line: { state: string }): void {
    const jobs = this.outcomes.get(source);
    if (!jobs) invalid();
    if (line.state === 'not_asked') return;
    if (!['rows', 'empty', 'late', 'failed'].includes(line.state)) invalid();
    jobs!.push(line.state as JobState);
    if (jobs!.length > 20) jobs!.shift();
  }
  snapshot(): Readonly<Record<string, RecentCount>> {
    return immutable(
      Object.fromEntries(
        [...this.outcomes].map(([name, jobs]) => [
          name,
          {
            searches: jobs.length,
            late: jobs.filter((state) => state === 'late').length,
            failed: jobs.filter((state) => state === 'failed').length,
          },
        ]),
      ),
    );
  }
}

/** Pure limits projection over captured capabilities and read-only local state;
 * never calls a registry, session owner or live account itself. */
export function projectSearchLimits(
  views: readonly SourceView[],
  settings: SearchSettings,
  local: Readonly<Record<string, LocalSearchState>> = {},
  recent: Readonly<Record<string, RecentCount>> = {},
) {
  const plan = planSearch({ query: '__local_limits__' }, views, settings);
  const sources = Object.fromEntries(
    views.map((view) => {
      const state = local[view.name] ?? {},
        breaker = state.breaker ?? 'closed',
        admission = state.admission ?? 'open';
      if (
        !['closed', 'open', 'half_open'].includes(breaker) ||
        !['open', 'backoff'].includes(admission)
      )
        invalid();
      const waits: number[] = [];
      for (const [enabled, value] of [
        [breaker === 'open' || state.breakerBlocked === true, state.breakerRetryAfterS],
        [admission === 'backoff', state.admissionRetryAfterS],
      ] as const) {
        if (enabled) {
          if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) invalid();
          waits.push(value!);
        }
      }
      const count = recent[view.name] ?? { searches: 0, late: 0, failed: 0 };
      if (
        Object.values(count).some((value) => !Number.isInteger(value) || value < 0) ||
        count.searches > 20 ||
        count.late + count.failed > count.searches
      )
        invalid();
      return [
        view.name,
        {
          ...view.capabilities,
          filters: {
            year_from: 'local',
            year_to: 'local',
            languages: 'local',
            formats: 'local',
            exact: 'local',
          },
          default_search: plan.intent.includes(view.name),
          ...(!plan.intent.includes(view.name) ? { off_reason: plan.excluded[view.name] } : {}),
          breaker,
          admission,
          ...(waits.length ? { retry_after_s: Math.ceil(Math.max(...waits)) } : {}),
          recent: count,
        },
      ];
    }),
  );
  return immutable(
    structuredClone({
      search: {
        default_sources: plan.intent,
        ...(settings.operatorSources ? { route_order: settings.operatorSources } : {}),
        wait_ms: settings.waitMs,
        ordering: 'fit-v1',
        ...(settings.formatPreference ? { format_preference: settings.formatPreference } : {}),
      },
      sources,
    }),
  );
}
