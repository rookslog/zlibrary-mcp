import { jest, describe, beforeEach, afterEach, test, expect } from '@jest/globals';
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';
import { execFileSync, spawn } from 'child_process';
import { pathToFileURL } from 'url';

jest.setTimeout(30000);

/**
 * Tests for the bounded, killable bridge runner.
 *
 * These spawn a real Python child rather than mocking PythonShell: the whole
 * point of the module is what happens to an operating-system process when its
 * caller gives up, and a mock cannot show that. The bug being guarded against
 * left three python_bridge.py processes alive on dionysus 2026-08-11, the
 * oldest 9h10m old, after the sessions that started them had exited.
 */

const PYTHON = ['python3', 'python'].find((candidate) => {
  try {
    execFileSync(candidate, ['-c', 'pass'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
});

const maybe = PYTHON ? describe : describe.skip;

let tmpDir;
let runner;
let baselineProcessListeners;
let ownedCleanups;

/** True if the OS still knows about this pid. */
function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    if (process.platform === 'linux') {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
      if (stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z ')) return false;
    }
    return true;
  } catch {
    return false;
  }
}

/** Signal only a fixture-published identity; tolerate the exit/signal race. */
function killOwned(pid) {
  try { process.kill(pid, 'SIGKILL'); } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
}

/** Wait until `predicate()` holds, or the deadline passes. */
async function waitFor(predicate, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return predicate();
}

/** Observe child exit without missing an event that fired before registration. */
function waitForExit(child, timeoutMs = 8000) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolve, reject) => {
    const onExit = (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal });
    };
    const timer = setTimeout(() => {
      child.removeListener('exit', onExit);
      reject(new Error('Test-owned server exit watchdog expired'));
    }, timeoutMs);
    child.once('exit', onExit);
  });
}

maybe('python-runner', () => {
  beforeEach(async () => {
    jest.resetModules();
    ownedCleanups = [];
    baselineProcessListeners = new Map(
      ['exit', 'SIGINT', 'SIGTERM'].map((event) => [event, new Set(process.rawListeners(event))]),
    );
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'zlib-runner-'));
    runner = await import('../lib/python-runner.js');
  });

  afterEach(async () => {
    // Try every cleanup even when a previous assertion or cleanup failed.
    const results = await Promise.allSettled(ownedCleanups.reverse().map((cleanup) => cleanup()));
    runner.killAllPythonChildren('SIGKILL');
    const treesGone = await waitFor(() => runner.liveChildCount() === 0);
    for (const [event, baseline] of baselineProcessListeners) {
      for (const listener of process.rawListeners(event)) {
        if (!baseline.has(listener)) process.removeListener(event, listener);
      }
      for (const listener of baseline) {
        if (!process.rawListeners(event).includes(listener)) process.on(event, listener);
      }
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
    const failure = results.find((result) => result.status === 'rejected');
    if (failure) throw failure.reason;
    expect(treesGone).toBe(true);
  });

  /** Write a throwaway script into the temp script dir. */
  function script(name, body) {
    fs.writeFileSync(path.join(tmpDir, name), body);
    return name;
  }

  test('returns the child stdout lines on success', async () => {
    const name = script('ok.py', 'print("hello")\nprint("world")\n');

    const lines = await runner.runPythonBridge(name, {
      mode: 'text',
      pythonPath: PYTHON,
      scriptPath: tmpDir,
    });

    expect(lines).toEqual(['hello', 'world']);
  });

  const STARTUP_WATCHDOG_MS = 8000;

  /** Each process writes its own identity, then readiness after signal setup. */
  function lifecycleFixture({ descendant = false, ignoreTerm = false, successful = false,
    delayMs = 0, neverReady = false, parentIgnoreTerm = false } = {}) {
    const parentPidFile = path.join(tmpDir, 'fixture-parent.pid');
    const descendantPidFile = path.join(tmpDir, 'fixture-descendant.pid');
    const readyFile = path.join(tmpDir, 'fixture.ready');
    const childReadyFile = path.join(tmpDir, 'fixture-descendant.ready');
    const releaseFile = path.join(tmpDir, 'fixture.release');
    const publish = (file, value) => [
      `marker = pathlib.Path(${JSON.stringify(file)})`,
      `marker.with_suffix('.pending').write_text(${value})`,
      "marker.with_suffix('.pending').replace(marker)",
    ];
    const childBody = [
      'import os, pathlib, signal, time',
      ...publish(descendantPidFile, 'str(os.getpid())'),
      ...(ignoreTerm ? ['signal.signal(signal.SIGTERM, signal.SIG_IGN)'] : []),
      ...publish(childReadyFile, '"ready"'),
      'time.sleep(600)',
    ].join('\n');
    const name = script('fixture.py', [
      'import os, pathlib, signal, subprocess, sys, time',
      ...publish(parentPidFile, 'str(os.getpid())'),
      ...((!descendant || parentIgnoreTerm) && ignoreTerm ? ['signal.signal(signal.SIGTERM, signal.SIG_IGN)'] : []),
      ...(descendant ? [
        `child = subprocess.Popen([sys.executable, "-c", ${JSON.stringify(childBody)}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)`,
        `while not pathlib.Path(${JSON.stringify(childReadyFile)}).exists(): time.sleep(0.01)`,
      ] : []),
      `time.sleep(${delayMs / 1000})`,
      ...(neverReady ? [] : publish(readyFile, '"ready"')),
      ...(successful ? [
        `while not pathlib.Path(${JSON.stringify(releaseFile)}).exists(): time.sleep(0.01)`,
        'print("complete")',
      ] : ['time.sleep(600)']),
      '',
    ].join('\n'));
    const readPid = (file) => fs.existsSync(file) ? Number(fs.readFileSync(file, 'utf8')) : -1;
    return {
      name, readyFile,
      parentPid: () => readPid(parentPidFile),
      descendantPid: () => readPid(descendantPidFile),
      release: () => fs.writeFileSync(releaseFile, 'go'),
    };
  }

  /**
   * Test-only timer seam: intercept ONLY the synchronous runner deadline setup.
   * No fake clocks: after readiness its original callback gets the original
   * budget on a real wall-clock timer. Grace timers and polling are untouched.
   */
  async function withReadyBridge(fixture, runOptions, check, startupMs = STARTUP_WATCHDOG_MS) {
    const realSetTimeout = globalThis.setTimeout;
    const realClearTimeout = globalThis.clearTimeout;
    const controller = new AbortController();
    let callback;
    let delay;
    let deadline;
    let cancelled = false;
    let intercepted = 0;
    const token = { unref() { return this; } };
    const clearSpy = jest.spyOn(globalThis, 'clearTimeout').mockImplementation((timer) => {
      if (timer !== token) return realClearTimeout(timer);
      cancelled = true;
      if (deadline) realClearTimeout(deadline);
    });
    const timerSpy = jest.spyOn(globalThis, 'setTimeout').mockImplementation((fn, ms, ...args) => {
      if (ms !== runOptions.timeoutMs || intercepted > 0) return realSetTimeout(fn, ms, ...args);
      intercepted += 1;
      callback = () => fn(...args);
      delay = ms;
      return token; // READINESS_GATE_HOLD
    });
    let promise;
    let settled = false;
    let cleaned = false;
    const cleanup = async () => {
      if (cleaned) return;
      cleaned = true;
      try {
        controller.abort();
        // This freshly imported runner owns only this test's bridge tree.
        runner.killAllPythonChildren('SIGKILL');
        // Fallback targets only identities published by this fixture itself.
        for (const pid of [fixture.parentPid(), fixture.descendantPid()]) {
          if (isAlive(pid)) killOwned(pid);
        }
        if (!await waitFor(() => settled && !isAlive(fixture.parentPid()) &&
          !isAlive(fixture.descendantPid()) && runner.liveChildCount() === 0)) {
          throw new Error('Test-owned bridge cleanup did not complete');
        }
        await promise;
      } finally {
        if (deadline) realClearTimeout(deadline);
        clearSpy.mockRestore();
      }
    };
    ownedCleanups.push(cleanup);
    try {
      promise = runner.runPythonBridge(fixture.name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { ...runOptions, signal: controller.signal },
      ).catch((error) => error).then((result) => { settled = true; return result; });
    } finally {
      timerSpy.mockRestore();
    }
    try {
      expect(intercepted).toBe(1);
      const ready = await waitFor(() => fs.existsSync(fixture.readyFile), startupMs);
      if (!ready) throw new Error(`Fixture startup watchdog expired after ${startupMs}ms without readiness`);
      // A spawn failure / premature deadline is not a successful timeout test.
      expect(settled).toBe(false);
      expect(cancelled).toBe(false);
      const readyAt = Date.now();
      deadline = realSetTimeout(callback, delay); // READINESS_GATE_ARM
      await check({ promise, controller, readyAt, fixture });
    } finally {
      await cleanup();
    }
  }

  async function assertWallClockTimeout({ promise, readyAt, fixture }, timeoutMs, label) {
    const error = await promise;
    expect(error).toBeInstanceOf(Error);
    expect(error.code).toBe('TIMEOUT');
    expect(error.context.reason).toBe('timeout');
    expect(error.message).toContain(label);
    expect(Date.now() - readyAt).toBeGreaterThanOrEqual(timeoutMs - 20);
    expect(fixture.parentPid()).toBeGreaterThan(0);
    expect(await waitFor(() => !isAlive(fixture.parentPid()))).toBe(true);
  }

  test('kills the child when the wall-clock budget expires', async () => {
    await withReadyBridge(lifecycleFixture(), { timeoutMs: 500, label: 'hang-test' },
      (context) => assertWallClockTimeout(context, 500, 'hang-test'));
  });

  test('delayed readiness does not consume the short wall-clock budget', async () => {
    // Deliberately exceeds the old 300/500ms startup deadline, not a startup fix.
    await withReadyBridge(lifecycleFixture({ delayMs: 1000 }),
      { timeoutMs: 300, label: 'delayed-ready-test' },
      (context) => assertWallClockTimeout(context, 300, 'delayed-ready-test'));
  });

  test('never-ready startup fails its watchdog and cleans up the whole tree', async () => {
    const descendant = process.platform !== 'win32';
    const fixture = lifecycleFixture({ descendant, ignoreTerm: true, neverReady: true });
    let checked = false;
    await expect(withReadyBridge(fixture, { timeoutMs: 300 }, async () => {
      checked = true;
    })).rejects.toThrow(/Fixture startup watchdog expired after 8000ms without readiness/);
    expect(checked).toBe(false);
    expect(fixture.parentPid()).toBeGreaterThan(0);
    if (descendant) expect(fixture.descendantPid()).toBeGreaterThan(0);
    expect(isAlive(fixture.parentPid())).toBe(false);
    expect(isAlive(fixture.descendantPid())).toBe(false);
    expect(runner.liveChildCount()).toBe(0);
  });

  test('a timed-out call is fatal, so the retry layer does not multiply it', async () => {
    const { isRetryableError } = await import('../lib/retry-manager.js');
    await withReadyBridge(lifecycleFixture(), { timeoutMs: 300 }, async ({ promise }) => {
      const error = await promise;
      expect(error.fatal).toBe(true);
      expect(isRetryableError(error)).toBe(false);
    });
  });

  test('an abort signal kills the child instead of orphaning it', async () => {
    await withReadyBridge(lifecycleFixture(), { timeoutMs: 60000, label: 'abort-test' },
      async ({ promise, controller, fixture }) => {
        const pid = fixture.parentPid();
        expect(pid).toBeGreaterThan(0);
        controller.abort();
        const error = await promise;
        expect(error.code).toBe('TIMEOUT');
        expect(error.message).toMatch(/aborted/);
        expect(await waitFor(() => !isAlive(pid))).toBe(true);
      });
  });

  test('an abort signal kills descendants spawned by the bridge', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ descendant: true }), { timeoutMs: 60000 },
      async ({ promise, controller, fixture }) => {
        const parentPid = fixture.parentPid();
        const descendantPid = fixture.descendantPid();
        expect(parentPid).toBeGreaterThan(0);
        expect(descendantPid).toBeGreaterThan(0);
        expect(isAlive(descendantPid)).toBe(true);
        controller.abort();
        expect((await promise).code).toBe('TIMEOUT');
        expect(await waitFor(() => !isAlive(parentPid))).toBe(true);
        expect(await waitFor(() => !isAlive(descendantPid), 1500)).toBe(true);
      });
  });

  test('keeps a descendant-only process group owned after the bridge parent exits', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ descendant: true, ignoreTerm: true }),
      { timeoutMs: 300, label: 'descendant-only-test' }, async ({ promise, fixture }) => {
        expect((await promise).code).toBe('TIMEOUT');
        expect(await waitFor(() => !isAlive(fixture.parentPid()))).toBe(true);
        expect(isAlive(fixture.descendantPid())).toBe(true);
        expect(runner.liveChildCount()).toBe(1);
        expect(runner.killAllPythonChildren('SIGKILL')).toBe(1);
        expect(await waitFor(() => !isAlive(fixture.descendantPid()))).toBe(true);
        expect(await waitFor(() => runner.liveChildCount() === 0)).toBe(true);
      });
  });

  test('automatically reaps a surviving descendant after a successful parent exit', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ descendant: true, ignoreTerm: true, successful: true }),
      { timeoutMs: 60000, label: 'successful-parent-descendant-test' }, async ({ promise, fixture }) => {
        fixture.release();
        expect(await promise).toEqual(['complete']);
        expect(isAlive(fixture.descendantPid())).toBe(true);
        expect(await waitFor(() => !isAlive(fixture.descendantPid()) && runner.liveChildCount() === 0,
          runner.KILL_GRACE_MS + 3000)).toBe(true);
      });
  });

  test('a bridge that ignores SIGTERM is force-killed after the grace period', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ ignoreTerm: true }),
      { timeoutMs: 300, label: 'sigkill-escalation-test' }, async ({ promise, fixture }) => {
        expect((await promise).code).toBe('TIMEOUT');
        const pid = fixture.parentPid();
        expect(pid).toBeGreaterThan(0);
        expect(isAlive(pid)).toBe(true);
        expect(await waitFor(() => !isAlive(pid), runner.KILL_GRACE_MS + 3000)).toBe(true);
      });
  });

  test('installed signal hooks reject new work and accelerate cleanup on a second signal', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ ignoreTerm: true }),
      { timeoutMs: 60000, label: 'in-process-shutdown-test' }, async ({ promise, fixture }) => {
        const realKill = process.kill.bind(process);
        const selfSignals = [];
        let shutdownStarted = false;
        const killSpy = jest.spyOn(process, 'kill').mockImplementation((pid, signal) => {
          if (pid === process.pid) {
            selfSignals.push(signal);
            return true;
          }
          return realKill(pid, signal);
        });
        try {
          shutdownStarted = true;
          process.emit('SIGTERM', 'SIGTERM');
          await expect(runner.runPythonBridge(fixture.name, {
            mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir,
          })).rejects.toThrow(/shutting down/i);
          expect(isAlive(fixture.parentPid())).toBe(true);
          process.emit('SIGTERM', 'SIGTERM');
          expect(await waitFor(() => !isAlive(fixture.parentPid()))).toBe(true);
          expect(await waitFor(() => selfSignals.includes('SIGTERM'))).toBe(true);
          expect(runner.liveChildCount()).toBe(0);
          await promise;
        } finally {
          try {
            runner.killAllPythonChildren('SIGKILL');
            await promise;
            // Keep self-signal interception until the shutdown observer finishes,
            // including when an assertion above fails.
            if (shutdownStarted) {
              expect(await waitFor(() => selfSignals.includes('SIGTERM'))).toBe(true);
            }
          } finally {
            killSpy.mockRestore();
          }
        }
      });
  });

  function ownServer(node, fixture) {
    let cleaned = false;
    const cleanup = async () => {
      if (cleaned) return;
      cleaned = true;
      if (node.exitCode === null && node.signalCode === null) node.kill('SIGTERM');
      try {
        await waitForExit(node);
      } catch {
        node.kill('SIGKILL');
        await waitForExit(node);
      }
      const parentPid = fixture.parentPid();
      if (parentPid > 0 && (isAlive(parentPid) || isAlive(fixture.descendantPid()))) {
        try { process.kill(-parentPid, 'SIGKILL'); } catch (error) {
          if (error.code !== 'ESRCH') throw error;
        }
      }
      for (const pid of [parentPid, fixture.descendantPid()]) {
        if (isAlive(pid)) killOwned(pid);
      }
      expect(await waitFor(() => !isAlive(parentPid) && !isAlive(fixture.descendantPid()))).toBe(true);
    };
    ownedCleanups.push(cleanup);
    return cleanup;
  }

  test('server shutdown waits for TERM-grace-KILL and rejects new bridge work', async () => {
    if (process.platform === 'win32') return;

    const fixture = lifecycleFixture({ descendant: true, ignoreTerm: true });
    const descendantFile = path.join(tmpDir, 'fixture-descendant.pid');
    const rejectionFile = path.join(tmpDir, 'shutdown-rejection.txt');
    script('shutdown-new-work.py', 'print("must not spawn")\n');
    const runnerUrl = pathToFileURL(path.resolve('dist/lib/python-runner.js')).href;
    const childCode = [
      `import * as runner from ${JSON.stringify(runnerUrl)};`,
      `import fs from 'fs';`,
      `const options = { mode: 'text', pythonPath: ${JSON.stringify(PYTHON)}, scriptPath: ${JSON.stringify(tmpDir)} };`,
      `runner.runPythonBridge('fixture.py', options, { timeoutMs: 60000 }).catch(() => {});`,
      `const startupDeadline = Date.now() + 8000;`,
      `while (!fs.existsSync(${JSON.stringify(fixture.readyFile)})) {`,
      `  if (Date.now() >= startupDeadline) throw new Error('Fixture startup watchdog expired');`,
      `  await new Promise(r => setTimeout(r, 10));`,
      `}`,
      `process.on('SIGTERM', async () => {`,
      `  try { await runner.runPythonBridge('shutdown-new-work.py', options); }`,
      `  catch (error) { fs.writeFileSync(${JSON.stringify(rejectionFile)}, error.message); }`,
      `});`,
      `process.kill(process.pid, 'SIGTERM');`,
      `await new Promise(() => {});`,
    ].join('\n');
    const node = spawn(process.execPath, ['--input-type=module', '--eval', childCode], {
      env: { ...process.env, PYTHON_BRIDGE_KILL_GRACE: '150' },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    const cleanup = ownServer(node, fixture);
    let descendantPid = -1;

    try {
      if (!await waitFor(() => fs.existsSync(fixture.readyFile))) {
        throw new Error('Fixture startup watchdog expired without descendant readiness');
      }
      descendantPid = Number(fs.readFileSync(descendantFile, 'utf8'));

      const result = await waitForExit(node);

      expect(result).toEqual({ code: null, signal: 'SIGTERM' });
      expect(await waitFor(() => !isAlive(descendantPid), 3000)).toBe(true);
      expect(fs.readFileSync(rejectionFile, 'utf8')).toMatch(/shutting down/i);
    } finally {
      await cleanup();
    }
  });

  test('a second shutdown signal skips the remaining grace period', async () => {
    if (process.platform === 'win32') return;

    const fixture = lifecycleFixture({ descendant: true, ignoreTerm: true, parentIgnoreTerm: true });
    const descendantFile = path.join(tmpDir, 'fixture-descendant.pid');
    const runnerUrl = pathToFileURL(path.resolve('dist/lib/python-runner.js')).href;
    const childCode = [
      `import * as runner from ${JSON.stringify(runnerUrl)};`,
      `import fs from 'fs';`,
      `const options = { mode: 'text', pythonPath: ${JSON.stringify(PYTHON)}, scriptPath: ${JSON.stringify(tmpDir)} };`,
      `runner.runPythonBridge('fixture.py', options, { timeoutMs: 60000 }).catch(() => {});`,
      `const startupDeadline = Date.now() + 8000;`,
      `while (!fs.existsSync(${JSON.stringify(fixture.readyFile)})) {`,
      `  if (Date.now() >= startupDeadline) throw new Error('Fixture startup watchdog expired');`,
      `  await new Promise(r => setTimeout(r, 10));`,
      `}`,
      `await new Promise(() => {});`,
    ].join('\n');
    const node = spawn(process.execPath, ['--input-type=module', '--eval', childCode], {
      env: { ...process.env, PYTHON_BRIDGE_KILL_GRACE: '2000' },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    const cleanup = ownServer(node, fixture);
    let descendantPid = -1;

    try {
      if (!await waitFor(() => fs.existsSync(fixture.readyFile))) {
        throw new Error('Fixture startup watchdog expired without descendant readiness');
      }
      descendantPid = Number(fs.readFileSync(descendantFile, 'utf8'));
      node.kill('SIGTERM');
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(node.exitCode).toBeNull();
      expect(node.signalCode).toBeNull();

      const secondSignalAt = Date.now();
      node.kill('SIGTERM');
      const result = await waitForExit(node);

      expect(result).toEqual({ code: null, signal: 'SIGTERM' });
      expect(Date.now() - secondSignalAt).toBeLessThan(1000);
      expect(await waitFor(() => !isAlive(descendantPid))).toBe(true);
    } finally {
      await cleanup();
    }
  });

  test('keeps an unkillable process tree registered until it actually exits', async () => {
    if (process.platform === 'win32') return;
    await withReadyBridge(lifecycleFixture({ ignoreTerm: true }),
      { timeoutMs: 300, label: 'surviving-tree-test' }, async ({ promise, fixture }) => {
        const pid = fixture.parentPid();
        const realKill = process.kill.bind(process);
        const killSpy = jest.spyOn(process, 'kill').mockImplementation((target, signal) => {
          if (target === -pid && signal === 'SIGKILL') return true;
          return realKill(target, signal);
        });
        try {
          expect((await promise).code).toBe('TIMEOUT');
          await new Promise((resolve) => setTimeout(resolve, runner.KILL_GRACE_MS + 300));
          expect(isAlive(pid)).toBe(true);
          expect(runner.liveChildCount()).toBe(1);
        } finally {
          killSpy.mockRestore();
        }
      });
  });

  test('does not spawn anything when the caller has already aborted', async () => {
    const name = script('never.py', 'print("should not run")\n');
    const controller = new AbortController();
    controller.abort();

    const before = runner.liveChildCount();
    await expect(
      runner.runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { signal: controller.signal },
      ),
    ).rejects.toThrow(/aborted before it started/);

    expect(runner.liveChildCount()).toBe(before);
  });

  test.each([Infinity, Number.NaN, 0, -1, 1.5, 2147483648, Number.MAX_SAFE_INTEGER])(
    'falls back to the default budget for the invalid direct override %p',
    async (timeoutMs) => {
      // Mutation caught: passing timeoutMs straight to setTimeout makes these
      // values fire after 1ms instead of using the finite default budget.
      const name = script(
        `invalid-timeout-${String(timeoutMs).replace(/\W/g, '_')}.py`,
        'import time\ntime.sleep(0.05)\nprint("done")\n',
      );

      await expect(
        runner.runPythonBridge(
          name,
          { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
          { timeoutMs, label: 'invalid-direct-timeout-test' },
        ),
      ).resolves.toEqual(['done']);
    },
  );

  test('killAllPythonChildren reaps children still running', async () => {
    await withReadyBridge(lifecycleFixture(), { timeoutMs: 60000 }, async ({ promise, fixture }) => {
      const pid = fixture.parentPid();
      expect(pid).toBeGreaterThan(0);
      expect(runner.liveChildCount()).toBe(1);
      expect(runner.killAllPythonChildren('SIGKILL')).toBe(1);
      expect(await waitFor(() => !isAlive(pid))).toBe(true);
      await promise;
      expect(runner.liveChildCount()).toBe(0);
    });
  });

  test('deregisters the child after a normal run', async () => {
    const name = script('quick.py', 'print("{}")\n');

    await runner.runPythonBridge(name, {
      mode: 'text',
      pythonPath: PYTHON,
      scriptPath: tmpDir,
    });

    expect(runner.liveChildCount()).toBe(0);
  });

  test('captures the child stderr in the failure', async () => {
    const name = script(
      'fails.py',
      'import sys\nsys.stderr.write("boom on stderr\\n")\nsys.exit(1)\n',
    );

    const error = await runner
      .runPythonBridge(name, {
        mode: 'text',
        pythonPath: PYTHON,
        scriptPath: tmpDir,
      })
      .catch((err) => err);

    expect(error).toBeInstanceOf(Error);
    expect(String(error.stderr)).toContain('boom on stderr');
  });

  test('keeps the latest 200 stderr lines so the final provider envelope remains parseable', async () => {
    const { parseBridgeErrorEnvelope } = await import('../lib/python-bridge.js');
    const envelope = {
      error: 'libgen mirror did not accept a connection',
      type: 'ProviderUnreachableError',
      details: { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
    };
    const name = script(
      'verbose-failure.py',
      [
        'import json, sys',
        'for i in range(201):',
        '    print(f"diagnostic-{i}", file=sys.stderr)',
        `print(${JSON.stringify(JSON.stringify(envelope))}, file=sys.stderr)`,
        'sys.exit(1)',
        '',
      ].join('\n'),
    );

    const error = await runner
      .runPythonBridge(name, {
        mode: 'text',
        pythonPath: PYTHON,
        scriptPath: tmpDir,
      })
      .catch((err) => err);
    const stderr = String(error.stderr).split('\n');

    expect(stderr).toHaveLength(200);
    expect(stderr[0]).toBe('diagnostic-2');
    expect(parseBridgeErrorEnvelope(error.stderr)).toEqual(envelope);
  });

  test('killAllPythonChildren is a no-op with nothing running', () => {
    expect(runner.liveChildCount()).toBe(0);
    expect(runner.killAllPythonChildren()).toBe(0);
  });

  test('installExitHooks is idempotent', () => {
    const before = process.listenerCount('exit');

    runner.installExitHooks();
    runner.installExitHooks();
    runner.installExitHooks();

    expect(process.listenerCount('exit')).toBeLessThanOrEqual(before + 1);
  });
});

/**
 * Budget parsing, which needs no Python child: a fresh import per case, since
 * the budgets are module-level constants read from the environment once.
 */
describe('python-runner budget configuration', () => {
  const saved = { ...process.env };

  afterEach(() => {
    process.env = { ...saved };
  });

  /** Import the runner with the given env vars applied. */
  async function importWith(env) {
    jest.resetModules();
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    return import('../lib/python-runner.js');
  }

  test('uses the documented defaults when unset', async () => {
    const mod = await importWith({
      PYTHON_BRIDGE_TIMEOUT: undefined,
      PYTHON_BRIDGE_LONG_TIMEOUT: undefined,
      PYTHON_BRIDGE_KILL_GRACE: undefined,
    });

    expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(240000);
    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(2400000);
    expect(mod.KILL_GRACE_MS).toBe(3000);
  });

  test('honours a valid override', async () => {
    const mod = await importWith({ PYTHON_BRIDGE_TIMEOUT: '90000' });
    expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(90000);
  });

  test('honours a valid long-operation override', async () => {
    const mod = await importWith({ PYTHON_BRIDGE_LONG_TIMEOUT: '2700000' });
    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(2700000);
  });

  test('accepts Node maximum timer delay for every bridge timer', async () => {
    const mod = await importWith({
      PYTHON_BRIDGE_TIMEOUT: '2147483647',
      PYTHON_BRIDGE_LONG_TIMEOUT: '2147483647',
      PYTHON_BRIDGE_KILL_GRACE: '2147483647',
    });

    expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(2147483647);
    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(2147483647);
    expect(mod.KILL_GRACE_MS).toBe(2147483647);
  });

  test.each(['2147483648', '9007199254740991'])(
    'falls back for the oversized timer delay %s',
    async (raw) => {
      const mod = await importWith({
        PYTHON_BRIDGE_TIMEOUT: raw,
        PYTHON_BRIDGE_LONG_TIMEOUT: raw,
        PYTHON_BRIDGE_KILL_GRACE: raw,
      });

      expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(240000);
      expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(2400000);
      expect(mod.KILL_GRACE_MS).toBe(3000);
    },
  );

  // A malformed value must never SHORTEN the budget. parseInt is not enough of
  // a guard on its own: it stops at the first character it cannot use, so
  // '1.5' and '1e6' both yield 1 — a 1ms deadline that kills every bridge call
  // instantly — and '240000abc' silently yields 240000. Falling back to the
  // default is the only safe reading of any of these.
  test.each([
    ['abc'],
    ['0'],
    ['-1'],
    [''],
    ['   '],
    ['1.5'],
    ['1e6'],
    ['240000abc'],
    ['0x10'],
    ['+240000'],
    ['Infinity'],
  ])(
    'falls back to the default for the nonsense value %p',
    async (raw) => {
      const mod = await importWith({
        PYTHON_BRIDGE_TIMEOUT: raw,
        PYTHON_BRIDGE_LONG_TIMEOUT: raw,
        PYTHON_BRIDGE_KILL_GRACE: raw,
      });

      expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(240000);
      expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(2400000);
      expect(mod.KILL_GRACE_MS).toBe(3000);
    },
  );

  test('the long budget exceeds the ordinary one by default', async () => {
    // A download or an OCR pass is slow, not hung. Sharing the search budget
    // would turn a slow success into a killed subprocess.
    const mod = await importWith({
      PYTHON_BRIDGE_TIMEOUT: undefined,
      PYTHON_BRIDGE_LONG_TIMEOUT: undefined,
    });
    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBeGreaterThan(mod.DEFAULT_BRIDGE_TIMEOUT_MS);
  });

  test('the long default covers resolution, transfer, OCR, and finalization', async () => {
    const mod = await importWith({ PYTHON_BRIDGE_LONG_TIMEOUT: undefined });

    const libgenResolutionSeconds = 3 * (2 * 5 + 45);
    const transferSeconds = 1500;
    const ocrSeconds = 600;
    const finalizationHeadroomSeconds = 135;

    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(
      (libgenResolutionSeconds +
        transferSeconds +
        ocrSeconds +
        finalizationHeadroomSeconds) *
        1000,
    );
  });
});

describe('python-runner Linux procfs liveness', () => {
  const processGroup = 4242;

  function procStat(pid, state) {
    return `${pid} (bridge child) ${state} 1 ${processGroup} 0 0`;
  }

  test('treats unavailable procfs as possibly alive', async () => {
    const mod = await import('../lib/python-runner.js');
    const unavailable = () => {
      throw new Error('EACCES: permission denied, scandir /proc');
    };

    expect(mod.linuxProcessGroupPossiblyAlive(processGroup, unavailable)).toBe(true);
  });

  test('treats a scan with every stat read denied as possibly alive', async () => {
    const mod = await import('../lib/python-runner.js');
    const denied = () => {
      throw new Error('EACCES: permission denied, open stat');
    };

    expect(mod.linuxProcessGroupPossiblyAlive(processGroup, () => ['100', '101'], denied)).toBe(
      true,
    );
  });

  test('treats a readable process group containing only zombies as gone', async () => {
    const mod = await import('../lib/python-runner.js');
    const stats = new Map([
      ['100', procStat(100, 'Z')],
      ['101', procStat(101, 'Z')],
    ]);

    expect(
      mod.linuxProcessGroupPossiblyAlive(
        processGroup,
        () => [...stats.keys()],
        (entry) => stats.get(entry),
      ),
    ).toBe(false);
  });

  test('keeps ownership when a denied stat accompanies a readable zombie member', async () => {
    const mod = await import('../lib/python-runner.js');
    const denied = Object.assign(new Error('EACCES: permission denied, open stat'), {
      code: 'EACCES',
    });
    // The denied entry may be a still-running descendant of this very group
    // that changed credentials; a readable zombie sibling says nothing about
    // it, so the group must stay owned and reachable by the SIGKILL path.
    const scan = (options) =>
      mod.linuxProcessGroupPossiblyAlive(
        processGroup,
        () => ['99', '100'],
        (entry) => {
          if (entry === '99') throw denied;
          return procStat(100, 'Z');
        },
        options,
      );

    expect(scan()).toBe(true);
    expect(scan({ killDelivered: false })).toBe(true);
    // Bound: once SIGKILL has gone to the whole group, holding the record can
    // no longer kill anything and would block shutdown forever.
    expect(scan({ killDelivered: true })).toBe(false);
  });

  test('ignores a proc entry that exits during an otherwise readable zombie-only scan', async () => {
    const mod = await import('../lib/python-runner.js');
    const disappeared = Object.assign(new Error('ENOENT: process exited'), { code: 'ENOENT' });

    expect(
      mod.linuxProcessGroupPossiblyAlive(
        processGroup,
        () => ['99', '100'],
        (entry) => {
          if (entry === '99') throw disappeared;
          return procStat(100, 'Z');
        },
      ),
    ).toBe(false);
  });

  test('retains ownership when procfs listing throws even after SIGKILL delivered', async () => {
    const mod = await import('../lib/python-runner.js');
    const unavailable = () => {
      throw new Error('EACCES: permission denied, scandir /proc');
    };

    // A failed/unavailable procfs listing is not evidence of death:
    // killDelivered may bound retention only when individual entries are unreadable,
    // not when the entire listing is absent and the kernel signal says alive.
    expect(
      mod.linuxProcessGroupPossiblyAlive(processGroup, unavailable, undefined, {
        killDelivered: false,
      }),
    ).toBe(true);
    expect(
      mod.linuxProcessGroupPossiblyAlive(processGroup, unavailable, undefined, {
        killDelivered: true,
      }),
    ).toBe(true);
  });
});
