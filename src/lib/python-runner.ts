/**
 * Bounded, killable execution of the Python bridge subprocess.
 *
 * `PythonShell.run()` returns a promise with no timeout and no handle on the
 * child, so a bridge call that never finishes has no way to end: abandoning the
 * promise (as an MCP client does when it hits its own idle timeout) leaves the
 * Python process running with nothing waiting on it. That is how three
 * `python_bridge.py search...` processes were found alive on dionysus
 * 2026-08-11, the oldest 9h10m old and belonging to a session that had already
 * exited.
 *
 * This module owns the child instead:
 *  - every run has a wall-clock deadline,
 *  - expiry (or an AbortSignal from the MCP request) sends SIGTERM and then
 *    SIGKILL, so a process wedged in an uninterruptible syscall still dies,
 *  - every live child is registered, and the registry is drained when the
 *    server itself exits.
 *
 * The Python side has its own per-provider budgets (lib/sources/config.py);
 * PYTHON_BRIDGE_TIMEOUT must stay above their worst-case sum, or the kill here
 * preempts a legitimate slow provider walk instead of catching a hang.
 */

import { PythonShell } from 'python-shell';
import type { Options as PythonShellOptions } from 'python-shell';
import { spawnSync } from 'child_process';
import { logger } from './logger.js';
import { BridgeTimeoutError } from './errors.js';

/**
 * Read a positive-integer millisecond budget from the environment.
 *
 * A malformed value must not shorten the budget: `setTimeout(fn, NaN)` fires on
 * the next tick, so a typo would kill every bridge call instantly rather than
 * loosen anything. `parseInt` is not enough of a guard on its own — it stops at
 * the first character it cannot use, so `'1.5'` and `'1e6'` both yield 1 (a 1ms
 * deadline) and `'240000abc'` yields 240000. The whole string must therefore be
 * a plain positive integer, or we fall back to the default. Mirrors
 * `_positive_float` on the Python side (lib/sources/config.py), which is
 * already safe here because it parses a float.
 */
function positiveIntEnv(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw || !/^\d+$/.test(raw)) return fallback;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** Wall-clock budget for one bridge call. */
export const DEFAULT_BRIDGE_TIMEOUT_MS = positiveIntEnv('PYTHON_BRIDGE_TIMEOUT', 240000);

/**
 * Budget for operations that are legitimately slow rather than hung: fetching
 * a multi-hundred-megabyte book, or OCR-ing a scanned one. The ordinary budget
 * is sized for a provider walk that should answer in seconds; applying it to
 * these would kill real work, which is a worse failure than the orphan this
 * module exists to prevent.
 */
export const LONG_BRIDGE_TIMEOUT_MS = positiveIntEnv('PYTHON_BRIDGE_LONG_TIMEOUT', 1800000);

/** Grace period between SIGTERM and SIGKILL. */
export const KILL_GRACE_MS = positiveIntEnv('PYTHON_BRIDGE_KILL_GRACE', 3000);

/**
 * Every bridge child currently running. Held so shutdown can reap them: a
 * child outlives its parent by default, so without this an exiting server
 * leaves orphans behind exactly like an abandoned promise does.
 */
const liveShells = new Set<PythonShell>();

let exitHooksInstalled = false;

/**
 * Signal the bridge and every subprocess it spawned.
 *
 * POSIX children are launched as their own process group, so a negative pid
 * targets the whole group. Windows has no equivalent in Node's kill API;
 * taskkill /T is the platform facility for terminating a process tree.
 */
function signalProcessTree(shell: PythonShell, signal: NodeJS.Signals): boolean {
  const pid = shell.childProcess?.pid;
  if (!pid) return false;

  if (process.platform === 'win32') {
    const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    if (result.status === 0) return true;
    try {
      return shell.childProcess.kill(signal);
    } catch {
      return false;
    }
  }

  try {
    process.kill(-pid, signal);
    return true;
  } catch {
    // The group may already be gone, or a caller may have supplied a Python
    // implementation that cannot start a new session. Direct-child fallback
    // preserves the old best effort in either case.
    try {
      return shell.childProcess.kill(signal);
    } catch {
      return false;
    }
  }
}

/** Whether any member of the isolated POSIX process group still exists. */
function processTreeIsAlive(shell: PythonShell): boolean {
  const pid = shell.childProcess?.pid;
  if (!pid) return false;
  if (process.platform === 'win32') return shell.childProcess.exitCode === null;
  try {
    process.kill(-pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * Kill every bridge subprocess still running.
 *
 * @param signal - Signal to send (SIGTERM by default)
 * @returns Number of children signalled
 */
export function killAllPythonChildren(signal: NodeJS.Signals = 'SIGTERM'): number {
  let killed = 0;
  for (const shell of Array.from(liveShells)) {
    if (signalProcessTree(shell, signal)) killed += 1;
  }
  return killed;
}

/**
 * Install process-level handlers that reap bridge children on shutdown.
 * Idempotent, and safe to call from module scope.
 */
export function installExitHooks(): void {
  if (exitHooksInstalled) return;
  exitHooksInstalled = true;

  // 'exit' is synchronous, so only a synchronous kill is possible here — which
  // is all that is needed, since the signal itself is delivered synchronously.
  process.on('exit', () => {
    killAllPythonChildren('SIGKILL');
  });

  for (const signal of ['SIGINT', 'SIGTERM'] as NodeJS.Signals[]) {
    process.on(signal, () => {
      const killed = killAllPythonChildren('SIGTERM');
      if (killed > 0) {
        logger.info(`Received ${signal}; signalled ${killed} Python bridge child(ren)`);
      }
      // Re-raise with the default disposition so normal exit semantics hold.
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
    });
  }
}

export interface RunBridgeOptions {
  /** Wall-clock budget in ms. Defaults to DEFAULT_BRIDGE_TIMEOUT_MS. */
  timeoutMs?: number;
  /** Abort signal from the MCP request, if the transport supplies one. */
  signal?: AbortSignal;
  /** Short label used in log lines and error messages. */
  label?: string;
}

/**
 * Run the Python bridge and collect its stdout lines.
 *
 * @param scriptName - Script filename, resolved against options.scriptPath
 * @param options - PythonShell options (pythonPath, scriptPath, args, mode)
 * @param runOptions - Timeout, abort signal, and log label
 * @returns Lines the script wrote to stdout
 * @throws {BridgeTimeoutError} If the budget expires or the caller aborts.
 *   Marked fatal so the retry layer does not multiply an already-long wait.
 */
export function runPythonBridge(
  scriptName: string,
  options: PythonShellOptions,
  runOptions: RunBridgeOptions = {},
): Promise<string[]> {
  const timeoutMs = runOptions.timeoutMs ?? DEFAULT_BRIDGE_TIMEOUT_MS;
  const label = runOptions.label ?? scriptName;

  installExitHooks();

  // Never spawn work the caller has already given up on.
  if (runOptions.signal?.aborted) {
    return Promise.reject(
      new BridgeTimeoutError(`${label} aborted before it started`, {
        label,
        reason: 'aborted',
      }),
    );
  }

  return new Promise<string[]>((resolve, reject) => {
    const shell = new PythonShell(scriptName, {
      ...options,
      // On POSIX this creates a new session/process group whose id is the
      // Python pid. OCR and other grandchildren inherit it, making the whole
      // bridge-owned tree addressable without walking /proc.
      detached: process.platform === 'win32' ? options.detached : true,
    });
    liveShells.add(shell);

    const output: string[] = [];
    const stderrLines: string[] = [];
    let settled = false;
    let killTimer: NodeJS.Timeout | undefined;

    // Armed here so `cleanup` below can close over it. Its callback calls
    // `failWith`, which is declared further down — safe because a setTimeout
    // callback cannot run until this synchronous block has finished.
    const timer: NodeJS.Timeout = setTimeout(() => {
      failWith(
        new BridgeTimeoutError(
          `${label} exceeded its ${timeoutMs}ms budget and was terminated. ` +
            `The subprocess was killed, so no orphan remains. ` +
            `Raise PYTHON_BRIDGE_TIMEOUT if this operation is legitimately slower.`,
          { label, timeoutMs, reason: 'timeout', stderr: stderrLines.join('\n') },
        ),
        'timeout',
      );
    }, timeoutMs);
    if (typeof timer.unref === 'function') timer.unref();

    const cleanup = () => {
      liveShells.delete(shell);
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      if (runOptions.signal) runOptions.signal.removeEventListener('abort', onAbort);
    };

    /**
     * Terminate the child: SIGTERM first, SIGKILL if it is still alive after
     * the grace period. SIGKILL alone would skip Python's own cleanup; SIGTERM
     * alone cannot dislodge a process blocked in an uninterruptible syscall,
     * which is the case this exists for.
     */
    const terminate = (why: string) => {
      signalProcessTree(shell, 'SIGTERM');
      killTimer = setTimeout(() => {
        if (process.platform === 'win32' || processTreeIsAlive(shell)) {
          logger.warn(`${label}: process tree survived SIGTERM after ${why}; sending SIGKILL`);
          signalProcessTree(shell, 'SIGKILL');
        }
      }, KILL_GRACE_MS);
      if (typeof killTimer.unref === 'function') killTimer.unref();
    };

    const failWith = (error: Error, why: string) => {
      if (settled) return;
      settled = true;
      terminate(why);
      // Leave the shell registered until terminate's grace window elapses so
      // a shutdown in the meantime still SIGKILLs it.
      clearTimeout(timer);
      if (runOptions.signal) runOptions.signal.removeEventListener('abort', onAbort);
      setTimeout(() => liveShells.delete(shell), KILL_GRACE_MS + 100).unref?.();
      reject(error);
    };

    function onAbort() {
      failWith(
        new BridgeTimeoutError(
          `${label} was aborted by the caller; the subprocess was killed.`,
          { label, reason: 'aborted', stderr: stderrLines.join('\n') },
        ),
        'abort',
      );
    }

    runOptions.signal?.addEventListener('abort', onAbort, { once: true });

    shell.on('message', (message: string) => {
      output.push(message);
    });

    shell.on('stderr', (line: string) => {
      // Bounded: a runaway child must not be able to grow this without limit.
      if (stderrLines.length < 200) stderrLines.push(line);
    });

    shell.end((err: any) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (err) {
        if (stderrLines.length > 0 && !err.stderr) {
          err.stderr = stderrLines.join('\n');
        }
        reject(err);
      } else {
        resolve(output);
      }
    });
  });
}

/** Number of bridge children currently registered (used by tests). */
export function liveChildCount(): number {
  return liveShells.size;
}
