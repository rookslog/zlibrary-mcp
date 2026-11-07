import { RefStore } from './refs.js';
import { ToolFailure } from './tool-errors.js';
import {
  immutable,
  prepareSearch,
  renderSearchPage,
  type SearchPlan,
  type SearchReply,
  type PreparedSearch,
  type CollectedSource,
  type CatalogueCopy,
} from './search-assembly.js';
import type { MergedRow, MergeCopy } from './merge.js';

export type RefillWindow = (page: number, signal: AbortSignal) => Promise<CollectedSource>;
export interface RetainedSelection {
  copy: CatalogueCopy & Pick<MergeCopy, 'rank' | 'filterResult'>;
  request: SearchPlan['request'];
}
interface Window {
  prepared: PreparedSearch;
}
interface RecordState {
  id: number;
  plan: SearchPlan;
  refill?: RefillWindow;
  dispatched: readonly string[];
  refusals: readonly Extract<CollectedSource, { state: 'not_asked' }>[];
  aliases: Set<string>;
  windows: Map<number, Window>;
  pages: Map<string, SearchReply>;
  cursors: Map<string, string>;
  pending: Map<
    number,
    { promise: Promise<Window>; controller: AbortController; subscribers: Set<symbol> }
  >;
  md5s: Set<string>;
  contexts: Set<number>;
  acceptedRecords: number;
}
type Alias = { record: RecordState } & (
  | { kind: 'r'; selection: RetainedSelection; context: number }
  | { kind: 'c'; window: number; offset: number }
);
const unavailable = (): ToolFailure =>
  new ToolFailure('ref_unavailable', 'Retained search expired.', { detail: { reason: 'expired' } });
const md5Of = (copy: CatalogueCopy): string | undefined =>
  typeof copy.md5 === 'string' && /^[a-f0-9]{32}$/iu.test(copy.md5)
    ? copy.md5.toLowerCase()
    : undefined;

/** Process-local search owner. Capacity
 * counts whole searches, so selections, local pages and refill pages share one lifetime. */
export class SearchRecords {
  private readonly refs: RefStore;
  private readonly records = new Map<number, RecordState>();
  private serial = 0;
  private contextSerial = 0;
  private readonly contexts = new Map<
    number,
    Readonly<{ copies: readonly MergeCopy[]; rows: readonly MergedRow[] }>
  >();
  constructor(
    private readonly options: {
      capacity: number;
      nonce?: bigint;
      maxWindows?: number;
      relationshipCapacity?: number;
    },
  ) {
    if (!Number.isSafeInteger(options.capacity) || options.capacity < 1)
      throw new RangeError('Retained search capacity must be a positive integer.');
    this.refs = new RefStore({ nonce: options.nonce, capacity: Number.MAX_SAFE_INTEGER });
    if (!Number.isSafeInteger(options.maxWindows ?? 20) || (options.maxWindows ?? 20) < 1)
      throw new RangeError('Retained window bound must be a positive integer.');
    if (
      !Number.isSafeInteger(
        options.relationshipCapacity ?? options.capacity * (options.maxWindows ?? 20),
      ) ||
      (options.relationshipCapacity ?? options.capacity * (options.maxWindows ?? 20)) < 1
    )
      throw new RangeError('Relationship capacity must be a positive integer.');
  }
  owns(ref: string): boolean {
    return ref.slice(1, 14) === this.refs.nonce;
  }
  private touch(record: RecordState): void {
    if (!this.records.has(record.id)) throw unavailable();
    this.records.delete(record.id);
    this.records.set(record.id, record);
  }
  private evict(record: RecordState): void {
    this.records.delete(record.id);
    for (const context of record.contexts) this.contexts.delete(context);
    for (const ref of record.aliases) this.refs.discard(ref);
    for (const pending of record.pending.values()) pending.controller.abort('search_evicted');
  }
  private alias(value: Alias): string {
    const ref = this.refs.mint(value.kind, value);
    value.record.aliases.add(ref);
    return ref;
  }
  private lookup(ref: string, kind: 'r' | 'c'): Alias {
    const alias = this.refs.get<Alias>(ref, kind);
    this.touch(alias.record);
    return alias;
  }
  select(ref: string): RetainedSelection {
    const alias = this.lookup(ref, 'r');
    if (alias.kind !== 'r') throw unavailable();
    return alias.selection;
  }
  related(ref: string) {
    const alias = this.lookup(ref, 'r');
    if (alias.kind !== 'r') throw unavailable();
    const context = this.contexts.get(alias.context);
    if (context) {
      this.contexts.delete(alias.context);
      this.contexts.set(alias.context, context);
    }
    return context;
  }
  private prepare(plan: SearchPlan, input: readonly CollectedSource[]): PreparedSearch {
    // Placeholder identities make allocation independent of completion order. No
    // selectable handle is minted for a rejected, ungrouped catalogue record.
    const canonical = plan.sources.flatMap((source, sourceIndex) =>
      input
        .filter((result) => result.source === source.name)
        .map((result) =>
          result.state === 'answered'
            ? {
                ...result,
                copies: result.copies.map((copy, index) => ({
                  ...copy,
                  ref: `pending-${sourceIndex}-${index}`,
                })),
              }
            : result,
        ),
    );
    // Preserve the assembler's duplicate/unknown-source validation before normalization.
    prepareSearch(plan, input);
    return prepareSearch(plan, canonical);
  }
  private retainWindow(record: RecordState, prepared: PreparedSearch): Window {
    this.touch(record);
    const byRef = new Map(prepared.copies.map((copy) => [copy.ref, copy as CatalogueCopy]));
    const kept = prepared.rows.filter((row) => {
      const md5 = md5Of(byRef.get(row.ref)!);
      return !md5 || !record.md5s.has(md5);
    });
    const visible = new Set(
      kept.flatMap((row) => [row.ref, ...(row.other_copies ?? []).map((copy) => copy.ref)]),
    );
    const mapping = new Map<string, string>();
    const context = ++this.contextSerial;
    record.contexts.add(context);
    for (const copy of prepared.copies)
      if (visible.has(copy.ref)) {
        const selection: RetainedSelection = {
          copy: structuredClone(copy) as RetainedSelection['copy'],
          request: record.plan.request,
        };
        const ref = this.alias({ kind: 'r', record, selection, context });
        selection.copy.ref = ref;
        immutable(selection);
        mapping.set(copy.ref, ref);
      }
    const rows: MergedRow[] = kept.map((row) => {
      const md5 = md5Of(byRef.get(row.ref)!);
      if (md5) record.md5s.add(md5);
      const { likely_same_file_as, ...rest } = row;
      return {
        ...rest,
        ref: mapping.get(row.ref)!,
        ...(row.other_copies
          ? {
              other_copies: row.other_copies.map((copy) => ({
                ...copy,
                ref: mapping.get(copy.ref)!,
              })),
            }
          : {}),
        ...(likely_same_file_as && mapping.has(likely_same_file_as)
          ? { likely_same_file_as: mapping.get(likely_same_file_as)! }
          : {}),
      };
    });
    this.contexts.set(
      context,
      immutable({
        rows,
        copies: prepared.copies
          .filter((copy) => visible.has(copy.ref))
          .map((copy) => ({ ...copy, ref: mapping.get(copy.ref)! })),
      }),
    );
    while (
      this.contexts.size >
      (this.options.relationshipCapacity ?? this.options.capacity * (this.options.maxWindows ?? 20))
    )
      this.contexts.delete(this.contexts.keys().next().value!);
    record.acceptedRecords += prepared.acceptedRecords;
    return { prepared: immutable({ ...prepared, rows }) };
  }
  retain(
    plan: SearchPlan,
    input: readonly CollectedSource[],
    options: { refill?: RefillWindow } = {},
  ): SearchReply {
    const prepared = this.prepare(plan, input);
    if (prepared.error) return renderSearchPage(prepared);
    const record: RecordState = {
      id: ++this.serial,
      plan,
      refill: options.refill,
      dispatched: immutable(
        Object.entries(prepared.sources)
          .filter(([, line]) => line.state !== 'not_asked')
          .map(([name]) => name),
      ),
      refusals: immutable(
        input.flatMap((result) =>
          result.state === 'not_asked'
            ? [
                {
                  ...result,
                  ...(result.failure
                    ? {
                        failure: new ToolFailure(
                          result.failure.code,
                          result.failure.message,
                          structuredClone(result.failure.info),
                        ),
                      }
                    : {}),
                },
              ]
            : [],
        ),
      ),
      aliases: new Set(),
      windows: new Map(),
      pages: new Map(),
      cursors: new Map(),
      pending: new Map(),
      md5s: new Set(),
      contexts: new Set(),
      acceptedRecords: 0,
    };
    this.records.set(record.id, record);
    while (this.records.size > this.options.capacity)
      this.evict(this.records.values().next().value!);
    record.windows.set(1, this.retainWindow(record, prepared));
    return this.materialize(record, 1, 0);
  }
  private materialize(record: RecordState, number: number, offset: number): SearchReply {
    this.touch(record);
    const key = `${number}:${offset}`,
      existing = record.pages.get(key);
    if (existing) return existing;
    const prepared = record.windows.get(number)!.prepared;
    const limit = record.plan.request.limit ?? 10;
    const reply = renderSearchPage(prepared, offset, limit, record.acceptedRecords);
    if ('error' in reply) throw new ToolFailure(reply.error.code, reply.error.message, reply.error);
    let next: { window: number; offset: number } | undefined;
    if (offset + limit < prepared.rows.length) next = { window: number, offset: offset + limit };
    else if (
      number < (this.options.maxWindows ?? 20) &&
      record.dispatched.length === 1 &&
      record.refill
    ) {
      const line = prepared.sources[record.dispatched[0]!]!;
      if ('more' in line && line.more) next = { window: number + 1, offset: 0 };
    }
    let cursor: string | undefined;
    if (next) {
      const position = `${next.window}:${next.offset}`;
      cursor = record.cursors.get(position);
      if (!cursor) {
        cursor = this.alias({ kind: 'c', record, ...next });
        record.cursors.set(position, cursor);
      }
    }
    const page = immutable({ ...reply, ...(cursor ? { next_cursor: cursor } : {}) });
    record.pages.set(key, page);
    return page;
  }
  private expand(record: RecordState, number: number): Promise<Window> {
    const existing = record.pending.get(number);
    if (existing) return existing.promise;
    const controller = new AbortController();
    let work: Promise<CollectedSource> | undefined;
    let settled = false;
    const promise = Promise.resolve()
      .then(async () => {
        this.touch(record);
        if (controller.signal.aborted) throw unavailable();
        const stopped = new Promise<never>((_resolve, reject) =>
          controller.signal.addEventListener(
            'abort',
            () =>
              reject(
                controller.signal.reason === 'search_evicted'
                  ? unavailable()
                  : new ToolFailure('cancelled', 'Refill cancelled.', { retry: 'safe' }),
              ),
            { once: true },
          ),
        );
        work = Promise.resolve(record.refill!(number, controller.signal));
        void work.then(
          () => {
            settled = true;
          },
          () => {
            settled = true;
          },
        );
        const result = await Promise.race([work, stopped]);
        if (controller.signal.aborted)
          throw new ToolFailure('cancelled', 'Refill cancelled.', { retry: 'safe' });
        this.touch(record); // Fence eviction before any aliases or pages are allocated.
        if (result.source !== record.dispatched[0])
          throw new ToolFailure('invalid_argument', 'Refill changed source.');
        const prepared = this.prepare(record.plan, [result, ...record.refusals]);
        if (prepared.error && result.state !== 'answered')
          throw new ToolFailure(prepared.error.code, prepared.error.message, prepared.error);
        const window = this.retainWindow(record, prepared);
        record.windows.set(number, window);
        return window;
      })
      .finally(() => {
        const release = () => {
          record.pending.delete(number);
        };
        if (controller.signal.aborted && work && !settled) void work.then(release, release);
        else release();
      });
    record.pending.set(number, { promise, controller, subscribers: new Set() });
    return promise;
  }
  async page(
    cursor: string,
    options: { signal?: AbortSignal; timeoutMs?: number } = {},
  ): Promise<SearchReply> {
    if (options.signal?.aborted)
      throw new ToolFailure('cancelled', 'Call cancelled.', { retry: 'safe' });
    const alias = this.lookup(cursor, 'c');
    if (alias.kind !== 'c') throw unavailable();
    if (!alias.record.windows.has(alias.window)) {
      const promise = this.expand(alias.record, alias.window);
      const pending = alias.record.pending.get(alias.window)!;
      const subscriber = Symbol();
      const waitingAt = performance.now();
      const waitFailure = (
        code: 'cancelled' | 'provider_timeout',
        reason: string,
        message: string,
      ) =>
        new ToolFailure(code, message, {
          source: alias.record.dispatched[0],
          retry: 'safe',
          detail: { reason },
          attempts: alias.record.plan.sources.map((view) => {
            const refusal = alias.record.refusals.find((line) => line.source === view.name);
            return refusal
              ? {
                  source: refusal.source,
                  state: refusal.state,
                  reason: refusal.reason,
                  ...(refusal.retry_after_s !== undefined
                    ? { retry_after_s: refusal.retry_after_s }
                    : {}),
                }
              : {
                  source: view.name,
                  state: 'failed',
                  code,
                  reason,
                  ms: Math.max(0, Math.floor(performance.now() - waitingAt)),
                };
          }),
        });
      if (pending.controller.signal.aborted)
        throw waitFailure(
          'provider_timeout',
          'refill_draining',
          'Previous refill is still draining; retry this cursor.',
        );
      pending.subscribers.add(subscriber);
      await new Promise<void>((resolve, reject) => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        let finished = false;
        const finish = () => {
          if (finished) return false;
          finished = true;
          if (timer !== undefined) clearTimeout(timer);
          options.signal?.removeEventListener('abort', abort);
          pending.subscribers.delete(subscriber);
          return true;
        };
        const abort = () => {
          if (!finish()) return;
          if (!pending.subscribers.size) pending.controller.abort('caller_cancelled');
          reject(waitFailure('cancelled', 'aborted', 'Call cancelled.'));
        };
        // A cursor's work is shared, but each caller owns its own wait budget.
        // Expiry withdraws that subscriber; it is not a provider health verdict.
        if (
          options.timeoutMs !== undefined &&
          Number.isFinite(options.timeoutMs) &&
          options.timeoutMs > 0 &&
          options.timeoutMs <= 2147483647
        )
          timer = setTimeout(() => {
            if (!finish()) return;
            if (!pending.subscribers.size) pending.controller.abort('caller_timeout');
            reject(
              waitFailure(
                'provider_timeout',
                'caller_timeout',
                'Refill caller wait budget exceeded.',
              ),
            );
          }, options.timeoutMs);
        options.signal?.addEventListener('abort', abort, { once: true });
        if (options.signal?.aborted) abort();
        void promise.then(
          () => {
            if (finish()) resolve();
          },
          (error) => {
            if (finish())
              reject(
                error instanceof ToolFailure && error.code === 'cancelled'
                  ? waitFailure('cancelled', 'aborted', error.message)
                  : error,
              );
          },
        );
      });
    }
    if (options.signal?.aborted)
      throw new ToolFailure('cancelled', 'Call cancelled.', { retry: 'safe' });
    return this.materialize(alias.record, alias.window, alias.offset);
  }
}
