/**
 * Bounded, killable execution of the Python bridge subprocess.
 *
 * `PythonShell.run()` returns a promise with no timeout and no handle on the
 * child, so a bridge call that never finishes has no way to end: abandoning the
 * promise (as an MCP client does when it hits its own idle timeout) leaves the
 * Python process running with nothing waiting on it. That is how three
 * `python_bridge.py search...` processes were found alive on a development machine on
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
 * The Python side bounds each provider walk with one deadline sized to this
 * timer (lib/sources/config.py); a guard test checks the walk fits under it, so
 * the kill here catches a hang instead of preempting a legitimate slow walk.
 */

import type { Readable } from 'stream';
import { PythonShell } from 'python-shell';
import type { Options as PythonShellOptions } from 'python-shell';
import { spawnSync } from 'child_process';
import { readdirSync, readFileSync } from 'fs';
import { logger } from './logger.js';
import { BridgeKilledError, BridgeSpawnError, BridgeTimeoutError } from './errors.js';
import { sanitizeSessionError } from './eapi-session.js';
import { safeEapiFailureEnvelope } from './auth-diagnostics.js';

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
const MAX_STDERR_LINES = 32;
const MAX_STDERR_BYTES = 8192;

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

export interface ProtocolDiagnostic {
  phase:
    | 'request-data'
    | 'request-end'
    | 'request-close'
    | 'request-error'
    | 'request-accepted'
    | 'grant'
    | 'deny'
    | 'child-exit'
    | 'child-close'
    | 'stdin-end'
    | 'stdin-finish'
    | 'stdin-close'
    | 'stdin-error'
    | 'failure'
    | 'completion';
  sequence: number;
  elapsedMs: number;
  demanded: boolean;
  granted: boolean;
  settled: boolean;
  count?: number;
  code?: string;
  errno?: number;
}

/** Internal corruption identity; never a public error-code or payload carrier. */
export class PrivateSessionProtocolError extends Error {
  readonly privateSessionProtocolCorruption = true;
  constructor(message: string) {
    super(message);
    this.name = 'PrivateSessionProtocolError';
  }
}

const DEMAND_BODY = Buffer.from('{"type":"session_needed","terminal":true}', 'ascii');
const DEMAND_MAGIC = Buffer.from('ZLAD', 'ascii');

/** Internal lazy-v2 decoder: commit is logical readiness, NOT child success.
 * Each complete delivered chunk must contain exactly one canonical transaction.
 * Later bytes remain violations before AND after grant; EOF is only final drain.
 */
export class PrivateDemandDecoder {
  private pending = Buffer.alloc(0);
  private committed = false;
  private terminal = false;

  push(chunk: Buffer): boolean {
    if (!chunk.length) return false;
    const invalid = () => new PrivateSessionProtocolError('Invalid private session request');
    if (this.committed || this.terminal) throw invalid();
    if (this.pending.length + chunk.length > 7 + DEMAND_BODY.length) throw invalid();
    this.pending = Buffer.concat([this.pending, chunk]);
    const data = this.pending;
    const magicLength = Math.min(4, data.length);
    if (!data.subarray(0, magicLength).equals(DEMAND_MAGIC.subarray(0, magicLength)))
      throw invalid();
    if (data.length >= 5 && data[4] !== 2) throw invalid();
    if (
      data.length >= 7 &&
      (data.readUInt16BE(5) > 128 || data.readUInt16BE(5) !== DEMAND_BODY.length)
    )
      throw invalid();
    if (data.length < 7 + DEMAND_BODY.length) return false;
    if (!data.subarray(7).equals(DEMAND_BODY)) throw invalid();
    this.committed = true;
    this.pending = Buffer.alloc(0);
    return true;
  }

  end(): void {
    if (this.pending.length)
      throw new PrivateSessionProtocolError('Invalid private session request');
    this.terminal = true;
  }
}

export interface RunBridgeOptions {
  /** Wall-clock budget in ms. Defaults to DEFAULT_BRIDGE_TIMEOUT_MS. */
  timeoutMs?: number;
  /** Abort signal from the MCP request, if the transport supplies one. */
  signal?: AbortSignal;
  /** Short label used in log lines and error messages. */
  label?: string;
  /** Internal stdin input / fd 3 output. Never included in argv or diagnostics. */
  sessionInput?: Record<string, unknown>;
  onSession?: (update: unknown) => void;
  /** Opt-in synthetic metadata only, no streams/private frames/exception text. */
  onProtocolDiagnostic?: (event: Readonly<ProtocolDiagnostic>) => void;
  /** Internal post-admission demand, never a public tool/session snapshot. */
  onSessionNeeded?: (channel: {
    grant(input: Record<string, unknown>): void;
    deny(error: Error): void;
  }) => void;
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
  const lazy = !!runOptions.onSessionNeeded;
  if (lazy && runOptions.sessionInput)
    return Promise.reject(new Error('Conflicting private session modes'));

  installExitHooks();

  if (shutdownSignal) {
    return Promise.reject(
      new Error(`${label} rejected because the bridge runner is shutting down`),
    );
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
    let settled = false;
    const processState: { tree?: ProcessTreeRecord } = {};
    let cleanupCall = () => {};
    let failWith: (error: Error, why: string) => void = () => {};
    let childClosed = false;
    let closeChild!: () => void;
    const childCloseDone = new Promise<void>((done) => {
      closeChild = done;
    });
    let demanded = false;
    let granted = false;
    let diagnosticSequence = 0;
    const diagnosticStart = performance.now();
    const diagnostic = (phase: ProtocolDiagnostic['phase'], count?: number, error?: any) => {
      if (!runOptions.onProtocolDiagnostic || diagnosticSequence >= 256) return;
      try {
        const event: ProtocolDiagnostic = {
          phase,
          sequence: ++diagnosticSequence,
          elapsedMs: Math.max(0, Math.round(performance.now() - diagnosticStart)),
          demanded,
          granted,
          settled,
        };
        if (Number.isSafeInteger(count)) event.count = Math.min(8193, Math.max(0, count!));
        const code = error?.code;
        if (typeof code === 'string')
          event.code = [
            'EPIPE',
            'ECONNRESET',
            'ECONNABORTED',
            'ERR_STREAM_DESTROYED',
            'ETIMEDOUT',
            'ENOENT',
            'SIGTERM',
            'SIGKILL',
            'SIGINT',
            'SIGQUIT',
          ].includes(code)
            ? code
            : 'OTHER';
        if (
          Number.isSafeInteger(error?.errno) &&
          error.errno >= -(2 ** 31) &&
          error.errno < 2 ** 32
        )
          event.errno = error.errno;
        runOptions.onProtocolDiagnostic(Object.freeze(event));
      } catch {
        /* observational only */
      }
    };
    const privateSecrets: string[] =
      typeof runOptions.sessionInput?.remix_userkey === 'string'
        ? [runOptions.sessionInput.remix_userkey]
        : [];

    const onShellError = (error: NodeJS.ErrnoException) => {
      const errnoCode = error.code ?? 'UNKNOWN_SPAWN_ERROR';
      const spawnError = new BridgeSpawnError(
        `${label} could not start the Python bridge (${errnoCode})`,
        errnoCode,
        // Native spawn errors retain argv and the configured interpreter path.
        // Project before either the no-PID rejection or owned-child failure path.
        { label, errno: error.errno },
      );

      if (processState.tree) {
        failWith(spawnError, 'spawn error');
      } else if (!settled) {
        settled = true;
        cleanupCall();
        reject(spawnError);
      }
    };

    const shell = new PythonShell(scriptName, {
      ...options,
      ...(runOptions.sessionInput ? { stdio: ['pipe', 'pipe', 'pipe', 'pipe'] } : {}),
      ...(lazy
        ? {
            // Windows child stdin must support IOCP, including Python3.10.
            stdio: ['overlapped', 'pipe', 'pipe', 'pipe', 'pipe'],
          }
        : {}),
      env: {
        ...(options.env ?? process.env),
        PYTHON_BRIDGE_CALL_TIMEOUT_MS: String(timeoutMs),
        ...(lazy
          ? {
              ZLIBRARY_EAPI_CHANNEL: 'lazy-v2',
              ZLIBRARY_EAPI_HANDOFF_TIMEOUT_MS: String(timeoutMs),
            }
          : {}),
      },
      // On POSIX this creates a new session/process group whose id is the
      // Python pid. OCR and other grandchildren inherit it, making the whole
      // bridge-owned tree addressable without walking /proc.
      detached: process.platform === 'win32' ? options.detached : true,
    });
    // PythonShell otherwise keeps an unbounded errorData string and an unbounded
    // partial-line splitter, independent of our tail. Replace only its stderr
    // data plumbing immediately after construction; keep its end/exit lifecycle.
    shell.stderr.unpipe();
    shell.stderr.removeAllListeners('data');
    let pendingStderr = '';
    let discardStderrLine = false;
    shell.stderr.on('data', (chunk: string | Buffer) => {
      const pieces = String(chunk).split('\n');
      for (let index = 0; index < pieces.length; index++) {
        const piece = pieces[index]!;
        if (!discardStderrLine) {
          if (Buffer.byteLength(pendingStderr) + Buffer.byteLength(piece) > MAX_STDERR_BYTES) {
            pendingStderr = '';
            discardStderrLine = true;
          } else pendingStderr += piece;
        }
        if (index < pieces.length - 1) {
          if (!discardStderrLine) shell.emit('stderr', pendingStderr.replace(/\r$/, ''));
          pendingStderr = '';
          discardStderrLine = false;
        }
      }
    });
    shell.stderr.prependOnceListener('end', () => {
      if (pendingStderr && !discardStderrLine) shell.emit('stderr', pendingStderr);
      pendingStderr = '';
    });
    // unpipe() pauses the source; attaching a new data listener does not undo it.
    shell.stderr.resume();
    shell.on('error', onShellError);
    shell.childProcess.once('close', () => {
      childClosed = true;
      closeChild();
      diagnostic('child-close');
    });
    if (runOptions.onProtocolDiagnostic) {
      shell.childProcess.once('exit', (code, signal) =>
        diagnostic('child-exit', code ?? undefined, { code: signal }),
      );
      shell.stdin.on('finish', () => diagnostic('stdin-finish'));
      shell.stdin.on('close', () => diagnostic('stdin-close'));
      shell.stdin.on('error', (error) => diagnostic('stdin-error', undefined, error));
    }

    const pid = shell.childProcess?.pid;
    if (!pid) {
      // A failed spawn has no pid; PythonShell forwards its ChildProcess error
      // asynchronously through the listener registered above. The missing-
      // interpreter subprocess regression exercises this no-pid branch: it
      // must reject and leave zero live tree records without a timeout/abort
      // handler, because Node emits an error when spawn cannot create a child.
      return;
    }
    let privateDone = Promise.resolve();
    let requestDone = Promise.resolve();
    let requestTerminal = Promise.resolve();
    let privateUpdate: unknown;
    let hasPrivateUpdate = false;
    if (runOptions.sessionInput || lazy) {
      const channel = shell.childProcess.stdio[3] as Readable;
      privateDone = new Promise<void>((done) => {
        let pending = '';
        let terminal = false;
        channel.setEncoding('utf8');
        channel.on('data', (chunk: string) => {
          if (settled) return;
          if (lazy && !granted) {
            failWith(
              new PrivateSessionProtocolError('Invalid private session response'),
              'private channel',
            );
            return;
          }
          pending += chunk;
          if (Buffer.byteLength(pending) > 8192) {
            pending = '';
            failWith(
              new PrivateSessionProtocolError('Invalid private session response'),
              'private channel',
            );
            return;
          }
          const newline = pending.indexOf('\n');
          if (newline === -1) return;
          try {
            if (hasPrivateUpdate || newline !== pending.length - 1) throw new Error();
            privateUpdate = JSON.parse(pending.slice(0, newline));
            hasPrivateUpdate = true;
            if (typeof (privateUpdate as any)?.remix_userkey === 'string')
              privateSecrets.push((privateUpdate as any).remix_userkey);
            pending = '';
          } catch {
            failWith(
              new PrivateSessionProtocolError('Invalid private session response'),
              'private channel',
            );
          }
        });
        const terminalPrivate = () => {
          if (terminal) return;
          terminal = true;
          if (!settled && pending)
            failWith(
              new PrivateSessionProtocolError('Invalid private session response'),
              'private channel',
            );
          done();
        };
        channel.on('end', terminalPrivate);
        channel.on('close', terminalPrivate);
        channel.on('error', () => {
          failWith(
            new PrivateSessionProtocolError('Private session channel closed'),
            'private channel',
          );
          terminalPrivate();
        });
      });
      shell.stdin.on('error', () => {}); // A dying leader may close stdin before reading.
      if (runOptions.sessionInput)
        shell.stdin.write(JSON.stringify(runOptions.sessionInput) + '\n');
    }
    if (lazy) {
      const request = shell.childProcess.stdio[4] as Readable;
      const decoder = new PrivateDemandDecoder();
      let invalid = false;
      let physicalTerminal = false;
      let endRequest!: () => void;
      requestTerminal = new Promise<void>((done) => {
        endRequest = done;
      });
      requestDone = new Promise<void>((done) => {
        const corrupt = (message = 'Invalid private session request') => {
          invalid = true;
          done();
          failWith(new PrivateSessionProtocolError(message), 'private request');
        };
        request.on('data', (chunk: Buffer) => {
          diagnostic('request-data', chunk.length);
          if (settled) return;
          try {
            if (!decoder.push(chunk)) return;
            demanded = true;
            diagnostic('request-accepted');
            done(); // Logical readiness only; physical drain still gates FINAL.
            runOptions.onSessionNeeded!({
              grant(input) {
                if (
                  settled ||
                  invalid ||
                  runOptions.signal?.aborted ||
                  childClosed ||
                  shell.childProcess.exitCode !== null ||
                  shell.childProcess.signalCode !== null
                )
                  return;
                if (granted) {
                  failWith(
                    new PrivateSessionProtocolError('Invalid private session handoff'),
                    'private grant',
                  );
                  return;
                }
                const frame = JSON.stringify({ v: 1, type: 'session', input }) + '\n';
                if (Buffer.byteLength(frame) > 8192) {
                  failWith(
                    new PrivateSessionProtocolError('Invalid private session handoff'),
                    'private grant',
                  );
                  return;
                }
                granted = true; // Atomic once before the write; never replay.
                diagnostic('grant');
                if (typeof input.remix_userkey === 'string')
                  privateSecrets.push(input.remix_userkey);
                shell.stdin.end(frame, () => diagnostic('stdin-end'));
              },
              deny(error) {
                if (settled) return;
                diagnostic('deny');
                shell.stdin.end('{"v":1,"type":"denied","code":"auth_unavailable"}\n', () =>
                  diagnostic('stdin-end'),
                );
                failWith(error, 'private session denied');
              },
            });
          } catch {
            corrupt();
          }
        });
        const terminalRequest = () => {
          if (physicalTerminal) return;
          physicalTerminal = true;
          try {
            if (!settled) decoder.end();
          } catch {
            corrupt();
          }
          done(); // Empty physical terminal is the receipt/no-demand case.
          endRequest();
        };
        request.on('end', () => {
          diagnostic('request-end');
          terminalRequest();
        });
        request.on('close', () => {
          diagnostic('request-close');
          terminalRequest();
        });
        request.on('error', (error) => {
          diagnostic('request-error', undefined, error);
          corrupt('Private session request channel closed');
          terminalRequest();
        });
      });
    }

    const treeRecord: ProcessTreeRecord = { shell, pid, label };
    processState.tree = treeRecord;
    liveTrees.add(treeRecord);

    const output: string[] = [];
    const stderrLines: string[] = [];

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

    cleanupCall = () => {
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
      terminateProcessTree(treeRecord, why);
    };

    failWith = (error: Error, why: string) => {
      if (settled) return;
      settled = true;
      diagnostic('failure', undefined, error);
      terminate(why);
      // Leave the shell registered until terminate's grace window elapses so
      // a shutdown in the meantime still SIGKILLs it.
      clearTimeout(timer);
      if (options.env?.ZLIBRARY_DIAGNOSTIC_EAPI === '1' && (error as any).context)
        delete (error as any).context.stderr;
      if (runOptions.signal) runOptions.signal.removeEventListener('abort', onAbort);
      if (lazy || runOptions.sessionInput) {
        sanitizeSessionError(error, privateSecrets);
        const finishFailure = () => {
          shell.stdin.destroy();
          (shell.childProcess.stdio[3] as Readable)?.destroy();
          (shell.childProcess.stdio[4] as Readable)?.destroy();
          monitorTree(treeRecord);
          reject(error);
        };
        if (childClosed) finishFailure();
        else shell.childProcess.once('close', finishFailure);
      } else reject(error);
    };

    function onAbort() {
      failWith(
        new BridgeTimeoutError(`${label} was aborted by the caller; the subprocess was killed.`, {
          label,
          reason: 'aborted',
          stderr: stderrLines.join('\n'),
        }),
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
      // Oversized records are discarded whole, not truncated into valid-looking JSON.
      if (Buffer.byteLength(line) > MAX_STDERR_BYTES) return;
      stderrLines.push(line);
      while (
        stderrLines.length > MAX_STDERR_LINES ||
        Buffer.byteLength(stderrLines.join('\n')) > MAX_STDERR_BYTES
      )
        stderrLines.shift();
    });

    const finish = async (err: any) => {
      if (runOptions.sessionInput || lazy)
        await Promise.all([childCloseDone, privateDone, requestDone, requestTerminal]);
      if (settled) {
        cleanupCall();
        monitorTree(treeRecord);
        return;
      }
      if (lazy && demanded && !granted) {
        failWith(
          new PrivateSessionProtocolError('Private session handoff incomplete'),
          'private grant',
        );
        return;
      }
      if (hasPrivateUpdate) {
        try {
          runOptions.onSession?.(privateUpdate);
        } catch {
          failWith(
            new PrivateSessionProtocolError('Invalid private session response'),
            'private channel',
          );
          return;
        }
      }
      settled = true;
      diagnostic('completion');
      cleanupCall();
      // A successful direct child can leave detached-stdio descendants in its
      // process group. Preserve its output/result, but bound ownership of the
      // surviving group with the same TERM/grace/KILL lifecycle as a timeout.
      if (processTreeIsAlive(treeRecord)) terminate('direct parent exit');
      else monitorTree(treeRecord);

      const signal = shell.childProcess.signalCode;
      if (signal) {
        const oomHint =
          signal === 'SIGKILL' ? ' SIGKILL may indicate an out-of-memory (OOM) kill.' : '';
        reject(
          new BridgeKilledError(`${label} was terminated by ${signal}.${oomHint}`, signal, {
            label,
            signal,
            ...(options.env?.ZLIBRARY_DIAGNOSTIC_EAPI === '1'
              ? {}
              : { stderr: stderrLines.join('\n') }),
          }),
        );
        return;
      }

      if (err) {
        const retained = stderrLines.join('\n');
        if (options.env?.ZLIBRARY_DIAGNOSTIC_EAPI === '1') {
          let envelope: Record<string, any> | undefined;
          for (const line of [...stderrLines].reverse()) {
            if (Buffer.byteLength(line) > 2048) continue;
            try {
              envelope = safeEapiFailureEnvelope(JSON.parse(line));
            } catch {
              /* not a failure record */
            }
            if (envelope) break;
          }
          // PythonShell's message/stack/args/traceback can embed its complete raw buffers.
          // Retain neither those properties nor their cause when crossing this boundary.
          err = Object.assign(new Error('EAPI bridge request failed'), {
            exitCode: err.exitCode,
            stderr: envelope ? JSON.stringify(envelope) : '',
          });
        } else {
          err.stderr = retained;
        }
        if (lazy) sanitizeSessionError(err, privateSecrets);
        reject(err);
      } else {
        resolve(output);
      }
    };
    if (lazy) {
      // PythonShell.end closes stdin; lazy children must keep it until grant/denial.
      let pythonError: Error | undefined;
      shell.on('pythonError', (error) => {
        pythonError = error;
      });
      shell.once('close', () => {
        void finish(pythonError);
      });
    } else shell.end(finish);
  });
}

/** Number of bridge children currently registered (used by tests). */
export function liveChildCount(): number {
  for (const tree of Array.from(liveTrees)) releaseTreeIfGone(tree);
  return liveTrees.size;
}
