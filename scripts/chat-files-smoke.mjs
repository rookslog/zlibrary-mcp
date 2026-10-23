#!/usr/bin/env node
/** Offline, real-book stdio check. --fifo adapts stdio to restricted sandboxes
 * where Node cannot recognize child socket handles; protocol bytes are unchanged.
 * No provider tools are called. Sources are read-only hardlinks in local-smoke-root.
 */
import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { EventEmitter } from 'node:events';
import { open, readFile, writeFile, mkdir, readdir, mkdtemp, unlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const wt = path.resolve(new URL('..', import.meta.url).pathname);
const root = path.resolve(process.env.ZLIBRARY_OUTPUT_ROOT ?? path.join(wt, 'local-smoke-root'));
const python = process.env.ZLIBRARY_PDF_PYTHON;
if (!python) throw new Error('Set ZLIBRARY_PDF_PYTHON to an interpreter with PyMuPDF');
const output = path.resolve(
  process.env.CHAT_FILES_SMOKE_REPORT ?? path.join(wt, 'chat-files-checks', 'smoke.json'),
);
const max = Number(process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES ?? 7750000);
const fifo = process.argv.includes('--fifo');
const cold = process.argv.includes('--cold');
const sha = (b) => createHash('sha256').update(b).digest('hex');
const bookNames = (await readdir(root)).filter(
  (n) => n.endsWith('.pdf') && /Kant|Heidegger|Glas|Johnson/.test(n),
);
assert.equal(
  bookNames.filter((n) => /Kant|Heidegger|Glas/.test(n)).length,
  3,
  'Expected Kant, Heidegger and Glas hardlinks (Johnson copies are optional)',
);
if (cold) {
  const cache = path.join(root, '.chat-files/cache');
  for (const name of await readdir(cache).catch(() => []))
    if (/^map-[a-f0-9]{64}\.json$/.test(name)) await unlink(path.join(cache, name));
}
const report = { date: new Date().toISOString(), fifo_adapter: fifo, max_bytes: max, books: [] };
const env = {
  PATH: process.env.PATH,
  HOME: process.env.HOME,
  ZLIBRARY_CHAT_FILES: '1',
  ZLIBRARY_OUTPUT_ROOT: root,
  ZLIBRARY_PDF_PYTHON: python,
  ...(process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES
    ? { ZLIBRARY_CHAT_FILES_MAX_BYTES: String(max) }
    : {}),
};
let child, incoming, send, pipes, pipeRoot;
if (fifo) {
  pipeRoot = await mkdtemp(path.join(tmpdir(), 'chat-files-fifo-'));
  await new Promise((resolve, reject) => {
    const p = spawn('mkfifo', [path.join(pipeRoot, 'in'), path.join(pipeRoot, 'out')]);
    p.on('error', reject);
    p.on('close', (c) => (c === 0 ? resolve() : reject(new Error('mkfifo failed'))));
  });
  pipes = await Promise.all(
    ['in', 'out'].map((n) => open(path.join(pipeRoot, n), constants.O_RDWR)),
  );
  child = spawn(process.execPath, [path.join(wt, 'dist/index.js')], {
    env,
    stdio: [pipes[0].fd, pipes[1].fd, 'pipe'],
  });
  incoming = new EventEmitter();
  incoming.stopped = false;
  incoming.destroy = () => {
    incoming.stopped = true;
  };
  const poll = async () => {
    while (!incoming.stopped) {
      try {
        const b = Buffer.alloc(65536);
        const { bytesRead } = await pipes[1].read(b, 0, b.length, null);
        if (bytesRead) incoming.emit('data', b.subarray(0, bytesRead));
      } catch (error) {
        if (error.code !== 'EAGAIN' && !incoming.stopped) throw error;
      }
      await new Promise((r) => setTimeout(r, 5));
    }
  };
  setTimeout(() => void poll(), 0);
  send = (line) => pipes[0].write(line);
} else {
  child = spawn(process.execPath, [path.join(wt, 'dist/index.js')], {
    env,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  incoming = child.stdout;
  send = (line) => child.stdin.write(line);
}
let buffer = '',
  nextId = 0;
const pending = new Map();
let stderr = '';
child.stderr.on('data', (b) => {
  stderr += b.toString();
});
child.on('error', (error) => {
  for (const p of pending.values()) p.reject(error);
});
child.on('exit', (code) => {
  for (const p of pending.values()) p.reject(new Error(`Server exited ${code}: ${stderr}`));
});
incoming.on('data', (chunk) => {
  buffer += chunk.toString();
  let index;
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    let m;
    try {
      m = JSON.parse(line);
    } catch {
      throw new Error('Non-protocol stdout');
    }
    const p = pending.get(m.id);
    if (p) {
      pending.delete(m.id);
      clearTimeout(p.timer);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    }
  }
});
const rpc = (method, params, timeout = 115000) =>
  new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timeout: ${method}`));
    }, timeout);
    pending.set(id, { resolve, reject, timer });
    send(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
const tool = async (name, args) => {
  const r = await rpc('tools/call', { name, arguments: args });
  assert.ok(!r.isError, JSON.stringify(r));
  return r;
};
const metadata = (r) => JSON.parse(r.content.find((c) => c.type === 'text').text);
const count = (nodes) => nodes.reduce((s, n) => s + 1 + count(n.children ?? []), 0);
try {
  await rpc(
    'initialize',
    {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'chat-files-offline-smoke', version: '1' },
    },
    5000,
  );
  await send(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  for (const file of bookNames) {
    const start = performance.now();
    const map = metadata(await tool('get_book_map', { file }));
    const coldMs = performance.now() - start;
    assert.ok(coldMs < 60000, `Cold map over 60s: ${file} ${coldMs}`);
    const warmStart = performance.now();
    await tool('get_book_map', { file });
    const warmMs = performance.now() - warmStart;
    assert.ok(warmMs < 5000);
    const row = {
      file,
      pages: map.pages,
      cold_ms: Math.round(coldMs),
      warm_ms: Math.round(warmMs),
      sections: count(map.sections),
      parts: [],
      safe_window_pages: map.safe_window_pages,
      safe_window_estimated: map.safe_window_estimated,
      repair_messages: map.repair_messages ?? 0,
      round_trip: null,
      image: null,
    };
    for (const part of map.suggested_parts) {
      const r = await tool('share_book_file', { file, pages: part.pdf_pages });
      const j = metadata(r),
        link = r.content.find((c) => c.type === 'resource_link');
      assert.ok(link);
      // The map's sizes are estimates; the send is exact and must not overflow.
      assert.ok(j.bytes <= max);
      assert.equal(j.pdf_pages, part.pdf_pages, 'estimated part overflowed its exact send');
      assert.ok(!j.remaining);
      const read = await rpc('resources/read', { uri: link.uri });
      const bytes = Buffer.from(read.contents[0].blob, 'base64');
      assert.equal(bytes.length, j.bytes);
      assert.equal(sha(bytes), j.sha256);
      row.parts.push({
        pdf_pages: j.pdf_pages,
        printed_pages: j.printed_pages,
        bytes: bytes.length,
        estimated_bytes: part.estimated_bytes,
        estimate_error: Number((part.estimated_bytes / bytes.length - 1).toFixed(4)),
        max_bytes: max,
        sha256: j.sha256,
        subsetting: j.subsetting,
      });
      row.round_trip ??= {
        pdf_pages: j.pdf_pages,
        sha256: j.sha256,
        bytes: bytes.length,
        match: true,
      };
    }
    const partial = await tool('share_book_file', { file, pages: `1-${map.pages}` });
    const p = metadata(partial);
    assert.ok(p.bytes <= max);
    assert.ok(p.remaining);
    row.partial = { pdf_pages: p.pdf_pages, remaining: p.remaining, bytes: p.bytes };
    const image = await tool('get_page_image', { file, page: 1 });
    assert.equal(image.content[0].type, 'image');
    row.image = {
      metadata: image.content[1].text,
      sha256: sha(Buffer.from(image.content[0].data, 'base64')),
    };
    report.books.push(row);
    process.stdout.write(
      `${file}: map ${row.cold_ms} ms, ${row.sections} sections, ${row.parts.length} parts, safe ${row.safe_window_pages}\n`,
    );
  }
  report.ok = true;
} finally {
  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  for (const p of pending.values()) clearTimeout(p.timer);
  child.kill();
  if (fifo) {
    await pipes[1].write('\n');
    incoming.destroy();
    await Promise.all(pipes.map((p) => p.close()));
  }
}
