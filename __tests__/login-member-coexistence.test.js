import { jest, test, expect } from '@jest/globals';
import { spawn, execFileSync, spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';

jest.setTimeout(30000);
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];

test('saved login and member key retain independent search state and public limits', async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'login-member-'));
  const previous = { ...process.env };
  Object.assign(process.env, {
    XDG_STATE_HOME: path.join(directory, 'state'),
    ZLIBRARY_OUTPUT_ROOT: path.join(directory, 'downloads'),
    ANNAS_SECRET_KEY: 'worker-key-canary',
    RETRY_MAX_RETRIES: '0',
    ZLIBRARY_SESSION_MODE: 'memory',
  });
  delete process.env.ZLIBRARY_EMAIL;
  delete process.env.ZLIBRARY_PASSWORD;
  delete process.env.ZLIBRARY_PREFER_ENV;
  delete process.env.BOOK_SOURCE_DEFAULT_ORDER;
  delete process.env.BOOK_SOURCE_SEARCH_WAIT_MS;
  delete process.env.BOOK_FORMAT_PREFERENCE;
  const worker = new AnnasMemberWorker(async () =>
    spawn(path.resolve('.venv/bin/python'), ['__tests__/fixtures/annas-member-worker.py'], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: process.env,
    }),
  );
  let client, server;
  const bridge = jest.fn(async (_script, options, control) => {
    expect(control.sessionInput).toBeUndefined();
    expect(options.env.ZLIBRARY_LOGIN_EXPECTED_ORIGIN).toBe('saved');
    expect(options.env.ZLIBRARY_LOGIN_EXPECTED_IDENTITY).toBe('saved@example.test');
    if (options.args[0] === 'search')
      return envelope({ books: [{ id: '123', hash: 'synthetic', title: 'Saved Z-Library book' }] });
    if (options.args[0] === 'get_download_limits')
      return envelope({ daily_limit: 10, downloads_today: 3, daily_remaining: 7 });
    throw new Error('Unexpected bridge operation: ' + options.args[0]);
  });
  try {
    execFileSync(
      path.resolve('.venv/bin/python'),
      [
        '-c',
        'from lib.login_store import save; save(identity="saved@example.test", user_id="42", user_key="saved-key-canary", domain="fake.example.test")',
      ],
      { env: process.env },
    );
    jest.resetModules();
    jest.unstable_mockModule('../lib/annas-member-worker.js', () => ({
      annasMemberWorker: worker,
    }));
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge: bridge,
      LONG_BRIDGE_TIMEOUT_MS: 3900000,
    }));
    const { createServer } = await import('../dist/index.js');
    server = createServer();
    client = new Client({ name: 'login-member-coexistence', version: '1' });
    const [a, b] = InMemoryTransport.createLinkedPair();
    await Promise.all([client.connect(a), server.connect(b)]);
    const call = async (name, args) => {
      const reply = await client.callTool({ name, arguments: args });
      expect(reply.isError).not.toBe(true);
      expect(JSON.stringify(reply)).not.toMatch(
        /saved-key-canary|worker-key-canary|worker-cookie-canary/,
      );
      return reply.structuredContent;
    };
    expect(worker.status()).toBe('none');
    const before = await call('get_limits', { sources: ['zlibrary', 'annas'] });
    expect(before.search).toMatchObject({
      default_sources: ['libgen', 'zlibrary'],
      wait_ms: 12000,
      ordering: 'fit-v1',
    });
    expect(before.sources.zlibrary.configured).toBe(true);
    expect(before.sources.annas).toMatchObject({
      configured: true,
      member_key_configured: true,
      member_session: 'none',
    });
    expect(bridge).not.toHaveBeenCalled();
    for (let i = 0; i < 2; i++) {
      const annas = await call('search', { query: 'books', sources: ['annas'] });
      expect(annas.rows[0].title).toBe('Sign-ins 1');
    }
    expect(worker.status()).toBe('active');
    const zlibrary = await call('search', { query: 'books', sources: ['zlibrary'] });
    expect(zlibrary.rows[0].title).toBe('Saved Z-Library book');
    expect(worker.status()).toBe('active');
    const after = await call('get_limits', { mode: 'live', sources: ['zlibrary', 'annas'] });
    expect(after.search).toEqual(before.search);
    expect(after.sources.zlibrary).toMatchObject({
      configured: true,
      limit: { kind: 'daily', remaining: 7 },
    });
    expect(after.sources.annas).toMatchObject({
      configured: true,
      member_key_configured: true,
      member_session: 'active',
    });
    expect(bridge).toHaveBeenCalledTimes(2);
  } finally {
    await client?.close();
    await server?.close();
    await worker.close();
    process.env = previous;
    jest.unstable_unmockModule('../lib/annas-member-worker.js');
    jest.unstable_unmockModule('../lib/python-runner.js');
    rmSync(directory, { recursive: true, force: true });
  }
});

const ownedAssets = [
  'lib/login_store.py',
  'lib/login_runtime.py',
  'lib/login_cli.py',
  'lib/annas_member_worker.py',
  'dist/lib/login.js',
  'dist/lib/annas-member-worker.js',
];
const sharedAssets = [
  'lib/python_bridge.py',
  'lib/enhanced_metadata.py',
  'lib/author_tools.py',
  'lib/term_tools.py',
  'lib/__init__.py',
  'dist/lib/eapi-session.js',
  'dist/index.js',
  'dist/lib/python-bridge.js',
  'dist/lib/zlibrary-api.js',
  'dist/lib/venv-manager.js',
  'package.json',
  'tsconfig.json',
  'pyproject.toml',
  'uv.lock',
];

test.each(ownedAssets)('asset validation rejects a missing merged runtime asset: %s', (omitted) => {
  const directory = mkdtempSync(path.join(tmpdir(), 'login-member-assets-'));
  try {
    for (const file of [...sharedAssets, ...ownedAssets]) {
      const target = path.join(directory, file);
      mkdirSync(path.dirname(target), { recursive: true });
      writeFileSync(target, file === 'package.json' ? '{"type":"module"}' : 'fixture');
    }
    const script = path.join(directory, 'scripts/validate-python-bridge.js');
    mkdirSync(path.dirname(script), { recursive: true });
    copyFileSync('scripts/validate-python-bridge.js', script);
    expect(spawnSync(process.execPath, [script], { encoding: 'utf8' }).status).toBe(0);
    rmSync(path.join(directory, omitted));
    const result = spawnSync(process.execPath, [script], { encoding: 'utf8' });
    expect(result.status).toBe(1);
    expect(result.stdout + result.stderr).toContain(omitted + ' NOT FOUND');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
