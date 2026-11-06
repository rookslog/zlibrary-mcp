import type { MergedFilterRequest } from './row-filters.js';
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
/** Adapters return accepted rows; keep rejected same-file copies separately in
 * the S4 orchestration when preparing inputs to mergeRows. */
export interface SearchWindow {
  rows: readonly unknown[];
  more?: true;
  filtering?: Readonly<Record<string, unknown>>;
}
export interface SourceSpec {
  name: string;
  capabilities(env: Env): SourceCapabilities;
  defaultSearch(env: Env): { on: boolean; reason?: string };
  searchWindow(req: WindowRequest, signal: AbortSignal): Promise<SearchWindow>;
  md5(book: unknown): string | undefined;
}

/** Fixed insertion order is accounting order, never source preference.
 * Production adapter registration is deferred to search integration.
 */
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
