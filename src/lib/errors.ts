/**
 * Custom error classes for Z-Library MCP
 * Implements the pattern from .claude/PATTERNS.md
 */

export interface ErrorContext {
  [key: string]: any;
  originalError?: any;
  stack?: string;
  timestamp?: string;
}

/**
 * Custom error class for Z-Library operations with context enrichment
 */
export class ZLibraryError extends Error {
  constructor(
    message: string,
    public code: string,
    public context?: ErrorContext,
    public retryable: boolean = true,
    public fatal: boolean = false
  ) {
    super(message);
    this.name = 'ZLibraryError';

    // Maintain proper stack trace in V8 environments
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, ZLibraryError);
    }
  }

  /**
   * Create a ZLibraryError from any error object with context enrichment
   * @param error - The original error
   * @param context - Additional context to attach
   * @returns Enriched ZLibraryError instance
   */
  static fromError(error: any, context?: ErrorContext): ZLibraryError {
    if (error instanceof ZLibraryError) {
      return error;
    }

    const message = error.message || String(error);
    const code = error.code || 'UNKNOWN_ERROR';

    // Determine if error is retryable based on code
    const retryable = !['AUTH_ERROR', 'FORBIDDEN', 'INVALID_INPUT', 'VALIDATION_ERROR'].includes(code);

    return new ZLibraryError(message, code, {
      ...context,
      originalError: error,
      stack: error.stack,
      timestamp: new Date().toISOString()
    }, retryable);
  }

  /**
   * Convert error to a JSON-serializable object
   */
  toJSON(): object {
    return {
      name: this.name,
      message: this.message,
      code: this.code,
      context: this.context,
      retryable: this.retryable,
      fatal: this.fatal,
      stack: this.stack
    };
  }
}

/**
 * Error thrown when network operations fail
 */
export class NetworkError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext) {
    super(message, 'NETWORK_ERROR', context, true, false);
    this.name = 'NetworkError';
  }
}

/**
 * Error thrown when authentication fails
 */
export class AuthenticationError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext) {
    super(message, 'AUTH_ERROR', context, false, true);
    this.name = 'AuthenticationError';
  }
}

/**
 * Error thrown when a domain is unavailable
 */
export class DomainError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext) {
    super(message, 'DOMAIN_ERROR', context, true, false);
    this.name = 'DomainError';
  }
}

/**
 * Error thrown when the Python bridge fails
 */
export class PythonBridgeError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext, retryable: boolean = true) {
    super(message, 'PYTHON_ERROR', context, retryable, false);
    this.name = 'PythonBridgeError';
  }
}

/**
 * Error thrown when operation times out
 */
export class TimeoutError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext) {
    super(message, 'TIMEOUT', context, true, false);
    this.name = 'TimeoutError';
  }
}

/**
 * Error thrown when the Python bridge subprocess exceeded its wall-clock
 * budget (or the caller aborted) and was killed.
 *
 * Deliberately fatal, unlike TimeoutError. The bridge budget is minutes long
 * and the Python side has already exhausted its own per-provider retries and
 * mirror failover by the time it fires, so retrying multiplies a wait the
 * caller is plainly not getting value from. `isRetryableError` short-circuits
 * on `fatal`, so this stops the retry loop before the code-based rules.
 */
export class BridgeTimeoutError extends ZLibraryError {
  constructor(message: string, context?: ErrorContext) {
    super(message, 'TIMEOUT', context, false, true);
    this.name = 'BridgeTimeoutError';
  }
}
