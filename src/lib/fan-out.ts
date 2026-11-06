import { classifyFailure, ToolFailure } from './tool-errors.js';
import type { ErrorCode } from './tool-errors.js';
import type { MergedFilterRequest } from './row-filters.js';
import type { Env, SearchWindow, SourceRegistry } from './source-registry.js';

export interface NotAsked {
  reason: string;
  retry_after_s?: number;
}
export type SourceLine =
  | {
      state: 'rows' | 'empty';
      rows: number;
      ms: number;
      more?: true;
      filtering?: Readonly<Record<string, unknown>>;
    }
  | { state: 'late'; ms: number }
  | { state: 'failed'; code: ErrorCode; reason: string; ms: number; retry_after_s?: number }
  | ({ state: 'not_asked' } & NotAsked);
export interface FanOutOptions {
  sources?: readonly string[];
  env?: Env;
  signal?: AbortSignal;
  waitMs?: number;
  /** Monotonic observation clock; injectable for boundary fixtures. */
  now?: () => number;
  /** Local admission snapshots, e.g. an open breaker. No network probes. */
  blocked?: Readonly<Record<string, NotAsked>>;
}
export interface FanOutResult {
  windows: ReadonlyMap<string, SearchWindow>;
  sources: Record<string, SourceLine>;
  all_answered: boolean;
  note: string;
}
function freeze<T>(value: T, seen = new WeakSet<object>()): T {
  if (value && typeof value === 'object' && !seen.has(value)) {
    seen.add(value);
    Object.values(value).forEach((child) => freeze(child, seen));
    Object.freeze(value);
  }
  return value;
}
function readonlyWindows(windows: Map<string, SearchWindow>): ReadonlyMap<string, SearchWindow> {
  const view: ReadonlyMap<string, SearchWindow> = Object.freeze({
    get size() {
      return windows.size;
    },
    get: (name: string) => windows.get(name),
    has: (name: string) => windows.has(name),
    entries: () => windows.entries(),
    keys: () => windows.keys(),
    values: () => windows.values(),
    [Symbol.iterator]: () => windows[Symbol.iterator](),
    forEach: (
      callback: (value: SearchWindow, key: string, map: ReadonlyMap<string, SearchWindow>) => void,
      thisArg?: unknown,
    ) => windows.forEach((value, key) => callback.call(thisArg, value, key, view)),
  });
  return view;
}
const cancelled = (): ToolFailure =>
  new ToolFailure('cancelled', 'Search cancelled.', {
    retry: 'safe',
    detail: { reason: 'aborted' },
  });

/** No public response or no-rows error policy here: S4 owns that layer.
 * Every pending child gets an individual signal. Deadline aborts cannot flow
 * through the failure path; adapters must pass the signal to bridge children.
 */
export async function fanOut(
  registry: SourceRegistry,
  request: MergedFilterRequest,
  options: FanOutOptions = {},
): Promise<FanOutResult> {
  const waitMs = options.waitMs ?? 12000;
  if (!Number.isSafeInteger(waitMs) || waitMs <= 0)
    throw new ToolFailure('invalid_argument', 'Search wait must be a positive integer.');
  if (options.signal?.aborted) throw cancelled();
  if (options.sources) registry.validate(options.sources);
  const clock = options.now ?? (() => performance.now());
  const start = clock();
  const deadlineAt = start + waitMs;
  const env = options.env ?? {};
  const wanted = new Set<string>();
  const lines = new Map<string, SourceLine>();
  const windows = new Map<string, SearchWindow>();
  const pending = new Set<string>();
  const controllers = new Map<string, AbortController>();
  for (const spec of registry.sources) {
    const defaults = spec.defaultSearch(env);
    const requested = options.sources ? options.sources.includes(spec.name) : defaults.on;
    if (!requested) {
      lines.set(spec.name, {
        state: 'not_asked',
        reason: options.sources ? 'not_requested' : (defaults.reason ?? 'not_in_default_order'),
      });
      continue;
    }
    wanted.add(spec.name);
    const capabilities = spec.capabilities(env);
    const blocked = options.blocked?.[spec.name];
    if (blocked || !capabilities.search_available) {
      lines.set(spec.name, {
        state: 'not_asked',
        ...(blocked ?? { reason: capabilities.off_reason ?? defaults.reason ?? 'unavailable' }),
      });
      continue;
    }
    pending.add(spec.name);
    controllers.set(spec.name, new AbortController());
  }
  const multiSource = pending.size >= 2;
  return new Promise<FanOutResult>((resolve, reject) => {
    let done = false;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const elapsed = (observed = clock()): number => Math.max(0, Math.floor(observed - start));
    const cleanup = (): void => {
      if (deadline !== undefined) clearTimeout(deadline);
      options.signal?.removeEventListener('abort', cancel);
    };
    const finish = (observed = clock()): void => {
      if (done) return;
      done = true;
      cleanup();
      const sources = Object.fromEntries(
        registry.sources.map((spec) => [spec.name, lines.get(spec.name)!]),
      );
      const all_answered = [...wanted].every((name) =>
        ['rows', 'empty'].includes(sources[name]!.state),
      );
      const answered = registry.sources
        .filter((spec) => ['rows', 'empty'].includes(sources[spec.name]!.state))
        .map((spec) => spec.name);
      const late = registry.sources
        .filter((spec) => sources[spec.name]!.state === 'late')
        .map((spec) => spec.name);
      const failed = registry.sources
        .filter((spec) => sources[spec.name]!.state === 'failed')
        .map((spec) => spec.name);
      const rows = [...windows.values()].reduce((sum, window) => sum + window.rows.length, 0);
      const note =
        `${rows} rows from ${answered.join(', ') || 'no sources'}.` +
        (late.length ? ` ${late.join(', ')} still searching at ${elapsed(observed)} ms.` : '') +
        (failed.length ? ` ${failed.join(', ')} failed.` : '') +
        (!all_answered
          ? ' This comparison is incomplete.'
          : ' These are source windows, not everything.');
      resolve(
        Object.freeze({
          sources: freeze(sources),
          all_answered,
          note,
          windows: readonlyWindows(
            new Map(
              registry.sources
                .filter((spec) => windows.has(spec.name))
                .map((spec) => [spec.name, windows.get(spec.name)!]),
            ),
          ),
        }),
      );
    };
    const cancel = (): void => {
      if (done) return;
      done = true;
      cleanup();
      for (const name of pending) controllers.get(name)!.abort();
      reject(cancelled());
    };
    options.signal?.addEventListener('abort', cancel, { once: true });
    if (options.signal?.aborted) {
      cancel();
      return;
    }
    if (!pending.size) {
      finish();
      return;
    }
    const cut = (observed: number): void => {
      if (done) return;
      if (options.signal?.aborted) {
        cancel();
        return;
      }
      for (const name of pending) lines.set(name, { state: 'late', ms: elapsed(observed) });
      const late = [...pending];
      pending.clear();
      finish(observed);
      // Returning the snapshot never transfers or releases resource ownership.
      // Each adapter/runner retains its work through TERM, grace and KILL.
      for (const name of late) controllers.get(name)!.abort('search_cut');
    };
    const deadlineTick = (): void => {
      const observed = clock();
      if (observed >= deadlineAt) cut(observed);
      else deadline = setTimeout(deadlineTick, Math.ceil(deadlineAt - observed));
    };
    if (multiSource) {
      if (clock() >= deadlineAt) {
        cut(clock());
        return;
      }
      deadline = setTimeout(deadlineTick, Math.ceil(deadlineAt - clock()));
    }
    const settle = (name: string, window?: SearchWindow, error?: unknown): void => {
      if (done || !pending.has(name)) return;
      if (options.signal?.aborted) {
        cancel();
        return;
      }
      const observed = clock();
      if (multiSource && observed >= deadlineAt) {
        cut(observed);
        return;
      }
      if (window) {
        try {
          window = freeze(structuredClone(window));
        } catch {
          window = undefined;
          error = new ToolFailure('provider_error', 'Provider returned an invalid window.', {
            detail: { reason: 'protocol_error' },
          });
        }
      }
      pending.delete(name);
      if (window) {
        windows.set(name, window);
        lines.set(name, {
          state: window.rows.length ? 'rows' : 'empty',
          rows: window.rows.length,
          ms: elapsed(observed),
          ...(window.more ? { more: true as const } : {}),
          ...(window.filtering ? { filtering: window.filtering } : {}),
        });
      } else {
        const failure = classifyFailure(error, 'search', name);
        lines.set(name, {
          state: 'failed',
          code: failure.code,
          reason: failure.info.detail?.reason ?? 'unknown',
          ms: elapsed(observed),
          ...(failure.info.retry_after_s !== undefined
            ? { retry_after_s: failure.info.retry_after_s }
            : {}),
        });
      }
      if (!pending.size) finish(observed);
    };
    for (const spec of registry.sources) {
      if (!pending.has(spec.name) || done) continue;
      if (multiSource && clock() >= deadlineAt) {
        cut(clock());
        break;
      }
      try {
        Promise.resolve(
          spec.searchWindow(
            { ...request, limit: 50, multiSource },
            controllers.get(spec.name)!.signal,
          ),
        ).then(
          (window) => {
            if (done) return;
            if (!window || !Array.isArray(window.rows))
              settle(
                spec.name,
                undefined,
                new ToolFailure('provider_error', 'Provider returned an invalid window.', {
                  detail: { reason: 'protocol_error' },
                }),
              );
            else settle(spec.name, window);
          },
          (error) => settle(spec.name, undefined, error),
        );
      } catch (error) {
        settle(spec.name, undefined, error);
      }
    }
  });
}
