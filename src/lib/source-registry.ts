import type { MergedFilterRequest, RowFilterResult } from './row-filters.js';
import type { CallOptions } from './zlibrary-api.js';
import { ToolFailure } from './tool-errors.js';

export type Env = Readonly<Record<string, string | undefined>>;
export interface SourceCapabilities {
  configured: boolean;
  search_available: boolean;
  download_available: boolean;
  operations?: readonly string[];
  filters?: Readonly<Record<string, string>>;
  limit?: Readonly<Record<string, unknown>>;
  off_reason?: string;
}
export interface WindowRequest extends MergedFilterRequest {
  limit: 50;
  page?: number;
  multiSource: boolean;
}
/** One returned catalogue record, with its original pre-filter position. */
export interface WindowRecord {
  readonly [field: string]: unknown;
  readonly rank: number;
  readonly filterResult: Pick<RowFilterResult, 'status' | 'checks'>;
}
/** Every returned record, including rejected/unknown alternatives, belongs to
 * this one snapshot. accepted counts pass records, never the raw window size. */
export interface SearchWindow {
  rows: readonly WindowRecord[];
  accepted: number;
  more?: true;
  filtering?: Readonly<Record<string, unknown>>;
}
export interface SourceSpec {
  name: string;
  capabilities(env: Env): SourceCapabilities;
  defaultSearch(env: Env): { on: boolean; reason?: string };
  /** Withdraw this waiter on abort before issuing work. Issued work retains its
   * owner through runner TERM/grace/KILL, even if this promise settles first.
   * Return all records with rank and filter evidence in one window; refs and
   * cursors are built only from the final frozen snapshot.
   */
  searchWindow(req: WindowRequest, signal: AbortSignal): Promise<SearchWindow>;
  md5(book: unknown): string | undefined;
  /** Optional owners for registered extensions; public adapters keep existing bridge owners. */
  details?(book: Readonly<Record<string, any>>, options: CallOptions): Promise<any>;
  acquire?(args: Readonly<Record<string, any>>, options: CallOptions): Promise<any>;
}

/** Fixed insertion order is accounting order, never source preference. */
export class SourceRegistry {
  readonly sources: readonly SourceSpec[];
  constructor(sources: readonly SourceSpec[]) {
    if (
      sources.some((source) => !source.name.trim()) ||
      new Set(sources.map((source) => source.name)).size !== sources.length
    )
      throw new ToolFailure(
        'invalid_argument',
        'Source registry names must be nonempty and distinct.',
      );
    this.sources = Object.freeze(sources.map((source) => Object.freeze({ ...source })));
  }
  validate(names: readonly string[]): void {
    const registered = new Set(this.sources.map((source) => source.name));
    if (
      !names.length ||
      new Set(names).size !== names.length ||
      names.some((name) => !registered.has(name))
    )
      throw new ToolFailure('invalid_argument', 'Choose distinct registered sources.');
  }
}
