/**
 * Circuit Breaker pattern implementation for Z-Library MCP
 * Implements the pattern from .claude/PATTERNS.md
 * Prevents cascading failures by opening circuit after repeated failures
 */

import { logger } from './logger.js';

export type CircuitState = 'CLOSED' | 'OPEN' | 'HALF_OPEN';

export type BreakerDependency = 'eapi' | 'source_walk' | 'local';

export class CircuitBreakerOpenError extends Error {
  readonly code = 'CIRCUIT_BREAKER_OPEN';
  readonly context;

  constructor(
    public readonly dependency: BreakerDependency,
    retryAfterMs: number,
  ) {
    super('Circuit breaker is OPEN');
    this.name = 'CircuitBreakerOpenError';
    this.context = {
      retryAfterMs,
      details: { reason: 'breaker_open', request_issued: false },
    };
  }
}

export interface CircuitBreakerOptions {
  dependency?: BreakerDependency;
  threshold?: number;
  timeout?: number;
  onStateChange?: (oldState: CircuitState, newState: CircuitState) => void;
  /**
   * Whether a rejection counts toward opening the circuit. Defaults to
   * counting everything. Override to exclude rejections that say nothing
   * about the dependency's health — a caller cancelling its own request,
   * for instance.
   */
  isFailure?: (error: any) => boolean;
  /**
   * Whether a fulfilled operation says nothing about the dependency's health —
   * a result served from local state without contacting it, for instance.
   * Such a result neither closes the circuit nor resets the failure count.
   * Defaults to treating every result as a success.
   */
  isNeutral?: (result: any) => boolean;
}

/**
 * What an admitted operation may tell the breaker before it settles. Both calls
 * act only while the operation is the HALF_OPEN probe; otherwise they do nothing.
 */
export interface Admission {
  /**
   * The dependency has answered: settle the probe's success now and close the
   * circuit. The operation's own later outcome then counts as an ordinary call.
   */
  confirm(): void;
  /**
   * Hold HALF_OPEN exclusively for at most `ms`; afterwards further callers are
   * admitted as ordinary calls. The probe keeps its verdict.
   */
  holdFor(ms: number): void;
}

interface Probe {
  exclusive: boolean;
  holdUntil?: number;
  timer?: ReturnType<typeof setTimeout>;
}

export class CircuitBreaker {
  private readonly dependency: BreakerDependency;
  private failureCount = 0;
  private lastFailureTime: number | null = null;
  private state: CircuitState = 'CLOSED';
  private readonly threshold: number;
  private readonly timeout: number;
  private readonly onStateChange?: (oldState: CircuitState, newState: CircuitState) => void;
  private readonly isFailure: (error: any) => boolean;
  private readonly isNeutral: (result: any) => boolean;
  /** The single HALF_OPEN probe in flight, if any. */
  private probe: Probe | null = null;

  constructor(options: CircuitBreakerOptions = {}) {
    this.dependency = options.dependency ?? 'local';
    this.threshold = options.threshold ?? 5;
    this.timeout = options.timeout ?? 60000;
    this.onStateChange = options.onStateChange;
    this.isFailure = options.isFailure ?? (() => true);
    this.isNeutral = options.isNeutral ?? (() => false);
  }

  /**
   * Execute an operation through the circuit breaker
   * @param operation - The async operation to execute; it receives its
   *   {@link Admission}, which only a HALF_OPEN probe needs
   * @returns Promise resolving with the operation result
   * @throws Error if circuit is OPEN, a HALF_OPEN probe holds the circuit, or
   *   the operation fails
   */
  async execute<T>(operation: (admission: Admission) => Promise<T>): Promise<T> {
    let probe: Probe | null = null;
    if (this.state === 'OPEN') {
      if (!this.lastFailureTime || Date.now() - this.lastFailureTime <= this.timeout)
        throw this.openError();
      this.transitionTo('HALF_OPEN');
    }
    if (this.state === 'HALF_OPEN') {
      // One probe decides recovery; everyone else is refused fast while it holds.
      // Once its hold expires, later callers are admitted as ordinary calls.
      if (this.probe?.exclusive) throw this.openError();
      if (!this.probe) probe = this.probe = { exclusive: true };
    }
    // A probe settled early (confirm), superseded by another verdict, or reset
    // finishes as an ordinary call: its outcome is accounted once, like any other.
    const holding = () => probe !== null && this.probe === probe;
    const admission: Admission = {
      confirm: () => {
        if (holding()) this.onSuccess();
      },
      holdFor: (ms) => {
        if (!holding() || probe!.timer) return;
        const token = probe!;
        token.holdUntil = Date.now() + ms;
        token.timer = setTimeout(() => (token.exclusive = false), ms);
        token.timer.unref?.();
      },
    };

    try {
      const result = await operation(admission);
      if (!this.isNeutral(result)) this.onSuccess();
      else if (holding()) this.endProbe();
      return result;
    } catch (error) {
      // Not every rejection is evidence the dependency is unhealthy. A caller
      // that cancelled its own request tells us nothing about the far side, and
      // counting those would let a user who aborts five searches trip the
      // breaker and fail unrelated tools for the whole timeout window.
      if (this.isFailure(error)) {
        this.onFailure();
      } else if (holding()) {
        this.endProbe();
      }
      throw error;
    } finally {
      if (probe?.timer) clearTimeout(probe.timer);
    }
  }

  private openError(): CircuitBreakerOpenError {
    return new CircuitBreakerOpenError(
      this.dependency,
      this.probe?.exclusive && this.probe.holdUntil !== undefined
        ? Math.max(1, this.probe.holdUntil - Date.now())
        : Math.max(0, this.timeout - (Date.now() - (this.lastFailureTime ?? Date.now()))),
    );
  }

  /** A probe that learned nothing leaves the circuit as it found it: OPEN, cooldown spent. */
  private endProbe(): void {
    this.probe = null;
    this.transitionTo('OPEN');
  }

  /**
   * Get current circuit breaker state
   */
  getState(): CircuitState {
    return this.state;
  }

  /** Observe admission without advancing OPEN to HALF_OPEN or taking a probe. */
  snapshot() {
    const blocked =
      (this.state === 'OPEN' && this.openError().context.retryAfterMs > 0) ||
      (this.state === 'HALF_OPEN' && this.probe?.exclusive === true);
    return {
      breaker: this.state.toLowerCase() as 'closed' | 'open' | 'half_open',
      breakerBlocked: blocked,
      ...(this.state !== 'CLOSED'
        ? { breakerRetryAfterS: this.openError().context.retryAfterMs / 1000 }
        : {}),
    };
  }

  /** Return the same owner refusal without taking a probe or changing state. */
  refusal(): CircuitBreakerOpenError | undefined {
    return this.snapshot().breakerBlocked ? this.openError() : undefined;
  }

  /**
   * Get current failure count
   */
  getFailureCount(): number {
    return this.failureCount;
  }

  /**
   * Manually reset the circuit breaker to CLOSED state
   */
  reset(): void {
    this.failureCount = 0;
    this.lastFailureTime = null;
    this.probe = null;
    this.transitionTo('CLOSED');
  }

  private onSuccess(): void {
    this.failureCount = 0;
    this.probe = null;
    this.transitionTo('CLOSED');
  }

  private onFailure(): void {
    this.failureCount++;
    this.lastFailureTime = Date.now();

    if (this.failureCount >= this.threshold || this.state === 'HALF_OPEN') {
      this.probe = null;
      this.transitionTo('OPEN');
      console.error('Circuit breaker opened due to repeated failures', {
        failureCount: this.failureCount,
        threshold: this.threshold,
        lastFailureTime: new Date(this.lastFailureTime).toISOString(),
      });
    }
  }

  private transitionTo(newState: CircuitState): void {
    if (this.state !== newState) {
      const oldState = this.state;
      this.state = newState;

      logger.info(`Circuit breaker state transition: ${oldState} -> ${newState}`);

      if (this.onStateChange) {
        this.onStateChange(oldState, newState);
      }
    }
  }
}
