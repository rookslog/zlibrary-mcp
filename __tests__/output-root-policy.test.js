import { afterEach, expect, jest, test } from '@jest/globals';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { createServer, start } from '../dist/index.js';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';

const previous = { ...process.env };
const platformDescriptor = Object.getOwnPropertyDescriptor(process, 'platform');
afterEach(() => {
  process.env = { ...previous };
  Object.defineProperty(process, 'platform', platformDescriptor);
  jest.restoreAllMocks();
});

test.each(['true', '', 'yes', '2', ' 1'])('startup refuses unrestricted value %j', async value => {
  process.env.ZLIBRARY_UNRESTRICTED_OUTPUT = value;
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  expect(() => createServer()).toThrow(/ZLIBRARY_UNRESTRICTED_OUTPUT/);
  expect(await start({ testing: true })).toBeNull();
  expect(log).toHaveBeenCalled();
});

test.each(['relative', ''])('startup refuses a relative root %j', async value => {
  process.env.ZLIBRARY_OUTPUT_ROOT = value;
  expect(() => createServer()).toThrow(/ZLIBRARY_OUTPUT_ROOT/);
});

test.each(['\\foo', 'D:foo'])('Windows startup refuses partially anchored root %j', value => {
  Object.defineProperty(process, 'platform', { ...platformDescriptor, value: 'win32' });
  process.env.ZLIBRARY_OUTPUT_ROOT = value;
  expect(() => createServer()).toThrow(/ZLIBRARY_OUTPUT_ROOT/);
});

test.each(['C:\\private', '\\\\server\\share\\private'])('Windows startup accepts fully qualified root %j', async value => {
  Object.defineProperty(process, 'platform', { ...platformDescriptor, value: 'win32' });
  expect(path.win32.isAbsolute(value)).toBe(true);
  process.env.ZLIBRARY_OUTPUT_ROOT = value;
  const server = createServer();
  await server.close();
});

test.each([undefined, '0', '1'])('startup accepts unrestricted value %j', async value => {
  if (value === undefined) delete process.env.ZLIBRARY_UNRESTRICTED_OUTPUT;
  else process.env.ZLIBRARY_UNRESTRICTED_OUTPUT = value;
  process.env.ZLIBRARY_OUTPUT_ROOT = os.tmpdir();
  const server = createServer();
  await server.close();
});

test('startup writes one warning per notice to stderr and reports the new data root', async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'output-policy-'));
  fs.mkdirSync(path.join(cwd, 'downloads'));
  const previousCwd = process.cwd();
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  jest.spyOn(StdioServerTransport.prototype, 'start').mockResolvedValue();
  let running;
  try {
    process.chdir(cwd);
    delete process.env.ZLIBRARY_OUTPUT_ROOT;
    Object.assign(process.env, { XDG_DATA_HOME: cwd, LOCALAPPDATA: cwd,
      LOG_LEVEL: 'warn', ZLIBRARY_UNRESTRICTED_OUTPUT: '1' });
    running = await start({ testing: true });
    expect(running).not.toBeNull();
    const lines = log.mock.calls.map(args => args.join(' '));
    expect(lines.filter(line => line.includes('ZLIBRARY_UNRESTRICTED_OUTPUT'))).toHaveLength(1);
    expect(lines.filter(line => line.includes(path.join(cwd, 'zlibrary-mcp', 'downloads')))).toHaveLength(1);
    expect(lines.find(line => line.includes('ZLIBRARY_UNRESTRICTED_OUTPUT'))).toMatch(/\[warn\]/);
  } finally {
    await running?.server.close();
    process.chdir(previousCwd);
    fs.rmSync(cwd, { recursive: true, force: true });
  }
});

test('public download envelope preserves a pre-contact output_refused', async () => {
  const tools = new AcquisitionTools(async operation => {
    if (operation === 'search_multi_source') return { books: [{ source: 'libgen', md5: 'a'.repeat(32), title: 'Book' }] };
    throw { context: { details: { reason: 'output_refused', request_issued: false } } };
  }, {});
  const found = await tools.call('search', { query: 'Book', sources: ['libgen'] });
  const reply = await tools.call('download', { ref: found.rows[0].ref, output_dir: '../escape' });
  expect(reply.error).toMatchObject({ code: 'output_refused', operation: 'download', retry: 'no',
    detail: { reason: 'output_refused', request_issued: false } });
});
