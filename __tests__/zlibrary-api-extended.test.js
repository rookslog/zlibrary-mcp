import { jest, describe, beforeEach, test, expect } from '@jest/globals';
import * as path from 'path';

// ============================================================================
// Tests for uncovered API methods in src/lib/zlibrary-api.ts
// Covers: getBookMetadata, searchByTerm, searchByAuthor, fetchBooklist,
//         searchAdvanced, searchMultiSource, and filterMetadataResponse logic
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
      LONG_BRIDGE_TIMEOUT_MS: 1800000,
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

  describe('getBookMetadata', () => {
    test('should call Python bridge with correct args and return filtered metadata', async () => {
      const fullMetadata = {
        id: '12345',
        book_hash: 'abcdef',
        title: 'Being and Time',
        author: 'Martin Heidegger',
        year: '1927',
        publisher: 'Max Niemeyer',
        language: 'German',
        pages: '437',
        isbn_10: '0123456789',
        isbn_13: '9780123456789',
        rating: '4.8',
        cover: 'http://example.com/cover.jpg',
        categories: ['Philosophy'],
        extension: 'pdf',
        filesize: '5000000',
        terms: ['phenomenology', 'dasein', 'being'],
        booklists: [{ id: 'bl1', name: 'Philosophy Essentials' }],
        description: 'A foundational text of existential phenomenology.',
        ipfs_cids: ['Qm123abc'],
        quality_score: 0.95,
        extra_field: 'should be excluded',
      };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      const result = await zlibApi.getBookMetadata('12345', 'abcdef', ['terms', 'description']);

      // Verify Python bridge call
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['get_book_metadata_complete', JSON.stringify({
          book_id: '12345',
          book_hash: 'abcdef',
        })],
      }), expect.objectContaining({ label: expect.any(String) }));

      // Core fields should be present
      expect(result.title).toBe('Being and Time');
      expect(result.author).toBe('Martin Heidegger');
      expect(result.year).toBe('1927');
      expect(result.publisher).toBe('Max Niemeyer');
      expect(result.isbn_13).toBe('9780123456789');
      expect(result.extension).toBe('pdf');
      expect(result.categories).toEqual(['Philosophy']);

      // Requested include groups should be present
      expect(result.terms).toEqual(['phenomenology', 'dasein', 'being']);
      expect(result.description).toBe('A foundational text of existential phenomenology.');

      // Non-requested optional fields should be absent
      expect(result.booklists).toBeUndefined();
      expect(result.ipfs_cids).toBeUndefined();
      expect(result.quality_score).toBeUndefined();

      // Extra non-core, non-include fields should be absent
      expect(result.extra_field).toBeUndefined();
    });

    test('should return only core fields when no include groups specified', async () => {
      const fullMetadata = {
        id: '1',
        title: 'Test',
        author: 'Auth',
        terms: ['t1', 't2'],
        description: 'desc',
      };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      const result = await zlibApi.getBookMetadata('1', 'hash1');

      // Core fields present
      expect(result.id).toBe('1');
      expect(result.title).toBe('Test');
      expect(result.author).toBe('Auth');

      // Optional fields excluded (no include specified)
      expect(result.terms).toBeUndefined();
      expect(result.description).toBeUndefined();
    });

    test('should handle include groups with empty array', async () => {
      const fullMetadata = { id: '2', title: 'Book', terms: ['t1'] };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      const result = await zlibApi.getBookMetadata('2', 'hash2', []);

      expect(result.id).toBe('2');
      expect(result.title).toBe('Book');
      expect(result.terms).toBeUndefined();
    });

    test('should include booklists and ipfs groups when requested', async () => {
      const fullMetadata = {
        id: '3',
        title: 'Book',
        booklists: [{ id: 'bl1' }],
        ipfs_cids: ['Qm123'],
        quality_score: 0.9,
      };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      const result = await zlibApi.getBookMetadata('3', 'hash3', ['booklists', 'ipfs', 'ratings']);

      expect(result.booklists).toEqual([{ id: 'bl1' }]);
      expect(result.ipfs_cids).toEqual(['Qm123']);
      expect(result.quality_score).toBe(0.9);
    });

    test('should handle non-object metadata (null/undefined) gracefully', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(null));

      const result = await zlibApi.getBookMetadata('4', 'hash4');
      expect(result).toBeNull();
    });

    test('should handle include groups that have no matching fields in metadata', async () => {
      const fullMetadata = { id: '5', title: 'Book' };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      // Request 'terms' group but metadata has no 'terms' field
      const result = await zlibApi.getBookMetadata('5', 'hash5', ['terms']);

      expect(result.id).toBe('5');
      expect(result.title).toBe('Book');
      expect(result.terms).toBeUndefined();
    });

    test('should handle unknown include group names gracefully', async () => {
      const fullMetadata = { id: '6', title: 'Book', terms: ['t1'] };

      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(fullMetadata));

      const result = await zlibApi.getBookMetadata('6', 'hash6', ['nonexistent_group']);

      // Core fields present, but no extra fields added for unknown group
      expect(result.id).toBe('6');
      expect(result.title).toBe('Book');
      expect(result.terms).toBeUndefined();
    });
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

  describe('fetchBooklist', () => {
    test('should call Python bridge with correct args', async () => {
      const mockResult = { books: [{ title: 'Listed Book' }], total: 50, page: 2 };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse(mockResult));

      const result = await zlibApi.fetchBooklist({
        booklistId: 'bl123',
        booklistHash: 'blhash456',
        topic: 'Continental Philosophy',
        page: 2,
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['fetch_booklist_bridge', JSON.stringify({
          booklist_id: 'bl123',
          booklist_hash: 'blhash456',
          topic: 'Continental Philosophy',
          page: 2,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockResult);
    });

    test('should use default page of 1 when not specified', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce(mockMcpResponse({ books: [] }));

      await zlibApi.fetchBooklist({
        booklistId: 'bl1',
        booklistHash: 'hash1',
        topic: 'test',
      });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        args: ['fetch_booklist_bridge', JSON.stringify({
          booklist_id: 'bl1',
          booklist_hash: 'hash1',
          topic: 'test',
          page: 1,
        })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });

    test('should propagate errors from Python bridge', async () => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(new Error('Booklist failed'));

      await expect(zlibApi.fetchBooklist({
        booklistId: 'bl1', booklistHash: 'h1', topic: 't1',
      })).rejects.toThrow(/Python bridge execution failed for fetch_booklist_bridge/);
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

    test('a response-level failure is not marked non-retryable', async () => {
      // Only reachability failures get the explicit veto. An HTTP error means
      // the host answered, so whether to retry is left to the general
      // classifier rather than being decided here.
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

      expect(error.retryable).toBe(true);
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
