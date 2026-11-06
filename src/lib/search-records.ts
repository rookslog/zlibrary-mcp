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
import type { MergedRow } from './merge.js';

export type RefillWindow = (page: number, signal: AbortSignal) => Promise<CollectedSource>;
export interface RetainedSelection {
  copy: CatalogueCopy;
  request: SearchPlan['request'];
}
interface Window {
  prepared: PreparedSearch;
}
interface RecordState {
  id: number;
  plan: SearchPlan;
  refill?: RefillWindow;
  aliases: Set<string>;
  windows: Map<number, Window>;
  pages: Map<string, SearchReply>;
  cursors: Map<string, string>;
  pending: Map<number, { promise: Promise<Window>; controller: AbortController }>;
  md5s: Set<string>;
}
type Alias = { record: RecordState } & (
  { kind: 'r'; selection: RetainedSelection } | { kind: 'c'; window: number; offset: number }
);
const unavailable = (): ToolFailure =>
  new ToolFailure('ref_unavailable', 'Retained search expired.', { detail: { reason: 'expired' } });
const md5Of = (copy: CatalogueCopy): string | undefined =>
  typeof copy.md5 === 'string' && /^[a-f0-9]{32}$/iu.test(copy.md5)
    ? copy.md5.toLowerCase()
    : undefined;

/** Internal preparation: no public handler owns this store before S4c. Capacity
 * counts whole searches, so selections, local pages and refill pages share one lifetime. */
export class SearchRecords {
  private readonly refs: RefStore;
  private readonly records = new Map<number, RecordState>();
  private serial = 0;
  constructor(private readonly options: { capacity: number; nonce?: bigint }) {
    if (!Number.isSafeInteger(options.capacity) || options.capacity < 1)
      throw new RangeError('Retained search capacity must be a positive integer.');
    this.refs = new RefStore({ nonce: options.nonce, capacity: Number.MAX_SAFE_INTEGER });
  }
  private touch(record: RecordState): void {
    if (!this.records.has(record.id)) throw unavailable();
    this.records.delete(record.id);
    this.records.set(record.id, record);
  }
  private evict(record: RecordState): void {
    this.records.delete(record.id);
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
    for (const copy of prepared.copies)
      if (visible.has(copy.ref)) {
        const selection = {
          copy: structuredClone(copy as CatalogueCopy),
          request: record.plan.request,
        };
        const ref = this.alias({ kind: 'r', record, selection });
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
      aliases: new Set(),
      windows: new Map(),
      pages: new Map(),
      cursors: new Map(),
      pending: new Map(),
      md5s: new Set(),
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
    const reply = renderSearchPage(prepared, offset);
    if ('error' in reply) return reply;
    const limit = record.plan.request.limit ?? 10;
    let next: { window: number; offset: number } | undefined;
    if (offset + limit < prepared.rows.length) next = { window: number, offset: offset + limit };
    else if (record.plan.intent.length === 1 && record.refill) {
      const line = prepared.sources[record.plan.intent[0]!]!;
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
    const promise = Promise.resolve()
      .then(async () => {
        this.touch(record);
        const stopped = new Promise<never>((_resolve, reject) =>
          controller.signal.addEventListener('abort', () => reject(unavailable()), { once: true }),
        );
        const result = await Promise.race([record.refill!(number, controller.signal), stopped]);
        this.touch(record); // Fence eviction before any aliases or pages are allocated.
        if (result.source !== record.plan.intent[0])
          throw new ToolFailure('invalid_argument', 'Refill changed source.');
        const prepared = this.prepare(record.plan, [result]);
        if (prepared.error)
          throw new ToolFailure(prepared.error.code, prepared.error.message, prepared.error);
        const window = this.retainWindow(record, prepared);
        record.windows.set(number, window);
        return window;
      })
      .finally(() => record.pending.delete(number));
    record.pending.set(number, { promise, controller });
    return promise;
  }
  async page(cursor: string): Promise<SearchReply> {
    const alias = this.lookup(cursor, 'c');
    if (alias.kind !== 'c') throw unavailable();
    if (!alias.record.windows.has(alias.window)) await this.expand(alias.record, alias.window);
    return this.materialize(alias.record, alias.window, alias.offset);
  }
}
