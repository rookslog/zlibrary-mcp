import { BridgeTimeoutError, PythonBridgeError } from './errors.js';
import { readAuthDiagnostics, type AuthDiagnostics } from './auth-diagnostics.js';

export interface EapiSession {
  identity: string;
  generation: number;
  domain: string;
  remix_userid: string;
  remix_userkey: string;
}
export interface SessionInput extends Partial<EapiSession> {
  generation: number;
  identity: string;
  action: 'login' | 'validate' | 'operation';
  authRetry?: boolean;
}
export function getSessionMode(): 'memory' | 'per-call' {
  const mode = process.env.ZLIBRARY_SESSION_MODE ?? 'memory';
  if (mode !== 'memory' && mode !== 'per-call')
    throw new Error('ZLIBRARY_SESSION_MODE must be memory or per-call');
  return mode;
}
function milliseconds(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isSafeInteger(value) && value > 0 && value <= 2147483647 ? value : fallback;
}
export function needsEapi(functionName: string, args: Record<string, any>): boolean {
  if (['search_multi_source', 'acquire_book'].includes(functionName)) return false;
  return (
    functionName !== 'download_book' ||
    !['libgen', 'annas', 'annas_archive'].includes(
      String(args.book_details?.source || '').toLowerCase(),
    )
  );
}
export function authRejected(generation: number, token: string): PythonBridgeError {
  return new PythonBridgeError(token, { reason: 'auth_rejected', generation }, false);
}

interface Acquisition {
  identity: string;
  generation: number;
  controller: AbortController;
  signal: AbortSignal;
  promise: Promise<void>;
}
interface LoginFailure {
  count: number;
  retryAt: number;
  cause: string;
  acquiredAt?: number;
  refreshed: boolean;
  diagnostics?: AuthDiagnostics;
}
function loginCause(error: any): string {
  const status = error?.context?.loginStatus;
  const diagnostics = readAuthDiagnostics(error?.context?.diagnostics);
  const rejected = diagnostics
    ? diagnostics.failure_kind === 'auth_rejected'
    : error?.context?.reason === 'auth_rejected' ||
      (error?.context?.reason === 'login_failed' &&
        (status === 200 || (status >= 400 && status < 500)));
  let cause = rejected ? 'Z-Library rejected the login' : 'Z-Library login failed';
  if (Number.isInteger(status) && status >= 100 && status <= 599) cause += ` (HTTP ${status})`;
  const token =
    error?.context?.loginToken ??
    (error?.context?.reason === 'auth_rejected' ? 'Authentication rejected' : undefined);
  if (typeof token === 'string' && /^[A-Za-z0-9 .#-]{1,64}$/.test(token)) cause += `: ${token}`;
  return cause;
}
function deadlineError(signal: AbortSignal): BridgeTimeoutError {
  const timedOut = signal.reason?.name === 'TimeoutError';
  return new BridgeTimeoutError(timedOut ? 'EAPI call deadline exceeded' : 'EAPI call aborted', {
    reason: timedOut ? 'timeout' : 'aborted',
  });
}
/** Waiting never transfers one caller's abort authority to another caller. */
function waitFor<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(deadlineError(signal));
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
    void promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

/** One server owns one active account/session, never a disk record or account pool. */
export class EapiSessionOwner {
  private identity?: string;
  private generation = 0;
  private session?: EapiSession;
  private acquisition?: Acquisition;
  private lastUse = 0;
  private validationEpoch = 0;
  private validatedEpoch = 0;
  private loginFailures = new Map<string, LoginFailure>();

  generationFor(identity: string): number {
    if (identity !== this.identity) {
      this.acquisition?.controller.abort();
      this.acquisition = undefined;
      this.identity = identity;
      this.session = undefined;
      this.lastUse = 0;
      this.validatedEpoch = ++this.validationEpoch;
      this.generation++;
    }
    return this.generation;
  }
  private assertIdentity(identity: string): void {
    if (identity !== this.identity)
      throw new PythonBridgeError(
        'EAPI account configuration changed during the call',
        { reason: 'identity_changed' },
        false,
      );
  }
  get(identity: string): EapiSession | undefined {
    return identity === this.identity ? this.session : undefined;
  }
  consume(update: any, identity: string, expected: number): void {
    if (
      identity !== this.identity ||
      expected !== this.generation ||
      update?.generation !== expected ||
      update?.identity !== identity
    )
      return;
    if (
      !['domain', 'remix_userid', 'remix_userkey'].every(
        (key) => typeof update[key] === 'string' && update[key],
      )
    )
      return;
    if (update.validate_next === true) {
      this.validationEpoch++;
      this.lastUse = 0;
    }
    if (
      this.session &&
      ['domain', 'remix_userid', 'remix_userkey'].every(
        (key) => update[key] === this.session![key as keyof EapiSession],
      )
    )
      return;
    this.session = {
      identity,
      generation: ++this.generation,
      domain: update.domain,
      remix_userid: update.remix_userid,
      remix_userkey: update.remix_userkey,
    };
  }
  reject(identity: string, expected: number): void {
    if (identity === this.identity && expected === this.generation) {
      this.session = undefined;
      this.generation++;
    }
  }
  private stateFor(identity: string): LoginFailure {
    let state = this.loginFailures.get(identity);
    if (!state) {
      state = { count: 0, retryAt: 0, cause: 'Z-Library login failed', refreshed: false };
      this.loginFailures.set(identity, state);
    }
    return state;
  }
  private resetIfSurvived(identity: string): void {
    const state = this.stateFor(identity);
    if (
      state.acquiredAt !== undefined &&
      Date.now() - state.acquiredAt >= milliseconds('ZLIBRARY_SESSION_LOGIN_COOLDOWN_MS', 180000)
    ) {
      state.count = 0;
      state.refreshed = false;
      state.retryAt = 0;
    }
  }
  private backoff(identity: string, error: unknown, since = Date.now()): void {
    const state = this.stateFor(identity);
    state.count = Math.min(state.count + 1, 18);
    const delay = Math.min(
      milliseconds('ZLIBRARY_SESSION_LOGIN_COOLDOWN_MS', 180000),
      30000 * 2 ** (state.count - 1),
    );
    state.retryAt = since + delay;
    state.cause = loginCause(error);
    state.diagnostics = readAuthDiagnostics((error as any)?.context?.diagnostics);
  }
  private backoffError(state: LoginFailure): PythonBridgeError {
    const remaining = Math.max(0, state.retryAt - Date.now());
    return new PythonBridgeError(
      `${state.cause}; next attempt allowed in ${Math.ceil(remaining / 1000)} s`,
      { reason: 'auth_backoff', retryAfterMs: remaining, diagnostics: state.diagnostics },
      false,
    );
  }
  private async acquire(
    identity: string,
    invoke: (input: SessionInput, signal?: AbortSignal) => Promise<unknown>,
    signal: AbortSignal,
    authRetry: boolean,
  ): Promise<EapiSession> {
    for (;;) {
      if (signal.aborted) throw deadlineError(signal);
      this.assertIdentity(identity);
      let flight = this.acquisition;
      if (!flight) {
        const session = this.get(identity);
        if (session) return { ...session };
        const failure = this.loginFailures.get(identity);
        if (failure && Date.now() < failure.retryAt) throw this.backoffError(failure);
        const controller = new AbortController();
        const leaderSignal = AbortSignal.any([signal, controller.signal]);
        const input: SessionInput = {
          identity,
          generation: this.generation,
          action: 'login',
          authRetry,
        };
        flight = {
          identity,
          generation: input.generation,
          controller,
          signal: leaderSignal,
          promise: Promise.resolve(),
        };
        this.acquisition = flight;
        const active = flight;
        // Publish the flight before starting the child so cold callers join it.
        const work = Promise.resolve().then(() => invoke(input, leaderSignal));
        active.promise = waitFor(work, leaderSignal)
          .then(() => {
            this.assertIdentity(identity);
            if (!this.get(identity))
              throw new PythonBridgeError('Login returned no valid private session', {}, false);
            const state = this.stateFor(identity);
            state.acquiredAt = Date.now();
            state.retryAt = 0; // Do not reset the episode until this session survives.
            this.validatedEpoch = this.validationEpoch;
            this.lastUse = Date.now();
          })
          .catch((error) => {
            if (leaderSignal.aborted) {
              if (leaderSignal.reason?.name === 'TimeoutError') {
                this.backoff(
                  identity,
                  new PythonBridgeError(
                    'EAPI login deadline exceeded',
                    {
                      reason: 'login_failed',
                      loginToken: 'Login deadline exceeded',
                    },
                    false,
                  ),
                );
              }
              // Fence late acquisition output only; never invalidate other warm callers.
              if (identity === this.identity && this.generation === input.generation)
                this.generation++;
            } else if (
              error?.code !== 'CIRCUIT_BREAKER_OPEN' &&
              !(
                error?.context?.details?.request_issued === false &&
                ['invalid_argument', 'configuration_error'].includes(
                  error?.context?.details?.reason,
                )
              )
            ) {
              // Admission was refused before login; it is not a login failure
              // and must not start or extend the owner's cooldown episode.
              this.backoff(identity, error);
            }
            throw error;
          })
          .finally(() => {
            if (this.acquisition === active) this.acquisition = undefined;
          });
      }
      try {
        await waitFor(flight.promise, signal);
      } catch (error) {
        // A live waiter can replace a dead/cancelled acquisition leader.
        if (flight.signal.aborted && !signal.aborted) continue;
        throw error;
      }
      this.assertIdentity(identity);
      const session = this.get(identity);
      if (session) return { ...session };
    }
  }
  run<T>(
    identity: string,
    invoke: (input: SessionInput, signal?: AbortSignal) => Promise<T>,
    options: {
      signal?: AbortSignal;
      timeoutMs?: number;
      operationMode?: 'at-most-once';
    } = {},
  ): Promise<T> {
    const budget =
      Number.isSafeInteger(options.timeoutMs) &&
      options.timeoutMs! > 0 &&
      options.timeoutMs! <= 2147483647
        ? options.timeoutMs!
        : milliseconds('PYTHON_BRIDGE_TIMEOUT', 240000);
    const newSignal = () => {
      const timeout = AbortSignal.timeout(budget);
      return options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    };
    const acquisitionSignal = newSignal();
    this.generationFor(identity);
    const execute = async (initial: EapiSession, signal: AbortSignal) => {
      let session = initial;
      let handedOff = false;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          if (signal.aborted) throw deadlineError(signal);
          if (
            this.lastUse === 0 ||
            Date.now() - this.lastUse >= milliseconds('ZLIBRARY_SESSION_IDLE_MS', 300000)
          ) {
            const epoch = this.validationEpoch;
            await invoke({ ...session, action: 'validate', authRetry: attempt > 0 }, signal);
            session = await this.acquire(identity, invoke, signal, attempt > 0);
            this.validatedEpoch = epoch;
          }
          if (signal.aborted) throw deadlineError(signal);
          // This immutable snapshot belongs to this operation, not a future generation.
          handedOff = true;
          const result = await invoke(
            { ...session, action: 'operation', authRetry: attempt > 0 },
            signal,
          );
          if (signal.aborted) throw deadlineError(signal);
          if ((result as any)?.success === 0) {
            this.validationEpoch++;
            this.lastUse = 0;
          } else if (this.get(identity)?.generation === session.generation) {
            this.lastUse = this.validationEpoch === this.validatedEpoch ? Date.now() : 0;
            this.resetIfSurvived(identity);
          }
          return result;
        } catch (error: any) {
          if (signal.aborted) throw deadlineError(signal);
          if (error?.context?.reason !== 'auth_rejected') throw error;
          if (this.get(identity)?.generation === error.context.generation) {
            this.resetIfSurvived(identity);
            const state = this.stateFor(identity);
            this.reject(identity, error.context.generation);
            if (state.refreshed) this.backoff(identity, error, state.acquiredAt);
            else state.refreshed = true; // One immediate refresh per surviving-session episode.
            if (state.retryAt > Date.now()) throw this.backoffError(state);
          }
          if (attempt || (handedOff && options.operationMode === 'at-most-once')) throw error;
          session = await this.acquire(identity, invoke, signal, true);
        }
      }
      throw new Error('Unreachable EAPI session state');
    };
    return this.acquire(identity, invoke, acquisitionSignal, false).then((session) => {
      const operationSignal = newSignal();
      return waitFor(execute(session, operationSignal), operationSignal);
    });
  }
}

/** Never retain subprocess environments or private input in public diagnostics. */
export function sanitizeSessionError(error: any, secrets: string[]): void {
  const seen = new WeakSet<object>();
  function visit(value: any): void {
    if (!value || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    for (const key of Object.getOwnPropertyNames(value)) {
      if (['env', 'stdout', 'sessionInput', '_eapi_session', 'ZLIBRARY_EAPI_SESSION'].includes(key))
        delete value[key];
      else if (['code', 'reason', 'name', 'type', 'pythonErrorType', 'errnoCode'].includes(key))
        continue;
      else if (key === 'diagnostics' && readAuthDiagnostics(value[key])) continue;
      else if (
        key === 'loginToken' &&
        [
          'Authentication rejected',
          'Upstream request failed',
          'TLS certificate verification failed',
        ].includes(value[key])
      )
        continue;
      else if (typeof value[key] === 'string') {
        for (const secret of secrets.filter(Boolean))
          value[key] = value[key].split(secret).join('[redacted]');
      } else visit(value[key]);
    }
  }
  visit(error);
}
