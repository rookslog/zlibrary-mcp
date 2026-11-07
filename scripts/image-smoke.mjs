#!/usr/bin/env node
// Offline SDK -> container Node -> container Python -> rehearsal HTTPS journey.
// Requires a locally built image, Docker and openssl. Never pulls an image.
import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, mkdir, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { waitForFakeReady } from '../__tests__/rehearsal/fake-startup.js';
import { assertToolSchemas, resetEgressCanary } from './image-smoke-checks.mjs';

const run = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rehearsal = path.join(root, '__tests__/rehearsal');
const python = '/app/.venv/bin/python';
const mount = (src, dst, readonly = true) => [
  '--mount',
  `type=bind,src=${src},dst=${dst}${readonly ? ',readonly' : ''}`,
];

async function main(image) {
  assert(
    image && !image.startsWith('-') && process.argv.length === 3,
    'Usage: node scripts/image-smoke.mjs <local-image>',
  );
  const expectedVersion = JSON.parse(await readFile(path.join(root, 'package.json'))).version;
  const expectedSchemas = JSON.parse(await readFile(path.join(rehearsal, 'tools-schemas.json')));
  const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'zlib-image-smoke-')));
  const name = `zlib-image-smoke-${randomUUID()}`;
  const serverName = `${name}-mcp`;
  const user = `${process.getuid()}:${process.getgid()}`;
  let fake, transport, client;
  let fakeStderr = '';
  let failure;
  const docker = (args) => run('docker', args, { timeout: 10000, maxBuffer: 1024 * 1024 });
  const removeContainer = async (container) => {
    try {
      await docker(['rm', '--force', container]);
    } catch (error) {
      // --rm may already have removed a successfully stopped container.
      if (!String(error.stderr).includes(`No such container: ${container}`)) throw error;
    }
  };
  try {
    const tls = path.join(temporary, 'tls');
    const state = path.join(temporary, 'state');
    await mkdir(tls);
    await mkdir(state);
    await run(
      'openssl',
      [
        'req',
        '-x509',
        '-newkey',
        'rsa:2048',
        '-nodes',
        '-days',
        '1',
        '-subj',
        '/CN=Offline image smoke',
        '-addext',
        'subjectAltName=IP:127.0.0.1',
        '-keyout',
        path.join(tls, 'ca.key'),
        '-out',
        path.join(tls, 'ca.crt'),
      ],
      { timeout: 10000 },
    );
    const mounts = [
      ...mount(rehearsal, '/smoke/rehearsal'),
      ...mount(tls, '/smoke/tls'),
      ...mount(path.join(root, '__tests__/image-smoke'), '/smoke/controls'),
    ];
    // The fake's namespace has loopback only. The MCP container joins exactly
    // that namespace; neither has a route to a host/real provider or DNS server.
    fake = spawn(
      'docker',
      [
        'run',
        '--rm',
        '-i',
        '--pull',
        'never',
        '--name',
        name,
        '--network',
        'none',
        ...mounts,
        '--user',
        user,
        '--env',
        'PYTHONDONTWRITEBYTECODE=1',
        '--entrypoint',
        python,
        image,
        '/smoke/rehearsal/fake_eapi.py',
        '/smoke/tls/ca.crt',
        '/smoke/tls/ca.key',
      ],
      { stdio: ['pipe', 'pipe', 'pipe'] },
    );
    fake.stderr.on('data', (data) => {
      fakeStderr = (fakeStderr + data).slice(-8192);
    });
    const port = await waitForFakeReady(fake, { timeoutMs: 15000, stderr: () => fakeStderr });
    const environment = {
      HOME: '/smoke/state',
      XDG_STATE_HOME: '/smoke/state',
      XDG_CACHE_HOME: '/smoke/state',
      XDG_CONFIG_HOME: '/smoke/state',
      ZLIBRARY_SESSION_DIR: '/smoke/state/session',
      ZLIBRARY_EMAIL: 'rehearsal-account@example.invalid',
      ZLIBRARY_PASSWORD: 'rehearsal-password-canary',
      ZLIBRARY_EAPI_DOMAIN: `127.0.0.1:${port}`,
      SSL_CERT_FILE: '/smoke/tls/ca.crt',
      PYTHONPATH: '/smoke/rehearsal',
      PYTHONDONTWRITEBYTECODE: '1',
      REHEARSAL_ALLOWED_PORT: String(port),
      REHEARSAL_EGRESS_TRAP: '/smoke/state/egress-trap.txt',
      REHEARSAL_LOG_DIR: '/smoke/state/logs',
      BOOK_SOURCE_PREFLIGHT: 'false',
      BOOK_SOURCE_FALLBACK_ENABLED: 'false',
      RETRY_MAX_RETRIES: '0',
      PYTHON_BRIDGE_TIMEOUT: '15000',
    };
    transport = new StdioClientTransport({
      command: 'docker',
      args: [
        'run',
        '--rm',
        '-i',
        '--pull',
        'never',
        '--name',
        serverName,
        '--network',
        `container:${name}`,
        ...mounts,
        ...mount(state, '/smoke/state', false),
        '--user',
        user,
        '--workdir',
        '/smoke/state',
        ...Object.entries(environment).flatMap(([key, value]) => ['--env', `${key}=${value}`]),
        '--entrypoint',
        'node',
        image,
        '--import',
        '/smoke/rehearsal/node-isolation.mjs',
        '/app/dist/index.js',
      ],
      stderr: 'pipe',
    });
    // Drain diagnostics without echoing synthetic authentication/session data.
    transport.stderr.on('data', () => {});
    client = new Client({ name: 'offline-image-smoke', version: '1.0.0' });
    const protocolErrors = [];
    client.onerror = (error) => {
      protocolErrors.push(error);
    };
    // connect performs initialize and notifications/initialized through the SDK.
    await client.connect(transport, { timeout: 20000 });
    // docker exec inherits the running MCP container's user, working directory
    // and environment, including PYTHONPATH and the exact image interpreter.
    // A kernel denial does not pass: require the fence exception AND trap file.
    await docker(['exec', serverName, python, '/smoke/controls/egress-canary.py']);
    await resetEgressCanary(path.join(state, 'egress-trap.txt'));
    assert.equal(
      client.getServerVersion()?.version,
      expectedVersion,
      'serverInfo.version must equal the package version',
    );
    const { tools } = await client.listTools({}, { timeout: 10000 });
    assert.deepEqual(
      tools.map((tool) => tool.name).sort(),
      ['download', 'get_book', 'get_history', 'get_limits', 'search'],
      'exactly five public tools',
    );
    assertToolSchemas(tools, expectedSchemas);
    const reply = await client.callTool(
      {
        name: 'search',
        arguments: { query: 'sequential', sources: ['zlibrary'] },
      },
      undefined,
      { timeout: 25000 },
    );
    assert.notEqual(reply.isError, true, 'search must not return a tool error');
    assert.equal(reply.content?.length, 1, 'search has one JSON text envelope');
    assert.equal(reply.content[0].type, 'text', 'search has a JSON text envelope');
    const data = JSON.parse(reply.content[0].text);
    assert(Array.isArray(data.rows) && data.rows.length === 1, 'search rows envelope');
    assert.equal(data.rows[0].title, 'Rehearsal sequential', 'fixed fake catalogue result');
    assert.equal(data.rows[0].source, 'zlibrary', 'selected fake source');
    assert.equal(typeof data.rows[0].ref, 'string', 'process-local selection ref');
    assert(data.rows[0].ref.length > 0, 'nonempty selection ref');
    assert.deepEqual(reply.structuredContent, data, 'text and structured search envelopes agree');
    assert.equal(data.all_answered, true, 'all intended sources answered');
    assert.equal(data.sources?.zlibrary?.state, 'rows', 'selected source accounting');
    assert.equal(data.sources?.zlibrary?.rows, 1, 'selected source row count');
    assert(Number.isInteger(data.sources?.zlibrary?.ms) && data.sources.zlibrary.ms >= 0,
      'selected source elapsed time');
    for (const source of ['libgen', 'annas'])
      assert.equal(data.sources?.[source]?.state, 'not_asked', 'unrequested source accounting');
    // Fetch stats inside the isolated namespace, not via a published host port.
    const { stdout } = await docker([
      'exec',
      name,
      python,
      '-c',
      'import json,ssl,urllib.request,sys; print(urllib.request.urlopen(sys.argv[1],context=ssl.create_default_context(cafile="/smoke/tls/ca.crt"),timeout=3).read().decode())',
      `https://127.0.0.1:${port}/__stats`,
    ]);
    const stats = JSON.parse(stdout);
    assert.equal(
      stats.calls['/eapi/book/search'],
      1,
      'exactly one fake provider search via Python bridge',
    );
    assert(stats.loginSuccesses >= 1, 'container Python bridge authenticated to fake provider');
    await client.close();
    assert.equal(protocolErrors.length, 0, 'stdio must contain only valid MCP messages');
    let trap = '';
    try {
      trap = await readFile(path.join(state, 'egress-trap.txt'), 'utf8');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    assert.equal(trap, '', 'no blocked egress attempt');
  } catch (error) {
    failure = error;
  } finally {
    const cleanup = await Promise.allSettled([
      (async () => {
        try {
          await client?.close();
        } finally {
          await removeContainer(serverName);
        }
      })(),
      removeContainer(name),
    ]);
    fake?.stdin.end();
    // Removing the container closes the attached Docker client. Bound its owner
    // process as well, including startup failures before a container was created.
    if (fake && fake.exitCode === null && fake.signalCode === null) {
      await new Promise((resolve) => {
        const timer = setTimeout(() => {
          fake.kill('SIGKILL');
          resolve();
        }, 2000);
        fake.once('close', () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
    for (const result of cleanup)
      if (result.status === 'rejected')
        failure ??= new Error(`Docker cleanup failed: ${result.reason.message}`);
    await rm(temporary, { recursive: true, force: true });
  }
  if (failure) throw failure;
  process.stdout.write(
    `Image smoke passed: ${image}; version=${expectedVersion}; tools=5; Python bridge search=1; egress=0\n`,
  );
}

main(process.argv[2]).catch((error) => {
  process.stderr.write(`Image smoke failed: ${error.message}\n`);
  process.exitCode = 1;
});
