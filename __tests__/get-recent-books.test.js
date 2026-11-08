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

  test('removed recent-books MCP call returns a typed migration error without a subprocess', async () => {
    const result = await client.callTool({ name: 'get_recent_books', arguments: {} });
    expect(result.isError).toBe(true);
    expect(result.structuredContent.error.code).toBe('invalid_argument');
    expect(mockRunPythonBridge).not.toHaveBeenCalled();
  });
});

describe('recent-books positive count contract', () => {
  test.each([-1, 0, 1.5, Infinity, NaN])(
    'facade rejects count=%p before dispatch',
    async (count) => {
      jest.resetModules();
      mockRunPythonBridge.mockReset();
      const { getRecentBooks } = await import('../dist/lib/zlibrary-api.js');
      await expect(getRecentBooks({ count })).rejects.toThrow(/positive integer/i);
      expect(mockRunPythonBridge).not.toHaveBeenCalled();
    },
  );

  test.each([-1, 0, 1.5])('MCP rejects count=%p before dispatch', async (count) => {
    jest.resetModules();
    mockRunPythonBridge.mockReset();
    const { createServer } = await import('../dist/index.js');
    const server = createServer();
    const [localClientTransport, localServerTransport] = InMemoryTransport.createLinkedPair();
    const localClient = new Client({ name: 'positive-count', version: '1.0.0' });
    try {
      await Promise.all([
        server.connect(localServerTransport),
        localClient.connect(localClientTransport),
      ]);
      const result = await localClient.callTool({ name: 'get_recent_books', arguments: { count } });
      expect(result.isError).toBe(true);
      expect(result.structuredContent.error.code).toBe('invalid_argument');
      expect(mockRunPythonBridge).not.toHaveBeenCalled();
    } finally {
      await localClient.close();
      await server.close();
    }
  });
});
