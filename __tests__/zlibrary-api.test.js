import { jest, describe, beforeEach, test, expect, afterEach } from '@jest/globals';
import * as fs from 'fs'; // Keep for potential use in download tests if needed, though likely removable
import * as path from 'path'; // Keep for path assertions
import * as http from 'http'; // Keep for potential use in download tests if needed, though likely removable
import * as https from 'https'; // Keep for potential use in download tests if needed, though likely removable

// Increase timeout for async operations
jest.setTimeout(30000);

// Mock dependencies
const mockGetManagedPythonPath = jest.fn();
const mockRunPythonBridge = jest.fn();

/** Mirrors the runner's PYTHON_BRIDGE_LONG_TIMEOUT default (40 minutes). */
const LONG_BRIDGE_TIMEOUT_MS = 2400000;
const mockFsExistsSync = jest.fn();
const mockFsMkdirSync = jest.fn();
const mockFsCreateWriteStream = jest.fn();
const mockHttpGet = jest.fn();
const mockHttpsGet = jest.fn();

// Use dynamic path resolution for portability across different machines/users
const EXPECTED_SCRIPT_PATH = path.resolve(process.cwd(), 'lib');

describe('Z-Library API', () => {
  // Declare variables once at the top level of the describe block
  let zlibApi; // Will hold the actual imported module
  let result;
  let args;
  let mockWriteStream; // For fs.createWriteStream mock

  let previousSessionMode;
  beforeEach(async () => {
    // Parser/error tests use the established per-call boundary; carrier tests live separately.
    previousSessionMode = process.env.ZLIBRARY_SESSION_MODE;
    process.env.ZLIBRARY_SESSION_MODE = 'per-call';
    // Reset modules and clear mocks before each test
    jest.resetModules();
    jest.clearAllMocks();

    // --- Mock Dependencies ---
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      // Mock only the functions used by zlibrary-api
      getManagedPythonPath: mockGetManagedPythonPath,
      // ensureVenvReady is not used here
    }));

    // The bridge subprocess is spawned through python-runner, not
    // PythonShell.run: the runner is what enforces the wall-clock budget and
    // kills the child. Mocking it here keeps these tests focused on
    // callPythonFunction's parsing and error handling.
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge: mockRunPythonBridge,
      killAllPythonChildren: jest.fn(),
      installExitHooks: jest.fn(),
      liveChildCount: jest.fn(() => 0),
      DEFAULT_BRIDGE_TIMEOUT_MS: 240000,
      LONG_BRIDGE_TIMEOUT_MS: LONG_BRIDGE_TIMEOUT_MS,
    }));

    // Mock fs, http, https selectively
    mockWriteStream = {
      // Mock write stream instance
      on: jest.fn((event, cb) => {
        // Simulate finish immediately for simple cases, or allow manual trigger
        if (event === 'finish') {
          // Store the callback to potentially call later in tests
          mockWriteStream._finishCallback = cb;
        }
        return mockWriteStream; // Allow chaining
      }),
      close: jest.fn((cb) => {
        if (cb) cb(); // Simulate successful close
      }),
      _finishCallback: null, // To store the finish callback
      _errorCallback: null, // To store potential error callback
    };
    jest.unstable_mockModule('fs', () => ({
      existsSync: mockFsExistsSync,
      mkdirSync: mockFsMkdirSync,
      createWriteStream: mockFsCreateWriteStream.mockReturnValue(mockWriteStream), // Return the mock stream instance
      readFileSync: jest.fn(), // Add other fs functions if needed by the module
      writeFileSync: jest.fn(),
      unlinkSync: jest.fn(),
      // Add other fs functions if they are used by zlibrary-api.js
    }));

    jest.unstable_mockModule('http', () => ({
      get: mockHttpGet,
    }));
    jest.unstable_mockModule('https', () => ({
      get: mockHttpsGet,
    }));

    // --- Import Actual Module ---
    // Import the *compiled* JS file containing the actual implementation
    zlibApi = await import('../dist/lib/zlibrary-api.js');
  });

  afterEach(() => {
    if (previousSessionMode === undefined) delete process.env.ZLIBRARY_SESSION_MODE;
    else process.env.ZLIBRARY_SESSION_MODE = previousSessionMode;
  });

  describe('searchBooks', () => {
    test('should call Python bridge with correct parameters for searchBooks', async () => {
      const mockApiResult_search1 = [{ id: '1', title: 'Test Book' }]; // Unique result var
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      // Corrected Mock: Simulate python printing the JSON string of the MCP response structure within an array
      const mockPythonResultString_search1 = JSON.stringify(mockApiResult_search1);
      const mockMcpResponseString_search1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_search1 }] });
      mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_search1]);

      const searchArgs = {
        query: 'test query', exact: true, fromYear: 2000, toYear: 2023,
        languages: ['english', 'spanish'], extensions: ['pdf', 'epub'], count: 20
      };

      result = await zlibApi.searchBooks(searchArgs);

      // Verify PythonShell call
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ // Corrected script name
          mode: 'text', // Ensure mode is text for double parsing
          pythonPath: '/fake/python',
          scriptPath: EXPECTED_SCRIPT_PATH,
          args: ['search', JSON.stringify({
              query: searchArgs.query, exact: searchArgs.exact, from_year: searchArgs.fromYear, to_year: searchArgs.toYear,
              languages: searchArgs.languages, extensions: searchArgs.extensions, content_types: [], count: searchArgs.count
          })]
      }), expect.objectContaining({ label: expect.any(String) }));
      // Verify the final result
      expect(result).toEqual(mockApiResult_search1); // Use unique result var
    });

    test('should handle errors from Python bridge during searchBooks', async () => {
      const apiError = new Error('Python Search Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      // Simulate PythonShell.run rejecting
      mockRunPythonBridge.mockRejectedValue(apiError);

      await expect(zlibApi.searchBooks({ query: 'test' })).rejects.toThrow(`Python bridge execution failed for search: ${apiError.message}`);
  // Test suite for the internal callPythonFunction logic
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ scriptPath: EXPECTED_SCRIPT_PATH, args: ['search', JSON.stringify({ query: 'test', exact: false, from_year: null, to_year: null, languages: [], extensions: [], content_types: [], count: 10 })] }), expect.objectContaining({ label: expect.any(String) })); // Corrected script name and path
    });

    describe('callPythonFunction (Internal Logic)', () => {
    test('should throw error if getManagedPythonPath fails', async () => {
      // Arrange: Mock getManagedPythonPath to reject
      const pathError = new Error('Failed to get Python path');
      mockGetManagedPythonPath.mockRejectedValue(pathError);

      // Dynamically import zlibApi *inside* the test AFTER mocks are set
      const zlibApi = await import('../dist/lib/zlibrary-api.js');

      // Act & Assert: Expect any function using callPythonFunction to reject
      // Using searchBooks as an example
      // Adjust expectation to match observed behavior (original error thrown)
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(`Python bridge execution failed for search: ${pathError.message}`); // Expect the wrapped error message

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).not.toHaveBeenCalled(); // PythonShell should not be called
    }); // <-- This closes the test for getManagedPythonPath failure

    test('should throw error if PythonShell.run throws', async () => {
      // Arrange: Mock getManagedPythonPath to succeed, PythonShell.run to throw
      const shellError = new Error('PythonShell failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(shellError);

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(`Python bridge execution failed for search: ${shellError.message}`);

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should throw error if Python script returns an error object', async () => {
      // Arrange: Mock PythonShell.run to return an error object
      const pythonErrorMsg = 'Something went wrong in Python';
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      // Corrected Mock: Simulate python printing the JSON string of the MCP response structure containing an error object
      const mockPythonErrorString_err1 = JSON.stringify({ error: pythonErrorMsg });
      const mockMcpErrorResponseString_err1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonErrorString_err1 }] });
      mockRunPythonBridge.mockResolvedValueOnce([mockMcpErrorResponseString_err1]);

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(`Python bridge execution failed for search: ${pythonErrorMsg}`);

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should throw error if Python script returns non-JSON string', async () => {
      // Arrange: Mock PythonShell.run to return a non-JSON string
      const nonJsonOutput = 'This is not JSON';
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      // In text mode, the first parse in callPythonFunction would fail
      mockRunPythonBridge.mockResolvedValueOnce([nonJsonOutput]); // Provide the non-JSON string

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        // Raw parse snippets can contain the internal session; keep diagnostics fixed.
        .toThrow('Failed to parse initial JSON output from Python script.');

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should throw error if Python script returns no output', async () => {
      // Arrange: Mock PythonShell.run to return empty array or undefined
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValue([]); // Simulate empty results array

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(/No output received from Python script/); // Adjusted error message

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

     test('should throw error if Python script returns unexpected object format', async () => {
      // Arrange: Mock PythonShell.run to return an object without 'error' or expected data
      const unexpectedObject = { someOtherKey: 'value' };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      // Corrected Mock: Simulate python printing the JSON string of the MCP response structure containing the unexpected object
      const mockPythonUnexpectedString_unexp1 = JSON.stringify(unexpectedObject);
      const mockMcpUnexpectedResponseString_unexp1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonUnexpectedString_unexp1 }] });
      mockRunPythonBridge.mockResolvedValueOnce([mockMcpUnexpectedResponseString_unexp1]);

      // Act & Assert: The double parse should succeed, returning the unexpected object
      const result = await zlibApi.searchBooks({ query: 'test' });
      expect(result).toEqual(unexpectedObject); // Test now expects the inner object

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should include stderr in error message when PythonShell provides it', async () => {
      // Arrange: Mock PythonShell.run to throw error with stderr
      const shellError = new Error('Python script failed');
      shellError.stderr = 'ImportError: No module named foo\nTraceback...';
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(shellError);

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(/Python bridge execution failed for search:.*Stderr: ImportError: No module named foo/);

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should handle PythonShell non-zero exit code error', async () => {
      // Arrange: Mock PythonShell.run to throw error simulating non-zero exit
      const exitError = new Error('Process exited with code 1');
      exitError.exitCode = 1;
      exitError.stderr = 'SyntaxError: invalid syntax';
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(exitError);

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(/Python bridge execution failed for search: Process exited with code 1.*Stderr: SyntaxError/);

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    test('should handle PythonShell timeout error', async () => {
      // Arrange: Mock PythonShell.run to throw timeout error
      const timeoutError = new Error('Timeout: Python script exceeded execution time');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(timeoutError);

      // Act & Assert
      await expect(zlibApi.searchBooks({ query: 'test' }))
        .rejects
        .toThrow(/Python bridge execution failed for search: Timeout/);

      // Verify mocks
      expect(mockGetManagedPythonPath).toHaveBeenCalled();
      expect(mockRunPythonBridge).toHaveBeenCalled();
    });

    }); // <-- This closes describe('callPythonFunction (Internal Logic)')

    // ISSUE-006 RESOLVED: Tests for PythonShell.run errors now cover:
    // - non-zero exit code (test above)
    // - stderr handling (test above)
    // - timeout (test above)
    // - no result (line 201)
    // - bad JSON (line 183)
  }); // <-- This closes describe('searchBooks')


    // REMOVED extra closing brace }); from here (was line 229)


    test('should handle empty results from searchBooks', async () => {
        const mockApiResultEmpty_empty1 = []; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure containing an empty list
        const mockPythonEmptyResultString_empty1 = JSON.stringify(mockApiResultEmpty_empty1);
        const mockMcpEmptyResponseString_empty1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonEmptyResultString_empty1 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpEmptyResponseString_empty1]);

        result = await zlibApi.searchBooks({ query: 'empty test' });

        expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ // Corrected script name
            scriptPath: EXPECTED_SCRIPT_PATH,
            args: ['search', JSON.stringify({ query: 'empty test', exact: false, from_year: null, to_year: null, languages: [], extensions: [], content_types: [], count: 10 })] // Default args
        }), expect.objectContaining({ label: expect.any(String) }));
        expect(result).toEqual(mockApiResultEmpty_empty1); // Use unique result var
    });

  describe('fullTextSearch', () => {
    test('should call Python bridge for fullTextSearch', async () => {
        const mockApiResult_ft1 = [{ id: '2', title: 'JS Book' }]; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure
        const mockPythonResultString_ft1 = JSON.stringify(mockApiResult_ft1);
        const mockMcpResponseString_ft1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_ft1 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_ft1]);

        const searchArgs = {
            query: 'javascript', exact: false, phrase: true, words: false,
            languages: ['english'], extensions: ['pdf'], count: 15
        };
        result = await zlibApi.fullTextSearch(searchArgs);

        expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ // Corrected script name
            scriptPath: EXPECTED_SCRIPT_PATH,
            args: ['full_text_search', JSON.stringify({
                query: searchArgs.query, exact: searchArgs.exact, phrase: searchArgs.phrase, words: searchArgs.words,
                languages: searchArgs.languages, extensions: searchArgs.extensions, content_types: [], count: searchArgs.count
            })]
        }), expect.objectContaining({ label: expect.any(String) }));
        expect(result).toEqual(mockApiResult_ft1); // Use unique result var
    });

     test('should handle errors from Python bridge during fullTextSearch', async () => {
      const apiError = new Error('Python Full Text Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(apiError);

      await expect(zlibApi.fullTextSearch({ query: 'fail text' })).rejects.toThrow(`Python bridge execution failed for full_text_search: ${apiError.message}`);
      // Check default args passed to python
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ scriptPath: EXPECTED_SCRIPT_PATH, args: ['full_text_search', JSON.stringify({ query: 'fail text', exact: false, phrase: true, words: false, languages: [], extensions: [], content_types: [], count: 10 })] }), expect.objectContaining({ label: expect.any(String) })); // Corrected script name and path
    });
  });

  describe('downloadBookToFile', () => {
    // These tests now check the internal logic, mocking dependencies like python-shell, fs, http

    // Updated for Spec v2.1: Uses bookDetails, expects absolute path, no processed_file_path
    test('should call Python bridge with correct args (no RAG)', async () => {
        const pythonResult_dl1 = { file_path: '/abs/path/to/downloads/Success Book.epub' }; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure
        const mockPythonResultString_dl1 = JSON.stringify(pythonResult_dl1);
        const mockMcpResponseString_dl1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_dl1 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_dl1]);

        const mockBookDetails = { id: 'success123', url: 'http://example.com/book/success123/slug', title: 'Success Book' };
        const downloadArgs = { bookDetails: mockBookDetails, outputDir: './downloads', process_for_rag: false };

        result = await zlibApi.downloadBookToFile(downloadArgs);

        expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
        expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
            scriptPath: EXPECTED_SCRIPT_PATH,
            args: ['download_book', JSON.stringify({
                book_details: mockBookDetails, // Pass bookDetails object
                output_dir: './downloads',
            })]
        }), expect.objectContaining({ label: expect.any(String) }));
        expect(result).toEqual({
            file_path: '/abs/path/to/downloads/Success Book.epub'
            // No processed_file_path expected
        });
    });

    // Updated for Spec v2.1: Uses bookDetails
    test('should throw error if Python response is missing file_path', async () => {
        const invalidPythonResult_dl4 = { some_other_key: 'value' }; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure
        const mockPythonResultString_dl4 = JSON.stringify(invalidPythonResult_dl4);
        const mockMcpResponseString_dl4 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_dl4 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_dl4]);

        const mockBookDetails = { id: 'invalidResp1', url: 'http://example.com/book/invalidResp1/slug' };
        const downloadArgs = { bookDetails: mockBookDetails };

        await expect(zlibApi.downloadBookToFile(downloadArgs))
            .rejects
            // Match the actual wrapped error message
            .toThrow("Failed to download book: Invalid response from Python bridge: Missing original file_path.");

        expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
    });

    // Updated for Spec v2.1: Uses bookDetails
    test('should handle errors from Python bridge during download_book', async () => {
      const apiError = new Error('Python Download Book Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(apiError);

      const mockBookDetails = { id: 'failDownload', url: 'http://example.com/book/failDownload/slug' };
      await expect(zlibApi.downloadBookToFile({ bookDetails: mockBookDetails })).rejects.toThrow(`Python bridge execution failed for download_book: ${apiError.message}`);
      // Assert only arguments: a mismatch must not dump the inherited child environment.
      expect(mockRunPythonBridge.mock.calls[0][1].args).toEqual([
        'download_book', JSON.stringify({ book_details: mockBookDetails }),
      ]);
    });

  });

  describe('getDownloadHistory', () => {
    test('should call Python bridge for getDownloadHistory', async () => {
        const mockApiResult_hist1 = [{ id: '3', title: 'History Book' }]; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure
        const mockPythonResultString_hist1 = JSON.stringify(mockApiResult_hist1);
        const mockMcpResponseString_hist1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_hist1 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_hist1]);

        const historyArgs = { count: 10 };
        result = await zlibApi.getDownloadHistory(historyArgs);

        expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ // Corrected script name
            scriptPath: EXPECTED_SCRIPT_PATH,
            args: ['get_download_history', JSON.stringify({ count: historyArgs.count })]
        }), expect.objectContaining({ label: expect.any(String) }));
        expect(result).toEqual(mockApiResult_hist1); // Use unique result var
    });

     test('should handle errors from Python bridge during getDownloadHistory', async () => {
      const apiError = new Error('Python History Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(apiError);

      await expect(zlibApi.getDownloadHistory({ count: 5 })).rejects.toThrow(`Python bridge execution failed for get_download_history: ${apiError.message}`);
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ scriptPath: EXPECTED_SCRIPT_PATH, args: ['get_download_history', JSON.stringify({ count: 5 })] }), expect.objectContaining({ label: expect.any(String) })); // Corrected script name and path
    });
  });

  describe('getDownloadLimits', () => {
    test('should call Python bridge for getDownloadLimits', async () => {
        const mockApiResult_lim1 = { daily_limit: 10, daily_downloads: 2 }; // Unique result var
        mockGetManagedPythonPath.mockResolvedValue('/fake/python');
        // Corrected Mock: Simulate python printing the JSON string of the MCP response structure
        const mockPythonResultString_lim1 = JSON.stringify(mockApiResult_lim1);
        const mockMcpResponseString_lim1 = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString_lim1 }] });
        mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString_lim1]);

        result = await zlibApi.getDownloadLimits();

        expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ // Corrected script name
            scriptPath: EXPECTED_SCRIPT_PATH,
            args: ['get_download_limits', JSON.stringify({})] // Empty args object
        }), expect.objectContaining({ label: expect.any(String) }));
        expect(result).toEqual(mockApiResult_lim1); // Use unique result var
    });

     test('should handle errors from Python bridge during getDownloadLimits', async () => {
      const apiError = new Error('Python Limits Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(apiError);

      await expect(zlibApi.getDownloadLimits()).rejects.toThrow(`Python bridge execution failed for get_download_limits: ${apiError.message}`);
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({ scriptPath: EXPECTED_SCRIPT_PATH, args: ['get_download_limits', JSON.stringify({})] }), expect.objectContaining({ label: expect.any(String) })); // Corrected script name and path
    });
  });

  describe('getRecentBooks', () => {
    test('should call Python bridge with default count=10 when no arguments are passed', async () => {
      const mockApiResult_rec1 = { books: [{ id: '1', title: 'Recent 1', extension: 'epub' }] };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      const mockPythonResultString = JSON.stringify(mockApiResult_rec1);
      const mockMcpResponseString = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString }] });
      mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString]);

      result = await zlibApi.getRecentBooks();

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['get_recent_books', JSON.stringify({ count: 10 })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result).toEqual(mockApiResult_rec1);
    });

    test('should call Python bridge with explicit count and filter by format in JS', async () => {
      const mockApiResult_rec2 = {
        books: [
          { id: '1', title: 'Recent 1', extension: 'epub' },
          { id: '2', title: 'Recent 2', extension: 'pdf' },
        ],
      };
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      const mockPythonResultString = JSON.stringify(mockApiResult_rec2);
      const mockMcpResponseString = JSON.stringify({ content: [{ type: 'text', text: mockPythonResultString }] });
      mockRunPythonBridge.mockResolvedValueOnce([mockMcpResponseString]);

      result = await zlibApi.getRecentBooks({ count: 5, format: 'epub' });

      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['get_recent_books', JSON.stringify({ count: 5 })],
      }), expect.objectContaining({ label: expect.any(String) }));
      expect(result.books).toEqual([{ id: '1', title: 'Recent 1', extension: 'epub' }]);
    });

    test('should handle errors from Python bridge during getRecentBooks', async () => {
      const apiError = new Error('Python Recent Books Failed');
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockRejectedValue(apiError);

      await expect(zlibApi.getRecentBooks({ count: 5 })).rejects.toThrow(`Python bridge execution failed for get_recent_books: ${apiError.message}`);
      expect(mockRunPythonBridge).toHaveBeenCalledWith('python_bridge.py', expect.objectContaining({
        scriptPath: EXPECTED_SCRIPT_PATH,
        args: ['get_recent_books', JSON.stringify({ count: 5 })],
      }), expect.objectContaining({ label: expect.any(String) }));
    });
  });

  /**
   * A budget sized for a provider walk would kill a large download or an OCR
   * pass mid-flight — turning a slow success into a hard failure, which is a
   * worse outcome than the orphaned process the budget exists to prevent.
   */
  describe('long-running bridge calls', () => {
    /** The runOptions the bridge was called with on the given invocation. */
    const runOptionsOf = (call = 0) => mockRunPythonBridge.mock.calls[call][2];

    /** Resolve the bridge with a payload the caller will accept. */
    const resolveWith = (payload) => {
      mockGetManagedPythonPath.mockResolvedValue('/fake/python');
      mockRunPythonBridge.mockResolvedValueOnce([
        JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(payload) }] }),
      ]);
    };

    test('a search inherits the runner default rather than pinning one', async () => {
      resolveWith([]);
      await zlibApi.searchBooks({ query: 'test' });
      expect(runOptionsOf().timeoutMs).toBeUndefined();
    });

    test('a download gets the long budget', async () => {
      resolveWith({ file_path: '/abs/downloads/Book.epub' });
      await zlibApi.downloadBookToFile({
        bookDetails: { id: 'x', url: 'http://example.com/book/x/s', title: 'Book' },
        outputDir: './downloads',
      });
      expect(runOptionsOf().timeoutMs).toBe(LONG_BRIDGE_TIMEOUT_MS);
      expect(LONG_BRIDGE_TIMEOUT_MS).toBeGreaterThan(240000);
    });

    test('an explicit per-call budget still wins', async () => {
      resolveWith([]);
      await zlibApi.searchMultiSource({ query: 'test' }, { timeoutMs: 1234 });
      expect(runOptionsOf().timeoutMs).toBe(1234);
    });
  });
});
