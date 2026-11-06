/**
 * Retry logic with exponential backoff for Z-Library MCP
 * Implements the pattern from .claude/PATTERNS.md
 */

import { setTimeout as wait } from 'node:timers/promises';
import { logger } from './logger.js';
import { readAuthDiagnostics } from './auth-diagnostics.js';

export interface RetryOptions {
  signal?: AbortSignal;
  maxRetries?: number;
  initialDelay?: number;
  maxDelay?: number;
  factor?: number;
  shouldRetry?: (error: any) => boolean;
  onRetry?: (attempt: number, error: any, delay: number) => void;
}

/**
 * Execute an operation with retry logic and exponential backoff
 * @param operation - The async operation to execute
 * @param options - Retry configuration options
 * @returns Promise resolving with the operation result
 * @throws The last error if all retries are exhausted
 */
export async function withRetry<T>(
  operation: (attempt: number) => Promise<T>,
  options: RetryOptions = {},
): Promise<T> {
  const {
    maxRetries = 3,
    initialDelay = 1000,
    maxDelay = 30000,
    factor = 2,
    shouldRetry = (error) => !error.fatal,
    onRetry,
  } = options;

  let lastError: any;
  let delay = initialDelay;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      options.signal?.throwIfAborted();
      return await operation(attempt + 1);
    } catch (error) {
      lastError = error;

      if (attempt === maxRetries || !shouldRetry(error)) {
        throw error;
      }

      // Log retry attempt
      const diagnostics = readAuthDiagnostics((error as any)?.context?.diagnostics);
      logger.warn(
        'Dependency retry',
        JSON.stringify(diagnostics ?? { attempt: Math.min(65535, attempt + 1) }),
      );

      // Call custom retry callback if provided
      if (onRetry) {
        onRetry(attempt + 1, error, delay);
      }

      // Wait before retry
      await wait(delay, undefined, { signal: options.signal });

      // Calculate next delay with exponential backoff
      delay = Math.min(delay * factor, maxDelay);
    }
  }

  throw lastError;
}

/** Positively classified dependency-health failures, never message keywords. */
export function isTransientDependencyError(error: any): boolean {
  if (
    !error ||
    error.name === 'AbortError' ||
    ['aborted', 'auth_rejected', 'auth_backoff', 'identity_changed'].includes(error.context?.reason)
  )
    return false;
  if (error.code === 'BRIDGE_SPAWN')
    return ['EAGAIN', 'EMFILE', 'ENFILE'].includes(error.errnoCode ?? error.context?.errnoCode);
  if (error.code === 'TIMEOUT' && error.context?.reason === 'timeout') return true;
  if (
    error.fatal ||
    ['AUTH_ERROR', 'FORBIDDEN', 'INVALID_INPUT', 'VALIDATION_ERROR', 'BRIDGE_KILLED'].includes(
      error.code,
    )
  )
    return false;
  const transientStatus = (status: any) =>
    Number.isInteger(status) &&
    (status === 408 || status === 429 || (status >= 500 && status <= 599));
  const transientDetail = (detail: any): boolean => {
    if (!detail || typeof detail !== 'object') return false;
    if (Array.isArray(detail.failures)) return detail.failures.some(transientDetail);
    // Source adapters emit an exact HTTP NNN token in this structured field.
    if (detail.reason === 'http_error')
      return transientStatus(
        detail.status_code ?? Number(/^HTTP (\d{3})$/.exec(detail.detail)?.[1]),
      );
    return [
      'dns_failure',
      'dns_timeout',
      'connect_timeout',
      'connect_refused',
      'connect_error',
      'read_timeout',
      'search_timeout',
      'integrity_mismatch',
      'partial_transfer',
    ].includes(detail.reason);
  };
  if (error.context?.details !== undefined) return transientDetail(error.context.details);
  if (
    error.context?.pythonErrorType !== 'RedirectRefusedError' &&
    (error.context?.pythonErrorType === 'HTTPStatusError' ||
      (error.context?.reason === 'login_failed' && Number.isInteger(error.context.loginStatus)))
  )
    return transientStatus(error.context.httpStatus ?? error.context.loginStatus);
  const code = error.code === 'PYTHON_ERROR' ? error.context?.originalError?.code : error.code;
  return (
    [
      'NETWORK_ERROR',
      'TIMEOUT',
      'ECONNREFUSED',
      'ECONNRESET',
      'ENOTFOUND',
      'ETIMEDOUT',
      'EAI_AGAIN',
      'DOMAIN_ERROR',
      'SERVER_ERROR',
    ].includes(code) ||
    /^5\d\d$/.test(String(code)) ||
    [
      'ReadTimeout',
      'ConnectError',
      'RedirectRefusedError',
      'WriteTimeout',
      'ConnectTimeout',
      'PoolTimeout',
      'TimeoutException',
      'RemoteProtocolError',
      'ReadError',
      'WriteError',
      'ConnectionError',
      'TimeoutError',
    ].includes(error.context?.pythonErrorType)
  );
}

/** Python owns retries/failover once it emits a details-bearing envelope. */
export function isRetryableError(error: any): boolean {
  return (
    error?.context?.details === undefined &&
    error?.details === undefined &&
    !error?.fatal &&
    error?.retryable !== false &&
    isTransientDependencyError(error)
  );
}
