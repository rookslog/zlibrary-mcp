import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { getPythonScriptPath } from './paths.js';
import { getManagedPythonPath } from './venv-manager.js';
import { BridgeTimeoutError, PythonBridgeError } from './errors.js';
import type { CallOptions } from './zlibrary-api.js';

type SessionState = 'none' | 'active' | 'rejected';
type Launch = () => Promise<ChildProcessWithoutNullStreams>;
interface Pending {
  resolve: (value: any) => void;
  reject: (error: Error) => void;
  cleanup: () => void;
}

/** Owns a single private child; only coarse status survives outside Python. */
export class AnnasMemberWorker {
  private child?: ChildProcessWithoutNullStreams;
  private starting?: Promise<void>;
  private pending = new Map<number, Pending>();
  private cancelled = new Set<number>();
  private sequence = 0;
  private retryAt = 0;
  private state: SessionState = 'none';
  private buffer = '';
  private readonly onExit = () => {
    this.child?.kill('SIGKILL');
  };

  constructor(
    private readonly launch: Launch = async () => {
      const env = { ...process.env };
      delete env.ZLIBRARY_EMAIL;
      delete env.ZLIBRARY_PASSWORD;
      for (const key of Object.keys(env))
        if (/^ZLIBRARY_(EMAIL|PASSWORD)_\d+$/i.test(key)) delete env[key];
      return spawn(
        await getManagedPythonPath(),
        ['-u', getPythonScriptPath('annas_member_worker.py')],
        {
          stdio: ['pipe', 'pipe', 'pipe'],
          env,
        },
      );
    },
  ) {}

  status(): SessionState {
    return this.state;
  }

  private fail(): void {
    // An unacknowledged call may have posted before its final reply arrived.
    if (this.pending.size || this.cancelled.size)
      this.retryAt = Math.max(this.retryAt, Date.now() + 60_000);
    this.state = 'none';
    for (const entry of this.pending.values()) {
      entry.cleanup();
      entry.reject(
        new PythonBridgeError(
          'Anna member worker unavailable',
          { details: { provider: 'annas', reason: 'protocol_error' } },
          false,
        ),
      );
    }
    this.pending.clear();
    this.cancelled.clear();
  }

  private refs(active: boolean): void {
    const child = this.child;
    if (!child) return;
    if (active) child.ref();
    else child.unref();
    for (const stream of [child.stdin, child.stdout, child.stderr]) {
      const handle = stream as typeof stream & { ref?: () => void; unref?: () => void };
      if (active) handle.ref?.();
      else handle.unref?.();
    }
  }

  private async start(): Promise<void> {
    if (this.starting) return this.starting;
    if (this.child) return;
    this.starting = (async () => {
      const child = await this.launch();
      this.child = child;
      this.buffer = '';
      process.once('exit', this.onExit);
      let failed = false;
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk: string) => {
        if (failed || this.child !== child) return;
        this.buffer += chunk;
        if (Buffer.byteLength(this.buffer) > 4 * 1024 * 1024) {
          failed = true;
          this.fail();
          child.kill();
          return;
        }
        for (let end; (end = this.buffer.indexOf('\n')) >= 0;) {
          const line = this.buffer.slice(0, end);
          this.buffer = this.buffer.slice(end + 1);
          try {
            const message = JSON.parse(line);
            if (!['none', 'active', 'rejected'].includes(message.member_session)) throw Error();
            this.state = message.member_session;
            if (Number.isFinite(message.retry_at) && message.retry_at >= 0)
              this.retryAt = message.retry_at;
            this.cancelled.delete(message.id);
            if (message.status_only) continue;
            const entry = this.pending.get(message.id);
            if (!entry) continue;
            this.pending.delete(message.id);
            entry.cleanup();
            if (message.error)
              entry.reject(
                new PythonBridgeError(
                  message.error.message,
                  { details: message.error.details },
                  false,
                ),
              );
            else entry.resolve(message.result);
          } catch {
            failed = true;
            this.fail();
            child.kill();
            return;
          }
        }
        if (!this.pending.size) this.refs(false);
      });
      // Drain but never log/retain third-party stderr or startup tracebacks.
      child.stderr.resume();
      child.stdin.on('error', () => this.fail());
      child.on('error', () => this.fail());
      child.once('close', () => {
        if (this.child === child) {
          this.fail();
          this.child = undefined;
        }
        process.removeListener('exit', this.onExit);
      });
    })().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  async search(args: Record<string, any>, options: CallOptions = {}): Promise<any> {
    if (options.signal?.aborted)
      throw new BridgeTimeoutError('Anna search aborted', { reason: 'aborted' });
    await this.start();
    this.refs(true);
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const cancel = () => {
        const entry = this.pending.get(id);
        if (!entry) return;
        this.pending.delete(id);
        // Retain failure accounting until the child acknowledges cancellation.
        this.cancelled.add(id);
        entry.cleanup();
        this.child?.stdin.write(JSON.stringify({ id, cancel: true }) + '\n');
        reject(
          new BridgeTimeoutError('Anna search cancelled or timed out', {
            reason: options.signal?.aborted ? 'aborted' : 'timeout',
          }),
        );
        if (!this.pending.size) this.refs(false);
      };
      const timer = setTimeout(cancel, options.timeoutMs ?? 240000);
      this.pending.set(id, {
        resolve,
        reject,
        cleanup: () => {
          clearTimeout(timer);
          options.signal?.removeEventListener('abort', cancel);
        },
      });
      options.signal?.addEventListener('abort', cancel, { once: true });
      this.child!.stdin.write(JSON.stringify({ id, args, retry_at: this.retryAt }) + '\n');
      if (options.signal?.aborted) cancel();
    });
  }

  async close(): Promise<void> {
    await this.starting;
    const child = this.child;
    if (!child) return;
    this.refs(true);
    this.fail();
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
      child.once('close', () => {
        clearTimeout(timer);
        resolve();
      });
      child.stdin.end();
    });
  }
}

export const annasMemberWorker = new AnnasMemberWorker();
