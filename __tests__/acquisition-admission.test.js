import './helpers/environment-login.js';
import { afterAll, afterEach, beforeAll, beforeEach, expect, jest, test } from '@jest/globals';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { Rehearsal } from './rehearsal/harness.js';

jest.setTimeout(30000);
const actualRunner = await import('../dist/lib/python-runner.js');
let demands = 0;
jest.unstable_mockModule('../lib/python-runner.js', () => ({
  ...actualRunner,
  runPythonBridge: (script, options, runOptions) =>
    actualRunner.runPythonBridge(script, options, {
      ...runOptions,
      ...(runOptions?.onSessionNeeded
        ? {
            onSessionNeeded: (control) => {
              demands++;
              return runOptions.onSessionNeeded(control);
            },
          }
        : {}),
    }),
}));
jest.unstable_mockModule('../lib/venv-manager.js', () => ({
  getManagedPythonPath: async () => path.resolve('.venv/bin/python'),
}));
let h, previous, directory, client, server;
beforeAll(async () => {
  h = await Rehearsal.start('agent-eval');
});
afterAll(async () => {
  await h.close();
});
beforeEach(async () => {
  previous = { ...process.env };
  directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'admission-sdk-')));
  Object.assign(process.env, {
    PYTHONPATH: path.resolve('__tests__/rehearsal'),
    PYTHONDONTWRITEBYTECODE: '1',
    SSL_CERT_FILE: h.cert,
    REHEARSAL_ALLOWED_PORT: String(new URL(h.origin).port),
    REHEARSAL_EGRESS_TRAP: path.join(directory, 'egress-trap.txt'),
    REHEARSAL_LIBGEN_ORIGIN: h.origin,
    BOOK_SOURCE_PREFLIGHT: 'false',
    BOOK_SOURCE_FALLBACK_ENABLED: 'false',
    ZLIBRARY_EAPI_DOMAIN: h.domain,
    ZLIBRARY_EMAIL: 'rehearsal-account@example.invalid',
    ZLIBRARY_PASSWORD: 'rehearsal-password-canary',
    ZLIBRARY_OUTPUT_ROOT: directory,
    MCP_READABLE_ROOTS: directory,
    ZLIBRARY_SESSION_MODE: 'memory',
    CIRCUIT_BREAKER_THRESHOLD: '5',
    RETRY_MAX_RETRIES: '0',
    LOG_LEVEL: 'error',
  });
  await h.mode({ mode: 'healthy', reset: true, remaining: 8 });
  jest.resetModules();
  const { createServer } = await import('../dist/index.js');
  server = createServer();
  client = new Client({ name: 'admission-real-sdk', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
  demands = 0;
});
afterEach(async () => {
  await client?.close();
  await server?.close();
  expect(actualRunner.liveChildCount()).toBe(0);
  jest.restoreAllMocks();
  process.env = previous;
  await fs.rm(directory, { recursive: true, force: true });
});
const call = async (name, args) => client.callTool({ name, arguments: args });
const receipts = async () =>
  Object.fromEntries(
    await Promise.all(
      (await fs.readdir(directory))
        .filter((name) => name.endsWith('.acq.json'))
        .map(async (name) => [
          name,
          (await fs.readFile(path.join(directory, name))).toString('base64'),
        ]),
    ),
  );

test.each(
  ['libgen', 'zlibrary'].flatMap((source) =>
    ['modified', 'foreign', 'intact'].map((state) => [source, state]),
  ),
)(
  'real SDK %s %s destination admission precedes intent, lazy demand and transfer',
  async (source, state) => {
    const search = await call('search', {
      query: source === 'libgen' ? 'Persuasion' : 'Seneca Letters',
      sources: [source],
    });
    expect(search.isError).not.toBe(true);
    const row = search.structuredContent.rows[0];
    expect(row).toBeDefined();
    const filename = `chosen.${row.format}`;
    let artifact = path.join(directory, filename);
    if (state === 'foreign') await fs.writeFile(artifact, 'unrelated occupied bytes');
    else {
      if (source === 'libgen') {
        // Seed the already-complete receipt premise without relying on the fake's
        // unrelated LibGen link-resolver/download integration.
        const details = (await call('get_book', { ref: row.ref })).structuredContent;
        const bytes = Buffer.from('offline completed LibGen artifact');
        await fs.writeFile(artifact, bytes);
        await fs.writeFile(
          `${artifact}.acq.json`,
          JSON.stringify({
            record_version: 1,
            state: 'complete',
            path: artifact,
            bytes: bytes.length,
            sha256: createHash('sha256').update(bytes).digest('hex'),
            source,
            identifiers: details.identifiers,
            reused: false,
            transfer_skipped: false,
          }),
        );
      } else {
        const first = await call('download', { ref: row.ref, filename });
        expect(first.isError).not.toBe(true);
        artifact = first.structuredContent.path;
        expect(demands).toBe(1);
      }
      if (state === 'modified') await fs.writeFile(artifact, 'modified occupied bytes');
    }
    if (source === 'libgen' && state === 'intact') {
      const held = server.tools.refs.get(row.ref, 'r').book;
      const seed = JSON.parse(await fs.readFile(`${artifact}.acq.json`, 'utf8'));
      expect(seed.identifiers).toEqual({ md5: String(held.md5).toLowerCase() });
      expect(seed.sha256).toBe(
        createHash('sha256')
          .update(await fs.readFile(artifact))
          .digest('hex'),
      );
      expect(seed.path).toBe(await fs.realpath(artifact));
    }
    const priorBytes = await fs.readFile(artifact);
    const priorReceipts = await receipts();
    const stats = await h.stats();
    demands = 0;
    const reply = await call('download', { ref: row.ref, filename });
    if (state === 'intact') {
      expect(reply.isError).not.toBe(true);
      expect(reply.structuredContent.transfer_skipped).toBe(true);
    } else {
      expect(reply.isError).toBe(true);
      expect(reply.structuredContent.error).toMatchObject({
        code: 'output_refused',
        source,
        retry: 'no',
        detail: { reason: 'output_refused', request_issued: false },
      });
      expect(reply.structuredContent.error.message.length).toBeLessThanOrEqual(199);
      expect(reply.structuredContent.error.message).toMatch(/no matching receipt/);
      expect(reply.structuredContent.error.message).not.toContain('different bytes');
      expect(reply.structuredContent.error.recovery).toMatch(/no matching receipt/);
      expect(reply.structuredContent.error.recovery).not.toContain('different bytes');
      expect(reply.structuredContent.error.recovery).toMatch(/move|rename/i);
      expect(reply.structuredContent.error.recovery).toContain('output_dir');
    }
    expect(demands).toBe(0);
    expect((await h.stats()).calls).toEqual(stats.calls);
    expect((await h.stats()).quotaSpent).toBe(stats.quotaSpent);
    expect(await receipts()).toEqual(priorReceipts);
    expect(await fs.readFile(artifact)).toEqual(priorBytes);
    await expect(fs.stat(path.join(directory, 'egress-trap.txt'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
  },
);

test.each(['identical', 'different'])(
  'real SDK corrected target with %s unreceipted bytes is refused after issuance',
  async (state) => {
    const row = (await call('search', { query: 'Seneca Letters', sources: ['zlibrary'] }))
      .structuredContent.rows[0];
    const first = await call('download', { ref: row.ref, filename: 'chosen.pdf' });
    expect(first.isError).not.toBe(true);
    const target = first.structuredContent.path;
    await fs.unlink(first.structuredContent.record);
    if (state === 'different') await fs.writeFile(target, 'foreign canonical bytes');
    const original = await fs.readFile(target);
    // Sparse catalogue metadata predicts .bin; the real EAPI response names a PDF.
    server.tools.refs.get(row.ref, 'r').book.extension = '';
    const before = await h.stats();
    demands = 0;

    const reply = await call('download', { ref: row.ref, filename: 'chosen.bin' });

    expect(reply.isError).toBe(true);
    expect(reply.structuredContent.error).toMatchObject({
      code: 'output_refused',
      source: 'zlibrary',
      retry: 'no',
      detail: { reason: 'output_refused', request_issued: true },
    });
    expect(reply.structuredContent.error.message.length).toBeLessThanOrEqual(199);
    expect(reply.structuredContent.error.recovery.length).toBeLessThanOrEqual(199);
    expect(reply.structuredContent.error.recovery).toMatch(/may already have used a download/i);
    expect(reply.structuredContent.error.recovery).toMatch(/move|rename/i);
    expect(reply.structuredContent.error.recovery).toContain('output_dir');
    expect(demands).toBe(1);
    expect((await h.stats()).quotaSpent - before.quotaSpent).toBe(1);
    expect(await fs.readFile(target)).toEqual(original);
    await expect(fs.stat(`${target}.acq.json`)).rejects.toMatchObject({ code: 'ENOENT' });
    expect(
      JSON.parse(await fs.readFile(path.join(directory, 'chosen.bin.acq.json'))),
    ).toMatchObject({
      state: 'intent',
      path: path.join(directory, 'chosen.bin'),
      sha256: null,
    });
    expect((await fs.readdir(directory)).filter((name) => name.startsWith('.eapi-'))).toEqual([]);
    await expect(fs.stat(path.join(directory, 'egress-trap.txt'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
  },
);

test('open EAPI serves an intact receipt but refuses owner admission and transfer for a non-hit while LibGen search works', async () => {
  const row = (await call('search', { query: 'Seneca Letters', sources: ['zlibrary'] }))
    .structuredContent.rows[0];
  expect(row).toBeDefined();
  const cached = await call('download', { ref: row.ref, filename: 'cached.pdf' });
  expect(cached.isError).not.toBe(true);
  const cachedBytes = await fs.readFile(cached.structuredContent.path);
  const cachedRecord = await fs.readFile(cached.structuredContent.record);
  const { EapiSessionOwner } = await import('../dist/lib/eapi-session.js');
  const ownerAdmission = jest.spyOn(EapiSessionOwner.prototype, 'run');
  await h.mode({ mode: 'server503' });
  for (let i = 0; i < 5; i++) {
    const failure = await call('search', { query: 'outage', sources: ['zlibrary'] });
    expect(failure.isError).toBe(true);
    expect(failure.structuredContent.error.attempts[0]).toMatchObject({
      source: 'zlibrary',
      code: 'provider_error',
      reason: 'http_error',
    });
  }
  const before = await h.stats();
  const files = await fs.readdir(directory);
  demands = 0;
  ownerAdmission.mockClear();
  const hit = await call('download', { ref: row.ref, filename: 'cached.pdf' });
  expect(hit.isError).not.toBe(true);
  expect(hit.structuredContent).toMatchObject({ transfer_skipped: true });
  expect(demands).toBe(0);
  expect(ownerAdmission).not.toHaveBeenCalled();
  expect(await h.stats()).toEqual(before);
  expect(await fs.readdir(directory)).toEqual(files);
  expect(await fs.readFile(cached.structuredContent.path)).toEqual(cachedBytes);
  expect(await fs.readFile(cached.structuredContent.record)).toEqual(cachedRecord);
  const refused = await call('download', { ref: row.ref, filename: 'blocked.pdf' });
  expect(refused.isError).toBe(true);
  expect(refused.structuredContent.error).toMatchObject({
    code: 'breaker_open',
    source: 'zlibrary',
    retry: 'safe',
    detail: { reason: 'breaker_open', request_issued: false },
  });
  // The raw child asks only after its receipt check and durable intent.
  // Breaker admission must deny before owner work or an operation grant.
  expect(demands).toBe(1);
  expect(ownerAdmission).not.toHaveBeenCalled();
  expect(await h.stats()).toEqual(before);
  expect((await fs.readdir(directory)).sort()).toEqual([...files, 'blocked.pdf.acq.json'].sort());
  expect(JSON.parse(await fs.readFile(path.join(directory, 'blocked.pdf.acq.json')))).toMatchObject(
    {
      state: 'intent',
      path: path.join(directory, 'blocked.pdf'),
      sha256: null,
    },
  );
  await expect(fs.stat(path.join(directory, 'blocked.pdf'))).rejects.toMatchObject({
    code: 'ENOENT',
  });
  const alternate = await call('search', { query: 'fixture', sources: ['libgen'], limit: 1 });
  expect(alternate.isError).not.toBe(true);
  expect(alternate.structuredContent.rows[0]).toMatchObject({
    title: 'Rehearsal LibGen fixture',
    source: 'libgen',
  });
  const after = await h.stats();
  expect(after.logins).toBe(before.logins);
  expect(after.quotaSpent).toBe(before.quotaSpent);
  expect(after.calls['/eapi/book/search']).toBe(before.calls['/eapi/book/search']);
  expect(after.calls['/libgen/index.php']).toBe(1);
  await expect(fs.stat(path.join(directory, 'egress-trap.txt'))).rejects.toMatchObject({
    code: 'ENOENT',
  });
});
