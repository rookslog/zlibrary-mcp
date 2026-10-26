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
test('map description sends no file; every file argument says where names come from', async () => {
  await connect();
  const tools = (await client.listTools()).tools.filter((t) =>
    ['get_book_map', 'share_book_file', 'get_page_image'].includes(t.name),
  );
  expect(tools).toHaveLength(3);
  const map = tools.find((t) => t.name === 'get_book_map');
  expect(map.description).not.toMatch(/arrives|uploaded|\/mnt\/data/);
  expect(tools.find((t) => t.name === 'share_book_file').description).toMatch(/arrives/);
  for (const t of tools)
    expect(t.inputSchema.properties.file.description).toMatch(
      /name returned by search or download, or the exact path download returned/,
    );
});
test('whole file unchanged, immutable cached SHA, persistent restart resource', async () => {
  await connect();
  const r = await share();
  const l = link(r);
  const j = facts(r);
  expect(l.uri).toMatch(/^zlib-book:\/\/file\/[a-f0-9-]+$/);
  expect(j.bytes).toBe(14);
  expect(j.complete).toBe(true);
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
  expect(m.repair_messages).toBe(0);
  await fs.writeFile(path.join(root, 'big.epub'), Buffer.alloc(7750001));
  const r = await share({ file: 'big.epub' });
  expect(link(r)).toBeUndefined();
  expect(JSON.stringify(r)).toMatch(/EPUB splitting is not supported/);
});
test.each([2, 3])(
  'EPUB %s maps file navigation without a PDF interpreter or new reply fields',
  async (version) => {
    delete process.env.ZLIBRARY_PDF_PYTHON;
    await fs.copyFile(`test_files/epub-nav/epub${version}.epub`, path.join(root, 'book.epub'));
    const bytes = await fs.readFile(path.join(root, 'book.epub'));
    await connect();
    const reply = await call('get_book_map', { file: 'book.epub' });
    expect(reply.isError).toBeUndefined();
    const m = facts(reply);
    expect(Object.keys(m).sort()).toEqual([
      'bytes',
      'file',
      'fits_whole',
      'format',
      'guidance',
      'max_bytes',
      'pages',
      'repair_messages',
      'safe_window_pages',
      'sections',
      'suggested_parts',
    ]);
    expect(m.sections).toEqual([
      {
        id: '1',
        title: 'Part One',
        level: 1,
        children: [
          { id: '1.1', title: 'Chapter 1 & start', level: 2, children: [] },
          { id: '1.2', title: 'Chapter 2', level: 2, children: [] },
        ],
      },
      { id: '2', title: 'End', level: 1, children: [] },
    ]);
    expect(m.pages).toBeNull();
    expect(m.guidance).toMatch(/file.s own navigation/);
    expect(m.guidance).toMatch(/not verified against the text/);
    expect(m.guidance).toMatch(/splitting is not supported/);
    expect(m.guidance).toMatch(/section ids.*outline/i);
    expect(facts(await call('get_book_map', { file: 'book.epub' }))).toEqual(m);
    expect(
      (await fs.readdir(path.join(root, '.chat-files/cache'))).filter((f) => f.startsWith('map-')),
    ).toEqual([]);
    expectError(await share({ section: '1' }), /PDF/);
    const sent = await share();
    expect(facts(sent).sha256).toBe(sha(bytes));
    expect(
      Buffer.from((await client.readResource({ uri: link(sent).uri })).contents[0].blob, 'base64'),
    ).toEqual(bytes);
  },
);
test('malformed EPUB retains the existing map reply', async () => {
  delete process.env.ZLIBRARY_PDF_PYTHON;
  await connect();
  const r = await call('get_book_map', { file: 'book.epub' });
  expect(r.isError).toBeUndefined();
  expect(facts(r).sections).toEqual([]);
  expect(facts(r).guidance).toBe('Send whole if it fits; EPUB splitting is not supported yet.');
});
test('invalid max_bytes setting returns explicit error, including just below and above bounds', async () => {
  await connect();
  for (const value of ['999999', '33554433', 'NaN', '1.2']) {
    process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = value;
    expectError(await share(), /ZLIBRARY_CHAT_FILES_MAX_BYTES/);
  }
});
pdfTest('partial send is explicitly incomplete alongside the remaining pages', async () => {
  await generated();
  process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = '1100000';
  await connect();
  const r = await share({ file: 'nested.pdf', pages: '1-12' });
  expect(link(r)).toBeDefined();
  expect(facts(r).remaining).toMatch(/^\d+-12$/);
  expect(facts(r).complete).toBe(false);
});
pdfTest('map sizes, section resolution, partial send, cap and labels', async () => {
  await generated();
  process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES = '1100000';
  await connect();
  const m = facts(await call('get_book_map', { file: 'nested.pdf' }));
  expect(m.pages).toBe(12);
  expect(m.text_layer).toBe(1);
  expect(m.guidance).toMatch(
    /text_layer is the fraction of sampled pages with non-whitespace extracted text/,
  );
  expect(m.guidance).toMatch(/up to 20 evenly spaced pages/);
  expect(m.guidance).toMatch(/text_layer_sample_pages/);
  expect(m.guidance).toMatch(/not OCR accuracy/);
  expect(m.guidance).toMatch(
    /starts_at_section identifies the first section heading on the part's first page/,
  );
  expect(m.guidance).toMatch(/largest section boundary at any depth, including subsections/);
  expect(m.guidance).toMatch(/at least 60% of the furthest fitting span/);
  expect(m.guidance).toMatch(/otherwise they end at the furthest fitting page/);
  expect(m.guidance).toMatch(/A part can start at a subsection in the middle of a chapter/);
  expect(m.fits_whole).toBe(false);
  expect(m.sections[1].fits).toBe(false);
  expect(m.guidance).toMatch(/estimates with a 5% margin/);
  expect(link(await share({ file: 'nested.pdf' }))).toBeUndefined();
  for (const part of m.suggested_parts) {
    const r = await share({ file: 'nested.pdf', pages: part.pdf_pages });
    // Estimated parts never overflow: the exact send is the whole part.
    expect(facts(r).pdf_pages).toBe(part.pdf_pages);
    expect(facts(r).remaining).toBeUndefined();
    expect(facts(r).complete).toBe(true);
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
  expect(j.complete).toBe(false);
  expect(j.pdf_pages).toMatch(/^1-\d+$/);
  expect(j.bytes).toBeLessThanOrEqual(1100000);
  const section = await share({ file: 'nested.pdf', section: '2.1' });
  // 2.1 ends on the start page of 2.2 (3-7); over this cap, so the exact send
  // keeps the fitting prefix and reports the overlap page as remaining.
  expect(m.sections[1].children[0]).toMatchObject({ pdf_pages: '3-7', fits: false });
  expect(facts(section).pdf_pages).toBe('3-6');
  expect(facts(section).remaining).toBe('7');
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
    expect((await share({ file: 'nested.pdf', pages })).content[0].text).not.toMatch(
      /request was valid/i,
    );
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
  expect(m.repair_messages).toBeGreaterThan(0);
  expect(m.guidance).toMatch(/repair_messages counts nonempty MuPDF warning lines/);
  expect(m.guidance).toMatch(/not distinct repairs/);
  expect(m).not.toHaveProperty('repairs');
  expect(m).not.toHaveProperty('repaired');
  const r = await share({ file: 'malformed.pdf', pages: '1-3' });
  expect(link(r)).toBeDefined();
  expect(facts(r).pdf_pages).toBe('1-3');
});
pdfTest('clean and cached PDF maps expose zero repairs with current guidance', async () => {
  await generated();
  await connect();
  const m = facts(await call('get_book_map', { file: 'plain.pdf' }));
  expect(m.repair_messages).toBe(0);
  expect(m.guidance).toMatch(/repair_messages counts nonempty MuPDF warning lines/);
  const cache = path.join(root, '.chat-files/cache');
  const file = (await fs.readdir(cache)).find((f) => f.startsWith('map-'));
  const legacy = { ...m, guidance: 'Old guidance' };
  delete legacy.repair_messages;
  await fs.writeFile(path.join(cache, file), JSON.stringify(legacy));
  await fakePython('exit 3'); // A metadata upgrade must not need to re-map the PDF.
  const cached = facts(await call('get_book_map', { file: 'plain.pdf' }));
  expect(cached).toEqual(m);
});
test('map cache v5 bypasses v4 plans and preserves existing file resources', async () => {
  await fs.writeFile(path.join(root, 'any.pdf'), 'PRIVATE');
  await connect();
  const uri = link(await share()).uri;
  const resource = await client.readResource({ uri });
  const target = await fs.realpath(path.join(root, 'any.pdf'));
  const info = await fs.stat(target);
  const cache = path.join(root, '.chat-files/cache');
  const oldKey = sha(JSON.stringify([target, info.size, info.mtimeMs, info.ctimeMs, 7750000, 4]));
  const oldMap = path.join(cache, `map-${oldKey}.json`);
  await fs.writeFile(
    oldMap,
    JSON.stringify({ pages: 999, suggested_parts: [{ pdf_pages: '1-999' }] }),
  );
  await fakePython(`echo '{"pages":2,"sections":[],"suggested_parts":[{"pdf_pages":"1-2"}]}'`);
  const m = facts(await call('get_book_map', { file: 'any.pdf' }));
  expect(m.pages).toBe(2);
  expect(m.suggested_parts).toEqual([{ pdf_pages: '1-2' }]);
  expect(JSON.parse(await fs.readFile(oldMap, 'utf8')).pages).toBe(999);
  expect(await client.readResource({ uri })).toEqual(resource);
  await fakePython('exit 3');
  expect(facts(await call('get_book_map', { file: 'any.pdf' }))).toEqual(m);
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
  expectError(await map(), /request was valid; the PDF helper failed/i);
  const plain = path.join(directory, 'not-executable');
  await fs.writeFile(plain, '#!/bin/sh\n', { mode: 0o644 });
  process.env.ZLIBRARY_PDF_PYTHON = plain;
  expectError(await map(), /could not start \(EACCES\); check ZLIBRARY_PDF_PYTHON/);
  expectError(await map(), /request was valid; the PDF helper failed/i);
  await fakePython('exit 3');
  let r = await map();
  expectError(r, /exited abnormally \(status 3\)/);
  expectError(r, /request was valid; the PDF helper failed/i);
  expect(r.content[0].text).not.toMatch(/ZLIBRARY_PDF_PYTHON|timed out|time/);
  const deadline = PDF_DEADLINES_MS.map;
  PDF_DEADLINES_MS.map = 300;
  try {
    await fakePython('exec sleep 30');
    r = await map();
    expectError(r, /PDF map did not finish within 0.3 s and was stopped/);
    expectError(r, /request was valid; the PDF helper failed/i);
    expect(r.content[0].text).not.toMatch(/ZLIBRARY_PDF_PYTHON|could not start/);
  } finally {
    PDF_DEADLINES_MS.map = deadline;
  }
});
test.each([
  ['echo not-json', /invalid result/],
  ['head -c 5242881 /dev/zero', /output exceeded/],
])('valid request distinguishes a failed helper: %s', async (body, cause) => {
  await fs.writeFile(path.join(root, 'any.pdf'), 'PRIVATE');
  await fakePython(body);
  await connect();
  const r = await call('get_book_map', { file: 'any.pdf' });
  expectError(r, cause);
  expectError(r, /request was valid; the PDF helper failed/i);
});
