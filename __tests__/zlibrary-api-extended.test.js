import './helpers/environment-login.js';
import { jest, describe, beforeEach, afterEach, test, expect } from '@jest/globals';

// These fixtures describe a single per-call subprocess, not private acquisition.
let previousSessionMode;
beforeEach(() => {
  previousSessionMode = process.env.ZLIBRARY_SESSION_MODE;
  process.env.ZLIBRARY_SESSION_MODE = 'per-call';
});
afterEach(() => {
  if (previousSessionMode === undefined) delete process.env.ZLIBRARY_SESSION_MODE;
  else process.env.ZLIBRARY_SESSION_MODE = previousSessionMode;
});
import * as path from 'path';

// ============================================================================
// Tests for uncovered API methods in src/lib/zlibrary-api.ts
// Covers: searchByTerm, searchByAuthor, searchAdvanced and searchMultiSource
// ============================================================================

// Increase timeout for async operations
jest.setTimeout(30000);

// Mock dependencies
const mockGetManagedPythonPath = jest.fn();
const mockRunPythonBridge = jest.fn();

// Use dynamic path resolution for portability
const EXPECTED_SCRIPT_PATH = path.resolve(process.cwd(), 'lib');

describe('Z-Library API - Extended Coverage', () => {
  let zlibApi;

  // Helper: wraps result in the double-parse MCP response format expected by callPythonFunction
  function mockMcpResponse(innerResult) {
    const innerJson = JSON.stringify(innerResult);
    const mcpResponse = JSON.stringify({ content: [{ type: 'text', text: innerJson }] });
    return [mcpResponse];
  }

  beforeEach(async () => {
    jest.resetModules();
    jest.clearAllMocks();

    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      getManagedPythonPath: mockGetManagedPythonPath,
    }));

    // The bridge subprocess is spawned through python-runner, not
    // PythonShell.run: the runner is what enforces the wall-clock budget and
    // kills the child. Mocking it here keeps these tests focused on
    // callPythonFunction's parsing and error handling.
    // A factory mock replaces the module wholesale, so every export the module
    // under test imports has to appear here or the import fails at link time.
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge: mockRunPythonBridge,
      killAllPythonChildren: jest.fn(),
      installExitHooks: jest.fn(),
      liveChildCount: jest.fn(() => 0),
      DEFAULT_BRIDGE_TIMEOUT_MS: 240000,
      LONG_BRIDGE_TIMEOUT_MS: 2400000,
    }));

    // Minimal fs mock (required by module imports)
    jest.unstable_mockModule('fs', () => ({
      existsSync: jest.fn(),
      mkdirSync: jest.fn(),
      createWriteStream: jest.fn(),
      readFileSync: jest.fn(),
      writeFileSync: jest.fn(),
      unlinkSync: jest.fn(),
    }));

    jest.unstable_mockModule('http', () => ({ get: jest.fn() }));
    jest.unstable_mockModule('https', () => ({ get: jest.fn() }));

    zlibApi = await import('../dist/lib/zlibrary-api.js');
  });

  describe('searchByTerm', () => {
    test('should call Python bridge with correct args', async () => {
      const mockResult = [{ id: '1', title: 'Dialectic Book' }];
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(mockResult));

      const result = await zlibApi.searchByTerm({
        term: 'dialectic',
        yearFrom: 1800,
        yearTo: 2000,
        languages: ['english'],
        extensions: ['pdf'],
        limit: 30,
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['search_by_term_bridge', JSON.stringify({
          term: 'dialectic',
          year_from: 1800,
          year_to: 2000,
          languages: ['english'],
          extensions: ['pdf'],
          limit: 30,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockResult);
    });

    test('should use default limit of 25 when not specified', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse([]));

      await zlibApi.searchByTerm({ term: 'epistemology' });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        args: ['search_by_term_bridge', JSON.stringify({
          term: 'epistemology',
          year_from: undefined,
          year_to: undefined,
          languages: undefined,
          extensions: undefined,
          limit: 25,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });

    test('should propagate errors from Python bridge', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(new Error('Term search failed'));

      await expect(zlibApi.searchByTerm({ term: 'test' }))
        .rejects.toThrow(/Python bridge execution failed for search_by_term_bridge/);
    });
  });

  describe('searchByAuthor', () => {
    test('should call Python bridge with correct args', async () => {
      const mockResult = [{ id: '1', title: 'Critique of Pure Reason' }];
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(mockResult));

      const result = await zlibApi.searchByAuthor({
        author: 'Kant, Immanuel',
        exact: true,
        yearFrom: 1781,
        yearTo: 1790,
        languages: ['german'],
        extensions: ['epub'],
        limit: 10,
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['search_by_author_bridge', JSON.stringify({
          author: 'Kant, Immanuel',
          exact: true,
          year_from: 1781,
          year_to: 1790,
          languages: ['german'],
          extensions: ['epub'],
          limit: 10,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockResult);
    });

    test('should use defaults for exact (false) and limit (25)', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse([]));

      await zlibApi.searchByAuthor({ author: 'Hegel' });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        args: ['search_by_author_bridge', JSON.stringify({
          author: 'Hegel',
          exact: false,
          year_from: undefined,
          year_to: undefined,
          languages: undefined,
          extensions: undefined,
          limit: 25,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });

    test('should propagate errors from Python bridge', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(new Error('Author search failed'));

      await expect(zlibApi.searchByAuthor({ author: 'test' }))
        .rejects.toThrow(/Python bridge execution failed for search_by_author_bridge/);
    });
  });

  describe('searchAdvanced', () => {
    test('should call Python bridge with correct args', async () => {
      const mockResult = {
        exact_matches: [{ title: 'Exact Match' }],
        fuzzy_matches: [{ title: 'Fuzzy Match' }],
      };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(mockResult));

      const result = await zlibApi.searchAdvanced({
        query: 'phenomenology of spirit',
        exact: true,
        yearFrom: 1807,
        yearTo: 1807,
        count: 5,
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['search_advanced', JSON.stringify({
          query: 'phenomenology of spirit',
          exact: true,
          from_year: 1807,
          to_year: 1807,
          count: 5,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockResult);
    });

    test('should use defaults for exact (false) and count (10)', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse({}));

      await zlibApi.searchAdvanced({ query: 'test' });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        args: ['search_advanced', JSON.stringify({
          query: 'test',
          exact: false,
          from_year: undefined,
          to_year: undefined,
          count: 10,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });

    test('should propagate errors from Python bridge', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(new Error('Advanced search failed'));

      await expect(zlibApi.searchAdvanced({ query: 'test' }))
        .rejects.toThrow(/Python bridge execution failed for search_advanced/);
    });
  });

  describe('searchMultiSource', () => {
    test('should call Python bridge with correct args', async () => {
      const mockResult = [{ md5: 'abc123', title: 'Multi Book', source: 'annas' }];
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(mockResult));

      const result = await zlibApi.searchMultiSource({
        query: 'philosophy',
        source: 'annas',
        count: 20,
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['search_multi_source', JSON.stringify({
          query: 'philosophy',
          source: 'annas',
          count: 20,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockResult);
    });

    test('should use defaults for source (auto) and count (10)', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse([]));

      await zlibApi.searchMultiSource({ query: 'test' });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        args: ['search_multi_source', JSON.stringify({
          query: 'test',
          source: 'auto',
          count: 10,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });

    test('should propagate errors from Python bridge', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(new Error('Multi-source failed'));

      await expect(zlibApi.searchMultiSource({ query: 'test' }))
        .rejects.toThrow(/Python bridge execution failed for search_multi_source/);
    });

    test('should pass an abort signal through to the runner', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse([]));
      const controller = new AbortController();

      await zlibApi.searchMultiSource({ query: 'test' }, { signal: controller.signal });

      expect(mockRunPythonBridge).toHaveBeenCalledWith(
        'python_bridge.py',
        expect.any(Object),
        expect.objectContaining({ signal: controller.signal }),
      );
    });
  });

  describe('downloadBookToFile error envelope', () => {
    test('keeps provider details directly on its public-facing wrapper', async () => {
      const details = {
        operation: 'download',
        failures: [
          { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
        ],
      };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        Object.assign(new Error('bridge failed'), {
          stderr: JSON.stringify({
            error: 'download failed on every source',
            type: 'AllSourcesFailedError',
            details,
          }),
        }),
      );

      const error = await zlibApi
        .downloadBookToFile({
          bookDetails: {
            md5: '0123456789abcdef0123456789abcdef',
            source: 'libgen',
          },
        })
        .catch((err) => err);

      expect(error.context.details).toEqual(details);
      expect(error.cause).toBeUndefined();
    });
  });

  /**
   * The bridge writes a JSON envelope to stderr when a provider fails. These
   * assert it survives the trip to the caller: a caller that cannot tell
   * "annas-archive.gl does not resolve" from "libgen.li dropped the
   * connection" cannot pick a useful next step.
   */
  describe('provider failure surfacing', () => {
    /** Build a python-shell style rejection carrying a bridge stderr envelope. */
    function bridgeFailure(envelope, extraStderr = '') {
      const err = new Error('Python process exited with code 1');
      err.stderr = `${extraStderr}${JSON.stringify(envelope)}`;
      return err;
    }

    test('surfaces the provider, host and reason from the stderr envelope', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure({
          error: 'annas (annas-archive.gl) could not be resolved',
          type: 'ProviderUnreachableError',
          details: {
            provider: 'annas',
            host: 'annas-archive.gl',
            reason: 'dns_failure',
            detail: 'Name or service not known',
          },
        }),
      );

      const error = await zlibApi
        .searchMultiSource({ query: 'test', source: 'annas' })
        .catch((e) => e);

      expect(error.message).toContain('annas-archive.gl');
      expect(error.message).toContain('search_multi_source failed');
      expect(error.context.details.provider).toBe('annas');
      expect(error.context.details.reason).toBe('dns_failure');
      expect(error.context.pythonErrorType).toBe('ProviderUnreachableError');
    });

    test('finds the envelope even when log lines precede it on stderr', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure(
          {
            error: 'libgen mirrors all failed',
            details: {
              operation: 'search',
              failures: [
                { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
                { provider: 'libgen', host: 'libgen.vg', reason: 'connect_timeout' },
              ],
            },
          },
          'WARNING - LibGen mirror li unreachable\nnot json at all\n',
        ),
      );

      const error = await zlibApi
        .searchMultiSource({ query: 'test', source: 'libgen' })
        .catch((e) => e);

      expect(error.context.details.failures).toHaveLength(2);
      expect(error.context.details.failures[0].host).toBe('libgen.li');
    });

    test('an unreachable provider is not retried', async () => {
      const { isRetryableError } = await import('../lib/retry-manager.js');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure({
          error: 'libgen (libgen.li) resolved but did not accept a connection',
          details: { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
        }),
      );

      const error = await zlibApi
        .searchMultiSource({ query: 'test', source: 'libgen' })
        .catch((e) => e);

      // The word "connection" in the message would otherwise make the
      // heuristic classifier retry a host already known to be dead.
      expect(isRetryableError(error)).toBe(false);
      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
    });

    test('configuration failures do not open the shared bridge circuit', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure({
          error: 'download failed on every source',
          type: 'AllSourcesFailedError',
          details: {
            operation: 'download',
            failures: [
              {
                provider: 'annas',
                host: 'annas-archive.gl',
                reason: 'configuration_error',
                detail: 'ANNAS_SECRET_KEY not configured',
              },
            ],
          },
        }),
      );

      const messages = [];
      for (let attempt = 0; attempt < 6; attempt += 1) {
        const error = await zlibApi
          .searchMultiSource({ query: 'test', source: 'annas' })
          .catch((e) => e);
        messages.push(error.message);
      }

      expect(messages).not.toContain('Circuit breaker is OPEN');
      expect(mockRunPythonBridge).toHaveBeenCalledTimes(6);
    });

    test('quota exhaustion does not retry or open the shared bridge circuit', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure({
          error: 'annas quota exhausted',
          type: 'SourceError',
          details: {
            operation: 'download',
            provider: 'annas',
            reason: 'quota_exhausted',
          },
        }),
      );

      const messages = [];
      for (let attempt = 0; attempt < 6; attempt += 1) {
        const error = await zlibApi
          .downloadBookToFile({
            bookDetails: { md5: '0123456789abcdef0123456789abcdef', source: 'annas' },
          })
          .catch((e) => e);
        messages.push(error.message);
      }

      expect(messages).not.toContain('Circuit breaker is OPEN');
      expect(mockRunPythonBridge).toHaveBeenCalledTimes(6);
    });

    test('response-level details are never retried by Node', async () => {
      // Python owns the provider walk, including response-level failures.
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(
        bridgeFailure({
          error: 'annas (annas-archive.gl) returned an HTTP error',
          details: { provider: 'annas', host: 'annas-archive.gl', reason: 'http_error' },
        }),
      );

      const error = await zlibApi
        .searchMultiSource({ query: 'test', source: 'annas' })
        .catch((e) => e);

      expect(error.retryable).toBe(false);
    });

    test('falls back to the raw message when stderr holds no envelope', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      const err = new Error('boom');
      err.stderr = 'Traceback (most recent call last):\n  ValueError: nope';
      mockRunPythonBridge.mockRejectedValue(err);

      const error = await zlibApi.searchMultiSource({ query: 'test' }).catch((e) => e);

      expect(error.message).toContain('Python bridge execution failed');
      expect(error.message).toContain('Traceback');
    });
  });
});
