import { windowFixture } from './window-fixture.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setImmediate as immediate } from 'node:timers/promises';
import { fanOut } from '../../dist/lib/fan-out.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { EapiSessionOwner } from '../../dist/lib/eapi-session.js';
import { CircuitBreaker } from '../../dist/lib/circuit-breaker.js';
import { isTransientDependencyError } from '../../dist/lib/retry-manager.js';
import { runPythonBridge, liveChildCount, killAllPythonChildren, KILL_GRACE_MS } from '../../dist/lib/python-runner.js';

jest.setTimeout(30000);
const native = process.platform === 'linux' ? test : test.skip;
let dir, now, watchers, listeners;
beforeEach(() => {
  jest.useFakeTimers({ now: 1000000, doNotFake: ['nextTick', 'setImmediate', 'performance'] });
  now = 0; watchers = []; dir = fs.mkdtempSync(path.join(os.tmpdir(), 'merged-cut-'));
  listeners = new Map(['exit', 'SIGINT', 'SIGTERM'].map(event => [event, new Set(process.rawListeners(event))]));
});
const alive = pid => {
  try { const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); return !stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z '); } catch { return false; }
};
async function until(predicate) { while (!predicate()) await immediate(); }
function published(file) {
  return new Promise(resolve => {
    const watcher = fs.watch(dir, () => { if (fs.existsSync(file)) { watcher.close(); resolve(); } });
    watchers.push(watcher);
    if (fs.existsSync(file)) { watcher.close(); resolve(); }
  });
}
afterEach(async () => {
  killAllPythonChildren('SIGKILL'); await until(() => liveChildCount() === 0);
  await immediate(); await immediate();
  watchers.forEach(watcher => watcher.close());
  for (const [event, baseline] of listeners) for (const listener of process.rawListeners(event)) if (!baseline.has(listener)) process.removeListener(event, listener);
  jest.clearAllTimers(); jest.useRealTimers(); fs.rmSync(dir, { recursive: true, force: true });
});
const spec = (name, work) => ({ name, capabilities: () => ({ configured: true, search_available: true, download_available: true }), defaultSearch: () => ({ on: true }), md5: () => undefined, searchWindow: work });
const registry = work => new SourceRegistry([spec('fast', async () => windowFixture([{ id: 'fast' }])), spec('owned', work)]);
function script(privateMode = false) {
  const ready = path.join(dir, 'ready'); const release = path.join(dir, 'release');
  const gap = path.join(dir, 'readiness-gap'); const publish = path.join(dir, 'publish-ready');
  const temporary = ready + '.tmp';
  const child = `import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(600)`;
  const lines = [
    'import os,sys,json,pathlib,signal,subprocess,time',
    'signal.signal(signal.SIGTERM,signal.SIG_IGN)',
    ...(privateMode ? ['value=json.loads(sys.stdin.readline())'] : []),
    `child=subprocess.Popen([sys.executable,'-c',${JSON.stringify(child)}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)`,
    // Descendant publishes readiness after installing its own TERM disposition.
    'readiness=json.dumps([os.getpid(),child.pid])',
    `temporary=pathlib.Path(${JSON.stringify(temporary)})`,
    "with temporary.open('w') as publication:",
    '    publication.write(readiness[:1]); publication.flush()',
    `    pathlib.Path(${JSON.stringify(gap)}).touch()`,
    `    while not pathlib.Path(${JSON.stringify(publish)}).exists(): time.sleep(0.01)`,
    '    publication.write(readiness[1:])',
    `os.replace(temporary,${JSON.stringify(ready)})`,
    `while not pathlib.Path(${JSON.stringify(release)}).exists(): time.sleep(0.01)`,
    ...(privateMode ? ["value.update(domain='synthetic.invalid',remix_userid='1',remix_userkey='synthetic')", "os.write(3,(json.dumps(value)+'\\n').encode())"] : []),
    'print("late result",flush=True)',
  ];
  // Inherit ignored SIGTERM into the child before it executes its own handler.
  fs.writeFileSync(path.join(dir, 'native.py'), lines.join('\n'));
  return { ready, release, gap, publish, temporary, pids: () => JSON.parse(fs.readFileSync(ready, 'utf8')) };
}
async function ready(f) {
  const publication = published(f.ready);
  await published(f.gap);
  expect(fs.existsSync(f.ready)).toBe(false);
  expect(fs.readFileSync(f.temporary, 'utf8')).toBe('[');
  fs.writeFileSync(f.publish, 'go');
  await publication;
}
const options = () => ({ pythonPath: 'python3', scriptPath: dir, mode: 'text', env: { PATH: process.env.PATH } });

native('M01 real private login child and descendant remain owned after reply; late output never publishes', async () => {
  const f = script(true); const owner = new EapiSessionOwner(); const breaker = new CircuitBreaker({ threshold: 1, isFailure: isTransientDependencyError });
  let signal; let controls = 0;
  const backend = (input, childSignal) => breaker.execute(async () => {
    controls++;
    await runPythonBridge('native.py', options(), { signal: childSignal, timeoutMs: 60000, sessionInput: input,
      onSession: update => owner.consume(update, 'synthetic', input.generation) });
    return windowFixture([{ id: 'late' }]);
  });
  const response = fanOut(registry((_request, childSignal) => { signal = childSignal; return owner.run('synthetic', backend, { signal: childSignal, timeoutMs: 60000 }); }), {}, { waitMs: 100, now: () => now });
  await ready(f); const pids = f.pids(); expect(pids.every(alive)).toBe(true);
  now = 100; await jest.advanceTimersByTimeAsync(100); const result = await response;
  expect(signal.reason).toBe('search_cut'); expect(result.sources.owned.state).toBe('late');
  expect(liveChildCount()).toBe(1); expect(pids.every(alive)).toBe(true);
  const frozen = JSON.stringify([...result.windows]);
  fs.writeFileSync(f.release, 'go'); await until(() => !alive(pids[0]));
  expect(alive(pids[1])).toBe(true); expect(liveChildCount()).toBe(1);
  await jest.advanceTimersByTimeAsync(KILL_GRACE_MS + 1); await until(() => pids.every(pid => !alive(pid)) && liveChildCount() === 0);
  await jest.advanceTimersByTimeAsync(0);
  expect(owner.get('synthetic')).toBeUndefined(); expect(controls).toBe(1);
  expect(breaker.getFailureCount()).toBe(0); expect(breaker.getState()).toBe('CLOSED');
  expect(JSON.stringify([...result.windows])).toBe(frozen);
  await expect(owner.run('synthetic', async input => { if (input.action === 'login') owner.consume({ ...input, domain: 'synthetic.invalid', remix_userid: '1', remix_userkey: 'fresh' }, 'synthetic', input.generation); return { rows: [] }; })).resolves.toEqual({ rows: [] });
});

native.each(['caller', 'comparison'])('M02 source-owned lock outlives %s cancellation; queued waiters cannot steal a peer slot', async origin => {
  const f = script(); const lock = path.join(dir, 'slot.lock'); let held = false; let issued = 0; let released = false;
  const waiters = [];
  const acquire = signal => new Promise((resolve, reject) => {
    const entry = { resolve, reject, signal, abort: () => { const i = waiters.indexOf(entry); if (i >= 0) waiters.splice(i, 1); reject(Object.assign(new Error('withdrawn'), { context: { reason: 'aborted' } })); } };
    signal.addEventListener('abort', entry.abort, { once: true });
    const admit = () => { signal.removeEventListener('abort', entry.abort); held = true; const fd = fs.openSync(lock, 'wx'); resolve(() => { fs.closeSync(fd); fs.unlinkSync(lock); held = false; const next = waiters.shift(); if (next) next.admit(); }); };
    entry.admit = admit;
    if (signal.aborted) entry.abort(); else if (held) waiters.push(entry); else admit();
  });
  const work = async (_request, signal) => {
    const release = await acquire(signal); const number = ++issued;
    try {
      if (number === 1) await runPythonBridge('native.py', options(), { signal, timeoutMs: 60000 });
      return windowFixture([{ id: `issued-${number}` }]);
    } finally {
      // The adapter owns this lease through runner rejection and native cleanup.
      if (number === 1) await until(() => f.pids().every(pid => !alive(pid)) && liveChildCount() === 0);
      release(); released = true;
    }
  };
  const sources = registry(work);
  const before = new AbortController(); before.abort();
  await expect(fanOut(sources, {}, { signal: before.signal })).rejects.toMatchObject({ code: 'cancelled' }); expect(issued).toBe(0);
  const caller = new AbortController();
  const first = fanOut(sources, {}, { waitMs: 100, now: () => now, signal: caller.signal }).catch(e => e); await ready(f);
  now = 100; if (origin === 'caller') caller.abort();
  await jest.advanceTimersByTimeAsync(100); const firstResult = await first;
  if (origin === 'caller') expect(firstResult.code).toBe('cancelled'); else expect(firstResult.sources.owned.state).toBe('late');
  expect(held).toBe(true); expect(released).toBe(false); expect(fs.existsSync(lock)).toBe(true);
  const cancel = new AbortController();
  const cancelled = fanOut(sources, {}, { sources: ['owned'], signal: cancel.signal, now: () => now }).catch(e => e);
  cancel.abort(); expect((await cancelled).code).toBe('cancelled');
  const queued = fanOut(sources, {}, { waitMs: 100, now: () => now });
  const peer = fanOut(sources, {}, { sources: ['owned'], waitMs: 100, now: () => now });
  await jest.advanceTimersByTimeAsync(0); expect(issued).toBe(1);
  now = 200; await jest.advanceTimersByTimeAsync(100); expect((await queued).sources.owned.state).toBe('late');
  expect(issued).toBe(1); expect(waiters).toHaveLength(1); expect(held).toBe(true);
  await jest.advanceTimersByTimeAsync(KILL_GRACE_MS + 1);
  await until(() => released); await jest.advanceTimersByTimeAsync(0);
  expect((await peer).sources.owned.state).toBe('rows'); expect(issued).toBe(2);
  expect(held).toBe(false); expect(fs.existsSync(lock)).toBe(false); expect(liveChildCount()).toBe(0);
});

native('F1 watcher exists before a publication crosses the negative-exists check', async () => {
  const target = path.join(dir, 'race-ready');
  const originalWatch = fs.watch, originalExists = fs.existsSync;
  let watching = false, first = true, watchedAtCheck;
  const watch = jest.spyOn(fs, 'watch').mockImplementation((...args) => { watching = true; return originalWatch(...args); });
  const exists = jest.spyOn(fs, 'existsSync').mockImplementation(file => {
    if (file === target && first) {
      first = false; watchedAtCheck = watching;
      fs.writeFileSync(target, '[]');
      return false;
    }
    return originalExists(file);
  });
  try { const ready = published(target); expect(watchedAtCheck).toBe(true); await ready; }
  finally { exists.mockRestore(); watch.mockRestore(); }
});
