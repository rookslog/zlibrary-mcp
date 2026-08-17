import { jest, describe, beforeEach, test, expect } from '@jest/globals';

// ============================================================================
// Tests for uncovered handler paths in src/index.ts
// Covers: processDocumentForRag, getBookMetadata, searchByTerm, searchByAuthor,
//         fetchBooklist, searchAdvanced, searchMultiSource handlers
//         + wrapResult helper (error and success paths)
//         + toolRegistry entries for newer tools
// ============================================================================

describe('Tool Handlers - Extended Coverage', () => {

  beforeEach(() => {
    console.log = jest.fn();
    console.error = jest.fn();
  });

  // Helper: creates a mock zlibrary-api module and imports fresh handlers
  async function setupWithMocks(overrides = {}) {
    jest.resetModules();
    jest.clearAllMocks();

    const defaults = {
      searchBooks: jest.fn(),
      fullTextSearch: jest.fn(),
      getDownloadHistory: jest.fn(),
      getDownloadLimits: jest.fn(),
      downloadBookToFile: jest.fn(),
      processDocumentForRag: jest.fn(),
      getBookMetadata: jest.fn(),
      searchByTerm: jest.fn(),
      searchByAuthor: jest.fn(),
      fetchBooklist: jest.fn(),
      searchAdvanced: jest.fn(),
      searchMultiSource: jest.fn(),
      getRecentBooks: jest.fn(),
    };

    const mocks = { ...defaults, ...overrides };
    const registeredTools = new Map();
    const mockServer = {
      connect: jest.fn().mockResolvedValue(undefined),
      tool: jest.fn((...args) => registeredTools.set(args[0], args[4])),
      close: jest.fn(),
    };

    jest.unstable_mockModule('../lib/zlibrary-api.js', () => mocks);
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/mcp.js', () => ({
      McpServer: jest.fn(() => mockServer),
    }));
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
      StdioServerTransport: jest.fn(() => ({})),
    }));
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      ensureVenvReady: jest.fn().mockResolvedValue(undefined),
      getManagedPythonPath: jest.fn().mockResolvedValue('/fake/python'),
    }));

    const { toolRegistry, handlers, start } = await import('../dist/index.js');
    return { toolRegistry, handlers, start, registeredTools, mocks };
  }

  describe('processDocumentForRag handler', () => {
    test('should call zlibApi.processDocumentForRag with mapped args on success', async () => {
      const mockProcessDoc = jest.fn().mockResolvedValue({
        processed_file_path: '/output/doc.txt',
        metadata_file_path: '/output/doc.metadata.json',
        content_types_produced: ['body'],
        output_files: {
          body: '/output/doc.txt',
          metadata: '/output/doc.metadata.json',
        },
      });
      const { toolRegistry } = await setupWithMocks({ processDocumentForRag: mockProcessDoc });

      const handler = toolRegistry.process_document_for_rag.handler;
      const args = { file_path: '/input/doc.epub', output_format: 'markdown' };
      const validatedArgs = toolRegistry.process_document_for_rag.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockProcessDoc).toHaveBeenCalledWith(
        {
          filePath: '/input/doc.epub',
          outputFormat: 'markdown',
        },
        { signal: undefined },
      );
      expect(response).toEqual({
        processed_file_path: '/output/doc.txt',
        metadata_file_path: '/output/doc.metadata.json',
        content_types_produced: ['body'],
        output_files: {
          body: '/output/doc.txt',
          metadata: '/output/doc.metadata.json',
        },
      });
    });

    test('should return error object on failure', async () => {
      const mockProcessDoc = jest.fn().mockRejectedValue(new Error('Process failed'));
      const { toolRegistry } = await setupWithMocks({ processDocumentForRag: mockProcessDoc });

      const handler = toolRegistry.process_document_for_rag.handler;
      const args = { file_path: '/input/doc.epub' };
      const validatedArgs = toolRegistry.process_document_for_rag.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Process failed' } });
    });
  });

  describe('downloadBookToFile handler', () => {
    test('preserves structured provider details on failure', async () => {
      // Mutation caught: returning a message-only envelope discards the
      // provider, host, and reason needed to choose a useful recovery.
      const details = {
        operation: 'download',
        failures: [
          { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
        ],
      };
      const error = Object.assign(new Error('Download sources failed'), {
        context: { details },
      });
      const mockDownload = jest.fn().mockRejectedValue(error);
      const { toolRegistry } = await setupWithMocks({ downloadBookToFile: mockDownload });
      const args = toolRegistry.download_book_to_file.schema.parse({
        bookDetails: {
          md5: '0123456789abcdef0123456789abcdef',
          title: 'Test Book',
          source: 'libgen',
        },
      });

      await expect(toolRegistry.download_book_to_file.handler(args)).resolves.toEqual({
        error: { message: 'Download sources failed', details },
      });
    });

  });

  describe('getBookMetadata handler', () => {
    test('should call zlibApi.getBookMetadata with correct args on success', async () => {
      const mockGetMeta = jest.fn().mockResolvedValue({
        title: 'Test Book', author: 'Author', terms: ['philosophy'],
      });
      const { toolRegistry } = await setupWithMocks({ getBookMetadata: mockGetMeta });

      const handler = toolRegistry.get_book_metadata.handler;
      const args = { bookId: '123', bookHash: 'abc', include: ['terms'] };
      const validatedArgs = toolRegistry.get_book_metadata.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockGetMeta).toHaveBeenCalledWith('123', 'abc', ['terms'], { signal: undefined });
      expect(response).toEqual({
        title: 'Test Book', author: 'Author', terms: ['philosophy'],
      });
    });

    test('should return error object on failure', async () => {
      const mockGetMeta = jest.fn().mockRejectedValue(new Error('Metadata failed'));
      const { toolRegistry } = await setupWithMocks({ getBookMetadata: mockGetMeta });

      const handler = toolRegistry.get_book_metadata.handler;
      const args = { bookId: '123', bookHash: 'abc' };
      const validatedArgs = toolRegistry.get_book_metadata.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Metadata failed' } });
    });
  });

  describe('searchByTerm handler', () => {
    test('should call zlibApi.searchByTerm with mapped args on success', async () => {
      const mockSearchTerm = jest.fn().mockResolvedValue([{ title: 'Phenomenology Book' }]);
      const { toolRegistry } = await setupWithMocks({ searchByTerm: mockSearchTerm });

      const handler = toolRegistry.search_by_term.handler;
      const args = { term: 'phenomenology', yearFrom: 1900, yearTo: 2020, languages: ['english'], extensions: ['pdf'], count: 15 };
      const validatedArgs = toolRegistry.search_by_term.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockSearchTerm).toHaveBeenCalledWith(
        {
          term: 'phenomenology',
          yearFrom: 1900,
          yearTo: 2020,
          languages: ['english'],
          extensions: ['pdf'],
          limit: 15,
        },
        { signal: undefined },
      );
      expect(response).toEqual([{ title: 'Phenomenology Book' }]);
    });

    test('should return error object on failure', async () => {
      const mockSearchTerm = jest.fn().mockRejectedValue(new Error('Term search failed'));
      const { toolRegistry } = await setupWithMocks({ searchByTerm: mockSearchTerm });

      const handler = toolRegistry.search_by_term.handler;
      const args = { term: 'dialectic' };
      const validatedArgs = toolRegistry.search_by_term.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Term search failed' } });
    });
  });

  describe('searchByAuthor handler', () => {
    test('should call zlibApi.searchByAuthor with mapped args on success', async () => {
      const mockSearchAuthor = jest.fn().mockResolvedValue([{ title: 'Hegel Book' }]);
      const { toolRegistry } = await setupWithMocks({ searchByAuthor: mockSearchAuthor });

      const handler = toolRegistry.search_by_author.handler;
      const args = { author: 'Hegel', exact: true, yearFrom: 1800, yearTo: 1900, languages: ['german'], extensions: ['epub'], count: 10 };
      const validatedArgs = toolRegistry.search_by_author.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockSearchAuthor).toHaveBeenCalledWith(
        {
          author: 'Hegel',
          exact: true,
          yearFrom: 1800,
          yearTo: 1900,
          languages: ['german'],
          extensions: ['epub'],
          limit: 10,
        },
        { signal: undefined },
      );
      expect(response).toEqual([{ title: 'Hegel Book' }]);
    });

    test('should return error object on failure', async () => {
      const mockSearchAuthor = jest.fn().mockRejectedValue(new Error('Author search failed'));
      const { toolRegistry } = await setupWithMocks({ searchByAuthor: mockSearchAuthor });

      const handler = toolRegistry.search_by_author.handler;
      const args = { author: 'Kant' };
      const validatedArgs = toolRegistry.search_by_author.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Author search failed' } });
    });
  });

  describe('fetchBooklist handler', () => {
    test('should call zlibApi.fetchBooklist with mapped args on success', async () => {
      const mockFetchList = jest.fn().mockResolvedValue({ books: [{ title: 'Listed Book' }], total: 1 });
      const { toolRegistry } = await setupWithMocks({ fetchBooklist: mockFetchList });

      const handler = toolRegistry.fetch_booklist.handler;
      const args = { booklistId: 'bl1', booklistHash: 'hash1', topic: 'philosophy', page: 2 };
      const validatedArgs = toolRegistry.fetch_booklist.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockFetchList).toHaveBeenCalledWith(
        {
          booklistId: 'bl1',
          booklistHash: 'hash1',
          topic: 'philosophy',
          page: 2,
        },
        { signal: undefined },
      );
      expect(response).toEqual({ books: [{ title: 'Listed Book' }], total: 1 });
    });

    test('should return error object on failure', async () => {
      const mockFetchList = jest.fn().mockRejectedValue(new Error('Booklist failed'));
      const { toolRegistry } = await setupWithMocks({ fetchBooklist: mockFetchList });

      const handler = toolRegistry.fetch_booklist.handler;
      const args = { booklistId: 'bl1', booklistHash: 'hash1', topic: 'test' };
      const validatedArgs = toolRegistry.fetch_booklist.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Booklist failed' } });
    });
  });

  describe('searchAdvanced handler', () => {
    test('should call zlibApi.searchAdvanced with mapped args on success', async () => {
      const mockAdvanced = jest.fn().mockResolvedValue({
        exact_matches: [{ title: 'Exact' }],
        fuzzy_matches: [{ title: 'Fuzzy' }],
      });
      const { toolRegistry } = await setupWithMocks({ searchAdvanced: mockAdvanced });

      const handler = toolRegistry.search_advanced.handler;
      const args = { query: 'being and time', exact: true, yearFrom: 1927, yearTo: 1927, count: 5 };
      const validatedArgs = toolRegistry.search_advanced.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(mockAdvanced).toHaveBeenCalledWith(
        {
          query: 'being and time',
          exact: true,
          yearFrom: 1927,
          yearTo: 1927,
          count: 5,
        },
        { signal: undefined },
      );
      expect(response).toEqual({
        exact_matches: [{ title: 'Exact' }],
        fuzzy_matches: [{ title: 'Fuzzy' }],
      });
    });

    test('should return error object on failure', async () => {
      const mockAdvanced = jest.fn().mockRejectedValue(new Error('Advanced search failed'));
      const { toolRegistry } = await setupWithMocks({ searchAdvanced: mockAdvanced });

      const handler = toolRegistry.search_advanced.handler;
      const args = { query: 'test' };
      const validatedArgs = toolRegistry.search_advanced.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Advanced search failed' } });
    });
  });

  describe('searchMultiSource handler', () => {
    test('should call zlibApi.searchMultiSource with mapped args on success', async () => {
      const mockMulti = jest.fn().mockResolvedValue([{ title: 'Multi Book', source: 'libgen' }]);
      const { toolRegistry } = await setupWithMocks({ searchMultiSource: mockMulti });

      const handler = toolRegistry.search_multi_source.handler;
      const args = { query: 'philosophy', source: 'libgen', count: 20 };
      const validatedArgs = toolRegistry.search_multi_source.schema.parse(args);
      const response = await handler(validatedArgs);

      // Second argument carries the MCP request's abort signal, so a
      // cancelled call can kill the bridge subprocess rather than orphan it.
      expect(mockMulti).toHaveBeenCalledWith(
        {
          query: 'philosophy',
          source: 'libgen',
          count: 20,
        },
        { signal: undefined },
      );
      expect(response).toEqual([{ title: 'Multi Book', source: 'libgen' }]);
    });

    test('should return error object on failure', async () => {
      const mockMulti = jest.fn().mockRejectedValue(new Error('Multi-source failed'));
      const { toolRegistry } = await setupWithMocks({ searchMultiSource: mockMulti });

      const handler = toolRegistry.search_multi_source.handler;
      const args = { query: 'test' };
      const validatedArgs = toolRegistry.search_multi_source.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Multi-source failed' } });
    });

    test('should retain structured provider details on failure', async () => {
      const details = {
        failures: [
          { provider: 'annas', host: 'annas-archive.gl', reason: 'dns_failure' },
          { provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' },
        ],
      };
      const error = Object.assign(new Error('All sources failed'), {
        context: { details },
      });
      const mockMulti = jest.fn().mockRejectedValue(error);
      const { toolRegistry } = await setupWithMocks({ searchMultiSource: mockMulti });

      const handler = toolRegistry.search_multi_source.handler;
      const args = toolRegistry.search_multi_source.schema.parse({ query: 'test' });

      await expect(handler(args)).resolves.toEqual({
        error: { message: 'All sources failed', details },
      });
    });

    test('should preserve provider details in the MCP error response', async () => {
      const details = {
        provider: 'annas',
        host: 'annas-archive.gl',
        reason: 'dns_failure',
      };
      const error = Object.assign(new Error('Anna source failed'), {
        context: { details },
      });
      const mockMulti = jest.fn().mockRejectedValue(error);
      const { start, registeredTools } = await setupWithMocks({ searchMultiSource: mockMulti });
      await start({ testing: true });

      const callback = registeredTools.get('search_multi_source');
      const response = await callback({ query: 'test' }, {});

      expect(response).toEqual({
        content: [
          {
            type: 'text',
            text: 'Error from tool "search_multi_source": Anna source failed',
          },
        ],
        structuredContent: { error: { message: 'Anna source failed', details } },
        isError: true,
      });
    });
  });

  describe('toolRegistry entries for newer tools', () => {
    test('search_by_term registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.search_by_term;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('search_by_author registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.search_by_author;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('fetch_booklist registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.fetch_booklist;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('search_advanced registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.search_advanced;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('search_multi_source registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.search_multi_source;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('get_book_metadata registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.get_book_metadata;
      expect(entry.description).toBeDefined();
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });

    test('process_document_for_rag registry entry should have description, schema, and handler', async () => {
      const { toolRegistry } = await setupWithMocks();
      const entry = toolRegistry.process_document_for_rag;
      expect(entry.description).toBeDefined();
      expect(entry.description).toContain('metadata');
      expect(entry.schema).toBeDefined();
      expect(typeof entry.handler).toBe('function');
    });
  });

  describe('Handler error message fallbacks', () => {
    test('processDocumentForRag handler should use fallback message when error has no message', async () => {
      const mockProcessDoc = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ processDocumentForRag: mockProcessDoc });

      const handler = toolRegistry.process_document_for_rag.handler;
      const args = { file_path: '/input/doc.epub' };
      const validatedArgs = toolRegistry.process_document_for_rag.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to process document for RAG' } });
    });

    test('getBookMetadata handler should use fallback message when error has no message', async () => {
      const mockGetMeta = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ getBookMetadata: mockGetMeta });

      const handler = toolRegistry.get_book_metadata.handler;
      const args = { bookId: '1', bookHash: 'h' };
      const validatedArgs = toolRegistry.get_book_metadata.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to get book metadata' } });
    });

    test('searchByTerm handler should use fallback message when error has no message', async () => {
      const mockSearchTerm = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ searchByTerm: mockSearchTerm });

      const handler = toolRegistry.search_by_term.handler;
      const args = { term: 'test' };
      const validatedArgs = toolRegistry.search_by_term.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to search by term' } });
    });

    test('searchByAuthor handler should use fallback message when error has no message', async () => {
      const mockSearchAuthor = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ searchByAuthor: mockSearchAuthor });

      const handler = toolRegistry.search_by_author.handler;
      const args = { author: 'test' };
      const validatedArgs = toolRegistry.search_by_author.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to search by author' } });
    });

    test('fetchBooklist handler should use fallback message when error has no message', async () => {
      const mockFetch = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ fetchBooklist: mockFetch });

      const handler = toolRegistry.fetch_booklist.handler;
      const args = { booklistId: 'bl1', booklistHash: 'hash1', topic: 'test' };
      const validatedArgs = toolRegistry.fetch_booklist.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to fetch booklist' } });
    });

    test('searchAdvanced handler should use fallback message when error has no message', async () => {
      const mockAdvanced = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ searchAdvanced: mockAdvanced });

      const handler = toolRegistry.search_advanced.handler;
      const args = { query: 'test' };
      const validatedArgs = toolRegistry.search_advanced.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to perform advanced search' } });
    });

    test('searchMultiSource handler should use fallback message when error has no message', async () => {
      const mockMulti = jest.fn().mockRejectedValue({});
      const { toolRegistry } = await setupWithMocks({ searchMultiSource: mockMulti });

      const handler = toolRegistry.search_multi_source.handler;
      const args = { query: 'test' };
      const validatedArgs = toolRegistry.search_multi_source.schema.parse(args);
      const response = await handler(validatedArgs);

      expect(response).toEqual({ error: { message: 'Failed to search multi-source' } });
    });
  });

  /**
   * Cancellation has to reach the subprocess, not just the promise. Wiring it
   * on one tool and not the rest is how a cancelled download went on running
   * for its full budget with nobody waiting for the result, so this asserts
   * the signal arrives for every tool that spawns the bridge.
   */
  describe('client cancellation reaches every bridge-backed tool', () => {
    const CASES = [
      ['search_books', 'searchBooks', { query: 'x' }, 1],
      ['full_text_search', 'fullTextSearch', { query: 'x' }, 1],
      ['get_download_history', 'getDownloadHistory', {}, 1],
      ['get_download_limits', 'getDownloadLimits', {}, 0],
      ['download_book_to_file', 'downloadBookToFile', { bookDetails: { id: '1' } }, 1],
      ['process_document_for_rag', 'processDocumentForRag', { file_path: '/a.epub' }, 1],
      ['get_book_metadata', 'getBookMetadata', { bookId: '1', bookHash: 'h' }, 3],
      ['search_by_term', 'searchByTerm', { term: 't' }, 1],
      ['search_by_author', 'searchByAuthor', { author: 'a' }, 1],
      ['fetch_booklist', 'fetchBooklist', { booklistId: '1', booklistHash: 'h', topic: 't' }, 1],
      ['search_advanced', 'searchAdvanced', { query: 'x' }, 1],
      ['search_multi_source', 'searchMultiSource', { query: 'x' }, 1],
    ];

    test.each(CASES)(
      '%s forwards the abort signal to the API layer',
      async (toolName, apiName, rawArgs, optionsIndex) => {
        const spy = jest.fn().mockResolvedValue({});
        const { toolRegistry } = await setupWithMocks({ [apiName]: spy });

        const entry = toolRegistry[toolName];
        const validated = entry.schema ? entry.schema.parse(rawArgs) : rawArgs;
        const controller = new AbortController();

        await entry.handler(validated, { signal: controller.signal });

        expect(spy).toHaveBeenCalled();
        expect(spy.mock.calls[0][optionsIndex]).toEqual({ signal: controller.signal });
      },
    );
  });
});
