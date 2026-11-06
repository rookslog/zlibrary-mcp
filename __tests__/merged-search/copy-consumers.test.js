import { afterEach, expect, test } from '@jest/globals';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
const runFile = promisify(execFile),
  created = [];
afterEach(() => {
  for (const dir of created.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});
const env = {
  ZLIBRARY_EMAIL: 'synthetic.invalid',
  ZLIBRARY_PASSWORD: 'fixture-only',
  BOOK_SOURCE_DEFAULT_ORDER: 'libgen,zlibrary,annas',
};
const md5 = 'a'.repeat(32),
  sha256 = 'b'.repeat(64);
const raw = (source, extra = {}) => ({
  source,
  id: source,
  hash: 'opaque-' + source,
  md5,
  sha256,
  title: 'Solaris',
  author: 'Lem',
  extension: 'pdf',
  language: source === 'libgen' ? 'French' : source === 'zlibrary' ? 'English' : undefined,
  publisher: source === 'libgen' ? 'A' : 'B',
  ...extra,
});
const answer = (operation, args) => ({
  books: [raw(operation === 'search' ? 'zlibrary' : args.source)],
});
const success = {
  path: '/synthetic/book.pdf',
  bytes: 1,
  reused: false,
  transfer_skipped: false,
  verified: 'sha256',
  record: '/synthetic/book.acq.json',
};
function fixture(options = {}) {
  const calls = [];
  const tools = new AcquisitionTools(
    async (operation, args) => {
      calls.push({ operation, args });
      if (operation === 'get_book_metadata_complete')
        return { description: 'Selected detail', publisher: 'B' };
      if (operation === 'acquire_book') return success;
      return answer(operation, args);
    },
    env,
    options,
  );
  return { tools, calls };
}
const refs = (reply) => [reply.rows[0], ...(reply.rows[0].other_copies ?? [])];

test('F40 each copy detail retains its metadata and alternatives with only the selected Z detail call', async () => {
  const { tools, calls } = fixture();
  const search = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  const entries = refs(search);
  expect(entries.map((c) => c.source)).toEqual(['libgen', 'zlibrary']);
  const first = await tools.call('get_book', { ref: entries[0].ref });
  expect(first.source).toBe('libgen');
  expect(first.language).toBe('French');
  expect(first.other_copies[0]).toMatchObject({
    ref: entries[1].ref,
    source: 'zlibrary',
    language: 'English',
    identifiers: { md5, sha256 },
  });
  expect(first.disagreements).toContainEqual({
    field: 'language',
    values: [
      { source: 'libgen', value: 'French' },
      { source: 'zlibrary', value: 'English' },
    ],
  });
  const second = await tools.call('get_book', { ref: entries[1].ref });
  expect(second.source).toBe('zlibrary');
  expect(second.identifiers).toMatchObject({
    id: 'zlibrary',
    hash: 'opaque-zlibrary',
    md5,
    sha256,
  });
  expect(second.other_copies[0]).toMatchObject({
    ref: entries[0].ref,
    source: 'libgen',
    language: 'French',
  });
  expect(calls.filter((c) => c.operation === 'get_book_metadata_complete')).toHaveLength(1);
  expect(calls).toHaveLength(3);
});
test('A3 details and failure alternatives distinguish pass, fail and unknown under the remembered filters', async () => {
  const { tools, calls } = fixture();
  const reply = await tools.call('search', {
    query: 'Solaris',
    sources: ['libgen', 'zlibrary', 'annas'],
    languages: ['fr'],
  });
  const entries = refs(reply);
  const rejected = entries.find((c) => c.source === 'zlibrary');
  const detail = await tools.call('get_book', { ref: rejected.ref });
  expect(detail.filter).toBe('fail');
  expect(detail.other_copies.find((c) => c.source === 'libgen')).toMatchObject({
    filter: 'pass',
    language: 'French',
  });
  expect(detail.other_copies.find((c) => c.source === 'annas')).toMatchObject({
    filter: 'unknown',
    unknown: ['language'],
  });
  tools.invoke = async (operation, args) => {
    calls.push({ operation, args });
    throw {
      code: 'BRIDGE_TIMEOUT',
      context: { reason: 'timeout', details: { request_issued: true } },
    };
  };
  const failed = await tools.call('download', { ref: rejected.ref });
  expect(failed.error.code).toBe('outcome_unknown');
  expect(failed.error.source).toBe('zlibrary');
  expect(failed.error.detail.other_copies.find((c) => c.source === 'libgen')).toMatchObject({
    filter: 'pass',
  });
  expect(failed.error.detail.other_copies.find((c) => c.source === 'annas')).toMatchObject({
    filter: 'unknown',
    unknown: ['language'],
  });
  expect(failed).not.toHaveProperty('path');
  expect(calls.at(-1).args.book_details.source).toBe('zlibrary');
  expect(calls.filter((c) => c.operation === 'acquire_book')).toHaveLength(1);
});
test('likely editions are bounded at ten and never replace the selected copy fields', async () => {
  const tools = new AcquisitionTools(
    async () => ({
      books: Array.from({ length: 14 }, (_, i) =>
        raw('libgen', { id: String(i), md5: i.toString(16).padStart(32, '0'), year: 1900 + i }),
      ),
    }),
    {},
  );
  const search = await tools.call('search', { query: 'Solaris', sources: ['libgen'], limit: 50 });
  const detail = await tools.call('get_book', { ref: search.rows[0].ref });
  expect(detail.year).toBe(1900);
  expect(detail.likely_editions).toHaveLength(10);
  expect(detail.likely_editions.every((c) => c.ref !== detail.ref)).toBe(true);
  expect(new Set(detail.likely_editions.map((c) => c.ref)).size).toBe(10);
});
test('F41 expired relationship context omits its keys while the selected copy still answers locally', async () => {
  const { tools, calls } = fixture({ relationshipCapacity: 1 });
  const first = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  await tools.call('search', { query: 'Other', sources: ['libgen'] });
  const before = calls.length;
  const detail = await tools.call('get_book', { ref: first.rows[0].ref });
  expect(detail.source).toBe('libgen');
  for (const key of ['other_copies', 'disagreements', 'likely_editions'])
    expect(detail).not.toHaveProperty(key);
  expect(calls).toHaveLength(before);
});
test('F42 choosing an alternate downloads that exact source once and reports it on success', async () => {
  const { tools, calls } = fixture();
  const search = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  const selected = refs(search).find((c) => c.source === 'zlibrary');
  const downloaded = await tools.call('download', { ref: selected.ref });
  expect(downloaded).toMatchObject({ ...success, source: 'zlibrary' });
  const acquired = calls.filter((c) => c.operation === 'acquire_book');
  expect(acquired).toHaveLength(1);
  expect(acquired[0].args).toMatchObject({
    book_details: { source: 'zlibrary', id: 'zlibrary', hash: 'opaque-zlibrary' },
    request: { ref: selected.ref, query: 'Solaris' },
  });
});

function receiptFixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'merged-receipts-'));
  created.push(dir);
  let mode = 'normal';
  const calls = [];
  const tools = new AcquisitionTools(async (operation, args) => {
    calls.push({ operation, args });
    if (operation !== 'acquire_book') return answer(operation, args);
    const childEnv = { ...process.env, ZLIBRARY_OUTPUT_ROOT: dir };
    delete childEnv.ZLIBRARY_UNRESTRICTED_OUTPUT;
    delete childEnv.ZLIBRARY_EMAIL;
    delete childEnv.ZLIBRARY_PASSWORD;
    const { stdout } = await runFile(
      '.venv/bin/python',
      ['__tests__/merged-search/receipt-worker.py', JSON.stringify(args), dir, mode],
      { env: childEnv, maxBuffer: 1024 * 1024 },
    );
    const result = JSON.parse(stdout);
    if (result.synthetic_error) {
      if (result.synthetic_error.reason === 'outcome_unknown')
        throw {
          code: 'BRIDGE_TIMEOUT',
          context: { reason: 'timeout', details: { request_issued: true } },
        };
      throw { context: { details: result.synthetic_error } };
    }
    return result;
  }, env);
  return { dir, tools, calls, setMode: (value) => (mode = value) };
}
const sidecar = (dir) =>
  fs
    .readdirSync(dir)
    .filter((f) => f.endsWith('.acq.json'))
    .map((f) => path.join(dir, f));
const transfers = (dir) =>
  fs.existsSync(path.join(dir, 'transfers'))
    ? fs.readFileSync(path.join(dir, 'transfers'), 'utf8').trim().split('\n')
    : [];
const snapshot = (dir) =>
  Object.fromEntries(
    fs
      .readdirSync(dir)
      .sort()
      .map((name) => [name, fs.readFileSync(path.join(dir, name), 'hex')]),
  );

test('F27 same identity reuses intact receipt; foreign same-MD5 identity refuses unchanged bytes and provenance', async () => {
  const f = receiptFixture();
  const search = await f.tools.call('search', {
    query: 'Solaris',
    sources: ['libgen', 'zlibrary'],
  });
  const entries = refs(search),
    first = entries.find((c) => c.source === 'libgen'),
    foreign = entries.find((c) => c.source === 'zlibrary');
  const args = { ref: first.ref, output_dir: f.dir, filename: 'chosen.pdf' };
  const initial = await f.tools.call('download', args);
  expect(initial.source).toBe('libgen');
  expect(initial.transfer_skipped).toBe(false);
  const reused = await f.tools.call('download', args);
  expect(reused.transfer_skipped).toBe(true);
  expect(transfers(f.dir)).toEqual(['libgen']);
  const before = snapshot(f.dir);
  const refused = await f.tools.call('download', { ...args, ref: foreign.ref });
  expect(refused.error).toMatchObject({ code: 'output_refused', source: 'zlibrary' });
  expect(refused.error.detail.other_copies[0]).toMatchObject({ ref: first.ref, source: 'libgen' });
  expect(snapshot(f.dir)).toEqual(before);
  expect(transfers(f.dir)).toEqual(['libgen']);
  expect(f.calls.filter((c) => c.operation === 'acquire_book')).toHaveLength(3);
});
test.each(['cancelled', 'refused'])(
  'M13 %s before intent creates no receipt or source transfer',
  async (phase) => {
    const f = receiptFixture();
    const search = await f.tools.call('search', {
      query: 'Solaris',
      sources: ['libgen', 'zlibrary'],
    });
    const selected = search.rows[0].ref;
    let result;
    if (phase === 'cancelled') {
      const controller = new AbortController();
      controller.abort();
      result = await f.tools.call(
        'download',
        { ref: selected, output_dir: f.dir, filename: 'chosen.pdf' },
        { signal: controller.signal },
      );
      expect(result.error.code).toBe('cancelled');
      expect(f.calls.filter((c) => c.operation === 'acquire_book')).toHaveLength(0);
    } else {
      result = await f.tools.call('download', {
        ref: selected,
        output_dir: f.dir,
        filename: '../invalid.pdf',
      });
      expect(result.error.code).toBe('invalid_argument');
      expect(f.calls.filter((c) => c.operation === 'acquire_book')).toHaveLength(1);
    }
    expect(sidecar(f.dir)).toEqual([]);
    expect(transfers(f.dir)).toEqual([]);
    expect(fs.readdirSync(f.dir)).toEqual([]);
  },
);
test.each(['issued', 'checkpoint'])(
  'M13 %s failure keeps the real intent/recovery state and offers alternatives without substitution',
  async (phase) => {
    const f = receiptFixture();
    const search = await f.tools.call('search', {
      query: 'Solaris',
      sources: ['libgen', 'zlibrary'],
    });
    f.setMode(phase);
    fs.writeFileSync(path.join(f.dir, 'prior.bin'), 'unchanged');
    const result = await f.tools.call('download', {
      ref: search.rows[0].ref,
      output_dir: f.dir,
      filename: 'chosen.pdf',
    });
    expect(result.error.source).toBe('libgen');
    expect(result.error.detail.other_copies[0].source).toBe('zlibrary');
    expect(result).not.toHaveProperty('path');
    expect(fs.readFileSync(path.join(f.dir, 'prior.bin'), 'utf8')).toBe('unchanged');
    expect(transfers(f.dir)).toEqual(['libgen']);
    expect(f.calls.filter((c) => c.operation === 'acquire_book')).toHaveLength(1);
    const record = JSON.parse(fs.readFileSync(sidecar(f.dir)[0], 'utf8'));
    expect(record.state).toBe('intent');
    expect(record.source).toBe('libgen');
    expect(fs.existsSync(path.join(f.dir, 'chosen.pdf'))).toBe(false);
    if (phase === 'checkpoint')
      expect(record.sha256).toBe(
        createHash('sha256')
          .update(fs.readFileSync(path.join(f.dir, '.synthetic-stage')))
          .digest('hex'),
      );
    else expect(record.sha256).toBeNull();
  },
);
test.each(['complete', 'reused'])(
  'M13 lost reply with %s artifact leaves authoritative bytes and provenance intact',
  async (phase) => {
    const f = receiptFixture();
    const search = await f.tools.call('search', {
      query: 'Solaris',
      sources: ['libgen', 'zlibrary'],
    });
    const args = { ref: search.rows[0].ref, output_dir: f.dir, filename: 'chosen.pdf' };
    await f.tools.call('download', args);
    const before = snapshot(f.dir);
    f.setMode('lost');
    const failed = await f.tools.call('download', args);
    expect(failed.error.code).toBe('outcome_unknown');
    expect(failed.error.detail.other_copies[0].source).toBe('zlibrary');
    expect(snapshot(f.dir)).toEqual(before);
    expect(JSON.parse(fs.readFileSync(sidecar(f.dir)[0], 'utf8')).state).toBe('complete');
    expect(transfers(f.dir)).toEqual(['libgen']);
    expect(failed).not.toHaveProperty('path');
    if (phase === 'reused') {
      await f.tools.call('download', args);
      expect(snapshot(f.dir)).toEqual(before);
      expect(transfers(f.dir)).toEqual(['libgen']);
    }
  },
);

test('in-flight selected detail survives parent eviction and omits expired sibling refs', async () => {
  let release;
  const tools = new AcquisitionTools(
    async (operation, args) =>
      operation === 'get_book_metadata_complete'
        ? new Promise((resolve) => (release = resolve))
        : answer(operation, args),
    env,
    { recordCapacity: 1 },
  );
  const first = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  const selected = refs(first).find((c) => c.source === 'zlibrary');
  const pending = tools.call('get_book', { ref: selected.ref });
  await Promise.resolve();
  await tools.call('search', { query: 'Other', sources: ['libgen'] });
  release({ title: 'Fresh detail' });
  const reply = await pending;
  expect(reply.source).toBe('zlibrary');
  expect(reply.title).toBe('Fresh detail');
  expect(reply.identifiers.hash).toBe('opaque-zlibrary');
  expect(reply).not.toHaveProperty('other_copies');
  expect((await tools.call('get_book', { ref: selected.ref })).error.code).toBe('ref_unavailable');
});
test('eviction during a failed transfer cannot replace its source failure with a relation-cache error', async () => {
  let reject;
  const tools = new AcquisitionTools(
    async (operation, args) =>
      operation === 'acquire_book'
        ? new Promise((_resolve, fail) => (reject = fail))
        : answer(operation, args),
    env,
    { recordCapacity: 1 },
  );
  const first = await tools.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  const pending = tools.call('download', { ref: first.rows[0].ref });
  await Promise.resolve();
  await tools.call('search', { query: 'Other', sources: ['libgen'] });
  reject({ context: { details: { reason: 'read_timeout', request_issued: true } } });
  const reply = await pending;
  expect(reply.error).toMatchObject({
    code: 'provider_timeout',
    source: 'libgen',
    detail: { reason: 'read_timeout' },
  });
  expect(reply.error.detail).not.toHaveProperty('other_copies');
});

test('fresh detail cannot retarget the immutable source, selection hash or next transfer', async () => {
  const calls = [];
  const tools = new AcquisitionTools(async (operation, args) => {
    calls.push({ operation, args });
    if (operation === 'get_book_metadata_complete')
      return {
        source: 'annas',
        id: 'forged',
        hash: 'forged',
        md5: 'c'.repeat(32),
        title: 'Fresh metadata',
      };
    if (operation === 'acquire_book') return success;
    return answer(operation, args);
  }, env);
  const search = await tools.call('search', { query: 'Solaris', sources: ['zlibrary'] });
  const selected = search.rows[0].ref;
  const detail = await tools.call('get_book', { ref: selected });
  expect(detail.source).toBe('zlibrary');
  expect(detail.identifiers).toMatchObject({ id: 'zlibrary', hash: 'opaque-zlibrary', md5 });
  await tools.call('download', { ref: selected });
  expect(calls.at(-1).args.book_details).toMatchObject({
    source: 'zlibrary',
    id: 'zlibrary',
    hash: 'opaque-zlibrary',
    md5,
  });
});
