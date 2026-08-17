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

/** Observe child exit without missing an event that fired before registration. */
function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolve) => {
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });
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

  test('installed signal hooks reject new work and accelerate cleanup on a second signal', async () => {
    if (process.platform === 'win32') return;

    const pidFile = path.join(tmpDir, 'in-process-shutdown.pid');
    const name = script(
      'in-process-shutdown.py',
      [
        'import os, pathlib, signal, time',
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
        `pathlib.Path(${JSON.stringify(pidFile)}).write_text(str(os.getpid()))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    const run = runner
      .runPythonBridge(
        name,
        { mode: 'text', pythonPath: PYTHON, scriptPath: tmpDir },
        { timeoutMs: 60000, label: 'in-process-shutdown-test' },
      )
      .catch((error) => error);
    const realKill = process.kill.bind(process);
    const selfSignals = [];
    const killSpy = jest.spyOn(process, 'kill').mockImplementation((pid, signal) => {
      if (pid === process.pid) {
        selfSignals.push(signal);
        return true;
      }
      return realKill(pid, signal);
    });
    let pid = -1;

    try {
      expect(await waitFor(() => fs.existsSync(pidFile))).toBe(true);
      pid = Number(fs.readFileSync(pidFile, 'utf8'));

      process.emit('SIGTERM', 'SIGTERM');
      await expect(
        runner.runPythonBridge(name, {
          mode: 'text',
          pythonPath: PYTHON,
          scriptPath: tmpDir,
        }),
      ).rejects.toThrow(/shutting down/i);
      expect(isAlive(pid)).toBe(true);

      process.emit('SIGTERM', 'SIGTERM');
      expect(await waitFor(() => !isAlive(pid))).toBe(true);
      expect(await waitFor(() => selfSignals.includes('SIGTERM'))).toBe(true);
      expect(runner.liveChildCount()).toBe(0);
      await run;
    } finally {
      killSpy.mockRestore();
      if (pid > 0 && isAlive(pid)) realKill(pid, 'SIGKILL');
    }
  });

  test('server shutdown waits for TERM-grace-KILL and rejects new bridge work', async () => {
    if (process.platform === 'win32') return;

    const descendantFile = path.join(tmpDir, 'shutdown-descendant.pid');
    const rejectionFile = path.join(tmpDir, 'shutdown-rejection.txt');
    script(
      'shutdown-tree.py',
      [
        'import pathlib, subprocess, sys, time',
        'child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)',
        `pathlib.Path(${JSON.stringify(descendantFile)}).write_text(str(child.pid))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    script('shutdown-new-work.py', 'print("must not spawn")\n');
    const runnerUrl = pathToFileURL(path.resolve('dist/lib/python-runner.js')).href;
    const childCode = [
      `import * as runner from ${JSON.stringify(runnerUrl)};`,
      `import fs from 'fs';`,
      `const options = { mode: 'text', pythonPath: ${JSON.stringify(PYTHON)}, scriptPath: ${JSON.stringify(tmpDir)} };`,
      `runner.runPythonBridge('shutdown-tree.py', options, { timeoutMs: 60000 }).catch(() => {});`,
      `while (!fs.existsSync(${JSON.stringify(descendantFile)})) await new Promise(r => setTimeout(r, 10));`,
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
    let descendantPid = -1;

    try {
      expect(await waitFor(() => fs.existsSync(descendantFile))).toBe(true);
      descendantPid = Number(fs.readFileSync(descendantFile, 'utf8'));

      const result = await waitForExit(node);

      expect(result).toEqual({ code: null, signal: 'SIGTERM' });
      expect(await waitFor(() => !isAlive(descendantPid), 3000)).toBe(true);
      expect(fs.readFileSync(rejectionFile, 'utf8')).toMatch(/shutting down/i);
    } finally {
      if (node.exitCode === null && node.signalCode === null) node.kill('SIGKILL');
      if (descendantPid > 0 && isAlive(descendantPid)) process.kill(descendantPid, 'SIGKILL');
    }
  });

  test('a second shutdown signal skips the remaining grace period', async () => {
    if (process.platform === 'win32') return;

    const descendantFile = path.join(tmpDir, 'second-signal-descendant.pid');
    script(
      'second-signal-tree.py',
      [
        'import pathlib, signal, subprocess, sys, time',
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
        'child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)',
        `pathlib.Path(${JSON.stringify(descendantFile)}).write_text(str(child.pid))`,
        'time.sleep(600)',
        '',
      ].join('\n'),
    );
    const runnerUrl = pathToFileURL(path.resolve('dist/lib/python-runner.js')).href;
    const childCode = [
      `import * as runner from ${JSON.stringify(runnerUrl)};`,
      `import fs from 'fs';`,
      `const options = { mode: 'text', pythonPath: ${JSON.stringify(PYTHON)}, scriptPath: ${JSON.stringify(tmpDir)} };`,
      `runner.runPythonBridge('second-signal-tree.py', options, { timeoutMs: 60000 }).catch(() => {});`,
      `while (!fs.existsSync(${JSON.stringify(descendantFile)})) await new Promise(r => setTimeout(r, 10));`,
      `await new Promise(() => {});`,
    ].join('\n');
    const node = spawn(process.execPath, ['--input-type=module', '--eval', childCode], {
      env: { ...process.env, PYTHON_BRIDGE_KILL_GRACE: '2000' },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let descendantPid = -1;

    try {
      expect(await waitFor(() => fs.existsSync(descendantFile))).toBe(true);
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
      if (node.exitCode === null && node.signalCode === null) node.kill('SIGKILL');
      if (descendantPid > 0 && isAlive(descendantPid)) process.kill(descendantPid, 'SIGKILL');
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
});
