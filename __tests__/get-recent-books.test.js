import { jest, describe, beforeEach, afterEach, test, expect } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

const mockRunPythonBridge = jest.fn();
const mockGetManagedPythonPath = jest.fn();

let clientTransport;
let serverTransport;

// Mock dependencies before importing
jest.unstable_mockModule('../lib/venv-manager.js', () => ({
  getManagedPythonPath: mockGetManagedPythonPath,
}));

jest.unstable_mockModule('../lib/python-runner.js', () => ({
  runPythonBridge: mockRunPythonBridge,
  killAllPythonChildren: jest.fn(),
  installExitHooks: jest.fn(),
  liveChildCount: jest.fn(() => 0),
  DEFAULT_BRIDGE_TIMEOUT_MS: 240000,
  LONG_BRIDGE_TIMEOUT_MS: 2400000,
}));

// Substitute InMemoryTransport for StdioServerTransport in start() while keeping
// the real McpServer, tool registration, and Zod schema validation completely intact.
jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
  StdioServerTransport: jest.fn(() => serverTransport),
}));

describe('get_recent_books suite', () => {
  let zlibApi;
  let client;
  let serverInstance;

  const sampleBooks = [
    { id: '1', title: 'Book 1', author: 'Author A', extension: 'epub' },
    { id: '2', title: 'Book 2', author: 'Author B', extension: 'pdf' },
    { id: '3', title: 'Book 3', author: 'Author C', extension: 'EPUB' },
    { id: '4', title: 'Book 4', author: 'Author D', extension: 'txt' },
  ];

  function makeBridgeOutput(books) {
    const payload = JSON.stringify({ books });
    return [JSON.stringify({ content: [{ type: 'text', text: payload }] })];
  }

  beforeEach(async () => {
    jest.resetModules();
    jest.clearAllMocks();

    mockGetManagedPythonPath.mockResolvedValue('/fake/python');

    [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();

    zlibApi = await import('../dist/lib/zlibrary-api.js');
    const indexModule = await import('../dist/index.js');

    const startResult = await indexModule.start({ testing: true });
    serverInstance = startResult.server;

    client = new Client({ name: 'test-client', version: '1.0.0' }, { capabilities: {} });
    await client.connect(clientTransport);
  });

  afterEach(async () => {
    if (client) {
      await client.close();
    }
    if (serverInstance) {
      await serverInstance.close();
    }
  });

  describe('TypeScript API Wrapper: zlibraryApi.getRecentBooks', () => {
    test('is exported as a function on zlibrary-api and defaults count to 10', async () => {
      expect(typeof zlibApi.getRecentBooks).toBe('function');

      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));
      const result = await zlibApi.getRecentBooks();

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 10 });
      expect(result).toEqual({ books: sampleBooks });
    });

    test('passes explicit count and filters by format without passing format to Python', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const result = await zlibApi.getRecentBooks({ count: 4, format: 'epub' });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 4 });
      // Case-insensitive format match: 'epub' and 'EPUB'
      expect(result.books).toEqual([
        { id: '1', title: 'Book 1', author: 'Author A', extension: 'epub' },
        { id: '3', title: 'Book 3', author: 'Author C', extension: 'EPUB' },
      ]);
    });

    test('propagates errors when Python bridge fails', async () => {
      mockRunPythonBridge.mockRejectedValueOnce(new Error('Subprocess crashed'));

      await expect(zlibApi.getRecentBooks({ count: 5 })).rejects.toThrow(
        /Python bridge execution failed for get_recent_books/,
      );
    });

    test('passes cancellation signal to Python bridge runner', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput([]));
      const controller = new AbortController();

      await zlibApi.getRecentBooks({ count: 5 }, { signal: controller.signal });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const runnerOptions = mockRunPythonBridge.mock.calls[0][2];
      expect(runnerOptions.signal).toBe(controller.signal);
    });
  });

  describe('Real MCP Client + McpServer exchange over InMemoryTransport', () => {
    test('advertises get_recent_books in listTools with description and parameter schema', async () => {
      const response = await client.listTools();
      const tool = response.tools.find((t) => t.name === 'get_recent_books');

      expect(tool).toBeDefined();
      expect(tool.description).toContain('recently added books');
      expect(tool.inputSchema.properties).toHaveProperty('count');
      expect(tool.inputSchema.properties).toHaveProperty('format');
    });

    test('executes callTool with defaults over real MCP protocol and returns structured content', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const response = await client.callTool({
        name: 'get_recent_books',
        arguments: {},
      });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 10 });

      expect(response).toEqual({
        content: [{ type: 'text', text: JSON.stringify({ books: sampleBooks }) }],
        structuredContent: { books: sampleBooks },
      });
    });

    test('executes callTool with explicit count and format over real MCP protocol', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const response = await client.callTool({
        name: 'get_recent_books',
        arguments: { count: 4, format: 'pdf' },
      });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 4 });

      const expected = {
        books: [{ id: '2', title: 'Book 2', author: 'Author B', extension: 'pdf' }],
      };
      expect(response).toEqual({
        content: [{ type: 'text', text: JSON.stringify(expected) }],
        structuredContent: expected,
      });
    });

    test('rejects invalid schema input before calling Python runner', async () => {
      const response = await client.callTool({
        name: 'get_recent_books',
        arguments: { count: 'not-a-number' },
      });

      expect(response.isError).toBe(true);
      expect(response.content[0].text).toContain('Invalid arguments');
      expect(mockRunPythonBridge).not.toHaveBeenCalled();
    });

    test('handles Python runner error and surfaces isError MCP response to client', async () => {
      mockRunPythonBridge.mockRejectedValueOnce(new Error('Subprocess crash failure'));

      const response = await client.callTool({
        name: 'get_recent_books',
        arguments: {},
      });

      expect(response.isError).toBe(true);
      expect(response.content[0].type).toBe('text');
      expect(response.content[0].text).toContain('Error:');
    });

    test('propagates real client cancellation to Python bridge runner without arbitrary sleep', async () => {
      let runnerStartedResolve;
      const runnerStartedPromise = new Promise((resolve) => {
        runnerStartedResolve = resolve;
      });

      mockRunPythonBridge.mockImplementationOnce((_scriptName, _options, runnerOpts) => {
        runnerStartedResolve(runnerOpts);
        return new Promise((_resolve, reject) => {
          if (runnerOpts?.signal) {
            runnerOpts.signal.addEventListener('abort', () => {
              const abortErr = new Error('This operation was aborted');
              abortErr.name = 'AbortError';
              reject(abortErr);
            });
          }
        });
      });

      const controller = new AbortController();
      const callPromise = client.callTool(
        { name: 'get_recent_books', arguments: {} },
        undefined,
        { signal: controller.signal },
      );

      // Wait deterministically for the mock runner to receive the call (no arbitrary sleep)
      const runnerOpts = await runnerStartedPromise;
      expect(runnerOpts.signal).toBeDefined();
      expect(runnerOpts.signal.aborted).toBe(false);

      // Issue cancellation from client
      controller.abort();

      // Ensure callTool rejects with abort and the runner's signal was marked aborted
      await expect(callPromise).rejects.toThrow();
      expect(runnerOpts.signal.aborted).toBe(true);
    });
  });
});
