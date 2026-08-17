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
import { readdirSync, readFileSync } from 'fs';
import { logger } from './logger.js';
import { BridgeTimeoutError } from './errors.js';

/**
 * Read a positive-integer millisecond budget within Node's timer range.
 *
 * A malformed value must not shorten the budget: `setTimeout(fn, NaN)` fires on
 * the next tick, so a typo would kill every bridge call instantly rather than
 * loosen anything. `parseInt` is not enough of a guard on its own — it stops at
 * the first character it cannot use, so `'1.5'` and `'1e6'` both yield 1 (a 1ms
 * deadline) and `'240000abc'` yields 240000. The whole string must therefore be
 * a plain positive integer no larger than 2,147,483,647, or we fall back to
 * the default. Mirrors
 * `_positive_float` on the Python side (lib/sources/config.py), which is
 * already safe here because it parses a float.
 */
const MAX_TIMER_DELAY_MS = 2_147_483_647;
const MAX_STDERR_LINES = 200;

function positiveIntOrFallback(value: unknown, fallback: number): number {
  return Number.isSafeInteger(value) &&
    (value as number) > 0 &&
    (value as number) <= MAX_TIMER_DELAY_MS
    ? (value as number)
    : fallback;
}

function positiveIntEnv(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw || !/^\d+$/.test(raw)) return fallback;
  return positiveIntOrFallback(Number(raw), fallback);
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
export const LONG_BRIDGE_TIMEOUT_MS = positiveIntEnv('PYTHON_BRIDGE_LONG_TIMEOUT', 2400000);

/** Grace period between SIGTERM and SIGKILL. */
export const KILL_GRACE_MS = positiveIntEnv('PYTHON_BRIDGE_KILL_GRACE', 3000);

/**
 * Every bridge child currently running. Held so shutdown can reap them: a
 * child outlives its parent by default, so without this an exiting server
 * leaves orphans behind exactly like an abandoned promise does.
 */
interface ProcessTreeRecord {
  shell: PythonShell;
  /** POSIX process-group id, retained after the direct child exits. */
  pid: number;
  label: string;
  livenessTimer?: NodeJS.Timeout;
  killTimer?: NodeJS.Timeout;
  terminationStarted?: boolean;
  /**
   * Whether SIGKILL has already been aimed at the whole group. Once it has,
   * an unreadable procfs entry can no longer justify holding the record: the
   * strongest signal this process can send has been sent, and holding on
   * would block shutdown forever instead of killing anything.
   */
  killDelivered?: boolean;
}

const liveTrees = new Set<ProcessTreeRecord>();

let exitHooksInstalled = false;
let shutdownSignal: NodeJS.Signals | undefined;
let shutdownObservationTimer: NodeJS.Timeout | undefined;

/**
 * Signal the bridge and every subprocess it spawned.
 *
 * POSIX children are launched as their own process group, so a negative pid
 * targets the whole group. Windows has no equivalent in Node's kill API;
 * taskkill /T is the platform facility for terminating a process tree.
 */
function signalProcessTree(tree: ProcessTreeRecord, signal: NodeJS.Signals): boolean {
  const { shell, pid } = tree;

  // Recorded on the attempt, not on success. A SIGKILL that cannot be
  // delivered means either the group is already gone or this process will
  // never have the privilege to kill it; retaining ownership changes neither
  // and would deadlock `observeShutdownUntilGone`.
  if (signal === 'SIGKILL') tree.killDelivered = true;

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

/**
 * Whether procfs shows an executable member of a Linux process group.
 *
 * Ownership is released only when the scan positively accounts for every
 * entry: `kill(-pid, 0)` has already said the group exists, so an entry whose
 * stat cannot be read is an unanswered question, not a negative answer. A
 * descendant that changed credentials under a restricted /proc mount reads as
 * exactly that, and a readable zombie sibling is no evidence about it — a
 * scan that saw one readable zombie member and one denied entry must still
 * report the group as possibly alive so the SIGTERM -> SIGKILL path runs.
 *
 * `killDelivered` is the bound on that conservatism for per-entry read failures.
 * Holding the record only buys the kill path, so once SIGKILL has been aimed at
 * the whole group, an entry whose stat remains permanently unreadable no longer
 * pins the record. However, when the /proc listing itself is completely unavailable,
 * a missing listing is not evidence of death — ownership is retained until
 * `kill(-pid, 0)` reports the group gone (ESRCH).
 *
 * The readers are injectable so permission and availability failures can be
 * exercised deterministically without weakening the real process-tree tests.
 */
export function linuxProcessGroupPossiblyAlive(
  pid: number,
  listEntries: () => string[] = () => readdirSync('/proc'),
  readStat: (entry: string) => string = (entry) => readFileSync(`/proc/${entry}/stat`, 'utf8'),
  { killDelivered = false }: { killDelivered?: boolean } = {},
): boolean {
  try {
    let sawUnreadableEntry = false;
    for (const entry of listEntries()) {
      if (!/^\d+$/.test(entry)) continue;
      try {
        const stat = readStat(entry);
        const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
        const state = fields[0];
        const processGroup = Number(fields[2]);
        // A zombie cannot execute or hold resources, so it alone does not keep
        // the group alive; it is simply not evidence either way.
        if (processGroup === pid && state !== 'Z') return true;
      } catch (error) {
        // A vanished entry is the ordinary list/stat race and answers itself.
        // Any other failure may be permission denial hiding a live member of
        // this very group.
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') sawUnreadableEntry = true;
      }
    }
    return sawUnreadableEntry && !killDelivered;
  } catch {
    // kill(0) already established that the group may exist. An unavailable
    // or unreadable procfs listing cannot safely contradict that kernel
    // liveness result; ownership is released only when kill(-pid, 0) reports ESRCH.
    return true;
  }
}

/** Whether any member of the isolated POSIX process group still exists. */
function processTreeIsAlive(tree: ProcessTreeRecord): boolean {
  const { shell, pid } = tree;
  if (process.platform === 'win32') return shell.childProcess.exitCode === null;
  try {
    process.kill(-pid, 0);
    if (process.platform !== 'linux') return true;

    // kill(0) also succeeds for zombies. A descendant whose direct parent
    // exited can remain as a zombie until the container's init reaps it; such
    // a task cannot execute or hold resources and must not pin the ownership
    // record forever. Inspect the Linux process-group field and require at
    // least one non-zombie member. Other POSIX hosts retain kill(0) semantics.
    return linuxProcessGroupPossiblyAlive(pid, undefined, undefined, {
      killDelivered: tree.killDelivered,
    });
  } catch {
    return false;
  }
}

function releaseTreeIfGone(tree: ProcessTreeRecord): boolean {
  if (processTreeIsAlive(tree)) return false;
  if (tree.livenessTimer) clearInterval(tree.livenessTimer);
  if (tree.killTimer) clearTimeout(tree.killTimer);
  liveTrees.delete(tree);
  return true;
}

function monitorTree(tree: ProcessTreeRecord): void {
  if (releaseTreeIfGone(tree) || tree.livenessTimer) return;
  tree.livenessTimer = setInterval(() => releaseTreeIfGone(tree), 250);
  tree.livenessTimer.unref?.();
}

/** Apply the one shared TERM -> grace -> KILL lifecycle to an owned tree. */
function terminateProcessTree(
  tree: ProcessTreeRecord,
  why: string,
  keepEventLoopAlive = false,
): boolean {
  if (releaseTreeIfGone(tree)) return false;
  if (tree.terminationStarted) {
    if (keepEventLoopAlive) tree.killTimer?.ref?.();
    return false;
  }

  tree.terminationStarted = true;
  const signalled = signalProcessTree(tree, 'SIGTERM');
  tree.killTimer = setTimeout(() => {
    if (process.platform === 'win32' || processTreeIsAlive(tree)) {
      logger.warn(`${tree.label}: process tree survived SIGTERM after ${why}; sending SIGKILL`);
      signalProcessTree(tree, 'SIGKILL');
    }
    monitorTree(tree);
  }, KILL_GRACE_MS);
  if (!keepEventLoopAlive) tree.killTimer.unref?.();
  return signalled;
}

function forceProcessTree(tree: ProcessTreeRecord): boolean {
  if (releaseTreeIfGone(tree)) return false;
  if (tree.killTimer) clearTimeout(tree.killTimer);
  tree.killTimer = undefined;
  const signalled = signalProcessTree(tree, 'SIGKILL');
  monitorTree(tree);
  return signalled;
}

function observeShutdownUntilGone(): void {
  if (shutdownObservationTimer) return;
  const observe = () => {
    shutdownObservationTimer = undefined;
    for (const tree of Array.from(liveTrees)) releaseTreeIfGone(tree);
    if (liveTrees.size > 0) {
      shutdownObservationTimer = setTimeout(observe, 25);
      return;
    }

    const signal = shutdownSignal;
    if (!signal) return;
    process.removeAllListeners('SIGINT');
    process.removeAllListeners('SIGTERM');
    process.kill(process.pid, signal);
  };
  shutdownObservationTimer = setTimeout(observe, 0);
}

function beginShutdown(signal: NodeJS.Signals): void {
  if (shutdownSignal) {
    // A second signal is the operator's request to skip the remaining grace
    // period, but the parent still waits until the owned groups are observed gone.
    shutdownSignal = signal;
    for (const tree of Array.from(liveTrees)) forceProcessTree(tree);
    observeShutdownUntilGone();
    return;
  }

  shutdownSignal = signal;
  let signalled = 0;
  for (const tree of Array.from(liveTrees)) {
    if (terminateProcessTree(tree, 'server shutdown', true)) signalled += 1;
  }
  if (signalled > 0) {
    logger.info(`Received ${signal}; signalled ${signalled} Python bridge child(ren)`);
  }
  observeShutdownUntilGone();
}

/**
 * Kill every bridge subprocess still running.
 *
 * @param signal - Signal to send (SIGTERM by default)
 * @returns Number of children signalled
 */
export function killAllPythonChildren(signal: NodeJS.Signals = 'SIGTERM'): number {
  let killed = 0;
  for (const tree of Array.from(liveTrees)) {
    if (releaseTreeIfGone(tree)) continue;
    const signalled =
      signal === 'SIGTERM'
        ? terminateProcessTree(tree, 'shutdown request')
        : forceProcessTree(tree);
    if (signalled) killed += 1;
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
      beginShutdown(signal);
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
  const timeoutMs = positiveIntOrFallback(
    runOptions.timeoutMs ?? DEFAULT_BRIDGE_TIMEOUT_MS,
    DEFAULT_BRIDGE_TIMEOUT_MS,
  );
  const label = runOptions.label ?? scriptName;

  installExitHooks();

  if (shutdownSignal) {
    return Promise.reject(new Error(`${label} rejected because the bridge runner is shutting down`));
  }

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
    const pid = shell.childProcess?.pid;
    if (!pid) {
      return reject(new Error(`${label} did not expose a child process id`));
    }
    const tree: ProcessTreeRecord = { shell, pid, label };
    liveTrees.add(tree);

    const output: string[] = [];
    const stderrLines: string[] = [];
    let settled = false;

    // Armed here so `cleanup` below can close over it. Its callback calls
    // `failWith`, which is declared further down — safe because a setTimeout
    // callback cannot run until this synchronous block has finished.
    const timer: NodeJS.Timeout = setTimeout(() => {
      failWith(
        new BridgeTimeoutError(
          `${label} exceeded its ${timeoutMs}ms budget and was terminated. ` +
            `Termination was requested and the process group remains owned until exit. ` +
            `Raise PYTHON_BRIDGE_TIMEOUT if this operation is legitimately slower.`,
          { label, timeoutMs, reason: 'timeout', stderr: stderrLines.join('\n') },
        ),
        'timeout',
      );
    }, timeoutMs);
    if (typeof timer.unref === 'function') timer.unref();

    const cleanupCall = () => {
      clearTimeout(timer);
      if (runOptions.signal) runOptions.signal.removeEventListener('abort', onAbort);
    };

    /**
     * Terminate the child: SIGTERM first, SIGKILL if it is still alive after
     * the grace period. SIGKILL alone would skip Python's own cleanup; SIGTERM
     * alone cannot dislodge a process blocked in an uninterruptible syscall,
     * which is the case this exists for.
     */
    const terminate = (why: string) => {
      terminateProcessTree(tree, why);
    };

    const failWith = (error: Error, why: string) => {
      if (settled) return;
      settled = true;
      terminate(why);
      // Leave the shell registered until terminate's grace window elapses so
      // a shutdown in the meantime still SIGKILLs it.
      clearTimeout(timer);
      if (runOptions.signal) runOptions.signal.removeEventListener('abort', onAbort);
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
      // Keep a bounded tail: provider envelopes are written last, after any
      // diagnostics, and downstream error classification needs that envelope.
      stderrLines.push(line);
      if (stderrLines.length > MAX_STDERR_LINES) stderrLines.shift();
    });

    shell.end((err: any) => {
      if (settled) {
        cleanupCall();
        monitorTree(tree);
        return;
      }
      settled = true;
      cleanupCall();
      // A successful direct child can leave detached-stdio descendants in its
      // process group. Preserve its output/result, but bound ownership of the
      // surviving group with the same TERM/grace/KILL lifecycle as a timeout.
      if (processTreeIsAlive(tree)) terminate('direct parent exit');
      else monitorTree(tree);
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
  for (const tree of Array.from(liveTrees)) releaseTreeIfGone(tree);
  return liveTrees.size;
}
