import {
  afterAll,
  beforeAll,
  afterEach,
  beforeEach,
  expect,
  jest,
  test,
  describe,
} from '@jest/globals';
import fs from 'node:fs/promises';
import nativeFs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { EapiSessionOwner, authRejected } from '../dist/lib/eapi-session.js';
import { Rehearsal } from './rehearsal/harness.js';

jest.setTimeout(30000);

test('at-most-once owner operation never refreshes/replays after handoff rejection', async () => {
  const owner = new EapiSessionOwner();
  let logins = 0,
    operations = 0;
  const identity = 'synthetic-owner@example.invalid';
  const invoke = async (input) => {
    if (input.action === 'login') {
      logins++;
      owner.consume(
        {
          ...input,
          domain: 'synthetic.invalid',
          remix_userid: '1',
          remix_userkey: 'private-test-cookie',
        },
        identity,
        input.generation,
      );
      return { authenticated: true };
    }
    if (input.action === 'operation') {
      operations++;
      throw authRejected(input.generation, 'Authentication rejected');
    }
    return { authenticated: true };
  };
  await expect(
    owner.run(identity, invoke, { operationMode: 'at-most-once' }),
  ).rejects.toMatchObject({ context: { reason: 'auth_rejected' } });
  expect(operations).toBe(1);
  expect(logins).toBe(1);
  expect(owner.get(identity)).toBeUndefined();
});

describe('SDK lazy acquisition with real bridge and query-sensitive loopback fake', () => {
  let h, original, home, client, server;
  beforeAll(async () => {
    h = await Rehearsal.start('agent-eval');
  });
  afterAll(async () => {
    await h.close();
  });
  beforeEach(async () => {
    original = { ...process.env };
    home = await fs.mkdtemp(path.join(os.tmpdir(), 'lazy-sdk-'));
    const downloads = path.join(home, 'downloads');
    await fs.mkdir(downloads);
    Object.assign(process.env, {
      PYTHONPATH: path.resolve('__tests__/rehearsal'),
      PYTHONDONTWRITEBYTECODE: '1',
      SSL_CERT_FILE: h.cert,
      REHEARSAL_ALLOWED_PORT: String(new URL(h.origin).port),
      REHEARSAL_EGRESS_TRAP: path.join(home, 'egress-trap.txt'),
      ZLIBRARY_EAPI_DOMAIN: h.domain,
      ZLIBRARY_EMAIL: 'rehearsal-account@example.invalid',
      ZLIBRARY_PASSWORD: 'rehearsal-password-canary',
      ZLIBRARY_OUTPUT_ROOT: downloads,
      MCP_READABLE_ROOTS: downloads,
      ZLIBRARY_SESSION_MODE: 'memory',
      RETRY_MAX_RETRIES: '0',
      PYTHON_BRIDGE_TIMEOUT: '15000',
      LOG_LEVEL: 'error',
    });
    await h.mode({ mode: 'healthy', reset: true, remaining: 8 });
    jest.resetModules();
    const { createServer } = await import('../dist/index.js');
    server = createServer();
    client = new Client({ name: 'lazy-session-real-sdk', version: '1' });
    const [a, b] = InMemoryTransport.createLinkedPair();
    await Promise.all([client.connect(a), server.connect(b)]);
  });
  afterEach(async () => {
    await client?.close();
    await server?.close();
    process.env = original;
    await fs.rm(home, { recursive: true, force: true });
  });
  const call = async (name, args) => {
    const reply = await client.callTool({ name, arguments: args });
    expect(reply.isError).not.toBe(true);
    return reply.structuredContent;
  };
  test('search then two fresh destinations keep one login and spend exactly two synthetic transfers', async () => {
    const rows = (
      await call('search', { query: 'Seneca Letters', sources: ['zlibrary'], limit: 10 })
    ).rows;
    expect(rows).toHaveLength(2);
    expect((await h.stats()).logins).toBe(1);
    for (const row of rows) await call('download', { ref: row.ref });
    const stats = await h.stats();
    expect(stats.logins).toBe(1);
    expect(stats.quotaSpent).toBe(2);
    const repeat = await call('download', { ref: rows[0].ref });
    expect(repeat.transfer_skipped).toBe(true);
    expect((await h.stats()).logins).toBe(1);
    expect((await h.stats()).quotaSpent).toBe(2);
    await expect(fs.stat(path.join(home, 'egress-trap.txt'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
  });

  const book = (id) => ({
    source: 'zlibrary',
    id: String(id),
    hash: `hash${id}`,
    title: `Offline ${id}`,
    extension: 'epub',
  });
  const acquire = async (api, id, filename, options = {}) =>
    api.invokeAcquisitionBridge(
      'acquire_book',
      { book_details: book(id), output_dir: process.env.ZLIBRARY_OUTPUT_ROOT, filename },
      { timeoutMs: 15000, ...options },
    );
  test('warm available session is not issuance when acquisition spends its deadline on a lease', async () => {
    const api = await import('../dist/lib/zlibrary-api.js');
    await api.searchBooks({ query: 'Seneca Letters' });
    const before = await h.stats();
    expect(before.logins).toBe(1);
    const target = path.join(process.env.ZLIBRARY_OUTPUT_ROOT, 'held.epub');
    const holder = spawn(
      path.resolve('.venv/bin/python'),
      [
        '-c',
        [
          'import sys',
          'from pathlib import Path',
          'from lib.acquisition import DestinationLease',
          'with DestinationLease(Path(sys.argv[1]), 0):',
          ' print("held", flush=True)',
          ' sys.stdin.read()',
        ].join('\n'),
        target,
      ],
      {
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, ZLIBRARY_OUTPUT_ROOT: path.join(home, 'downloads') },
      },
    );
    const closed = once(holder, 'close');
    try {
      const [ready] = await once(holder.stdout, 'data', { signal: AbortSignal.timeout(5000) });
      expect(ready.toString().trim()).toBe('held');
      // The child advances a logical clock only after the real OS lease
      // rejects entry. Startup/load cannot consume the admission margin.
      const pythonPath = process.env.PYTHONPATH;
      let failure;
      try {
        process.env.PYTHONPATH = [
          path.resolve('__tests__/python/lease_clock_site'),
          pythonPath,
        ].join(path.delimiter);
        failure = await acquire(api, 202, 'held.epub', { timeoutMs: 15000 }).catch(
          (error) => error,
        );
      } finally {
        process.env.PYTHONPATH = pythonPath;
      }
      expect(failure.context.pythonErrorType).toBe('AcquisitionError');
      expect(failure.context.details).toMatchObject({
        reason: 'walk_budget_exhausted',
        request_issued: false,
      });
      const { errorEnvelope } = await import('../dist/lib/tool-errors.js');
      expect(errorEnvelope(failure, 'download', 'zlibrary').error).toMatchObject({
        code: 'provider_timeout',
        retry: 'safe',
        detail: { request_issued: false },
      });
      const after = await h.stats();
      expect(after.calls).toEqual(before.calls);
      expect(after.logins).toBe(before.logins);
      expect(after.quotaSpent).toBe(before.quotaSpent);
      await expect(fs.stat(target)).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      holder.stdin.end();
      await closed;
    }
    // Refusal neither invalidated the unused warm session nor poisoned its breaker.
    await api.searchBooks({ query: 'Seneca Letters' });
    expect((await h.stats()).logins).toBe(1);
    await expect(fs.stat(path.join(home, 'egress-trap.txt'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
  });
  test('cold distinct acquisitions and SDK search singleflight one login; same destination follower skips', async () => {
    const api = await import('../dist/lib/zlibrary-api.js');
    await h.mode({ mode: 'slow', reset: true, remaining: 8 });
    const [a, b, follower, search] = await Promise.all([
      acquire(api, 202, 'cold202.epub'),
      acquire(api, 203, 'cold203.epub'),
      acquire(api, 202, 'cold202.epub'),
      call('search', { query: 'Seneca Letters', sources: ['zlibrary'] }),
    ]);
    expect([a.transfer_skipped, follower.transfer_skipped].sort()).toEqual([false, true]);
    expect(b.transfer_skipped).toBe(false);
    expect(search.rows).toHaveLength(2);
    const stats = await h.stats();
    expect(stats.logins).toBe(1);
    expect(stats.quotaSpent).toBe(2);
  });
  test('receipt hits bypass missing credentials, unrelated pending login, and shared cooldown', async () => {
    const originalApi = await import('../dist/lib/zlibrary-api.js');
    const complete = await acquire(originalApi, 202, 'receipt.epub');
    delete process.env.ZLIBRARY_EMAIL;
    delete process.env.ZLIBRARY_PASSWORD;
    expect((await acquire(originalApi, 202, 'receipt.epub')).transfer_skipped).toBe(true);
    process.env.ZLIBRARY_EMAIL = 'rehearsal-account@example.invalid';
    process.env.ZLIBRARY_PASSWORD = 'rehearsal-password-canary';
    jest.resetModules();
    const api = await import('../dist/lib/zlibrary-api.js');
    await h.mode({ mode: 'slow' });
    // Independent fresh owner is awaiting login while this existing receipt returns.
    const pending = api.searchBooks({ query: 'slow-Seneca' }).catch((error) => error);
    expect((await acquire(api, 202, 'receipt.epub')).transfer_skipped).toBe(true);
    await pending;
    jest.resetModules();
    const failing = await import('../dist/lib/zlibrary-api.js');
    await h.mode({ mode: 'login400' });
    await expect(acquire(failing, 203, 'denied.epub')).rejects.toMatchObject({
      context: { reason: 'login_failed' },
    });
    const stats = await h.stats();
    expect((await acquire(failing, 202, 'receipt.epub')).transfer_skipped).toBe(true);
    await expect(failing.searchBooks({ query: 'Seneca Letters' })).rejects.toMatchObject({
      context: { reason: 'auth_backoff' },
    });
    await expect(acquire(failing, 203, 'cooldown.epub')).rejects.toMatchObject({
      context: { reason: 'auth_backoff' },
    });
    expect((await h.stats()).logins).toBe(stats.logins);
    expect((await h.stats()).calls['/eapi/user/profile']).toBe(stats.calls['/eapi/user/profile']);
    expect(await fs.readFile(complete.path)).toBeDefined();
  });
  test('search login failure gates fresh acquisition only after intent; invalid/foreign receipt never demands', async () => {
    const api = await import('../dist/lib/zlibrary-api.js');
    await h.mode({ mode: 'login400' });
    await expect(api.searchBooks({ query: 'Seneca Letters' })).rejects.toMatchObject({
      context: { reason: 'login_failed' },
    });
    await expect(acquire(api, 202, 'after-search.epub')).rejects.toMatchObject({
      context: { reason: 'auth_backoff' },
    });
    const target = path.join(process.env.ZLIBRARY_OUTPUT_ROOT, 'after-search.epub.acq.json');
    expect(JSON.parse(await fs.readFile(target, 'utf8')).state).toBe('intent');
    expect((await h.stats()).logins).toBe(1);
    await expect(acquire(api, 202, '../escape.epub')).rejects.toThrow();
    expect((await h.stats()).logins).toBe(1);
    await h.mode({ mode: 'healthy' });
    jest.resetModules();
    const warm = await import('../dist/lib/zlibrary-api.js');
    await acquire(warm, 202, 'foreign.epub');
    const stats = await h.stats();
    await expect(acquire(warm, 203, 'foreign.epub')).rejects.toThrow(/another selection/);
    expect((await h.stats()).logins).toBe(stats.logins);
    expect((await h.stats()).quotaSpent).toBe(stats.quotaSpent);
  });
  test('abort during real lazy login reaps both waiting acquisition and auth-control children', async () => {
    const api = await import('../dist/lib/zlibrary-api.js');
    const runner = await import('../dist/lib/python-runner.js');
    await h.mode({ mode: 'slow', reset: true });
    const controller = new AbortController();
    const outcome = acquire(api, 202, 'aborted-login.epub', { signal: controller.signal }).catch(
      (error) => error,
    );
    const end = Date.now() + 5000;
    while (!(await h.stats()).logins && Date.now() < end)
      await new Promise((resolve) => setTimeout(resolve, 10));
    expect((await h.stats()).logins).toBe(1);
    controller.abort();
    expect((await outcome).context.reason).toBe('aborted');
    expect(runner.liveChildCount()).toBe(0);
    const reply = await acquire(api, 202, 'aborted-login.epub');
    expect(reply.transfer_skipped).toBe(false);
    expect((await h.stats()).quotaSpent).toBe(1);
  });
  test('documented per-call acquisition still logs in per fresh transfer', async () => {
    process.env.ZLIBRARY_SESSION_MODE = 'per-call';
    const api = await import('../dist/lib/zlibrary-api.js');
    await acquire(api, 202, 'per202.epub');
    await acquire(api, 203, 'per203.epub');
    expect((await h.stats()).logins).toBe(2);
    expect((await h.stats()).quotaSpent).toBe(2);
  });
});

// These cases run unchanged on hosted Windows with Python3.10/3.13.
describe('native protocol real Node/Python pipes', () => {
  let directory,
    runner,
    diagnosticEvents,
    diagnosticCase = 0;
  const diagnostics = process.env.LAZY_PROTOCOL_DIAGNOSTICS === '1';
  const input = {
    identity: 'offline@example.invalid',
    generation: 0,
    action: 'operation',
    domain: 'offline.invalid',
    remix_userid: '7',
    remix_userkey: 'native-secret-canary',
  };
  const pythonPath =
    process.env.LAZY_PROTOCOL_PYTHON ||
    path.resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const options = (mode, startupDelayMs = 0) => ({
    pythonPath,
    scriptPath: path.resolve('__tests__/fixtures'),
    args: [mode, directory, String(startupDelayMs)],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', ZLIBRARY_OUTPUT_ROOT: directory },
  });
  const run = (mode, { startupDelayMs = 0, ...extra } = {}) =>
    runner.runPythonBridge('lazy-session-worker.py', options(mode, startupDelayMs), {
      timeoutMs: 4000,
      onSessionNeeded: (c) => c.grant(input),
      ...extra,
      onProtocolDiagnostic:
        diagnostics || extra.onProtocolDiagnostic
          ? (event) => {
              if (diagnostics && diagnosticEvents.length < 768) diagnosticEvents.push(event);
              extra.onProtocolDiagnostic?.(event);
            }
          : undefined,
    });
  const observe = (child) => {
    if (!diagnostics) return;
    const started = performance.now();
    let sequence = 0;
    const mark = (phase, count, error) => {
      if (diagnosticEvents.length >= 768) return;
      diagnosticEvents.push({
        phase,
        sequence: ++sequence,
        elapsedMs: Math.max(0, Math.round(performance.now() - started)),
        ...(count === undefined ? {} : { count: Math.min(8193, count) }),
        ...(error
          ? {
              code: ['EPIPE', 'ECONNRESET', 'ERR_STREAM_DESTROYED'].includes(error.code)
                ? error.code
                : 'OTHER',
            }
          : {}),
      });
    };
    child.stdio[4].on('data', (chunk) => mark('request-data', chunk.length));
    for (const event of ['end', 'close', 'error'])
      child.stdio[4].on(event, (error) =>
        mark(`request-${event}`, undefined, event === 'error' ? error : undefined),
      );
    for (const event of ['finish', 'close', 'error'])
      child.stdin.on(event, (error) =>
        mark(`stdin-${event}`, undefined, event === 'error' ? error : undefined),
      );
    child.on('exit', () => mark('child-exit'));
    child.on('close', () => mark('child-close'));
  };
  const waitFile = async (name) => {
    const end = Date.now() + 3000;
    while (Date.now() < end) {
      try {
        return await fs.readFile(path.join(directory, name), 'utf8');
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      await new Promise((r) => setTimeout(r, 10));
    }
    throw new Error(`Missing synthetic marker ${name}`);
  };
  beforeEach(async () => {
    directory = await fs.mkdtemp(path.join(os.tmpdir(), 'native-lazy-'));
    diagnosticEvents = [];
    diagnosticCase++;
    runner = await import('../dist/lib/python-runner.js');
  });
  afterEach(async () => {
    if (diagnostics) {
      let python = [];
      try {
        python = (await fs.readFile(path.join(directory, 'protocol-diagnostic.jsonl'), 'utf8'))
          .trim()
          .split('\n')
          .filter(Boolean)
          .slice(-768)
          .map((line) => JSON.parse(line));
      } catch (error) {
        if (error.code !== 'ENOENT') python = [{ phase: 'diagnostic-file', state: 'invalid' }];
      }
      const artifact = {
        case: diagnosticCase,
        parent: diagnosticEvents,
        python,
        liveChildren: runner.liveChildCount(),
      };
      const serialized = JSON.stringify(artifact, null, 2);
      expect(serialized).not.toContain(input.remix_userkey);
      expect(serialized).not.toContain('recovered-private-canary');
      expect(serialized).not.toContain('canary-not-public');
      await fs.mkdir('test_output/native-protocol', { recursive: true });
      await fs.writeFile(
        `test_output/native-protocol/case-${diagnosticCase}.json`,
        serialized + '\n',
      );
    }
    expect(runner.liveChildCount()).toBe(0);
    await fs.rm(directory, { recursive: true, force: true });
  });
  test('held lease + durable intent precede one grant; fragmented terminal update; receipt emits neither', async () => {
    let demands = 0;
    const updates = [];
    const first = await run('operation', {
      onSession: (u) => updates.push(u),
      onSessionNeeded: (c) => {
        demands++;
        // Synchronous files inspected before granting the waiting child.
        expect(nativeFs.readFileSync(path.join(directory, 'admitted'), 'utf8')).toBe(
          'intent+lease',
        );
        expect(
          JSON.parse(nativeFs.readFileSync(path.join(directory, 'synthetic.epub.acq.json'), 'utf8'))
            .state,
        ).toBe('intent');
        c.grant(input);
      },
    });
    expect(JSON.parse(first[0]).transfer_skipped).toBe(false);
    expect(updates).toHaveLength(1);
    expect(updates[0].validate_next).toBe(true);
    const repeat = await run('operation', {
      onSession: (u) => updates.push(u),
      onSessionNeeded: () => {
        demands++;
        throw new Error('Receipt requested auth');
      },
    });
    expect(JSON.parse(repeat[0]).transfer_skipped).toBe(true);
    expect(demands).toBe(1);
    expect(updates).toHaveLength(1);
    expect(await fs.readFile(path.join(directory, 'transfers'), 'utf8')).toBe('1\n');
  });
  test('diagnostic callback observes only bounded protocol metadata', async () => {
    const events = [];
    await run('operation', {
      onProtocolDiagnostic(event) {
        events.push(event);
      },
    });
    expect(events.some((event) => event.phase === 'request-data')).toBe(true);
    expect(events.some((event) => event.phase === 'request-end')).toBe(true);
    // Observational close can follow the already-decoded successful result.
    // Await that event in this test; do not change product completion semantics.
    const closeDeadline = performance.now() + 1000;
    while (
      !events.some((event) => event.phase === 'child-close') &&
      performance.now() < closeDeadline
    )
      await new Promise((resolve) => setTimeout(resolve, 5));
    expect(events.some((event) => event.phase === 'child-close')).toBe(true);
    expect(JSON.stringify(events)).not.toContain(input.remix_userkey);
    const fields = new Set([
      'phase',
      'sequence',
      'elapsedMs',
      'demanded',
      'granted',
      'settled',
      'count',
      'code',
      'errno',
    ]);
    expect(events.length).toBeLessThanOrEqual(256);
    expect(events.every((event) => Object.keys(event).every((field) => fields.has(field)))).toBe(
      true,
    );
    expect(
      events.every(
        (event) => Number.isInteger(event.sequence) && event.sequence > 0 && event.elapsedMs >= 0,
      ),
    ).toBe(true);
  });
  test('diagnostic callback failure cannot change the real private handoff outcome', async () => {
    const result = await run('operation', {
      onProtocolDiagnostic() {
        throw new Error('private-observer-canary');
      },
    });
    expect(JSON.parse(result[0]).transfer_skipped).toBe(false);
  });
  test('fragmented request is accepted once', async () => {
    let demands = 0;
    await run('request-fragmented', {
      onSessionNeeded: (c) => {
        demands++;
        nativeFs.writeFileSync(path.join(directory, 'parent-observed-demand'), 'yes');
        c.grant(input);
      },
    });
    expect(demands).toBe(1);
    expect(await fs.readFile(path.join(directory, 'parent-observed-demand'), 'utf8')).toBe('yes');
  });
  test.each(['duplicate', 'oversize', 'malformed', 'version', 'partial'])(
    'rejects %s request before grant and reaps child',
    async (mode) => {
      let demands = 0;
      let failure;
      try {
        await run(`request-${mode}`, {
          onSessionNeeded: () => {
            demands++;
          },
        });
      } catch (error) {
        failure = error;
      }
      expect(failure?.message).toBe('Invalid private session request');
      expect(JSON.stringify(failure)).not.toContain('canary-not-public');
      expect(demands).toBe(0);
    },
  );
  test.each(['early-update', 'duplicate-update', 'double-grant'])(
    'rejects %s without leaking bearer',
    async (mode) => {
      let failure;
      try {
        await run(mode === 'double-grant' ? 'operation' : mode, {
          onSessionNeeded: (c) => {
            c.grant(input);
            if (mode === 'double-grant') c.grant(input);
          },
        });
      } catch (error) {
        failure = error;
      }
      expect(failure?.message).toMatch(/Invalid private session/);
      expect(JSON.stringify(failure)).not.toContain(input.remix_userkey);
    },
  );
  test('delivered and recovered key canaries never survive a child error', async () => {
    let error;
    try {
      await run('leaked-error');
    } catch (failure) {
      error = failure;
    }
    expect(error).toBeInstanceOf(Error);
    expect(JSON.stringify(error)).not.toContain(input.remix_userkey);
    expect(JSON.stringify(error)).not.toContain('recovered-private-canary');
  });
  test('parent denial remains authoritative, no transfer and next call can reclaim lease', async () => {
    const denial = new Error('Synthetic owner cooldown');
    await expect(run('operation', { onSessionNeeded: (c) => c.deny(denial) })).rejects.toBe(denial);
    await expect(fs.stat(path.join(directory, 'transfers'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
    await run('operation');
  });
  test.each(['demand', 'granted'])(
    'caller abort at %s reaps child, removes listener and releases lease',
    async (phase) => {
      const controller = new AbortController();
      const promise = run('stall', {
        signal: controller.signal,
        onSessionNeeded: (c) => {
          if (phase === 'demand') controller.abort();
          else c.grant(input);
        },
      });
      const outcome = promise.catch((error) => error);
      try {
        if (phase === 'granted') {
          await waitFile('granted');
          controller.abort();
        }
        const error = await outcome;
        expect(error.context.reason).toBe('aborted');
        expect(JSON.stringify(error)).not.toContain(input.remix_userkey);
        const { getEventListeners } = await import('node:events');
        expect(getEventListeners(controller.signal, 'abort')).toHaveLength(0);
        await run('operation');
        expect(
          (await fs.readFile(path.join(directory, 'transfers'), 'utf8')).trim().split('\n'),
        ).toHaveLength(phase === 'granted' ? 2 : 1);
      } finally {
        controller.abort();
        await outcome;
      }
    },
  );
  test('private auth-control abort also waits for actual child exit', async () => {
    const controller = new AbortController();
    const pending = runner
      .runPythonBridge('lazy-session-worker.py', options('control'), {
        sessionInput: { action: 'login' },
        signal: controller.signal,
        timeoutMs: 4000,
      })
      .catch((error) => error);
    await waitFile('control-ready');
    controller.abort();
    expect((await pending).context.reason).toBe('aborted');
    expect(runner.liveChildCount()).toBe(0);
    if (process.platform !== 'win32') expect(await waitFile('control-cleaned')).toBe('yes');
  });
  test('unanswered demand expires and reaps without transfer', async () => {
    let demands = 0;
    await expect(
      // The child waits 30s; the 10s owner leaves ample Python startup margin.
      run('unanswered-reply', {
        startupDelayMs: 750,
        timeoutMs: 10000,
        onSessionNeeded: () => {
          demands++;
        },
      }),
    ).rejects.toMatchObject({ context: { reason: 'timeout' } });
    expect(demands).toBe(1);
    await expect(fs.stat(path.join(directory, 'transfers'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
    await run('operation');
  });
  test('short acquisition refuses before demanding a session or issuing transfer', async () => {
    let demands = 0;
    await expect(
      // A fixture reserve leaves <1s for useful work, independently of startup.
      run('refuse-admission', {
        startupDelayMs: 750,
        timeoutMs: 10000,
        onSessionNeeded: () => {
          demands++;
        },
      }),
    ).rejects.toThrow('process exited with code 1');
    expect(JSON.parse(await fs.readFile(path.join(directory, 'refusal.json'), 'utf8'))).toEqual({
      reason: 'walk_budget_exhausted',
      request_issued: false,
    });
    expect(demands).toBe(0);
    for (const marker of ['admitted', 'transfers'])
      await expect(fs.stat(path.join(directory, marker))).rejects.toMatchObject({ code: 'ENOENT' });
    await run('operation');
  });
  test.each(['fragmented', 'empty', 'duplicate', 'oversize', 'denied', 'partial', 'timeout'])(
    'Python asynchronous reply parser: %s',
    async (kind) => {
      const { spawn } = await import('node:child_process');
      const child = spawn(
        pythonPath,
        [path.resolve('__tests__/fixtures/lazy-session-worker.py'), 'reply', directory],
        {
          stdio: ['overlapped', 'pipe', 'pipe', 'pipe', 'pipe'],
          env: { ...process.env, ZLIBRARY_OUTPUT_ROOT: directory },
        },
      );
      observe(child);
      let stdout = '',
        stderr = '',
        demand = Buffer.alloc(0);
      child.stdout.on('data', (c) => {
        stdout += c;
      });
      child.stderr.on('data', (c) => {
        stderr += c;
      });
      child.stdin.on('error', () => {});
      const closed = new Promise((resolve, reject) => {
        child.on('error', reject);
        child.on('close', (code) => resolve(code));
      });
      const demandBody = Buffer.from('{"type":"session_needed","terminal":true}');
      const expectedDemand = Buffer.concat([
        Buffer.from([0x5a, 0x4c, 0x41, 0x44, 2, 0, demandBody.length]),
        demandBody,
      ]);
      await new Promise((resolve) => {
        child.stdio[4].on('data', (c) => {
          demand = Buffer.concat([demand, c]);
          if (demand.length >= expectedDemand.length) resolve();
        });
      });
      expect(demand).toEqual(expectedDemand);
      const frame = JSON.stringify({ v: 1, type: 'session', input }) + '\n';
      if (kind === 'fragmented') {
        for (const char of frame) {
          child.stdin.write(char);
        }
        child.stdin.end();
      } else if (kind === 'empty') child.stdin.end();
      else if (kind === 'duplicate') child.stdin.end(frame + frame);
      else if (kind === 'oversize') child.stdin.end('private-canary'.repeat(700) + '\n');
      else if (kind === 'denied')
        child.stdin.end('{"v":1,"type":"denied","code":"auth_unavailable"}\n');
      else if (kind === 'partial') child.stdin.end(frame.trim());
      expect(await closed).toBe(0);
      const result = JSON.parse(stdout);
      if (kind === 'fragmented') expect(result).toEqual({ ok: true });
      else
        expect(result.error).toBe(
          kind === 'denied'
            ? 'Private session request denied'
            : kind === 'timeout'
              ? 'Private session reply deadline exceeded'
              : 'Invalid private session reply',
        );
      expect(stdout + stderr).not.toContain(input.remix_userkey);
      expect(stdout + stderr).not.toContain('private-canary');
    },
  );
  test.each([
    'valid',
    'without-eof',
    'empty',
    'duplicate',
    'denied',
    'partial',
    'abort',
    'read-abort',
  ])('diagnostic reader-connected stdin independent of fd4 EOF: %s', async (kind) => {
    const { spawn } = await import('node:child_process');
    const child = spawn(
      pythonPath,
      [path.resolve('__tests__/fixtures/lazy-session-worker.py'), 'probe-reply', directory],
      {
        stdio: ['overlapped', 'pipe', 'pipe', 'pipe', 'pipe'],
        env: { ...process.env, LAZY_PROTOCOL_DIAGNOSTICS: '1', ZLIBRARY_OUTPUT_ROOT: directory },
      },
    );
    observe(child);
    child.stdio[4].resume();
    child.stdio[3].resume();
    let stderr = '';
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.stdin.on('error', () => {});
    let output = '',
      firstLine;
    const ready = new Promise((resolve) => {
      firstLine = resolve;
    });
    child.stdout.on('data', (chunk) => {
      output += chunk;
      if (output.includes('\n')) firstLine(JSON.parse(output.split('\n')[0]));
    });
    const closed = new Promise((resolve, reject) => {
      child.on('error', reject);
      child.on('close', (code) => {
        firstLine(undefined);
        resolve(code);
      });
    });
    try {
      // Test-only rendezvous after connect. Never used by the runner/owner:
      // product demand still requires the entire fd4 frame AND EOF.
      expect(await ready).toEqual({ reader_connected: true });
      const frame = JSON.stringify({ v: 1, type: 'session', input }) + '\n';
      if (kind === 'abort') child.kill('SIGTERM');
      else if (kind === 'read-abort') {
        child.stdin.write(frame.slice(0, 7));
        const deadline = performance.now() + 750;
        let received = false;
        while (!received && performance.now() < deadline) {
          const trace = await fs.readFile(
            path.join(directory, 'protocol-diagnostic.jsonl'),
            'utf8',
          );
          received = trace
            .trim()
            .split('\n')
            .map((line) => JSON.parse(line))
            .some((event) => event.phase === 'input-read' && event.state === 'done');
          if (!received) await new Promise((resolve) => setTimeout(resolve, 5));
        }
        expect(received).toBe(true);
        child.kill('SIGTERM');
      } else if (kind === 'valid') {
        for (const char of frame) child.stdin.write(char);
        child.stdin.end();
      } else if (kind === 'without-eof') child.stdin.write(frame);
      else if (kind === 'duplicate') child.stdin.end(frame + frame);
      else if (kind === 'partial') child.stdin.end(frame.trim());
      else if (kind === 'empty') child.stdin.end();
      else child.stdin.end('{"v":1,"type":"denied","code":"auth_unavailable"}\n');
      const code = await closed;
      if (kind !== 'abort' && kind !== 'read-abort') {
        expect(code).toBe(0);
        const reply = JSON.parse(output.trim().split('\n')[1]);
        expect(reply).toEqual(
          kind === 'valid'
            ? { ok: true }
            : {
                error:
                  kind === 'denied'
                    ? 'Private session request denied'
                    : kind === 'without-eof'
                      ? 'Private session reply deadline exceeded'
                      : 'Invalid private session reply',
              },
        );
      }
      expect(output + stderr).not.toContain(input.remix_userkey);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
      await closed;
    }
  });
  test('lazy-v2 commit grants before physical fd4 EOF while selected worker stays alive', async () => {
    let grants = 0;
    await run('request-hold', {
      onSessionNeeded(c) {
        grants++;
        nativeFs.writeFileSync(path.join(directory, 'parent-observed-demand'), 'yes');
        c.grant(input);
      },
    });
    expect(grants).toBe(1);
  });
  test('lazy-v2 delayed auth-pending extra invalidates and stale grant cannot write', async () => {
    let retained;
    const events = [];
    const outcome = run('request-delayed-extra', {
      onSessionNeeded(c) {
        retained = c;
      },
      onProtocolDiagnostic(e) {
        events.push(e);
      },
    }).catch((e) => e);
    await waitFile('extra');
    const error = await outcome;
    expect(retained).toBeDefined();
    retained.grant(input);
    expect(error.message).toBe('Invalid private session request');
    expect(error.privateSessionProtocolCorruption).toBe(true);
    expect(events.filter((e) => e.phase === 'grant')).toHaveLength(0);
    await expect(fs.stat(path.join(directory, 'transfers'))).rejects.toMatchObject({
      code: 'ENOENT',
    });
  });
  test.each(['request-same-extra', 'request-partial-open'])(
    'lazy-v2 %s never grants',
    async (mode) => {
      let grants = 0;
      const events = [];
      const error = await run(mode, {
        // Existing run() budget: prove bytes arrived, not fixture-startup expiry.
        onProtocolDiagnostic(event) {
          events.push(event);
        },
        onSessionNeeded() {
          grants++;
        },
      }).catch((e) => e);
      expect(events.some((event) => event.phase === 'request-data')).toBe(true);
      expect(grants).toBe(0);
      if (mode === 'request-partial-open') expect(error.context.reason).toBe('timeout');
      else expect(error.privateSessionProtocolCorruption).toBe(true);
    },
  );
  test.each(['postgrant-extra', 'stdout-late-extra'])(
    'lazy-v2 %s fails after <=1 transfer and never publishes buffered fd3',
    async (mode) => {
      let grants = 0;
      const updates = [];
      const error = await run(mode, {
        onSession: (u) => updates.push(u),
        onSessionNeeded(c) {
          grants++;
          c.grant(input);
        },
      }).catch((e) => e);
      expect(error.message).toBe('Invalid private session request');
      expect(error.privateSessionProtocolCorruption).toBe(true);
      expect(grants).toBe(1);
      expect(
        (await fs.readFile(path.join(directory, 'transfers'), 'utf8')).trim().split('\n'),
      ).toHaveLength(1);
      expect(updates).toEqual([]);
      expect(JSON.stringify(error)).not.toContain(input.remix_userkey);
      expect(JSON.stringify(error)).not.toContain('recovered-private-canary');
    },
  );
});

// Diagnostic only: never changes the product interpreter or v1 EOF contract.
describe('native protocol launcher EOF discriminator', () => {
  let directory;
  beforeEach(async () => {
    directory = await fs.mkdtemp(path.join(os.tmpdir(), 'launcher-eof-'));
  });
  afterEach(async () => {
    await fs.rm(directory, { recursive: true, force: true });
  });
  test('compares selected venv with its identified base while each worker responds before explicit finish', async () => {
    const { spawn, execFile } = await import('node:child_process');
    const selected =
      process.env.LAZY_PROTOCOL_PYTHON ||
      path.resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
    const fixture = path.resolve('__tests__/fixtures/fd4-launcher-worker.py');
    const request = Buffer.from('{"v":1,"type":"session_needed"}\n');
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const alive = (pid) => {
      if (!Number.isSafeInteger(pid) || pid <= 0) return false;
      try {
        process.kill(pid, 0); // Read-only observation, never PID-based signal authority.
        return true;
      } catch (error) {
        return error.code !== 'ESRCH';
      }
    };
    const identify = () =>
      new Promise((resolve) => {
        execFile(
          selected,
          [fixture, 'identify'],
          {
            timeout: 2000,
            maxBuffer: 2048,
            env: { ...process.env, ZLIBRARY_OUTPUT_ROOT: directory },
          },
          (error, stdout) => {
            if (error) return resolve(undefined);
            try {
              const value = JSON.parse(stdout);
              resolve(
                typeof value.base === 'string' && path.isAbsolute(value.base)
                  ? value.base
                  : undefined,
              );
            } catch {
              resolve(undefined);
            }
          },
        );
      });
    const compare = async (executable, role) => {
      const result = {
        role,
        version: null,
        launchedEqualsWorker: null,
        requestByteCount: 0,
        requestMatches: false,
        writeByteCount: null,
        writeClosed: false,
        readiness: false,
        heartbeatBeforeWindow: false,
        heartbeatAfterWindow: false,
        eofAtWindowStart: false,
        eofAtWindowEnd: false,
        eofBeforeFinish: false,
        finishACK: false,
        exitZero: false,
        childClosed: false,
        workerExited: false,
        forcedCleanup: false,
        cleanupCompleted: false,
        stderrSeen: false,
        errors: [],
      };
      const error = (stage, code) => {
        if (result.errors.length < 8)
          result.errors.push({
            stage: [
              'spawn',
              'stdin',
              'fd4',
              'stdout',
              'readiness',
              'heartbeat-before',
              'heartbeat-after',
              'finish',
              'cleanup',
              'control',
            ].includes(stage)
              ? stage
              : 'control',
            code: ['io', 'invalid', 'oversize', 'deadline', 'unknown'].includes(code)
              ? code
              : 'invalid',
          });
      };
      // Paths and raw PIDs are used only in this owned process's memory.
      let workerPID,
        finishSent = false,
        eof = false,
        pongs = 0,
        pending = '',
        bytes = Buffer.alloc(0);
      const child = spawn(executable, [fixture, 'probe'], {
        // This stdlib-only command reader is intentionally synchronous.
        // Product overlapped stdin and its reader are not used or changed.
        stdio: ['pipe', 'pipe', 'pipe', 'ignore', 'pipe'],
        detached: process.platform !== 'win32',
        windowsHide: true,
        env: { ...process.env, ZLIBRARY_OUTPUT_ROOT: directory },
      });
      child.on('error', () => error('spawn', 'io'));
      child.on('close', (code) => {
        result.childClosed = true;
        result.exitZero = code === 0;
      });
      child.stdin.on('error', () => error('stdin', 'io'));
      child.stderr.on('data', () => {
        result.stderrSeen = true;
      });
      child.stdio[4].on('data', (chunk) => {
        result.requestByteCount = Math.min(129, result.requestByteCount + chunk.length);
        if (bytes.length + chunk.length <= 128) bytes = Buffer.concat([bytes, chunk]);
      });
      child.stdio[4].on('end', () => {
        eof = true;
        if (!finishSent) result.eofBeforeFinish = true;
      });
      child.stdio[4].on('error', () => error('fd4', 'io'));
      child.stdout.on('data', (chunk) => {
        if (pending.length + chunk.length > 1024) {
          error('stdout', 'oversize');
          pending = '';
          return;
        }
        pending += chunk.toString('utf8');
        while (pending.includes('\n')) {
          const end = pending.indexOf('\n');
          const line = pending.slice(0, end);
          pending = pending.slice(end + 1);
          try {
            const message = JSON.parse(line);
            if (
              message.type === 'ready' &&
              !result.readiness &&
              Object.keys(message).sort().join(',') === 'closed,pid,type,version,written' &&
              Number.isSafeInteger(message.pid) &&
              message.pid > 0 &&
              Array.isArray(message.version) &&
              message.version.length === 3 &&
              message.version.every((v) => Number.isSafeInteger(v) && v >= 0 && v < 1000) &&
              Number.isSafeInteger(message.written) &&
              message.written >= 0 &&
              message.written <= 32 &&
              typeof message.closed === 'boolean'
            ) {
              workerPID = message.pid;
              result.launchedEqualsWorker = child.pid === workerPID;
              result.version = message.version;
              result.writeByteCount = message.written;
              result.writeClosed = message.closed;
              result.readiness = true;
            } else if (message.type === 'pong' && Object.keys(message).length === 1) pongs++;
            else if (message.type === 'finished' && Object.keys(message).length === 1 && finishSent)
              result.finishACK = true;
            else error('stdout', 'invalid');
          } catch {
            error('stdout', 'invalid');
          }
        }
      });
      const deadline = performance.now() + 4000;
      const wait = async (condition, stage, end = deadline) => {
        while (!condition() && performance.now() < end) await pause(5);
        if (!condition())
          throw Object.assign(new Error('Launcher diagnostic control deadline'), { stage });
      };
      try {
        await wait(() => result.readiness && result.requestByteCount >= 32, 'readiness');
        child.stdin.write('ping\n');
        await wait(() => pongs >= 1, 'heartbeat-before');
        result.heartbeatBeforeWindow = true;
        result.eofAtWindowStart = eof;
        await pause(400);
        result.eofAtWindowEnd = eof;
        child.stdin.write('ping\n');
        await wait(() => pongs >= 2, 'heartbeat-after');
        result.heartbeatAfterWindow = true;
        // EOF is an observation, never a hypothesis-outcome pass criterion.
        finishSent = true;
        child.stdin.end('finish\n');
        await wait(() => result.finishACK && result.childClosed, 'finish');
      } catch (failure) {
        error(failure.stage || 'control', 'deadline');
      } finally {
        if (!result.childClosed) {
          // Prefer explicit completion/EOF, including failed-control cleanup.
          if (!child.stdin.writableEnded && !child.stdin.destroyed) {
            finishSent = true;
            try {
              child.stdin.end('finish\n');
            } catch {
              error('stdin', 'io');
            }
          }
          const finishDeadline = performance.now() + 250;
          while (!result.childClosed && performance.now() < finishDeadline) await pause(5);
        }
        if (process.platform === 'win32') {
          // Only the still-owned launched tree is signal authority. A worker PID
          // reported on stdout may be unbound/reused and is NEVER a kill target.
          if (
            !result.childClosed &&
            child.exitCode === null &&
            child.signalCode === null &&
            child.pid
          ) {
            result.forcedCleanup = true;
            await new Promise((resolve) => {
              execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], { timeout: 1000 }, () =>
                resolve(),
              );
            });
          }
        } else if ((!result.childClosed || alive(workerPID)) && child.pid) {
          // POSIX retains only the known owned spawned process-group authority.
          result.forcedCleanup = true;
          try {
            process.kill(-child.pid, 'SIGKILL');
          } catch (failure) {
            if (failure.code !== 'ESRCH') error('cleanup', 'io');
          }
        }
        const cleanupDeadline = performance.now() + 2000;
        while ((!result.childClosed || alive(workerPID)) && performance.now() < cleanupDeadline)
          await pause(5);
        result.workerExited = !!workerPID && !alive(workerPID);
        result.cleanupCompleted = result.childClosed && result.workerExited;
        // Reported-PID liveness after launcher close cannot establish ownership.
        if (!result.cleanupCompleted) error('cleanup', 'unknown');
        result.requestMatches = bytes.equals(request);
      }
      return result;
    };
    const base = await identify(); // This path is never written to an artifact/log.
    const artifact = {
      schema: 'launcher-eof-v1',
      windowMs: 400,
      identifiedBase: !!base,
      roles: [],
    };
    if (base) {
      artifact.roles.push(await compare(selected, 'selected'));
      artifact.roles.push(await compare(base, 'base'));
    }
    await fs.mkdir('test_output/native-protocol', { recursive: true });
    await fs.writeFile(
      'test_output/native-protocol/launcher-eof.json',
      JSON.stringify(artifact, null, 2) + '\n',
    );
    expect(artifact.identifiedBase).toBe(true);
    expect(artifact.roles).toHaveLength(2);
    for (const role of artifact.roles) {
      expect(role).toMatchObject({
        requestByteCount: 32,
        requestMatches: true,
        writeByteCount: 32,
        writeClosed: true,
        readiness: true,
        heartbeatBeforeWindow: true,
        heartbeatAfterWindow: true,
        finishACK: true,
        exitZero: true,
        childClosed: true,
        workerExited: true,
        forcedCleanup: false,
        cleanupCompleted: true,
        errors: [],
      });
    }
  });
});

describe('native protocol lazy-v2 exact demand decoder', () => {
  const body = Buffer.from('{"type":"session_needed","terminal":true}');
  const header = Buffer.from([0x5a, 0x4c, 0x41, 0x44, 2, 0, body.length]);
  const wire = Buffer.concat([header, body]);
  test('every header/body boundary and every-byte fragmentation commits once', async () => {
    const { PrivateDemandDecoder } = await import('../dist/lib/python-runner.js');
    for (let split = 1; split < wire.length; split++) {
      const decoder = new PrivateDemandDecoder();
      expect(decoder.push(wire.subarray(0, split))).toBe(false);
      expect(decoder.push(wire.subarray(split))).toBe(true);
      decoder.end();
    }
    const decoder = new PrivateDemandDecoder();
    for (let i = 0; i < wire.length; i++)
      expect(decoder.push(wire.subarray(i, i + 1))).toBe(i === wire.length - 1);
    decoder.end();
  });
  test('wrong header/body/length and coalesced extra commands refuse before commit', async () => {
    const { PrivateDemandDecoder } = await import('../dist/lib/python-runner.js');
    const bad = [
      Buffer.from('NOPE'),
      Buffer.from('{\"v\":1,\"type\":\"session_needed\"}\n'),
      Buffer.from([0x5a, 0x4c, 0x41, 0x44, 1]),
      ...[0, 129, body.length - 1, body.length + 1].map((n) =>
        Buffer.from([0x5a, 0x4c, 0x41, 0x44, 2, 0, n]),
      ),
      Buffer.from([0x5a, 0x4c, 0x41, 0x44, 2, 1, body.length]),
      Buffer.concat([header, Buffer.from('{"terminal":true,"type":"session_needed"}')]),
      Buffer.concat([header, Buffer.alloc(body.length, 0x78)]),
      Buffer.concat([wire, wire]),
      Buffer.concat([wire, Buffer.from('x')]),
    ];
    for (const bytes of bad) {
      const decoder = new PrivateDemandDecoder();
      expect(() => decoder.push(bytes)).toThrow('Invalid private session request');
    }
  });
  test('partial EOF/close and delayed extra refuse; empty no-demand is valid', async () => {
    const { PrivateDemandDecoder } = await import('../dist/lib/python-runner.js');
    new PrivateDemandDecoder().end();
    for (let split = 1; split < wire.length; split++) {
      const decoder = new PrivateDemandDecoder();
      decoder.push(wire.subarray(0, split));
      expect(() => decoder.end()).toThrow('Invalid private session request');
    }
    const decoder = new PrivateDemandDecoder();
    decoder.push(wire);
    expect(() => decoder.push(Buffer.from('x'))).toThrow('Invalid private session request');
  });
});
