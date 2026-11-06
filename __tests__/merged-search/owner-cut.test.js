import { windowFixture } from './window-fixture.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { EapiSessionOwner } from '../../dist/lib/eapi-session.js';
import { CircuitBreaker } from '../../dist/lib/circuit-breaker.js';
import { isTransientDependencyError } from '../../dist/lib/retry-manager.js';
import { fanOut } from '../../dist/lib/fan-out.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
const identity = 'synthetic-owner';
let timeoutSpy;
beforeEach(() => {
  jest.useFakeTimers({ now: 1000000 });
  timeoutSpy = jest.spyOn(AbortSignal, 'timeout').mockImplementation(ms => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(Object.assign(new Error('deadline'), { name: 'TimeoutError' })), ms);
    return controller.signal;
  });
});
afterEach(() => { timeoutSpy.mockRestore(); jest.clearAllTimers(); jest.useRealTimers(); });
const session = input => ({ ...input, domain: 'synthetic.invalid', remix_userid: '1', remix_userkey: 'synthetic' });
const healthy = owner => async input => { if (input.action === 'login') owner.consume(session(input), identity, input.generation); return { rows: [] }; };
const pending = (_input, signal) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { context: { reason: 'aborted' } })), { once: true }));
const registryFor = work => new SourceRegistry(['fast', 'owner'].map(name => ({
  name, capabilities: () => ({ configured: true, search_available: true, download_available: true }), defaultSearch: () => ({ on: true }), md5: () => undefined,
  searchWindow: async (_request, signal) => { const result = name === 'fast' ? { rows: [] } : await work(signal); return windowFixture(result.rows, result); },
})));

test.each(['search_cut', Object.assign(new Error('comparison'), { name: 'TimeoutError', reason: 'search_cut' })])('M04 comparison origin %p cancels cold login without poisoning admission', async reason => {
  const owner = new EapiSessionOwner(); const controller = new AbortController();
  const first = owner.run(identity, pending, { signal: controller.signal, timeoutMs: 60000 }).catch(e => e);
  await jest.advanceTimersByTimeAsync(0); controller.abort(reason); await jest.advanceTimersByTimeAsync(0);
  expect((await first).context.reason).toBe('aborted');
  await expect(owner.run(identity, healthy(owner), { timeoutMs: 60000 })).resolves.toEqual({ rows: [] });
});
test('M04 genuine owner authentication deadline still starts backoff', async () => {
  const owner = new EapiSessionOwner();
  const first = owner.run(identity, pending, { timeoutMs: 50 }).catch(e => e);
  await jest.advanceTimersByTimeAsync(50);
  expect((await first).context.reason).toBe('timeout');
  await expect(owner.run(identity, healthy(owner))).rejects.toMatchObject({ context: { reason: 'auth_backoff' } });
});
test('M04 a warm comparison cut is neutral to a real HALF_OPEN breaker and admits the next healthy probe', async () => {
  const owner = new EapiSessionOwner(); await owner.run(identity, healthy(owner));
  const breaker = new CircuitBreaker({ threshold: 1, timeout: 10, isFailure: isTransientDependencyError });
  await expect(breaker.execute(async () => { throw Object.assign(new Error('synthetic'), { code: 'NETWORK_ERROR' }); })).rejects.toThrow('synthetic');
  await jest.advanceTimersByTimeAsync(11);
  const result = fanOut(registryFor(signal => breaker.execute(() => owner.run(identity, pending, { signal, timeoutMs: 60000 }))), {}, { waitMs: 100 });
  await jest.advanceTimersByTimeAsync(0); expect(breaker.getState()).toBe('HALF_OPEN');
  await jest.advanceTimersByTimeAsync(100); expect((await result).sources.owner.state).toBe('late');
  expect(breaker.getState()).toBe('OPEN'); expect(breaker.getFailureCount()).toBe(1);
  await expect(breaker.execute(() => owner.run(identity, healthy(owner)))).resolves.toEqual({ rows: [] });
  expect(breaker.getState()).toBe('CLOSED');
});
test('M03 cancelling one shared waiter retains the login work a surviving peer needs', async () => {
  const owner = new EapiSessionOwner(); let complete; let loginSignal; let calls = 0;
  const backend = (input, signal) => {
    if (input.action !== 'login') return Promise.resolve({ rows: [] });
    calls++; loginSignal = signal;
    return new Promise(resolve => { complete = () => { owner.consume(session(input), identity, input.generation); resolve({}); }; });
  };
  const peer = owner.run(identity, backend, { timeoutMs: 60000 });
  await jest.advanceTimersByTimeAsync(0);
  const result = fanOut(registryFor(signal => owner.run(identity, backend, { signal, timeoutMs: 60000 })), {}, { waitMs: 100 });
  await jest.advanceTimersByTimeAsync(100);
  expect((await result).sources.owner.state).toBe('late');
  expect(loginSignal.aborted).toBe(false); expect(calls).toBe(1);
  complete(); await expect(peer).resolves.toEqual({ rows: [] }); expect(calls).toBe(1);
});
