/**
 * Retry logic with exponential backoff for Z-Library MCP
 * Implements the pattern from .claude/PATTERNS.md
 */

import { logger } from './logger.js';

export interface RetryOptions {
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
  operation: () => Promise<T>,
  options: RetryOptions = {}
): Promise<T> {
  const {
    maxRetries = 3,
    initialDelay = 1000,
    maxDelay = 30000,
    factor = 2,
    shouldRetry = (error) => !error.fatal,
    onRetry
  } = options;

  let lastError: any;
  let delay = initialDelay;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;

      if (attempt === maxRetries || !shouldRetry(error)) {
        throw error;
      }

      // Log retry attempt
      const errorMessage = error instanceof Error ? error.message : String(error);
      logger.warn(`Attempt ${attempt + 1} failed, retrying in ${delay}ms...`, {
        error: errorMessage,
        attempt: attempt + 1,
        maxRetries,
        delay
      });

      // Call custom retry callback if provided
      if (onRetry) {
        onRetry(attempt + 1, error, delay);
      }

      // Wait before retry
      await new Promise(resolve => setTimeout(resolve, delay));

      // Calculate next delay with exponential backoff
      delay = Math.min(delay * factor, maxDelay);
    }
  }

  throw lastError;
}

/**
 * Determine if an error is retryable based on error characteristics
 * @param error - The error to classify
 * @returns true if the error should trigger a retry
 */
export function isRetryableError(error: any): boolean {
  // Fatal errors that should not be retried
  if (error.fatal === true) {
    return false;
  }

  // An explicit verdict from whoever built the error beats the heuristics
  // below. Those heuristics match on message text, so a provider error like
  // "resolved but did not accept a connection before the deadline" would
  // otherwise be retried on the strength of the word "connection" — even
  // though the caller already established the host is not answering.
  if (error.retryable === false) {
    return false;
  }

  // Authentication/authorization errors are not retryable
  if (error.code === 'AUTH_ERROR' || error.code === 'FORBIDDEN') {
    return false;
  }

  // Invalid input errors are not retryable
  if (error.code === 'INVALID_INPUT' || error.code === 'VALIDATION_ERROR') {
    return false;
  }

  // Network errors are retryable
  if (error.code === 'NETWORK_ERROR' ||
      error.code === 'TIMEOUT' ||
      error.code === 'ECONNREFUSED' ||
      error.code === 'ENOTFOUND' ||
      error.code === 'ETIMEDOUT') {
    return true;
  }

  // Python bridge errors might be transient
  if (error.code === 'PYTHON_ERROR') {
    // Check if it's a transient Python error
    const message = error.message?.toLowerCase() || '';
    if (message.includes('timeout') ||
        message.includes('connection') ||
        message.includes('network')) {
      return true;
    }
  }

  // Domain/server errors are retryable
  if (error.code === 'DOMAIN_ERROR' ||
      error.code === 'SERVER_ERROR' ||
      error.code?.startsWith('5')) {
    return true;
  }

  // Default to not retryable if we can't classify
  return false;
}
