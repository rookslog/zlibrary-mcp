import { jest } from '@jest/globals';
jest.setTimeout(30000);
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';

let directory;
beforeEach(() => {
  directory = mkdtempSync(path.join(tmpdir(), 'login-node-'));
});
afterEach(() => {
  rmSync(directory, { recursive: true, force: true });
});

async function loginModule() {
  try {
    return await import('../dist/lib/login.js');
  } catch {
    throw new Error('Python-owned login metadata query is not implemented');
  }
}
function save() {
  execFileSync(
    path.resolve('.venv/bin/python'),
    [
      '-c',
      'from lib.login_store import save; save(identity="saved@example.test", user_id="42", user_key="fake-node-key", domain="fake.example.test")',
    ],
    { env: { ...process.env, XDG_STATE_HOME: directory } },
  );
}
test('Node gets saved provenance from Python, without receiving a bearer value', async () => {
  save();
  const { readLoginMetadata } = await loginModule();
  const data = await readLoginMetadata({
    ...process.env,
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: 'env@example.test',
    ZLIBRARY_PASSWORD: 'fake-env-password',
  });
  expect(data.origin).toBe('saved');
  expect(data.identity).toBe('saved@example.test');
  expect(data.notice).toContain('ZLIBRARY_PASSWORD');
  expect(JSON.stringify(data)).not.toMatch(/fake-node-key|fake-env-password|user_key/);
});
test('Node rereads provenance after a new login in the same process', async () => {
  const { readLoginMetadata } = await loginModule();
  const env = {
    ...process.env,
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: '',
    ZLIBRARY_PASSWORD: '',
  };
  expect((await readLoginMetadata(env)).configured).toBe(false);
  save();
  expect((await readLoginMetadata(env)).origin).toBe('saved');
});
test.each([[[]], [['mcp']]])(
  'entrypoint %j starts MCP and keeps stdout as JSON-RPC',
  async (args) => {
    const child = spawn(process.execPath, ['dist/index.js', ...args], {
      env: { ...process.env, XDG_STATE_HOME: directory, ZLIBRARY_EMAIL: '', ZLIBRARY_PASSWORD: '' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const closed = new Promise((resolve) => child.once('close', resolve));
    let out = '',
      err = '';
    child.stdout.on('data', (chunk) => {
      out += chunk;
    });
    child.stderr.on('data', (chunk) => {
      err += chunk;
    });
    const reply = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        child.kill();
        reject(new Error('MCP did not respond: ' + err));
      }, 8000);
      child.stdout.on('data', () => {
        if (out.includes('\n')) {
          clearTimeout(timer);
          resolve();
        }
      });
      child.once('error', reject);
    });
    child.stdin.write(
      JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'initialize',
        params: {
          protocolVersion: '2025-11-25',
          capabilities: {},
          clientInfo: { name: 'login-test', version: '1' },
        },
      }) + '\n',
    );
    try {
      await reply;
      expect(JSON.parse(out.trim()).result.serverInfo.name).toBe('book-acquisition');
    } finally {
      child.stdin.end();
      await closed;
    }
  },
);
test('CLI status and unsupported password flag do not enter MCP or echo input', () => {
  const env = {
    ...process.env,
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: '',
    ZLIBRARY_PASSWORD: '',
  };
  const status = JSON.parse(
    execFileSync(process.execPath, ['dist/index.js', 'status', '--json'], {
      env,
      encoding: 'utf8',
      timeout: 8000,
    }),
  );
  expect(status).toMatchObject({
    origin: 'none',
    backend: 'file',
    works: 'not_configured',
    expiry: 'unknown',
  });
  try {
    execFileSync(process.execPath, ['dist/index.js', 'login', '--password', 'never-echo-this'], {
      env,
      encoding: 'utf8',
      stdio: 'pipe',
      timeout: 8000,
    });
    throw new Error('Password flag was accepted');
  } catch (error) {
    expect(error.status).toBe(2);
    expect(error.stdout + error.stderr).not.toContain('never-echo-this');
  }
});

test('MCP capabilities and search accept saved login with no environment password', async () => {
  save();
  const { AcquisitionTools } = await import('../dist/lib/acquisition-tools.js');
  const tools = new AcquisitionTools(
    async (name) => {
      if (name === 'search')
        return { books: [{ id: '123', hash: 'fake-hash', title: 'Fake title' }] };
      throw new Error('Unexpected operation: ' + name);
    },
    { ...process.env, XDG_STATE_HOME: directory, ZLIBRARY_EMAIL: '', ZLIBRARY_PASSWORD: '' },
  );
  const limits = await tools.call('get_limits', { mode: 'local' });
  expect(limits.sources.zlibrary.configured).toBe(true);
  const result = await tools.call('search', { query: 'fake', sources: ['zlibrary'] });
  expect(result.error).toBeUndefined();
  expect(result.rows[0].title).toBe('Fake title');
});

test('a refused store still allows LibGen local capabilities', async () => {
  save();
  const { chmodSync } = await import('node:fs');
  chmodSync(path.join(directory, 'zlibrary/logins/zlibrary.json'), 0o644);
  const { AcquisitionTools } = await import('../dist/lib/acquisition-tools.js');
  const tools = new AcquisitionTools(
    async () => {
      throw new Error('No network expected');
    },
    { ...process.env, XDG_STATE_HOME: directory, ZLIBRARY_EMAIL: '', ZLIBRARY_PASSWORD: '' },
  );
  const result = await tools.call('get_limits', { mode: 'local', sources: ['libgen'] });
  expect(result.error).toBeUndefined();
  expect(result.sources.libgen.download_available).toBe(true);
});

test('metadata lookup honors cancellation before spawning account work', async () => {
  const { readLoginMetadata } = await loginModule();
  await expect(
    readLoginMetadata(
      { ...process.env, XDG_STATE_HOME: directory },
      { signal: AbortSignal.abort() },
    ),
  ).rejects.toThrow(/cancel|abort/i);
});

test('a running MCP server reads a complete login while another process is writing', async () => {
  const { existsSync, writeFileSync } = await import('node:fs');
  const { Client } = await import('@modelcontextprotocol/sdk/client/index.js');
  const { InMemoryTransport } = await import('@modelcontextprotocol/sdk/inMemory.js');
  const { createServer } = await import('../dist/index.js');
  const { readLoginMetadata } = await loginModule();
  save();
  const original = { ...process.env };
  Object.assign(process.env, {
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: '',
    ZLIBRARY_PASSWORD: '',
  });
  const ready = path.join(directory, 'writer-ready');
  const release = path.join(directory, 'writer-release');
  const server = createServer();
  const client = new Client({ name: 'atomic-login-reader', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
  const writer = spawn(
    path.resolve('.venv/bin/python'),
    [
      '-c',
      `
import os, sys, time
from pathlib import Path
from lib import login_store
original = os.replace
def paused(*args, **kwargs):
    Path(sys.argv[1]).write_text('ready')
    deadline = time.monotonic() + 15
    while not Path(sys.argv[2]).exists():
        if time.monotonic() > deadline: raise RuntimeError('writer release missing')
        time.sleep(0.02)
    return original(*args, **kwargs)
os.replace = paused
login_store.save(identity='saved@example.test', user_id='42', user_key='fake-new-server-key', domain='fake.example.test')
`,
      ready,
      release,
    ],
    { env: process.env, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  const closed = new Promise((resolve) => writer.once('close', resolve));
  try {
    const end = Date.now() + 10000;
    while (!existsSync(ready) && Date.now() < end)
      await new Promise((resolve) => setTimeout(resolve, 20));
    expect(existsSync(ready)).toBe(true);
    const old = await client.callTool({
      name: 'get_limits',
      arguments: { mode: 'local', sources: ['zlibrary'] },
    });
    expect(old.isError).not.toBe(true);
    expect(old.structuredContent.sources.zlibrary.configured).toBe(true);
    expect((await readLoginMetadata()).generation).toBe(1);
    writeFileSync(release, 'continue');
    expect(await closed).toBe(0);
    expect((await readLoginMetadata()).generation).toBe(2);
    const fresh = await client.callTool({
      name: 'get_limits',
      arguments: { mode: 'local', sources: ['zlibrary'] },
    });
    expect(fresh.structuredContent.sources.zlibrary.configured).toBe(true);
  } finally {
    writeFileSync(release, 'continue');
    await closed;
    await client.close();
    await server.close();
    process.env = original;
  }
});

test('unusable storage preserves environment metadata and returns a closed notice', async () => {
  save();
  const { chmodSync } = await import('node:fs');
  chmodSync(path.join(directory, 'zlibrary/logins/zlibrary.json'), 0o644);
  const { readLoginMetadata } = await loginModule();
  const data = await readLoginMetadata({
    ...process.env,
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: 'env@example.test',
    ZLIBRARY_PASSWORD: 'fake-env-password',
  });
  expect(data).toMatchObject({
    configured: true,
    origin: 'environment',
    identity: 'env@example.test',
  });
  expect(data.notice).toContain('store');
  expect(data.notice).not.toContain(directory);
  expect(data.notice).not.toMatch(/fake-env-password|fake-node-key/);
});
