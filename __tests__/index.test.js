import { jest, describe, beforeEach, test, expect, afterAll } from '@jest/globals';

import { z } from 'zod'; // Import zod at top level
// Import compiled JS at top level - toolRegistry needed for schema tests
import { start, toolRegistry } from '../dist/index.js';

// =================================
// == TEST SUITES ==================
// =================================

describe('MCP Server', () => {
  let mockServerInstance; // Declare instance variable for cleanup

  beforeEach(() => {
    mockServerInstance = null;
    console.error = jest.fn();
    console.log = jest.fn();
  });

  test('should initialize MCP server with metadata', async () => {
    jest.resetModules();
    jest.clearAllMocks();

    let capturedServerArgs = null;
    const mockTransport = { send: jest.fn() };
    const mockMcpServer = {
      connect: jest.fn().mockResolvedValue(undefined),
      tool: jest.fn(), // McpServer uses .tool() for registration
      close: jest.fn(),
    };
    mockServerInstance = mockMcpServer;

    // Mock SDK modules using McpServer from server/mcp.js
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/mcp.js', () => ({
      McpServer: jest.fn().mockImplementation((serverInfo) => {
        capturedServerArgs = serverInfo;
        return mockMcpServer;
      }),
    }));
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
      StdioServerTransport: jest.fn().mockImplementation(() => mockTransport),
    }));
    // Mock venv-manager INSIDE the test
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      ensureVenvReady: jest.fn().mockResolvedValue(undefined),
      getManagedPythonPath: jest.fn().mockReturnValue('/fake/python'),
    }));

    // Dynamically import modules AFTER mocks are set
    const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp.js');
    const { start } = await import('../dist/index.js');

    await start({ testing: true });

    // Assert
    expect(McpServer).toHaveBeenCalled();
    // McpServer takes a single arg: { name, version }
    expect(capturedServerArgs).toEqual({
      name: "zlibrary-mcp",
      version: expect.any(String),
    });
    // McpServer uses .tool() for registration — should be called for each tool
    expect(mockMcpServer.tool).toHaveBeenCalled();
    expect(mockMcpServer.tool.mock.calls.length).toBeGreaterThanOrEqual(11);
    expect(mockMcpServer.connect).toHaveBeenCalledWith(mockTransport);
    // Startup banner goes to stderr: stdout is reserved for JSON-RPC traffic.
    expect(console.log).not.toHaveBeenCalled();
    expect(console.error).toHaveBeenCalledWith(
      expect.stringContaining('[info]'),
      'Z-Library MCP server (ESM/TS) is running via Stdio...'
    );
  });

  test('tools/list: McpServer should register all expected tools', async () => {
    jest.resetModules();
    jest.clearAllMocks();

    const mockTransport = { send: jest.fn() };
    const registeredToolNames = [];
    const mockMcpServer = {
      connect: jest.fn().mockResolvedValue(undefined),
      tool: jest.fn((...args) => {
        // server.tool(name, description, shape, annotations, handler)
        registeredToolNames.push(args[0]);
      }),
      close: jest.fn(),
    };
    mockServerInstance = mockMcpServer;

    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/mcp.js', () => ({
      McpServer: jest.fn().mockImplementation(() => mockMcpServer),
    }));
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
      StdioServerTransport: jest.fn().mockImplementation(() => mockTransport),
    }));
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      ensureVenvReady: jest.fn().mockResolvedValue(undefined),
      getManagedPythonPath: jest.fn().mockReturnValue('/fake/python'),
    }));

    const { start } = await import('../dist/index.js');
    await start({ testing: true });

    // Verify all expected tools registered
    expect(registeredToolNames).toContain('search_books');
    expect(registeredToolNames).toContain('download_book_to_file');
    expect(registeredToolNames).toContain('process_document_for_rag');
    expect(registeredToolNames).toContain('full_text_search');
    expect(registeredToolNames).toContain('get_download_history');
    expect(registeredToolNames).toContain('get_download_limits');
    expect(registeredToolNames).toContain('get_recent_books');
    expect(registeredToolNames).toContain('get_book_metadata');
    expect(registeredToolNames).toContain('search_by_term');
    expect(registeredToolNames).toContain('search_by_author');
    expect(registeredToolNames).toContain('fetch_booklist');
    expect(registeredToolNames).toContain('search_advanced');
    expect(registeredToolNames).toContain('search_multi_source');
    // #96 held the count at 13 by extending existing tools. The routing
    // contract adds two reporting surfaces and no fourteenth tool.
    expect(registeredToolNames).toHaveLength(13);
  });

  describe('get_download_limits accepts a per-source narrowing (#107)', () => {
    test('accepts canonical source names', async () => {
      const { toolRegistry } = await import('../dist/index.js');
      const parsed = toolRegistry.get_download_limits.schema.parse({
        sources: ['libgen', 'annas_archive'],
      });
      expect(parsed.sources).toEqual(['libgen', 'annas_archive']);
    });

    test('omitting sources means every source', async () => {
      const { toolRegistry } = await import('../dist/index.js');
      expect(toolRegistry.get_download_limits.schema.parse({}).sources).toBeUndefined();
    });

    test('rejects a source this server does not read from', async () => {
      const { toolRegistry } = await import('../dist/index.js');
      expect(() =>
        toolRegistry.get_download_limits.schema.parse({ sources: ['gutenberg'] }),
      ).toThrow();
    });

    test('rejects an explicitly empty narrowing rather than widening it', async () => {
      const { toolRegistry } = await import('../dist/index.js');
      expect(() =>
        toolRegistry.get_download_limits.schema.parse({ sources: [] }),
      ).toThrow();
    });
  });

  test('should handle McpServer instantiation exceptions gracefully', async () => {
    jest.resetModules();
    jest.clearAllMocks();

    const mockTransport = { send: jest.fn() };

    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/mcp.js', () => ({
      McpServer: jest.fn().mockImplementationOnce(() => { throw new Error('Server instantiation error'); }),
    }));
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
      StdioServerTransport: jest.fn().mockImplementation(() => mockTransport),
    }));
    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      ensureVenvReady: jest.fn().mockResolvedValue(undefined),
      getManagedPythonPath: jest.fn().mockReturnValue('/fake/python'),
    }));

    const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp.js');
    const { start } = await import('../dist/index.js');
    await start({ testing: true });

    expect(McpServer).toHaveBeenCalled();
    expect(console.error).toHaveBeenCalledWith(
      'Failed to start MCP server:',
      expect.objectContaining({ message: 'Server instantiation error' })
    );
  });

  afterAll(async () => {
    if (mockServerInstance) {
      if (typeof mockServerInstance.disconnect === 'function') {
        await mockServerInstance.disconnect();
      } else if (typeof mockServerInstance.close === 'function') {
        await mockServerInstance.close();
      }
    }
  });
}); // End MCP Server describe


describe('Tool Handlers (Direct)', () => {

 beforeEach(() => {
   console.log = jest.fn();
   console.error = jest.fn();
 });

    // Import Zod for schema testing
    // z is imported at the top level

    describe('Tool Schemas', () => {
      test('download_book_to_file schema should reflect v2.1 changes', () => {
        // toolRegistry is imported at the top level
        const schema = toolRegistry.download_book_to_file.schema;

        // Check input schema properties (v2.1)
        expect(schema.shape.bookDetails).toBeDefined();
        expect(schema.shape.id).toBeUndefined(); // Ensure old 'id' is removed
        expect(schema.shape.format).toBeUndefined(); // Ensure old 'format' is removed
        expect(schema.shape.outputDir).toBeDefined();
        expect(schema.shape.process_for_rag).toBeDefined();
        expect(schema.shape.process_for_rag).toBeInstanceOf(z.ZodOptional);
        expect(schema.shape.process_for_rag.unwrap()).toBeInstanceOf(z.ZodBoolean);
        expect(schema.shape.processed_output_format).toBeDefined();
      });

      test('process_document_for_rag schema should define input and output (v2.1)', () => {
        const schema = toolRegistry.process_document_for_rag.schema;

        // Check input schema properties
        expect(schema.shape.file_path).toBeDefined();
        expect(schema.shape.file_path).toBeInstanceOf(z.ZodString);
        expect(schema.shape.output_format).toBeDefined();
        expect(schema.shape.output_format).toBeInstanceOf(z.ZodOptional);
      });
    }); // End Tool Schemas describe


 describe('Handler Logic', () => {

   test('search_books handler should call zlibApi.searchBooks and handle success/error', async () => {
     jest.resetModules();
     jest.clearAllMocks();
     const mockSearchBooks = jest.fn();
     jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({
       searchBooks: mockSearchBooks,
       downloadBookToFile: jest.fn(),
       getDownloadInfo: jest.fn(),
       fullTextSearch: jest.fn(),
       getDownloadHistory: jest.fn(),
       getDownloadLimits: jest.fn(),
       getRecentBooks: jest.fn(),
       processDocumentForRag: jest.fn(),
     }));

     const { toolRegistry } = await import('../dist/index.js');
     const zlibApi = await import('../lib/zlibrary-api.js');

     const handler = toolRegistry.search_books.handler;
     const mockArgs = { query: 'test', count: 5 };
     const validatedArgs = toolRegistry.search_books.schema.parse(mockArgs);
     const mockResult = [{ title: 'Result Book' }];
     mockSearchBooks.mockResolvedValueOnce(mockResult);

     const response = await handler(validatedArgs);

     expect(mockSearchBooks).toHaveBeenCalledWith(validatedArgs, { signal: undefined });
     expect(response).toEqual(mockResult);

     // Error case
     const error = new Error('API Failed');
     mockSearchBooks.mockRejectedValueOnce(error);
     const errorResponse = await handler(validatedArgs);
     expect(errorResponse).toEqual({ error: { message: 'API Failed' } });
   });

    test('downloadBookToFile handler should call zlibApi.downloadBookToFile (v2.1)', async () => {
       jest.resetModules();
       jest.clearAllMocks();
       const mockDownloadBookToFile = jest.fn();
       jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({
         searchBooks: jest.fn(),
         downloadBookToFile: mockDownloadBookToFile,
         getDownloadInfo: jest.fn(),
         fullTextSearch: jest.fn(),
         getDownloadHistory: jest.fn(),
         getDownloadLimits: jest.fn(),
         getRecentBooks: jest.fn(),
         processDocumentForRag: jest.fn(),
       }));

       const { toolRegistry } = await import('../dist/index.js');
       const zlibApi = await import('../lib/zlibrary-api.js');

       const handler = toolRegistry.download_book_to_file.handler;
       const mockBookDetailsArg = { id: 'book456', url: 'http://example.com/book/456/slug', title: 'Test Download' };
       const mockArgs = { bookDetails: mockBookDetailsArg, outputDir: '/tmp/test' };
       const validatedArgs = toolRegistry.download_book_to_file.schema.parse(mockArgs);
       const mockResult = { file_path: '/tmp/test/book.epub', processed_file_path: null };
       mockDownloadBookToFile.mockResolvedValueOnce(mockResult);

       const response = await handler(validatedArgs);

       expect(mockDownloadBookToFile).toHaveBeenCalledWith(validatedArgs, { signal: undefined });
       expect(response).toEqual(mockResult);

      // Error case
      const error = new Error('Download Error');
      mockDownloadBookToFile.mockRejectedValueOnce(error);
      const errorResponse = await handler(validatedArgs);
      expect(errorResponse).toEqual({ error: { message: 'Download Error' } });
    });

    test('fullTextSearch handler should call zlibApi.fullTextSearch', async () => {
       jest.resetModules();
       jest.clearAllMocks();
       const mockFullTextSearch = jest.fn();
       jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({
         searchBooks: jest.fn(),
         downloadBookToFile: jest.fn(),
         getDownloadInfo: jest.fn(),
         fullTextSearch: mockFullTextSearch,
         getDownloadHistory: jest.fn(),
         getDownloadLimits: jest.fn(),
         getRecentBooks: jest.fn(),
         processDocumentForRag: jest.fn(),
       }));

       const { toolRegistry } = await import('../dist/index.js');
       const zlibApi = await import('../lib/zlibrary-api.js');

       const handler = toolRegistry.full_text_search.handler;
       const mockArgs = { query: 'search text' };
       const validatedArgs = toolRegistry.full_text_search.schema.parse(mockArgs);
       const mockResult = [{ title: 'Found Book' }];
       mockFullTextSearch.mockResolvedValueOnce(mockResult);

       const response = await handler(validatedArgs);

       expect(mockFullTextSearch).toHaveBeenCalledWith(validatedArgs, { signal: undefined });
        expect(response).toEqual(mockResult);

       const error = new Error('FullText Error');
       mockFullTextSearch.mockRejectedValueOnce(error);
       const errorResponse = await handler(validatedArgs);
       expect(errorResponse).toEqual({ error: { message: 'FullText Error' } });
    });

    test('getDownloadHistory handler should call zlibApi.getDownloadHistory', async () => {
       jest.resetModules();
       jest.clearAllMocks();
       const mockGetDownloadHistory = jest.fn();
       jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({
         searchBooks: jest.fn(),
         downloadBookToFile: jest.fn(),
         getDownloadInfo: jest.fn(),
         fullTextSearch: jest.fn(),
         getDownloadHistory: mockGetDownloadHistory,
         getDownloadLimits: jest.fn(),
         getRecentBooks: jest.fn(),
         processDocumentForRag: jest.fn(),
       }));

       const { toolRegistry } = await import('../dist/index.js');
       const zlibApi = await import('../lib/zlibrary-api.js');

       const handler = toolRegistry.get_download_history.handler;
       const mockArgs = { count: 5 };
       const validatedArgs = toolRegistry.get_download_history.schema.parse(mockArgs);
       const mockResult = [{ id: 'hist1' }];
       mockGetDownloadHistory.mockResolvedValueOnce(mockResult);

       const response = await handler(validatedArgs);

       expect(mockGetDownloadHistory).toHaveBeenCalledWith(validatedArgs, { signal: undefined });
       expect(response).toEqual(mockResult);

       const error = new Error('History Error');
       mockGetDownloadHistory.mockRejectedValueOnce(error);
       const errorResponse = await handler(validatedArgs);
       expect(errorResponse).toEqual({ error: { message: 'History Error' } });
    });

    test('getDownloadLimits handler should call zlibApi.getDownloadLimits', async () => {
       jest.resetModules();
       jest.clearAllMocks();
       const mockGetDownloadLimits = jest.fn();
       jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({
         searchBooks: jest.fn(),
         downloadBookToFile: jest.fn(),
         getDownloadInfo: jest.fn(),
         fullTextSearch: jest.fn(),
         getDownloadHistory: jest.fn(),
         getDownloadLimits: mockGetDownloadLimits,
         getRecentBooks: jest.fn(),
         processDocumentForRag: jest.fn(),
       }));

       const { toolRegistry } = await import('../dist/index.js');
       const zlibApi = await import('../lib/zlibrary-api.js');

       const handler = toolRegistry.get_download_limits.handler;
       const mockArgs = {};
       const validatedArgs = toolRegistry.get_download_limits.schema.parse(mockArgs);
       const mockResult = { daily: 5, used: 1 };
       mockGetDownloadLimits.mockResolvedValueOnce(mockResult);

       const response = await handler(validatedArgs);

       // Per-source since #107: the tool now takes an optional `sources`
       // narrowing, and the API layer receives it alongside the signal.
       expect(mockGetDownloadLimits).toHaveBeenCalledWith(
         { sources: undefined },
         { signal: undefined },
       );
       expect(response).toEqual(mockResult);

       const error = new Error('Limits Error');
       mockGetDownloadLimits.mockRejectedValueOnce(error);
       const errorResponse = await handler(validatedArgs);
       expect(errorResponse).toEqual({ error: { message: 'Limits Error' } });
    });

  }); // End Handler Logic describe
}); // End Tool Handlers (Direct) describe
