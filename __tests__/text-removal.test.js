import './helpers/environment-login.js';
process.env.ZLIBRARY_SESSION_MODE = 'per-call';
import { jest, test, expect, afterEach } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

const runBridge = jest.fn();
jest.unstable_mockModule('../lib/venv-manager.js', () => ({
  getManagedPythonPath: jest.fn().mockResolvedValue('/offline/python'),
}));
jest.unstable_mockModule('../lib/python-runner.js', () => ({
  runPythonBridge: runBridge, LONG_BRIDGE_TIMEOUT_MS: 2400000,
}));
const { createServer } = await import('../dist/index.js');
const api = await import('../dist/lib/zlibrary-api.js');
let client, server;
afterEach(async () => { await client?.close(); await server?.close(); runBridge.mockReset(); });
async function connect() {
  server = createServer();
  client = new Client({ name: 'saved-download-contract', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(b), client.connect(a)]);
}
const bookDetails = { id: '1', hash: 'offline' };
for (const legacy of [
  { process_for_rag: true }, { process_for_rag: 'false' }, { process_for_rag: null },
  { processed_output_format: 'txt' }, { processed_output_format: null },
  { process_for_rag: false, processed_output_format: 'markdown' },
]) {
  test(`SDK rejects saved processing request before any subprocess: ${JSON.stringify(legacy)}`, async () => {
    await connect();
    const result = await client.callTool({ name: 'download_book_to_file', arguments: { bookDetails, ...legacy } });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/text processing was removed in 2\.0/i);
    expect(runBridge).not.toHaveBeenCalled();
  });
  test(`direct API rejects saved request: ${JSON.stringify(legacy)}`, async () => {
    await expect(api.downloadBookToFile({ bookDetails, ...legacy })).rejects.toThrow(/text processing was removed in 2\.0/i);
    expect(runBridge).not.toHaveBeenCalled();
  });
}
test('SDK rejects even saved false: old names have no aliases in 2.0', async () => {
  await connect();
  const result = await client.callTool({ name: 'download_book_to_file', arguments: { bookDetails, process_for_rag: false } });
  expect(result.isError).toBe(true);
  expect(result.structuredContent.error.code).toBe('invalid_argument');
  expect(runBridge).not.toHaveBeenCalled();
});
test('unknown download fields fail before subprocess', async () => {
  await connect();
  const result = await client.callTool({ name: 'download_book_to_file', arguments: { bookDetails, typo: true } });
  expect(result.isError).toBe(true);
  expect(runBridge).not.toHaveBeenCalled();
});
test('tool listing no longer advertises processing and removed calls are unknown', async () => {
  await connect();
  const tools = await client.listTools();
  expect(tools.tools).toHaveLength(5);
  expect(JSON.stringify(tools)).not.toMatch(/process_for_rag|processed_output_format|process_document_for_rag/);
  const removed = await client.callTool({ name: 'process_document_for_rag', arguments: { file_path: '/offline/book.pdf' } });
  expect(removed.isError).toBe(true);
  expect(runBridge).not.toHaveBeenCalled();
});
test('stale backend processing bundles cannot escape the acquisition-only API', async () => {
  runBridge.mockResolvedValue([JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({
    file_path: '/offline/book.pdf', processed_file_path: '/offline/body.txt',
  }) }] })]);
  await expect(api.downloadBookToFile({ bookDetails })).rejects.toThrow(/unexpected.*processing/i);
});

test('download advertises a closed ref-only schema', async () => {
  await connect();
  const { tools } = await client.listTools();
  const { inputSchema } = tools.find((tool) => tool.name === 'download');
  expect(inputSchema.additionalProperties).toBe(false);
  expect(Object.keys(inputSchema.properties).sort()).toEqual(['filename', 'output_dir', 'ref']);
  expect(JSON.stringify(inputSchema)).not.toContain('"additionalProperties":{}');
});

test('default memory mode refuses saved true before session acquisition', async () => {
  const { EapiSessionOwner, getSessionMode } = await import('../dist/lib/eapi-session.js');
  const session = jest.spyOn(EapiSessionOwner.prototype, 'run');
  const originalMode = process.env.ZLIBRARY_SESSION_MODE;
  const originalEmail = process.env.ZLIBRARY_EMAIL;
  const originalPassword = process.env.ZLIBRARY_PASSWORD;
  delete process.env.ZLIBRARY_SESSION_MODE;
  process.env.ZLIBRARY_EMAIL = 'offline@example.test';
  process.env.ZLIBRARY_PASSWORD = 'offline-password';
  try {
    expect(getSessionMode()).toBe('memory');
    await connect();
    const result = await client.callTool({
      name: 'download_book_to_file',
      arguments: { bookDetails, process_for_rag: true },
    });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/text processing was removed in 2\.0/i);
    expect(session).not.toHaveBeenCalled();
    expect(runBridge).not.toHaveBeenCalled();
  } finally {
    session.mockRestore();
    for (const [name, value] of [
      ['ZLIBRARY_SESSION_MODE', originalMode], ['ZLIBRARY_EMAIL', originalEmail],
      ['ZLIBRARY_PASSWORD', originalPassword],
    ]) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
});
