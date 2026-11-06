import { windowFixture } from './window-fixture.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { CircuitBreaker } from '../../dist/lib/circuit-breaker.js';
import { isTransientDependencyError } from '../../dist/lib/retry-manager.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { fanOut } from '../../dist/lib/fan-out.js';
import { errorEnvelope, ToolFailure } from '../../dist/lib/tool-errors.js';

beforeEach(() => jest.useFakeTimers({ now: 0 }));
afterEach(() => { expect(jest.getTimerCount()).toBe(0); jest.useRealTimers(); });
function fixture(plans) {
  const signals = new Map();
  const calls = [];
  const specs = Object.entries(plans).map(([name, plan]) => ({
    name,
    capabilities: () => ({ configured: true, search_available: true, download_available: true }),
    defaultSearch: () => ({ on: true }),
    md5: book => book.md5,
    searchWindow: (request, signal) => {
      calls.push({ name, request, at: Date.now() });
      signals.set(name, signal);
      const invoke = () => new Promise((resolve, reject) => {
        const timer = setTimeout(() => plan.error ? reject(plan.error) : resolve(windowFixture(plan.rows ?? [{ id: name }], plan.window)), plan.after_ms);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          if (!plan.ignoreAbort) reject(signal.reason);
        }, { once: true });
      });
      return plan.breaker ? plan.breaker.execute(invoke) : invoke();
    },
  }));
  return { registry: new SourceRegistry(specs), signals, calls };
}

test('F11 parallel calls return at 12 seconds with a late source and incomplete coverage', async () => {
  const f = fixture({ libgen: { after_ms: 1000 }, zlibrary: { after_ms: 2000 }, annas: { after_ms: 13000 } });
  let settled = false;
  const promise = fanOut(f.registry, { query: 'Solaris' }).then(result => { settled = true; return result; });
  expect(f.calls.map(c => c.at)).toEqual([0, 0, 0]);
  expect(f.calls.every(c => c.request.limit === 50)).toBe(true);
  await jest.advanceTimersByTimeAsync(11999);
  expect(settled).toBe(false);
  await jest.advanceTimersByTimeAsync(1);
  const result = await promise;
  expect(result.sources.annas).toEqual({ state: 'late', ms: 12000 });
  expect(result.sources.libgen).toEqual({ state: 'rows', rows: 1, ms: 1000 });
  expect(result.all_answered).toBe(false);
  expect(result.note).toContain('annas');
  expect(result.note).toContain('incomplete');
  expect([...result.windows.keys()]).toEqual(['libgen', 'zlibrary']);
});
test('F12 returns as soon as all sources settle at 2 seconds and clears its deadline', async () => {
  const f = fixture({ libgen: { after_ms: 2000, rows: [], window: { more: true, filtering: { examined: 2 } } }, zlibrary: { after_ms: 500 } });
  let settled = false;
  const promise = fanOut(f.registry, {}).then(result => { settled = true; return result; });
  await jest.advanceTimersByTimeAsync(1999);
  expect(settled).toBe(false);
  await jest.advanceTimersByTimeAsync(1);
  const result = await promise;
  expect(result.all_answered).toBe(true);
  expect(result.sources.libgen).toEqual({ state: 'empty', rows: 0, ms: 2000, more: true, filtering: { examined: 2 } });
  expect(f.signals.get('libgen').aborted).toBe(false);
  expect(jest.getTimerCount()).toBe(0);
});
test('F13 a single issued source gets its full 30-second budget', async () => {
  const f = fixture({ libgen: { after_ms: 30000 }, annas: { after_ms: 1 } });
  const promise = fanOut(f.registry, {}, { sources: ['libgen'] });
  await jest.advanceTimersByTimeAsync(12000);
  expect(f.signals.get('libgen').aborted).toBe(false);
  await jest.advanceTimersByTimeAsync(18000);
  const result = await promise;
  expect(result.sources.libgen.ms).toBe(30000);
  expect(result.sources.annas).toEqual({ state: 'not_asked', reason: 'not_requested' });
  expect(result.all_answered).toBe(true);
});
test('F14 a deadline abort reaches the child and never becomes a provider failure or late mutation', async () => {
  const breaker = new CircuitBreaker({ threshold: 2, isFailure: isTransientDependencyError });
  await expect(breaker.execute(async () => { throw Object.assign(new Error('synthetic transient'), { code: 'NETWORK_ERROR' }); })).rejects.toThrow('synthetic transient');
  const f = fixture({ libgen: { after_ms: 1 }, annas: { after_ms: 13000, breaker } });
  const promise = fanOut(f.registry, {});
  await jest.advanceTimersByTimeAsync(12000);
  const result = await promise;
  expect(f.signals.get('annas').aborted).toBe(true);
  expect(f.signals.get('libgen').aborted).toBe(false);
  expect(result.sources.annas.state).toBe('late');
  expect(result.sources.annas).not.toHaveProperty('code');
  expect(breaker.getState()).toBe('CLOSED');
  expect(breaker.getFailureCount()).toBe(1);
  const bytes = JSON.stringify(result.sources);
  await jest.advanceTimersByTimeAsync(1000);
  expect(JSON.stringify(result.sources)).toBe(bytes);
});
test('F15 client cancellation aborts every running child and yields cancelled, not late', async () => {
  const f = fixture({ libgen: { after_ms: 20000 }, annas: { after_ms: 13000 } });
  const controller = new AbortController();
  const outcome = fanOut(f.registry, {}, { signal: controller.signal }).catch(error => errorEnvelope(error, 'search'));
  await jest.advanceTimersByTimeAsync(500);
  controller.abort();
  const result = await outcome;
  expect(result.error.code).toBe('cancelled');
  expect(result.error.retry).toBe('safe');
  expect([...f.signals.values()].every(signal => signal.aborted)).toBe(true);
  expect(result).not.toHaveProperty('sources');
});
test('registry rejects duplicate and unknown sources before calls; defaults and injected preflight get lines', async () => {
  const f = fixture({ libgen: { after_ms: 0 }, annas: { after_ms: 0 } });
  const specs = f.registry.sources;
  expect(() => new SourceRegistry([specs[0], specs[0]])).toThrow();
  await expect(fanOut(f.registry, {}, { sources: ['held'] })).rejects.toMatchObject({ code: 'invalid_argument' });
  await expect(fanOut(f.registry, {}, { sources: ['libgen', 'libgen'] })).rejects.toMatchObject({ code: 'invalid_argument' });
  expect(f.calls).toEqual([]);
  const registry = new SourceRegistry([specs[0], { ...specs[1], defaultSearch: () => ({ on: false, reason: 'route_off' }) }]);
  const promise = fanOut(registry, {}, { blocked: { libgen: { reason: 'breaker_open', retry_after_s: 3 } } });
  const result = await promise;
  expect(Object.keys(result.sources)).toEqual(['libgen', 'annas']);
  expect(result.sources.libgen).toEqual({ state: 'not_asked', reason: 'breaker_open', retry_after_s: 3 });
  expect(result.sources.annas).toEqual({ state: 'not_asked', reason: 'route_off' });
  expect(result.all_answered).toBe(false);
});
test('pre-cancelled requests issue no calls and a disabled default is not wanted', async () => {
  const f = fixture({ libgen: { after_ms: 0 }, annas: { after_ms: 0 } });
  const controller = new AbortController(); controller.abort();
  await expect(fanOut(f.registry, {}, { signal: controller.signal })).rejects.toMatchObject({ code: 'cancelled' });
  expect(f.calls).toEqual([]);
  const registry = new SourceRegistry(f.registry.sources.map(spec => ({ ...spec, defaultSearch: () => ({ on: false, reason: 'credentials_missing' }) })));
  const result = await fanOut(registry, {});
  expect(result.all_answered).toBe(true);
  expect(result.sources.libgen.reason).toBe('credentials_missing');
});
test('failures keep classified codes without exposing provider prose; named order does not reorder lines', async () => {
  const f = fixture({ libgen: { after_ms: 1, error: new ToolFailure('provider_error', 'Failed.', { detail: { reason: 'http_error' }, retry_after_s: 2 }) }, annas: { after_ms: 2 } });
  const promise = fanOut(f.registry, {}, { sources: ['annas', 'libgen'] });
  await jest.advanceTimersByTimeAsync(2);
  const result = await promise;
  expect(Object.keys(result.sources)).toEqual(['libgen', 'annas']);
  expect(result.sources.libgen).toEqual({ state: 'failed', code: 'provider_error', reason: 'http_error', retry_after_s: 2, ms: 1 });
  expect(result.all_answered).toBe(false);
});
test('configurable deadline, synchronous throws and abort-ignoring children settle safely', async () => {
  const f = fixture({ libgen: { after_ms: 1 }, annas: { after_ms: 2000, ignoreAbort: true } });
  const promise = fanOut(f.registry, {}, { waitMs: 100 });
  await jest.advanceTimersByTimeAsync(100);
  const result = await promise;
  expect(result.sources.annas).toEqual({ state: 'late', ms: 100 });
  const registry = new SourceRegistry([{ ...f.registry.sources[0], searchWindow: () => { throw new Error('private upstream prose'); } }]);
  const failure = await fanOut(registry, {});
  expect(failure.sources.libgen.state).toBe('failed');
  expect(failure.note).not.toContain('private upstream prose');
  await expect(fanOut(f.registry, {}, { waitMs: -1 })).rejects.toMatchObject({ code: 'invalid_argument' });
});
