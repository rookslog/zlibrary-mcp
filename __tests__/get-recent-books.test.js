import { jest, describe, beforeEach, test, expect } from '@jest/globals';

const mockRunPythonBridge = jest.fn();
const mockGetManagedPythonPath = jest.fn();

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

describe('get_recent_books regression suite', () => {
  let zlibApi;
  let registeredTools;
  let startServer;

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
    registeredTools = new Map();

    const mockServer = {
      connect: jest.fn().mockResolvedValue(undefined),
      tool: jest.fn((...args) => registeredTools.set(args[0], {
        name: args[0],
        description: args[1],
        schema: args[2],
        annotations: args[3],
        handler: args[4],
      })),
      close: jest.fn(),
    };

    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/mcp.js', () => ({
      McpServer: jest.fn(() => mockServer),
    }));
    jest.unstable_mockModule('@modelcontextprotocol/sdk/server/stdio.js', () => ({
      StdioServerTransport: jest.fn(() => ({})),
    }));

    zlibApi = await import('../dist/lib/zlibrary-api.js');
    const indexModule = await import('../dist/index.js');
    startServer = indexModule.start;
  });

  describe('TypeScript API Wrapper: zlibraryApi.getRecentBooks', () => {
    test('is exported as a function on zlibrary-api', () => {
      expect(typeof zlibApi.getRecentBooks).toBe('function');
    });

    test('defaults to count=10 when called without args', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const result = await zlibApi.getRecentBooks();

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 10 });
      expect(result).toEqual({ books: sampleBooks });
    });

    test('passes explicit count to Python bridge', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks.slice(0, 2)));

      const result = await zlibApi.getRecentBooks({ count: 2 });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 2 });
      expect(result.books).toHaveLength(2);
    });

    test('filters by format in TypeScript and does NOT pass format kwarg to Python', async () => {
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const result = await zlibApi.getRecentBooks({ count: 10, format: 'epub' });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      // Python bridge only accepts count, format must not be in kwargs
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 10 });
      // Both 'epub' and 'EPUB' books should match
      expect(result.books).toEqual([
        { id: '1', title: 'Book 1', author: 'Author A', extension: 'epub' },
        { id: '3', title: 'Book 3', author: 'Author C', extension: 'EPUB' },
      ]);
    });

    test('propagates errors when Python bridge fails', async () => {
      const bridgeError = new Error('Subprocess crashed');
      mockRunPythonBridge.mockRejectedValueOnce(bridgeError);

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

  describe('MCP Dispatch & Handler Wiring: server.tool("get_recent_books")', () => {
    test('registers get_recent_books tool with McpServer', async () => {
      await startServer({ testing: true });
      expect(registeredTools.has('get_recent_books')).toBe(true);
    });

    test('dispatches get_recent_books with defaults and returns wrapped MCP result', async () => {
      await startServer({ testing: true });
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const tool = registeredTools.get('get_recent_books');
      const response = await tool.handler({}, {});

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const bridgeCallArgs = mockRunPythonBridge.mock.calls[0][1].args;
      expect(bridgeCallArgs[0]).toBe('get_recent_books');
      expect(JSON.parse(bridgeCallArgs[1])).toEqual({ count: 10 });

      expect(response).toEqual({
        content: [{ type: 'text', text: JSON.stringify({ books: sampleBooks }) }],
        structuredContent: { books: sampleBooks },
      });
    });

    test('dispatches get_recent_books with explicit count and format', async () => {
      await startServer({ testing: true });
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput(sampleBooks));

      const tool = registeredTools.get('get_recent_books');
      const response = await tool.handler({ count: 4, format: 'pdf' }, {});

      const expected = {
        books: [{ id: '2', title: 'Book 2', author: 'Author B', extension: 'pdf' }],
      };
      expect(response).toEqual({
        content: [{ type: 'text', text: JSON.stringify(expected) }],
        structuredContent: expected,
      });
    });

    test('handles error in MCP dispatch and returns isError response', async () => {
      await startServer({ testing: true });
      mockRunPythonBridge.mockRejectedValueOnce(new Error('Process execution failed'));

      const tool = registeredTools.get('get_recent_books');
      const response = await tool.handler({}, {});

      expect(response.isError).toBe(true);
      expect(response.content[0].type).toBe('text');
      expect(response.content[0].text).toContain('Error:');
    });

    test('passes cancellation signal through MCP extra to bridge runner', async () => {
      await startServer({ testing: true });
      mockRunPythonBridge.mockResolvedValueOnce(makeBridgeOutput([]));
      const controller = new AbortController();

      const tool = registeredTools.get('get_recent_books');
      await tool.handler({}, { signal: controller.signal });

      expect(mockRunPythonBridge).toHaveBeenCalledTimes(1);
      const runnerOptions = mockRunPythonBridge.mock.calls[0][2];
      expect(runnerOptions.signal).toBe(controller.signal);
    });
  });
});
