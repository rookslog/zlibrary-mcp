import { jest, describe, test, expect, beforeEach } from '@jest/globals';

// Import from compiled dist directory
let retryManager;

describe('Retry Manager', () => {
  beforeEach(async () => {
    jest.resetModules();
    jest.clearAllMocks();
    retryManager = await import('../dist/lib/retry-manager.js');
  });

  describe('withRetry', () => {
    test('should succeed on first attempt if operation succeeds', async () => {
      const operation = jest.fn().mockResolvedValue('success');

      const result = await retryManager.withRetry(operation);

      expect(result).toBe('success');
      expect(operation).toHaveBeenCalledTimes(1);
    });

    test('should retry on failure and eventually succeed', async () => {
      const operation = jest.fn()
        .mockRejectedValueOnce(new Error('First failure'))
        .mockRejectedValueOnce(new Error('Second failure'))
        .mockResolvedValueOnce('success');

      const result = await retryManager.withRetry(operation, {
        maxRetries: 3,
        initialDelay: 10, // Short delay for tests
        factor: 1
      });

      expect(result).toBe('success');
      expect(operation).toHaveBeenCalledTimes(3);
    });

    test('should throw error after max retries exhausted', async () => {
      const operation = jest.fn().mockRejectedValue(new Error('Persistent failure'));

      await expect(
        retryManager.withRetry(operation, {
          maxRetries: 2,
          initialDelay: 10,
          factor: 1
        })
      ).rejects.toThrow('Persistent failure');

      expect(operation).toHaveBeenCalledTimes(3); // Initial + 2 retries
    });

    test('should respect shouldRetry predicate', async () => {
      const fatalError = { message: 'Fatal error', fatal: true };
      const operation = jest.fn().mockRejectedValue(fatalError);

      await expect(
        retryManager.withRetry(operation, {
          maxRetries: 3,
          shouldRetry: (error) => !error.fatal
        })
      ).rejects.toEqual(fatalError);

      expect(operation).toHaveBeenCalledTimes(1); // No retries for fatal errors
    });

    test('should apply exponential backoff', async () => {
      const operation = jest.fn()
        .mockRejectedValueOnce(new Error('Failure 1'))
        .mockRejectedValueOnce(new Error('Failure 2'))
        .mockResolvedValueOnce('success');

      const delays = [];
      const onRetry = jest.fn((attempt, error, delay) => {
        delays.push(delay);
      });

      await retryManager.withRetry(operation, {
        maxRetries: 3,
        initialDelay: 100,
        factor: 2,
        onRetry
      });

      expect(delays[0]).toBe(100); // First retry delay
      expect(delays[1]).toBe(200); // Second retry delay (100 * 2)
      expect(onRetry).toHaveBeenCalledTimes(2);
    });

    test('should respect maxDelay cap', async () => {
      const operation = jest.fn()
        .mockRejectedValueOnce(new Error('Failure 1'))
        .mockRejectedValueOnce(new Error('Failure 2'))
        .mockResolvedValueOnce('success');

      const delays = [];
      const onRetry = jest.fn((attempt, error, delay) => {
        delays.push(delay);
      });

      await retryManager.withRetry(operation, {
        maxRetries: 3,
        initialDelay: 50,
        maxDelay: 60,
        factor: 2,
        onRetry
      });

      expect(delays[0]).toBe(50); // First retry delay
      expect(delays[1]).toBe(60); // Second retry capped at maxDelay (50 * 2 = 100, capped to 60)
    });

    test('should call onRetry callback for each retry', async () => {
      const operation = jest.fn()
        .mockRejectedValueOnce(new Error('Failure 1'))
        .mockResolvedValueOnce('success');

      const onRetry = jest.fn();

      await retryManager.withRetry(operation, {
        maxRetries: 2,
        initialDelay: 10,
        onRetry
      });

      expect(onRetry).toHaveBeenCalledTimes(1);
      expect(onRetry).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ message: 'Failure 1' }),
        10
      );
    });
  });

  describe('isRetryableError', () => {
    test('should return false for fatal errors', () => {
      const error = { fatal: true };
      expect(retryManager.isRetryableError(error)).toBe(false);
    });

    test('should return false for auth errors', () => {
      expect(retryManager.isRetryableError({ code: 'AUTH_ERROR' })).toBe(false);
      expect(retryManager.isRetryableError({ code: 'FORBIDDEN' })).toBe(false);
    });

    test('should return false for validation errors', () => {
      expect(retryManager.isRetryableError({ code: 'INVALID_INPUT' })).toBe(false);
      expect(retryManager.isRetryableError({ code: 'VALIDATION_ERROR' })).toBe(false);
    });

    test('should return true for network errors', () => {
      expect(retryManager.isRetryableError({ code: 'NETWORK_ERROR' })).toBe(true);
      expect(retryManager.isRetryableError({ code: 'TIMEOUT' })).toBe(true);
      expect(retryManager.isRetryableError({ code: 'ECONNREFUSED' })).toBe(true);
      expect(retryManager.isRetryableError({ code: 'ETIMEDOUT' })).toBe(true);
    });

    test('should return true for server errors', () => {
      expect(retryManager.isRetryableError({ code: 'SERVER_ERROR' })).toBe(true);
      expect(retryManager.isRetryableError({ code: '500' })).toBe(true);
      expect(retryManager.isRetryableError({ code: '503' })).toBe(true);
    });

    test('should return true for domain errors', () => {
      expect(retryManager.isRetryableError({ code: 'DOMAIN_ERROR' })).toBe(true);
    });

    test('should return true for transient Python errors', () => {
      const error = {
        code: 'PYTHON_ERROR',
        message: 'Connection timeout occurred'
      };
      expect(retryManager.isRetryableError(error)).toBe(true);
    });

    test('should honor an explicit retryable=true before Python message heuristics', () => {
      const error = {
        code: 'PYTHON_ERROR',
        message: 'Provider returned HTTP 503',
        retryable: true,
        context: {
          details: {
            operation: 'download',
            provider: 'libgen',
            host: 'cdn.example',
            reason: 'http_error',
          },
        },
      };

      expect(retryManager.isRetryableError(error)).toBe(true);
    });

    test('should honor an explicit retryable=false before retryable code heuristics', () => {
      const error = {
        code: 'NETWORK_ERROR',
        message: 'Connection failed',
        retryable: false,
      };

      expect(retryManager.isRetryableError(error)).toBe(false);
    });

    test('should return false for non-transient Python errors', () => {
      const error = {
        code: 'PYTHON_ERROR',
        message: 'Import error: module not found'
      };
      expect(retryManager.isRetryableError(error)).toBe(false);
    });

    test('should return false for unknown errors by default', () => {
      expect(retryManager.isRetryableError({ code: 'UNKNOWN' })).toBe(false);
      expect(retryManager.isRetryableError({})).toBe(false);
    });
  });
});
