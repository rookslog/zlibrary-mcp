import { jest, describe, beforeEach, afterEach, test, expect } from '@jest/globals';
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';
import { execFileSync } from 'child_process';

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

/** True if the OS still knows about this pid. */
function isAlive(pid) {
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

/** Wait until `predicate()` holds, or the deadline passes. */
async function waitFor(predicate, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return predicate();
}

maybe('python-runner', () => {
  beforeEach(async () => {
    jest.resetModules();
    baselineProcessListeners = new Map(
      ['exit', 'SIGINT', 'SIGTERM'].map((event) => [event, new Set(process.rawListeners(event))]),
    );
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'zlib-runner-'));
    runner = await import('../lib/python-runner.js');
  });

  afterEach(() => {
    for (const [event, baseline] of baselineProcessListeners) {
      for (const listener of process.rawListeners(event)) {
        if (!baseline.has(listener)) process.removeListener(event, listener);
      }
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
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

  test('kills the child when the wall-clock budget expires', async () => {
    // A script with no timeout of its own, standing in for the un-timed
    // requests.get inside libgen_api_enhanced.
    const pidFile = path.join(tmpDir, 'hang.pid');
    const name = script(
      'hang.py',
      [
        'import os, pathlib, time',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(os.getpid()))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );

    const promise = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 500, label: 'hang-test' },
      )
      .catch((err) => err);

    expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
    const capturedPid = Number(fs.readFileSync(pidFile, 'utf8'));

    const error = await promise;

    expect(error).toBeInstanceOf(Error);
    expect(error.code).toBe('TIMEOUT');
    expect(error.message).toMatch(/hang-test/);
    expect(capturedPid).toBeGreaterThan(0);
    expect(await waitFor(() => !isAlive(capturedPid))).toBe(true);
  });

  test('a timed-out call is fatal, so the retry layer does not multiply it', async () => {
    const { isRetryableError } = await import('../lib/retry-manager.js');
    const name = script('hang2.py', 'import time\ntime.sleep(600)\n');

    const error = await runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 300 },
      )
      .catch((err) => err);

    expect(error.fatal).toBe(true);
    expect(isRetryableError(error)).toBe(false);
  });

  test('an abort signal kills the child instead of orphaning it', async () => {
    const name = script('hang3.py', 'import time\ntime.sleep(600)\n');
    const controller = new AbortController();

    const promise = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 60000, signal: controller.signal, label: 'abort-test' },
      )
      .catch((err) => err);

    await new Promise((resolve) => setTimeout(resolve, 300));
    const pid = lastSpawnedPid();
    controller.abort();

    const error = await promise;

    expect(error.code).toBe('TIMEOUT');
    expect(error.message).toMatch(/aborted/);
    expect(await waitFor(() => !isAlive(pid))).toBe(true);
  });

  test('an abort signal kills descendants spawned by the bridge', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'tree.pids');
    const name = script(
      'tree.py',
      [
        'import os, pathlib, subprocess, sys, time',
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(f"{os.getpid()} {child.pid}")`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    const controller = new AbortController();
    let parentPid = -1;
    let descendantPid = -1;

    const promise = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 60000, signal: controller.signal, label: 'tree-abort-test' },
      )
      .catch((err) => err);

    try {
      expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
      [parentPid, descendantPid] = fs
        .readFileSync(pidFile, 'utf8')
        .split(' ')
        .map(Number);
      expect(parentPid).toBeGreaterThan(0);
      expect(descendantPid).toBeGreaterThan(0);
      expect(isAlive(descendantPid)).toBe(true);

      controller.abort();
      const error = await promise;

      expect(error.code).toBe('TIMEOUT');
      expect(await waitFor(() => !isAlive(parentPid))).toBe(true);
      expect(await waitFor(() => !isAlive(descendantPid), 1500)).toBe(true);
    } finally {
      // RED leaves the grandchild alive; do not leak the regression fixture.
      if (!controller.signal.aborted) controller.abort();
      await promise;
      if (parentPid > 0 && isAlive(parentPid)) {
        process.kill(parentPid, 'SIGKILL');
      }
      if (descendantPid > 0 && isAlive(descendantPid)) {
        process.kill(descendantPid, 'SIGKILL');
      }
    }
  });

  test('keeps a descendant-only process group owned after the bridge parent exits', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'descendant-only.pid');
    const name = script(
      'parent-exits-on-term.py',
      [
        'import pathlib, subprocess, sys, time',
        'child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(child.pid))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    let descendantPid = -1;

    try {
      const error = await runner
        .runPythonBridge(
          name,
          { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
          { timeoutMs: 300, label: 'descendant-only-test' },
        )
        .catch((err) => err);
      expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
      descendantPid = Number(fs.readFileSync(pidFile, 'utf8'));
      expect(error.code).toBe('TIMEOUT');
      expect(isAlive(descendantPid)).toBe(true);
      expect(runner.liveChildCount()).toBe(1);

      expect(runner.killAllPythonChildren('SIGKILL')).toBe(1);
      expect(await waitFor(() => !isAlive(descendantPid))).toBe(true);
      expect(await waitFor(() => runner.liveChildCount() === 0)).toBe(true);
    } finally {
      if (descendantPid > 0 && isAlive(descendantPid)) {
        process.kill(descendantPid, 'SIGKILL');
      }
    }
  });

  test('automatically reaps a surviving descendant after a successful parent exit', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'successful-parent-descendant.pid');
    const name = script(
      'successful-parent-with-descendant.py',
      [
        'import pathlib, subprocess, sys',
        'child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(child.pid))`,
        'print("complete")',
        '',
      ].join('\n'),
    );
    let descendantPid = -1;

    try {
      await expect(
        runner.runPythonBridge(
          name,
          { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
          { timeoutMs: 60000, label: 'successful-parent-descendant-test' },
        ),
      ).resolves.toEqual(['complete']);
      descendantPid = Number(fs.readFileSync(pidFile, 'utf8'));
      expect(isAlive(descendantPid)).toBe(true);

      expect(
        await waitFor(
          () => !isAlive(descendantPid) && runner.liveChildCount() === 0,
          runner.KILL_GRACE_MS + 3000,
        ),
      ).toBe(true);
    } finally {
      if (descendantPid > 0 && isAlive(descendantPid)) {
        process.kill(descendantPid, 'SIGKILL');
      }
    }
  });

  test('a bridge that ignores SIGTERM is force-killed after the grace period', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'ignores-term.pid');
    const name = script(
      'ignores-term.py',
      [
        'import os, pathlib, signal, time',
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(os.getpid()))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    let pid = -1;

    const error = await runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 300, label: 'sigkill-escalation-test' },
      )
      .catch((err) => err);

    try {
      expect(error.code).toBe('TIMEOUT');
      expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
      pid = Number(fs.readFileSync(pidFile, 'utf8'));
      expect(pid).toBeGreaterThan(0);
      expect(isAlive(pid)).toBe(true);
      expect(await waitFor(() => !isAlive(pid), runner.KILL_GRACE_MS + 3000)).toBe(true);
    } finally {
      if (pid > 0 && isAlive(pid)) process.kill(pid, 'SIGKILL');
    }
  });

  test('keeps an unkillable process tree registered until it actually exits', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'survives-kill.pid');
    const name = script(
      'survives-kill.py',
      [
        'import os, pathlib, signal, time',
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(os.getpid()))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    const realKill = process.kill.bind(process);
    let pid = -1;
    let killSpy;
    const promise = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 300, label: 'surviving-tree-test' },
      )
      .catch((err) => err);

    try {
      expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
      pid = Number(fs.readFileSync(pidFile, 'utf8'));
      killSpy = jest.spyOn(process, 'kill').mockImplementation((target, signal) => {
        if (target === -pid && signal === 'SIGKILL') return true;
        return realKill(target, signal);
      });

      const error = await promise;
      expect(error.code).toBe('TIMEOUT');
      await new Promise((resolve) => setTimeout(resolve, runner.KILL_GRACE_MS + 300));

      expect(isAlive(pid)).toBe(true);
      expect(runner.liveChildCount()).toBe(1);
    } finally {
      killSpy?.mockRestore();
      if (pid > 0 && isAlive(pid)) realKill(-pid, 'SIGKILL');
      await waitFor(() => runner.liveChildCount() === 0);
    }
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

  test.each([Infinity, Number.NaN, 0, -1, 1.5])(
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
    const name = script('hang4.py', 'import time\ntime.sleep(600)\n');

    const promise = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 60000 },
      )
      .catch((err) => err);

    await new Promise((resolve) => setTimeout(resolve, 300));
    const pid = lastSpawnedPid();
    expect(runner.liveChildCount()).toBe(1);

    const killed = runner.killAllPythonChildren('SIGKILL');

    expect(killed).toBe(1);
    expect(await waitFor(() => !isAlive(pid))).toBe(true);
    await promise;
    expect(runner.liveChildCount()).toBe(0);
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
 * The pid of the most recently spawned python child of this process.
 *
 * Read from the OS rather than from the runner, so the assertion is about a
 * real process and not about bookkeeping that could itself be wrong.
 */
function lastSpawnedPid() {
  try {
    const out = execFileSync('pgrep', ['-P', String(process.pid)], {
      encoding: 'utf8',
    });
    const pids = out
      .split('\n')
      .map((line) => parseInt(line.trim(), 10))
      .filter((pid) => Number.isInteger(pid));
    return pids.length > 0 ? pids[pids.length - 1] : -1;
  } catch {
    return -1;
  }
}

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
    expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(1800000);
    expect(mod.KILL_GRACE_MS).toBe(3000);
  });

  test('honours a valid override', async () => {
    const mod = await importWith({ PYTHON_BRIDGE_TIMEOUT: '90000' });
    expect(mod.DEFAULT_BRIDGE_TIMEOUT_MS).toBe(90000);
  });

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
      expect(mod.LONG_BRIDGE_TIMEOUT_MS).toBe(1800000);
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
});
