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

// Mutations caught: flag leakage, containment/cap bypass, changed bytes, unbounded
// resource ids, stale ids, ignored page/DPI bounds, or leaking document text.
const python = process.env.ZLIBRARY_PROBE_PYTHON;
const imageTest = python ? test : test.skip;
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
let previous, directory, root, client, server;
beforeEach(async () => {
  previous = { ...process.env };
  directory = await fs.mkdtemp(path.join(os.tmpdir(), 'handoff-test-'));
  root = path.join(directory, 'root');
  await fs.mkdir(root);
  await fs.writeFile(
    path.join(root, 'sample.pdf'),
    Buffer.from('%PDF-1.4\nPRIVATE_CONTENT\n\x00\xff', 'latin1'),
  );
  await fs.writeFile(path.join(directory, 'outside.pdf'), 'OUTSIDE');
  process.env.ZLIBRARY_OUTPUT_ROOT = root;
  process.env.ZLIBRARY_PROBE_TOOLS = '1';
});
afterEach(async () => {
  await client?.close();
  await server?.close();
  client = server = undefined;
  jest.restoreAllMocks();
  process.env = previous;
  await fs.rm(directory, { recursive: true, force: true });
});
async function connect() {
  server = createServer();
  client = new Client({ name: 'handoff-contract', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(b), client.connect(a)]);
}
const call = (name, args) => client.callTool({ name, arguments: args });
const file = (args = {}) =>
  call('probe_file_link', { path: 'sample.pdf', mode: 'resource_link', ...args });
const image = (args = {}) => call('probe_page_image', { path: 'sample.pdf', page: 1, ...args });
function expectError(result, pattern) {
  expect(result.isError).toBe(true);
  expect(result.content).toEqual([{ type: 'text', text: expect.stringMatching(pattern) }]);
  expect(result.content[0].text).toMatch(/^Probe error: /);
}

test.each([undefined, '0', 'true', '01'])(
  'flag %s preserves exact frozen tools/list and no resources capability',
  async (flag) => {
    if (flag === undefined) delete process.env.ZLIBRARY_PROBE_TOOLS;
    else process.env.ZLIBRARY_PROBE_TOOLS = flag;
    await connect();
    const baseline = (
      await fs.readFile(new URL('./fixtures/handoff-tools-baseline.json', import.meta.url), 'utf8')
    ).trim();
    expect(JSON.stringify(await client.listTools())).toBe(baseline);
    expect(client.getServerCapabilities().resources).toBeUndefined();
    expect((await file()).isError).toBe(true);
  },
);

test('flag adds exactly the two probes and resources/read capability', async () => {
  await connect();
  expect((await client.listTools()).tools.map((t) => t.name)).toEqual([
    'search',
    'get_book',
    'download',
    'get_limits',
    'get_history',
    'probe_file_link',
    'probe_page_image',
  ]);
  expect(client.getServerCapabilities().resources).toEqual({});
});

test.each([
  '../outside.pdf',
  'nested/../../outside.pdf',
  '/etc/passwd',
  'C:\\outside.pdf',
  '\\outside.pdf',
  'bad\0name',
])('refuses unsafe path %s for both probes', async (unsafe) => {
  await connect();
  expectError(await file({ path: unsafe }), /relative|outside|path/i);
  expectError(await image({ path: unsafe }), /relative|outside|path/i);
});
test('refuses absolute paths even when they name a file inside the root', async () => {
  await connect();
  expectError(await file({ path: path.join(root, 'sample.pdf') }), /relative|path/i);
});
test('refuses symlinks escaping root and non-regular files for both probes', async () => {
  await fs.symlink(path.join(directory, 'outside.pdf'), path.join(root, 'escape.pdf'));
  await fs.symlink(directory, path.join(root, 'escape-dir'));
  await connect();
  for (const unsafe of ['escape.pdf', 'escape-dir/outside.pdf', '.']) {
    expectError(await file({ path: unsafe }), /outside|regular/i);
    expectError(await image({ path: unsafe }), /outside|regular/i);
  }
});
test('allows nested files and symlinks that stay inside the real root', async () => {
  await fs.mkdir(path.join(root, 'nested'));
  await fs.symlink(path.join(root, 'sample.pdf'), path.join(root, 'nested', 'inside.pdf'));
  await connect();
  expect((await file({ path: 'nested/inside.pdf' })).isError).not.toBe(true);
});
test('requires explicit output root and gives neutral errors for missing files', async () => {
  await connect();
  expectError(await file({ path: 'missing.pdf' }), /file|path/i);
  delete process.env.ZLIBRARY_OUTPUT_ROOT;
  expectError(await file(), /ZLIBRARY_OUTPUT_ROOT/);
});
test('accepts 6 MiB and refuses even one extra byte in both probes', async () => {
  const large = path.join(root, 'large.pdf');
  await fs.writeFile(large, Buffer.alloc(6 * 1024 * 1024));
  await connect();
  const linked = await file({ path: 'large.pdf' });
  expect(linked.isError).not.toBe(true);
  const reply = await client.readResource({ uri: linked.content[0].uri });
  expect(Buffer.from(reply.contents[0].blob, 'base64')).toHaveLength(6 * 1024 * 1024);
  expect(Buffer.byteLength(JSON.stringify(reply))).toBeLessThan(10 * 1024 * 1024);
  await fs.appendFile(large, Buffer.from([0]));
  expectError(await file({ path: 'large.pdf' }), /6 MiB/);
  expectError(await image({ path: 'large.pdf' }), /6 MiB/);
});
test.each(['resource_link', 'embedded'])(
  '%s contains unchanged bytes and only neutral metadata text',
  async (mode) => {
    await connect();
    const original = await fs.readFile(path.join(root, 'sample.pdf'));
    const result = await file({ mode });
    expect(result.isError).not.toBe(true);
    expect(result.content).toHaveLength(2);
    const payload = result.content[0];
    let resource;
    if (mode === 'resource_link') {
      expect(payload).toEqual({
        type: 'resource_link',
        uri: expect.stringMatching(/^zlib-probe:\/\/file\/[a-zA-Z0-9-]+$/),
        mimeType: 'application/pdf',
        name: 'sample.pdf',
        size: original.length,
      });
      resource = (await client.readResource({ uri: payload.uri })).contents[0];
      // Resource ids hold immutable bytes even if the source is subsequently replaced.
      await fs.writeFile(path.join(root, 'sample.pdf'), 'replacement');
      expect((await client.readResource({ uri: payload.uri })).contents[0]).toEqual(resource);
    } else {
      expect(payload.type).toBe('resource');
      resource = payload.resource;
    }
    expect(resource).toEqual({
      uri: expect.stringMatching(/^zlib-probe:\/\/file\//),
      mimeType: 'application/pdf',
      blob: original.toString('base64'),
    });
    expect(sha(Buffer.from(resource.blob, 'base64'))).toBe(sha(original));
    expect(result.content[1]).toEqual({
      type: 'text',
      text: JSON.stringify({ name: 'sample.pdf', bytes: original.length, sha256: sha(original) }),
    });
    expect(result.content[1].text).not.toContain('PRIVATE_CONTENT');
  },
);
test('unknown and expired ids are refused, with 30 minutes as the exact expiry', async () => {
  await connect();
  const now = Date.now();
  const clock = jest.spyOn(Date, 'now').mockReturnValue(now);
  const uri = (await file()).content[0].uri;
  await expect(client.readResource({ uri: 'zlib-probe://file/unknown' })).rejects.toThrow(
    /unknown|expired/i,
  );
  clock.mockReturnValue(now + 30 * 60 * 1000 - 1);
  expect((await client.readResource({ uri })).contents).toHaveLength(1);
  clock.mockReturnValue(now + 30 * 60 * 1000);
  await expect(client.readResource({ uri })).rejects.toThrow(/unknown|expired/i);
});
test('retains at most 20 ids and embedded responses do not consume that capacity', async () => {
  await connect();
  const first = (await file()).content[0].uri;
  for (let i = 0; i < 19; i++) await file();
  await file({ mode: 'embedded' });
  expect((await client.readResource({ uri: first })).contents).toHaveLength(1);
  const latest = (await file()).content[0].uri;
  await expect(client.readResource({ uri: first })).rejects.toThrow(/unknown|expired/i);
  expect((await client.readResource({ uri: latest })).contents).toHaveLength(1);
});
test.each([{ mode: 'bad' }, { path: 1 }, { unexpected: true }])(
  'refuses invalid file arguments %j',
  async (args) => {
    await connect();
    expectError(await file(args), /argument/i);
  },
);
test('reports missing renderer configuration clearly', async () => {
  delete process.env.ZLIBRARY_PROBE_PYTHON;
  await connect();
  expectError(await image(), /ZLIBRARY_PROBE_PYTHON/);
});
test.each([{ page: 0 }, { page: 1.5 }, { dpi: 1.5 }, { page: '1' }, { dpi: '100' }])(
  'refuses invalid image arguments %j',
  async (args) => {
    await connect();
    expectError(await image(args), /argument/i);
  },
);

async function generatePdf(width = 720, height = 360) {
  await promisify(execFile)(python, [
    '-I',
    '-B',
    '-c',
    'import fitz,sys; d=fitz.open(); p=d.new_page(width=int(sys.argv[2]),height=int(sys.argv[3])); p.insert_text((20,30), "PRIVATE_PAGE_TEXT"); d.save(sys.argv[1])',
    path.join(root, 'sample.pdf'),
    String(width),
    String(height),
  ]);
}
imageTest.each([
  [undefined, 1000, 500],
  [1, 500, 250],
  [999, 1600, 800],
])(
  'real one-page PDF renders at dpi %s with dimensions and no extracted text',
  async (dpi, expectedWidth, expectedHeight) => {
    await generatePdf();
    await connect();
    const result = await image(dpi === undefined ? {} : { dpi });
    expect(result.isError).not.toBe(true);
    expect(result.content).toHaveLength(2);
    const [block, text] = result.content;
    expect(block.type).toBe('image');
    expect(block.mimeType).toBe('image/png');
    const bytes = Buffer.from(block.data, 'base64');
    expect(bytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    expect(bytes.readUInt32BE(16)).toBe(expectedWidth);
    expect(bytes.readUInt32BE(20)).toBe(expectedHeight);
    expect(bytes.length).toBeLessThanOrEqual(3 * 1024 * 1024);
    expect(text).toEqual({
      type: 'text',
      text: `page 1 of 1, ${expectedWidth}x${expectedHeight} px, ${bytes.length} bytes`,
    });
  },
);
imageTest(
  'renderer rejects out-of-range pages and malformed PDFs without echoing contents',
  async () => {
    await generatePdf();
    await connect();
    expectError(await image({ page: 2 }), /page.*range/i);
    await fs.writeFile(path.join(root, 'sample.pdf'), 'PRIVATE_BAD_PDF');
    const result = await image();
    expectError(result, /render|PDF/i);
    expect(JSON.stringify(result)).not.toContain('PRIVATE_BAD_PDF');
  },
);
imageTest('noisy page falls back to JPEG below 3 MiB and 1600 px', async () => {
  await promisify(execFile)(python, [
    '-I',
    '-B',
    '-c',
    'import fitz,random,sys; d=fitz.open(); p=d.new_page(width=720,height=720); b=random.Random(7).randbytes(1400*1400*3); pix=fitz.Pixmap(fitz.csRGB,1400,1400,b,False); p.insert_image(p.rect,pixmap=pix); d.save(sys.argv[1])',
    path.join(root, 'sample.pdf'),
  ]);
  await connect();
  const result = await image({ dpi: 200 });
  expect(result.isError).not.toBe(true);
  const [block, text] = result.content;
  expect(block.mimeType).toBe('image/jpeg');
  const bytes = Buffer.from(block.data, 'base64');
  expect(bytes.subarray(0, 2)).toEqual(Buffer.from([255, 216]));
  expect(bytes.length).toBeLessThanOrEqual(3 * 1024 * 1024);
  expect(text.text).toBe(`page 1 of 1, 1600x1600 px, ${bytes.length} bytes`);
});
test('renderer launch errors return a clear neutral error', async () => {
  process.env.ZLIBRARY_PROBE_PYTHON = path.join(directory, 'nonexistent-python');
  await connect();
  expectError(await image(), /renderer.*process error/i);
});
imageTest('three concurrent probe renders queue instead of returning busy', async () => {
  await generatePdf();
  await connect();
  const results = await Promise.all([image(), image(), image()]);
  for (const result of results) {
    expect(result.isError).not.toBe(true);
    expect(result.content[0].type).toBe('image');
  }
});
