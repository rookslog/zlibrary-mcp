import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { fanOut } from '../../dist/lib/fan-out.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { RefStore } from '../../dist/lib/refs.js';
let now;
beforeEach(() => { jest.useFakeTimers({ now: 0 }); now = 0; });
afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function setup(window = { rows: [{ id: 'slow' }] }) {
  const slow = deferred(); const signals = {};
  const spec = (name, work) => ({ name, capabilities: () => ({ configured: true, search_available: true, download_available: true }), defaultSearch: () => ({ on: true }), md5: () => undefined,
    searchWindow: (_request, signal) => { signals[name] = signal; return work; } });
  return { registry: new SourceRegistry([spec('first', Promise.resolve({ rows: [{ id: 'first' }] })), spec('second', slow.promise)]), slow, signals, window };
}
test.each([11999, 12000, 12001])('M06 result observed at %i uses a strict monotonic cut before the deadline timer fires', async observed => {
  const f = setup(); const promise = fanOut(f.registry, {}, { now: () => now });
  await jest.advanceTimersByTimeAsync(0); now = observed; f.slow.resolve(f.window);
  await jest.advanceTimersByTimeAsync(0); const result = await promise;
  expect(result.sources.second.state).toBe(observed < 12000 ? 'rows' : 'late');
  expect(result.windows.has('second')).toBe(observed < 12000);
  if (observed >= 12000) expect(f.signals.second.reason).toBe('search_cut');
});
test('M06 equality has the same membership with the timer callback first; a delayed loop does not admit rows', async () => {
  for (const observed of [12000, 15000]) {
    const f = setup(); now = 0; const promise = fanOut(f.registry, {}, { now: () => now });
    await jest.advanceTimersByTimeAsync(0); now = observed;
    await jest.advanceTimersByTimeAsync(12000); const result = await promise;
    f.slow.resolve(f.window); await jest.advanceTimersByTimeAsync(0);
    expect(result.sources.second.state).toBe('late'); expect(result.windows.has('second')).toBe(false);
    expect(result.sources.second.ms).toBe(observed);
  }
});
test('M06 wall-clock jumps cannot change observation membership or elapsed milliseconds', async () => {
  const f = setup(); const promise = fanOut(f.registry, {}, { now: () => now });
  await jest.advanceTimersByTimeAsync(0); jest.setSystemTime(-900000); now = 5; f.slow.resolve(f.window);
  await jest.advanceTimersByTimeAsync(0); const result = await promise;
  expect(result.sources.second).toEqual({ state: 'rows', rows: 1, ms: 5 });
});
test('M06 freeze copies nested terminal data and fences late results from refs and retained state', async () => {
  const f = setup(); const data = { rows: [{ id: 'owned', nested: { title: 'original' } }], filtering: { matched: 1 } };
  const first = f.registry.sources[0];
  f.registry = new SourceRegistry([{ ...first, searchWindow: () => Promise.resolve(data) }, f.registry.sources[1]]);
  const promise = fanOut(f.registry, {}, { now: () => now }); await jest.advanceTimersByTimeAsync(0);
  now = 12000; await jest.advanceTimersByTimeAsync(12000); const result = await promise;
  const refs = new RefStore({ nonce: 1n });
  const rows = [...result.windows.values()].flatMap(w => w.rows).map(row => refs.mint('r', row));
  const before = JSON.stringify({ sources: result.sources, windows: [...result.windows], rows });
  data.rows[0].nested.title = 'changed'; data.rows.push({ id: 'extra' }); data.filtering.matched = 99;
  f.slow.resolve(f.window); await jest.advanceTimersByTimeAsync(0);
  expect(JSON.stringify({ sources: result.sources, windows: [...result.windows], rows })).toBe(before);
  expect(refs.counter).toBe(1n);
  expect(Object.isFrozen(result.windows.get('first').rows[0].nested)).toBe(true);
  expect(Object.isFrozen(data)).toBe(false);
  expect(() => result.windows.set('second', { rows: [] })).toThrow();
});
test('M06 caller cancellation already observed at the cut stays cancelled; completion cannot republish', async () => {
  const f = setup(); const controller = new AbortController();
  const result = fanOut(f.registry, {}, { signal: controller.signal, now: () => now }).catch(error => error);
  await jest.advanceTimersByTimeAsync(0); now = 12000; controller.abort(); f.slow.resolve(f.window);
  await jest.advanceTimersByTimeAsync(12000);
  expect((await result).code).toBe('cancelled');
  expect(f.signals.second.aborted).toBe(true);
});
test('M06 a synchronous stall past the cut cannot issue the next source', async () => {
  const f = setup(); let issued = 0;
  const sources = new SourceRegistry([
    { ...f.registry.sources[0], searchWindow: () => { now = 12000; return Promise.resolve({ rows: [{ id: 'late' }] }); } },
    { ...f.registry.sources[1], searchWindow: async () => { issued++; return { rows: [] }; } },
  ]);
  const result = await fanOut(sources, {}, { now: () => now });
  expect(issued).toBe(0);
  expect(result.sources.first.state).toBe('late'); expect(result.sources.second.state).toBe('late');
});
test('M06 default monotonic clock survives a wall-clock jump', async () => {
  const f = setup(); const result = fanOut(f.registry, {});
  await jest.advanceTimersByTimeAsync(0); jest.setSystemTime(-900000);
  await jest.advanceTimersByTimeAsync(5); f.slow.resolve(f.window); await jest.advanceTimersByTimeAsync(0);
  expect((await result).sources.second.ms).toBe(5);
});
test('M06 late terminal data is discarded without inspecting provider records', async () => {
  const f = setup(); const promise = fanOut(f.registry, {}, { now: () => now });
  await jest.advanceTimersByTimeAsync(0); now = 12000; await jest.advanceTimersByTimeAsync(12000); await promise;
  let reads = 0; f.slow.resolve({ get rows() { reads++; return [{ id: 'late' }]; } });
  await jest.advanceTimersByTimeAsync(0); expect(reads).toBe(0);
});
