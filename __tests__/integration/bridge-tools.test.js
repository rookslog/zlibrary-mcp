/**
 * Integration tests for all 11 MCP tools via the Python bridge.
 *
 * Modes:
 *   - Recorded (default): Uses static JSON fixtures, no network needed.
 *   - Live (TEST_LIVE=true): Calls real Python bridge via PythonShell.
 *
 * Run:
 *   npm run test:integration          # recorded mode
 *   npm run test:integration:live     # live mode (requires credentials)
 */
import { jest, describe, beforeEach, test, expect, afterAll } from '@jest/globals';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const IS_LIVE = process.env.TEST_LIVE === 'true';

// Tool-to-bridge-function mapping with minimal arguments
const TOOL_BRIDGE_MAP = {
  'search_books':             { fn: 'search',                    minArgs: { query: 'test', count: 1 }, fixture: 'search.json' },
  'full_text_search':         { fn: 'full_text_search',          minArgs: { query: 'test', count: 1 }, fixture: 'full_text_search.json' },
  'get_download_history':     { fn: 'get_download_history',      minArgs: { count: 1 },                fixture: 'get_download_history.json' },
  'get_download_limits':      { fn: 'get_download_limits',       minArgs: {},                          fixture: 'get_download_limits.json' },
  'download_book_to_file':    { fn: 'download_book',             minArgs: { book_details: { url: '/book/1/abc', id: '1' }, output_dir: '/tmp', process_for_rag: true }, fixture: 'download_book.json' },
  'process_document_for_rag': { fn: 'process_document',          minArgs: { file_path_str: '/tmp/test.pdf' },                                   fixture: 'process_document.json' },
  'get_book_metadata':        { fn: 'get_book_metadata_complete', minArgs: { book_id: '1', book_hash: 'abc' },                                  fixture: 'get_book_metadata_complete.json' },
  'search_by_term':           { fn: 'search_by_term_bridge',     minArgs: { term: 'test' },            fixture: 'search_by_term_bridge.json' },
  'search_by_author':         { fn: 'search_by_author_bridge',   minArgs: { author: 'test' },          fixture: 'search_by_author_bridge.json' },
  'fetch_booklist':           { fn: 'fetch_booklist_bridge',     minArgs: { booklist_id: '1', booklist_hash: 'abc', topic: 'test' }, fixture: 'fetch_booklist_bridge.json' },
  'search_advanced':          { fn: 'search_advanced',           minArgs: { query: 'test' },           fixture: 'search_advanced.json' },
};

const FIXTURES_DIR = path.join(__dirname, 'fixtures', 'recorded-responses');

function loadFixture(filename) {
  const raw = readFileSync(path.join(FIXTURES_DIR, filename), 'utf-8');
  return JSON.parse(raw);
}

// Results tracking for summary table
const results = [];

describe('Bridge Integration Tests', () => {
  const toolNames = Object.keys(TOOL_BRIDGE_MAP);

  if (!IS_LIVE) {
    // ── Recorded mode ──
    // Mock the Python runner so callPythonFunction returns fixture data.
    let mockRunPythonBridge;
    let mockGetManagedPythonPath;

    beforeEach(async () => {
      jest.resetModules();
      jest.clearAllMocks();

      mockGetManagedPythonPath = jest.fn().mockResolvedValue('/usr/bin/python3');
      mockRunPythonBridge = jest.fn();

      jest.unstable_mockModule('../../dist/lib/venv-manager.js', () => ({
        getManagedPythonPath: mockGetManagedPythonPath,
      }));

      jest.unstable_mockModule('../../dist/lib/python-runner.js', () => ({
        runPythonBridge: mockRunPythonBridge,
        LONG_BRIDGE_TIMEOUT_MS: 2400000,
      }));
    });

    describe.each(toolNames)('Tool: %s (recorded)', (toolName) => {
      test(`returns valid response shape`, async () => {
        const spec = TOOL_BRIDGE_MAP[toolName];
        const fixture = loadFixture(spec.fixture);

        // The fixture is a full MCP response object. The runner mock returns
        // its stdout lines without starting a Python process.
        mockRunPythonBridge.mockResolvedValue([JSON.stringify(fixture)]);

        // Dynamic import after mocking
        const zlibApi = await import('../../dist/lib/zlibrary-api.js');

        // Find the wrapper function that calls callPythonFunction(spec.fn, ...)
        // We call the appropriate exported function based on the tool name
        let result;
        try {
          switch (toolName) {
            case 'search_books':
              result = await zlibApi.searchBooks({ query: 'test', count: 1 });
              break;
            case 'full_text_search':
              result = await zlibApi.fullTextSearch({ query: 'test', count: 1 });
              break;
            case 'get_download_history':
              result = await zlibApi.getDownloadHistory({ count: 1 });
              break;
            case 'get_download_limits':
              result = await zlibApi.getDownloadLimits();
              break;
            case 'download_book_to_file':
              result = await zlibApi.downloadBookToFile({
                bookDetails: { url: '/book/1/abc', id: '1' },
                outputDir: '/tmp',
                process_for_rag: true,
              });
              break;
            case 'process_document_for_rag':
              result = await zlibApi.processDocumentForRag({ filePath: '/tmp/test.pdf' });
              break;
            case 'get_book_metadata':
              result = await zlibApi.getBookMetadata('1', 'abc');
              break;
            case 'search_by_term':
              result = await zlibApi.searchByTerm({ term: 'test' });
              break;
            case 'search_by_author':
              result = await zlibApi.searchByAuthor({ author: 'test' });
              break;
            case 'fetch_booklist':
              result = await zlibApi.fetchBooklist({ booklistId: '1', booklistHash: 'abc', topic: 'test' });
              break;
            case 'search_advanced':
              result = await zlibApi.searchAdvanced({ query: 'test' });
              break;
          }

          // Validate response is a non-null object
          expect(result).toBeDefined();
          expect(typeof result).toBe('object');
          expect(result).not.toBeNull();

          if (toolName === 'download_book_to_file') {
            expect(result).toEqual(expect.objectContaining({
              processed_file_path: expect.any(String),
              metadata_file_path: expect.any(String),
              output_files: expect.objectContaining({
                body: expect.any(String),
                metadata: expect.any(String),
              }),
            }));
          }

          if (toolName === 'process_document_for_rag') {
            expect(result).toEqual(expect.objectContaining({
              processed_file_path: expect.any(String),
              metadata_file_path: expect.any(String),
              output_files: expect.objectContaining({
                body: expect.any(String),
                metadata: expect.any(String),
              }),
            }));
          }

          expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
          const [, options] = mockRunPythonBridge.mock.calls[0];
          expect(options.args[0]).toBe(spec.fn);
          expect(JSON.parse(options.args[1])).toMatchObject(spec.minArgs);

          results.push({ tool: toolName, mode: 'recorded', status: 'PASS', error: null });
        } catch (err) {
          results.push({ tool: toolName, mode: 'recorded', status: 'FAIL', error: err.message });
          throw err;
        }
      }, 10000);
    });
  } else {
    // ── Live mode ──
    describe.each(toolNames)('Tool: %s (live)', (toolName) => {
      test(`calls real Python bridge and returns valid JSON`, async () => {
        const spec = TOOL_BRIDGE_MAP[toolName];

        // Import the real module (no mocks)
        const zlibApi = await import('../../dist/lib/zlibrary-api.js');

        let result;
        try {
          switch (toolName) {
            case 'search_books':
              result = await zlibApi.searchBooks({ query: 'philosophy', count: 1 });
              break;
            case 'full_text_search':
              result = await zlibApi.fullTextSearch({ query: 'epistemology', count: 1 });
              break;
            case 'get_download_history':
              result = await zlibApi.getDownloadHistory({ count: 1 });
              break;
            case 'get_download_limits':
              result = await zlibApi.getDownloadLimits();
              break;
            case 'download_book_to_file':
              // Skip in live mode — requires actual book details and network
              console.log('  [SKIP] download_book_to_file requires real book details');
              results.push({ tool: toolName, mode: 'live', status: 'SKIP', error: 'requires real book details' });
              return;
            case 'process_document_for_rag':
              // Skip in live mode — requires actual file
              console.log('  [SKIP] process_document_for_rag requires real file');
              results.push({ tool: toolName, mode: 'live', status: 'SKIP', error: 'requires real file' });
              return;
            case 'get_book_metadata':
              // Skip — requires valid book_id/hash
              console.log('  [SKIP] get_book_metadata requires valid book_id');
              results.push({ tool: toolName, mode: 'live', status: 'SKIP', error: 'requires valid book_id' });
              return;
            case 'search_by_term':
              result = await zlibApi.searchByTerm({ term: 'philosophy' });
              break;
            case 'search_by_author':
              result = await zlibApi.searchByAuthor({ author: 'Kant' });
              break;
            case 'fetch_booklist':
              // Skip — requires valid booklist URL
              console.log('  [SKIP] fetch_booklist requires valid booklist');
              results.push({ tool: toolName, mode: 'live', status: 'SKIP', error: 'requires valid booklist' });
              return;
            case 'search_advanced':
              result = await zlibApi.searchAdvanced({ query: 'philosophy' });
              break;
          }

          expect(result).toBeDefined();
          expect(typeof result).toBe('object');

          results.push({ tool: toolName, mode: 'live', status: 'PASS', error: null });
        } catch (err) {
          results.push({ tool: toolName, mode: 'live', status: 'FAIL', error: err.message });
          throw err;
        }
      }, 60000);
    });
  }

  afterAll(() => {
    // Print summary table
    const mode = IS_LIVE ? 'LIVE' : 'RECORDED';
    console.log(`\n${'='.repeat(70)}`);
    console.log(`  Bridge Integration Test Summary (${mode} mode)`);
    console.log(`${'='.repeat(70)}`);
    console.log(`  ${'Tool'.padEnd(30)} ${'Status'.padEnd(10)} Error`);
    console.log(`  ${'-'.repeat(28)} ${'-'.repeat(8)} ${'-'.repeat(30)}`);
    for (const r of results) {
      console.log(`  ${r.tool.padEnd(30)} ${r.status.padEnd(10)} ${r.error || ''}`);
    }
    const passed = results.filter(r => r.status === 'PASS').length;
    const failed = results.filter(r => r.status === 'FAIL').length;
    const skipped = results.filter(r => r.status === 'SKIP').length;
    console.log(`\n  Total: ${results.length} | Pass: ${passed} | Fail: ${failed} | Skip: ${skipped}`);
    console.log(`${'='.repeat(70)}\n`);
  });
});
