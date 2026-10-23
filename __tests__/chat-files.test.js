import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createServer } from '../dist/index.js';
import { PDF_DEADLINES_MS } from '../dist/lib/chat-files.js';

const python = process.env.ZLIBRARY_PDF_PYTHON;
if (!python) process.stderr.write('SKIP chat-files PyMuPDF tests: set ZLIBRARY_PDF_PYTHON\n');
const pdfTest = python ? test : test.skip;
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
let old, directory, root, client, server;
beforeEach(async () => {
  old = { ...process.env };
  directory = await fs.mkdtemp(path.join(os.tmpdir(), 'chat-files-'));
  root = path.join(directory, 'root');
  await fs.mkdir(root);
  await fs.writeFile(path.join(root, 'book.epub'), 'UNCHANGED_EPUB');
  await fs.writeFile(path.join(directory, 'outside'), 'SECRET');
  process.env.ZLIBRARY_OUTPUT_ROOT = root;
  process.env.ZLIBRARY_CHAT_FILES = '1';
  delete process.env.ZLIBRARY_PROBE_TOOLS;
  delete process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES;
});
afterEach(async () => {
  await client?.close();
  await server?.close();
  client = server = undefined;
  process.env = old;
  jest.restoreAllMocks();
  await fs.rm(directory, { recursive: true, force: true });
});
async function connect() {
  server = createServer();
  client = new Client({ name: 'chat-files-test', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(b), client.connect(a)]);
}
const call = (name, args) => client.callTool({ name, arguments: args });
const share = (args = {}) => call('share_book_file', { file: 'book.epub', ...args });
const facts = (r) => JSON.parse(r.content.find((c) => c.type === 'text').text);
const link = (r) => r.content.find((c) => c.type === 'resource_link');
const expectError = (r, pattern) => {
  expect(r.isError).toBe(true);
  expect(r.content[0].text).toMatch(pattern);
};
async function generated() {
  await promisify(execFile)(python, [
    '-I',
    '-B',
    path.resolve('__tests__/fixtures/generate-chat-pdfs.py'),
    root,
  ]);
}

test.each([undefined, '0', 'true', '01'])('flag %s preserves frozen tools/list', async (flag) => {
  if (flag === undefined) delete process.env.ZLIBRARY_CHAT_FILES;
  else process.env.ZLIBRARY_CHAT_FILES = flag;
  await connect();
  expect(JSON.stringify(await client.listTools())).toBe(
    (await fs.readFile('__tests__/fixtures/handoff-tools-baseline.json', 'utf8')).trim(),
  );
  expect(client.getServerCapabilities().resources).toBeUndefined();
});
test('opt-in lists three chat tools and preserves probes under their own flag', async () => {
  process.env.ZLIBRARY_PROBE_TOOLS = '1';
  await connect();
  expect((await client.listTools()).tools.map((t) => t.name)).toEqual([
    'search',
    'get_book',
    'download',
    'get_limits',
    'get_history',
    'probe_file_link',
    'probe_page_image',
    'get_book_map',
    'share_book_file',
    'get_page_image',
  ]);
});
test('whole file unchanged, immutable cached SHA, persistent restart resource', async () => {
  await connect();
  const r = await share();
  const l = link(r);
  const j = facts(r);
  expect(l.uri).toMatch(/^zlib-book:\/\/file\/[a-f0-9-]+$/);
  expect(j.bytes).toBe(14);
  expect(j.sha256).toBe(sha(Buffer.from('UNCHANGED_EPUB')));
  await fs.writeFile(path.join(root, 'book.epub'), 'REPLACED');
  const reply = await client.readResource({ uri: l.uri });
  expect(Buffer.from(reply.contents[0].blob, 'base64').toString()).toBe('UNCHANGED_EPUB');
  await client.close();
  await server.close();
  await connect();
  expect(await client.readResource({ uri: l.uri })).toEqual(reply);
  const files = await fs.readdir(path.join(root, '.chat-files/cache'));
  const pieces = files.filter((f) => f.startsWith('whole-'));
  expect(pieces).toHaveLength(1);
  expect(sha(await fs.readFile(path.join(root, '.chat-files/cache', pieces[0])))).toBe(j.sha256);
});
test.each(['../outside', '/etc/passwd', 'C:\\outside', '\\outside', 'a\0b', '.chat-files/cache/x'])(
  'refuses unsafe path %s in every tool',
  async (file) => {
    await connect();
    for (const name of ['share_book_file', 'get_book_map', 'get_page_image'])
      expectError(
        await call(name, { file, ...(name === 'get_page_image' ? { page: 1 } : {}) }),
        /path|root|cache|file/i,
      );
  },
);
test('absolute inside accepted; symlink escapes, cache aliases, FIFO and directories refused', async () => {
  await connect();
  expect(link(await share({ file: path.join(root, 'book.epub') }))).toBeDefined();
  await fs.symlink(path.join(directory, 'outside'), path.join(root, 'escape'));
  await fs.symlink(path.join(root, '.chat-files/cache'), path.join(root, 'alias'));
  await promisify(execFile)('mkfifo', [path.join(root, 'fifo')]);
  for (const file of ['escape', 'alias', 'fifo', '.'])
    expectError(await share({ file }), /path|root|cache|regular/i);
});
test('refuses a cache directory symlink before any writes', async () => {
  await fs.symlink(directory, path.join(root, '.chat-files'));
  await connect();
  expectError(await share(), /cache/i);
});
test('id expiry is exactly four hours and cap is 100 across restarts', async () => {
  await connect();
  const now = Date.now();
  const clock = jest.spyOn(Date, 'now').mockReturnValue(now);
  const first = link(await share()).uri;
  for (let i = 0; i < 99; i++) await share();
  await client.close();
  await server.close();
  await connect();
  expect((await client.readResource({ uri: first })).contents).toHaveLength(1);
  const latest = link(await share()).uri;
  await expect(client.readResource({ uri: first })).rejects.toThrow(/unknown|expired/i);
  clock.mockReturnValue(now + 4 * 60 * 60 * 1000 - 1);
  expect((await client.readResource({ uri: latest })).contents).toHaveLength(1);
  clock.mockReturnValue(now + 4 * 60 * 60 * 1000);
  await expect(client.readResource({ uri: latest })).rejects.toThrow(/unknown|expired/i);
}, 60_000);
test('resource read rechecks cap, regular file and hash', async () => {
  await connect();
  const uri = link(await share()).uri;
  const cache = path.join(root, '.chat-files/cache');
  const piece = (await fs.readdir(cache)).find((f) => f.startsWith('whole-'));
  await fs.writeFile(path.join(cache, piece), 'CORRUPTED');
  await expect(client.readResource({ uri })).rejects.toThrow(/changed|unavailable|integrity/i);
});
test('EPUB over cap refused, other map metadata needs no PDF interpreter', async () => {
  await connect();
  delete process.env.ZLIBRARY_PDF_PYTHON;
  const m = facts(await call('get_book_map', { file: 'book.epub' }));
  expect(m.fits_whole).toBe(true);
  expect(m.bytes).toBe(14);
  await fs.writeFile(path.join(root, 'big.epub'), Buffer.alloc(7750001));
  const r = await share({ file: 'big.epub' });
  expect(link(r)).toBeUndefined();
  expect(JSON.stringify(r)).toMatch(/EPUB splitting is not supported/);
});
test('invalid max_bytes setting returns explicit error, including just below and above bounds', async () => {
  await connect();
  for (const value of ['999999', '33554433', 'NaN', '1.2']) {
    process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = value;
    expectError(await share(), /ZLIBRARY_CHAT_FILES_MAX_BYTES/);
  }
});
pdfTest('map sizes, section resolution, partial send, cap and labels', async () => {
  await generated();
  process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = '1100000';
  await connect();
  const m = facts(await call('get_book_map', { file: 'nested.pdf' }));
  expect(m.pages).toBe(12);
  expect(m.text_layer).toBe(1);
  expect(m.fits_whole).toBe(false);
  expect(m.sections[1].fits).toBe(false);
  expect(m.guidance).toMatch(/estimates with a 5% margin/);
  expect(link(await share({ file: 'nested.pdf' }))).toBeUndefined();
  for (const part of m.suggested_parts) {
    const r = await share({ file: 'nested.pdf', pages: part.pdf_pages });
    // Estimated parts never overflow: the exact send is the whole part.
    expect(facts(r).pdf_pages).toBe(part.pdf_pages);
    expect(facts(r).remaining).toBeUndefined();
    expect(facts(r).bytes).toBeLessThanOrEqual(part.estimated_bytes);
    expect(link(r).size).toBeLessThanOrEqual(1100000);
    const bytes = Buffer.from(
      (await client.readResource({ uri: link(r).uri })).contents[0].blob,
      'base64',
    );
    expect(sha(bytes)).toBe(facts(r).sha256);
  }
  const r = await share({ file: 'nested.pdf', pages: '1-12' });
  const j = facts(r);
  expect(j.remaining).toMatch(/^\d+-12$/);
  expect(j.pdf_pages).toMatch(/^1-\d+$/);
  expect(j.bytes).toBeLessThanOrEqual(1100000);
  const section = await share({ file: 'nested.pdf', section: '2.1' });
  expect(facts(section).pdf_pages).toBe('3-6');
  expect(facts(section).printed_pages).toBeDefined();
  expectError(await share({ file: 'nested.pdf', section: '999' }), /section/i);
  const before = (
    await fs.stat(
      path.join(
        root,
        '.chat-files/cache',
        (await fs.readdir(path.join(root, '.chat-files/cache'))).find((x) => x.startsWith('map-')),
      ),
    )
  ).mtimeMs;
  expect(facts(await call('get_book_map', { file: 'nested.pdf' }))).toEqual(m);
  const after = (
    await fs.stat(
      path.join(
        root,
        '.chat-files/cache',
        (await fs.readdir(path.join(root, '.chat-files/cache'))).find((x) => x.startsWith('map-')),
      ),
    )
  ).mtimeMs;
  expect(after).toBe(before);
}, 60_000);
pdfTest.each(['0', '2-1', '1-13', '1.5', '1,2', '-1'])(
  'rejects invalid PDF range %s',
  async (pages) => {
    await generated();
    await connect();
    expectError(await share({ file: 'nested.pdf', pages }), /page|range/i);
  },
);
pdfTest('one page over budget sends no file and directs to page image', async () => {
  await generated();
  await promisify(execFile)(python, [
    '-I',
    '-B',
    '-c',
    'import fitz,random,sys; d=fitz.open();p=d.new_page();p.insert_image(p.rect,pixmap=fitz.Pixmap(fitz.csRGB,800,800,random.Random(1).randbytes(1920000),False));d.save(sys.argv[1])',
    path.join(root, 'huge.pdf'),
  ]);
  process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = '1000000';
  await connect();
  const r = await share({ file: 'huge.pdf', pages: '1' });
  expect(link(r)).toBeUndefined();
  expect(JSON.stringify(r)).toMatch(/get_page_image/);
});
pdfTest('three concurrent renders queue and report labels with no page text', async () => {
  await generated();
  await connect();
  const results = await Promise.all(
    [1, 2, 3].map((page) => call('get_page_image', { file: 'nested.pdf', page })),
  );
  for (const r of results) {
    expect(r.isError).not.toBe(true);
    expect(r.content[0].type).toBe('image');
    expect(r.content[1].text).toMatch(/printed page/);
    expect(r.content[1].text).not.toMatch(/PRIVATE|busy/);
  }
});
test('PDF configuration and argument errors are clear', async () => {
  await fs.writeFile(path.join(root, 'bad.pdf'), 'PRIVATE');
  await connect();
  delete process.env.ZLIBRARY_PDF_PYTHON;
  expectError(await call('get_book_map', { file: 'bad.pdf' }), /ZLIBRARY_PDF_PYTHON/);
  expectError(await share({ file: 'book.epub', pages: '1' }), /PDF/);
  expectError(await share({ file: 'book.epub', pages: '1', section: '1' }), /arguments|together/i);
});
pdfTest('malformed PDF maps and shares; MuPDF repair messages stay out of the reply', async () => {
  await generated();
  await connect();
  const m = facts(await call('get_book_map', { file: 'malformed.pdf' }));
  expect(m.pages).toBe(3);
  expect(m.repairs).toBeGreaterThan(0);
  const r = await share({ file: 'malformed.pdf', pages: '1-3' });
  expect(link(r)).toBeDefined();
  expect(facts(r).pdf_pages).toBe('1-3');
});
async function fakePython(body) {
  const file = path.join(directory, `fake-python-${Math.random().toString(36).slice(2)}`);
  await fs.writeFile(file, `#!/bin/sh\ncat >/dev/null\n${body}\n`, { mode: 0o755 });
  process.env.ZLIBRARY_PDF_PYTHON = file;
}
test('the helper reply is its last stdout line; earlier library output is ignored', async () => {
  await fs.writeFile(path.join(root, 'any.pdf'), 'PRIVATE');
  await fakePython(
    `echo 'MuPDF error: syntax error: invalid key in dict'\necho '{"pages":1,"sections":[],"suggested_parts":[]}'\necho`,
  );
  await connect();
  const m = facts(await call('get_book_map', { file: 'any.pdf' }));
  expect(m.pages).toBe(1);
});
test('PDF failures name their own cause; only a spawn failure names ZLIBRARY_PDF_PYTHON', async () => {
  await fs.writeFile(path.join(root, 'any.pdf'), 'PRIVATE');
  await connect();
  const map = () => call('get_book_map', { file: 'any.pdf' });
  process.env.ZLIBRARY_PDF_PYTHON = path.join(directory, 'missing-python');
  expectError(await map(), /could not start \(ENOENT\); check ZLIBRARY_PDF_PYTHON/);
  const plain = path.join(directory, 'not-executable');
  await fs.writeFile(plain, '#!/bin/sh\n', { mode: 0o644 });
  process.env.ZLIBRARY_PDF_PYTHON = plain;
  expectError(await map(), /could not start \(EACCES\); check ZLIBRARY_PDF_PYTHON/);
  await fakePython('exit 3');
  let r = await map();
  expectError(r, /exited abnormally \(status 3\)/);
  expect(r.content[0].text).not.toMatch(/ZLIBRARY_PDF_PYTHON|timed out|time/);
  const deadline = PDF_DEADLINES_MS.map;
  PDF_DEADLINES_MS.map = 300;
  try {
    await fakePython('exec sleep 30');
    r = await map();
    expectError(r, /PDF map did not finish within 0.3 s and was stopped/);
    expect(r.content[0].text).not.toMatch(/ZLIBRARY_PDF_PYTHON|could not start/);
  } finally {
    PDF_DEADLINES_MS.map = deadline;
  }
});
