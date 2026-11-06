import type { Env } from './source-registry.js';

/** Inputs for the later intent resolver. An explicit list retains its intent
 * across admission refusals; an absent list lets that resolver derive defaults.
 */
export interface SearchIntentInput {
  namedSources?: readonly string[];
  operatorSources?: readonly string[];
}
export interface SearchSettings {
  operatorSources?: readonly string[];
  waitMs: number;
  formatPreference?: readonly string[];
}
const PUBLIC_SOURCES = ['libgen', 'zlibrary', 'annas'] as const;
const MAX_TIMER_MS = 2147483647;

/** Pure preparation only. Startup and handlers do not call this before S4c. */
export function parseSearchSettings(
  env: Env,
  registered: readonly string[] = PUBLIC_SOURCES,
): Readonly<SearchSettings> {
  let operatorSources: readonly string[] | undefined;
  const rawSources = env.BOOK_SOURCE_DEFAULT_ORDER;
  // Preserve the legacy empty-string/unset distinction from malformed lists.
  if (rawSources) {
    const names = rawSources.split(',').map((name) => name.trim());
    if (new Set(names).size !== names.length || names.some((name) => !registered.includes(name)))
      throw new RangeError('BOOK_SOURCE_DEFAULT_ORDER must name distinct registered sources.');
    operatorSources = Object.freeze(names);
  }
  const rawWait = env.BOOK_SOURCE_SEARCH_WAIT_MS;
  let waitMs = 12000;
  if (rawWait !== undefined) {
    const value = rawWait.trim();
    waitMs = Number(value);
    if (
      !/^\d+$/u.test(value) ||
      !Number.isSafeInteger(waitMs) ||
      waitMs < 1 ||
      waitMs > MAX_TIMER_MS
    )
      throw new RangeError(
        'BOOK_SOURCE_SEARCH_WAIT_MS must be a positive whole decimal within the timer range.',
      );
  }
  let formatPreference: readonly string[] | undefined;
  const rawFormats = env.BOOK_FORMAT_PREFERENCE;
  if (rawFormats !== undefined) {
    const formats = rawFormats
      .split(',')
      .map((value) => value.normalize('NFC').trim().toLowerCase().replace(/^\./u, ''));
    if (
      formats.some((value) => !/^[a-z0-9]+$/u.test(value)) ||
      new Set(formats).size !== formats.length
    )
      throw new RangeError('BOOK_FORMAT_PREFERENCE must name distinct nonempty extensions.');
    formatPreference = Object.freeze(formats);
  }
  return Object.freeze({
    ...(operatorSources ? { operatorSources } : {}),
    waitMs,
    ...(formatPreference ? { formatPreference } : {}),
  });
}
