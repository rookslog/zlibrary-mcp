#!/usr/bin/env node
/* global process, setTimeout, clearTimeout */
// Offline installed-package smoke. Dependency installation may use registries;
// real provider egress is fenced; accounts used below are synthetic fixtures.
import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { spawn, execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import {
  access,
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  realpath,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { Rehearsal } from '../__tests__/rehearsal/harness.js';
import { getRuntimeDigest } from '../dist/lib/build-info.js';

const execute = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rehearsal = path.join(root, '__tests__/rehearsal');

// Release compatibility and build attribution are separate acceptance checks.
export function assertServerVersion(actual, release, buildInfo) {
  assert.equal(typeof actual, 'string', 'Server version must be a string');
  const [base, metadata, ...extra] = actual.split('+');
  assert.equal(base, release, 'Server release differs from package manifest');
  assert.equal(extra.length, 0, 'Invalid build metadata');
  if (metadata !== undefined)
    assert(/^[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*$/.test(metadata), 'Invalid build metadata');
  const identity = buildInfo?.commit == null
    ? undefined
    : `${buildInfo.commit}${buildInfo.dirty === true ? '.dirty' : ''}`;
  assert.equal(metadata, identity, 'Server build identity differs from packed stamp');
}

export async function inspectBuildIdentity(directory) {
  let info;
  try { info = JSON.parse(await readFile(path.join(directory, 'build-info.json'), 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
  if (info.commit === null) return info;
  assert(typeof info.commit === 'string' && /^[0-9a-f]{7,40}$/.test(info.commit), 'Invalid packed commit');
  assert.equal(info.runtime_sha256, getRuntimeDigest(path.join(directory, 'dist')), 'Packed runtime digest mismatch');
  return info;
}

export async function inspectInstalledPackage(directory) {
  const manifest = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
  const targets = Object.entries(
    typeof manifest.bin === 'string' ? { [manifest.name]: manifest.bin } : (manifest.bin ?? {}),
  ).map(([name, target]) => ({ owner: `bin:${name}`, target }));
  for (const [name, command] of Object.entries(manifest.scripts ?? {})) {
    // Build/test/prepare commands are documented source-checkout commands.
    if (!/^(doctor(?::.*)?|validate(?::.*)?|postbuild|audit:release|start)$/.test(name)) continue;
    const paths = command.match(/(?:scripts|dist)\/[^\s"';&|]+\.(?:m?js|py|sh)\b/g);
    assert(paths?.length, `Cannot inspect advertised command (${name}): ${command}`);
    targets.push(...paths.map((target) => ({ owner: name, target })));
  }
  for (const { owner, target } of targets) {
    const file = path.resolve(directory, target);
    assert(file.startsWith(directory + path.sep), `Target escapes installed package: ${target}`);
    assert(
      await stat(file).then(
        (s) => s.isFile(),
        () => false,
      ),
      `Advertised target missing (${owner}): ${target}`,
    );
  }
  assert(
    await stat(path.join(directory, 'lib/python_bridge.py')).then(
      (s) => s.isFile(),
      () => false,
    ),
    'Required packed asset missing: lib/python_bridge.py',
  );
  return { manifest, targets };
}

export async function run(command, args, options = {}) {
  process.stdout.write(`> ${command} ${args.join(' ')}\n`);
  try {
    const result = await execute(command, args, {
      timeout: 180000,
      maxBuffer: 4 * 1024 * 1024,
      ...options,
    });
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
    return result.stdout;
  } catch (error) {
    process.stdout.write(error.stdout ?? '');
    process.stderr.write(error.stderr ?? '');
    throw error;
  }
}

export async function packArtifact(destination, env) {
  await run('npm', ['pack', '--ignore-scripts', '--json', '--pack-destination', destination], {
    cwd: root,
    env: { ...env, npm_config_ignore_scripts: 'true' },
  });
  // npm 10 may emit a prepare/Husky diagnostic ahead of --json output.
  // The materialized artifact, not npm stdout, is the authority.
  const tarballs = (await readdir(destination, { withFileTypes: true })).filter(
    (entry) => entry.isFile() && entry.name.endsWith('.tgz'),
  );
  assert.equal(tarballs.length, 1, 'Expected exactly one .tgz in pack destination');
  return path.join(destination, tarballs[0].name);
}

export async function protocolSmoke(
  bin,
  cwd,
  env,
  expected,
  version,
  journey,
  { legacy = false, buildInfo } = {},
) {
  // Spawn the npm-created executable link itself, not node <source>/dist/index.js.
  await access(bin);
  const child = spawn(bin, [], { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const closed = once(child, 'close');
  // Attach handlers before any asynchronous work or failed spawn can surface.
  closed.catch(() => {});
  let stderr = '';
  let failure;
  let nextId = 0;
  let responses = 0;
  const pending = new Map();
  const fail = (error) => {
    failure ??= error;
    for (const request of pending.values()) request.reject(error);
    pending.clear();
  };
  child.stderr.on('data', (data) => {
    stderr += data;
  });
  child.once('error', fail);
  child.stdin.on('error', fail);
  child.once('close', (code, signal) => {
    if (pending.size)
      fail(new Error(`Installed bin exited during request (${code}/${signal}): ${stderr}`));
  });
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', (line) => {
    try {
      const message = JSON.parse(line); // No grep/filter: even blank/log lines fail.
      assert.equal(message.jsonrpc, '2.0');
      assert(message.method || Object.hasOwn(message, 'id'), 'Invalid JSON-RPC envelope');
      if (Object.hasOwn(message, 'id')) {
        const request = pending.get(message.id);
        assert(request, `Unexpected response ID: ${message.id}`);
        assert(!message.error, JSON.stringify(message.error));
        assert(Object.hasOwn(message, 'result'), 'Response has no result');
        pending.delete(message.id);
        responses++;
        request.resolve(message.result);
      }
    } catch (error) {
      fail(error);
    }
  });
  async function request(method, params) {
    if (failure) throw failure;
    const id = ++nextId;
    let timer;
    try {
      return await new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        timer = setTimeout(
          () => fail(new Error(`Installed MCP request timed out: ${method}; ${stderr}`)),
          15000,
        );
        child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      });
    } finally {
      clearTimeout(timer);
    }
  }
  async function call(name, args) {
    const reply = await request('tools/call', { name, arguments: args });
    assert(!reply.isError, `${name} returned a tool error: ${reply.content[0].text}`);
    const data = JSON.parse(reply.content[0].text);
    assert(
      !reply.isError && !data.error,
      `${name} returned a tool error: ${JSON.stringify(data.error)}`,
    );
    assert.deepEqual(data, reply.structuredContent);
    return data;
  }
  try {
    const initialized = await request('initialize', {
      protocolVersion: '2025-11-25',
      capabilities: {},
      clientInfo: { name: 'packed-install-smoke', version: '1' },
    });
    assert.equal(initialized.serverInfo.name, legacy ? 'zlibrary-mcp' : 'book-acquisition');
    assertServerVersion(initialized.serverInfo.version, version, buildInfo);
    child.stdin.write(
      JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n',
    );
    const listed = await request('tools/list', {});
    const surface = listed.tools
      .map((tool) => ({
        name: tool.name,
        arguments: Object.keys(tool.inputSchema.properties ?? {}).sort(),
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    if (expected)
      assert.deepEqual(surface, expected, 'Installed tools differ from the 2.0 fixture');
    if (legacy) {
      assert.equal(surface.length, 13);
      assert(surface.some((tool) => tool.name === 'process_document_for_rag'));
      process.stdout.write('Installed legacy bin: initialize and tools/list (13 tools) passed.\n');
    } else {
      const data = await call('get_limits', { mode: 'local' });
      assert.equal(data.sources.libgen.download_available, true);
      assert.equal(data.sources.zlibrary.configured, Boolean(env.ZLIBRARY_EMAIL));
      process.stdout.write(
        `Installed bin: initialize, tools/list (${surface.length} tools), get_limits local passed.\n`,
      );
    }
    if (journey) await journey(call, request);
  } finally {
    child.stdin.end();
    const kill = setTimeout(() => child.kill('SIGTERM'), 1000);
    const force = setTimeout(() => child.kill('SIGKILL'), 3000);
    try {
      const [code, signal] = await closed;
      assert.equal(signal, null, `Installed bin needed forced cleanup: ${stderr}`);
      assert.equal(code, 0, `Installed bin exited nonzero: ${stderr}`);
    } finally {
      clearTimeout(kill);
      clearTimeout(force);
      lines.close();
    }
  }
  if (failure) throw failure;
  assert.equal(responses, nextId);
}

export async function acquisitionJourney(call, fake, source, output, dropTransfer) {
  const zlibrary = source === 'zlibrary';
  const expectedBytes = zlibrary
    ? Buffer.from('Offline rehearsal book 101.\n')
    : Buffer.concat([
        Buffer.from([0x50, 0x4b, 3, 4]),
        Buffer.from('Synthetic fixture 301\n'.repeat(60)),
      ]);
  const query = zlibrary ? 'sequential' : 'Persuasion';
  const search = await call('search', { sources: [source], query, limit: 1 });
  assert.equal(search.rows.length, 1, `Expected one ${source} fixture row`);
  const row = search.rows[0];
  assert.equal(row.source, source);
  assert.equal(row.title, zlibrary ? 'Rehearsal sequential' : 'Persuasion');
  const detail = await call('get_book', { ref: row.ref });
  assert.equal(detail.ref, row.ref);
  assert.equal(detail.title, row.title);
  const args = {
    ref: row.ref,
    output_dir: output,
    filename: `packed-${source}.${zlibrary ? 'txt' : 'epub'}`,
  };
  if (dropTransfer) {
    await fake.mode({ mode: 'drop_transfer' });
    process.stdout.write(
      `Failure control: drop-transfer (${source}); search/details completed, file endpoint returns no bytes.\n`,
    );
  }
  const first = await call('download', args);
  assert.equal(first.path, path.join(output, args.filename));
  assert.equal(await realpath(first.path), first.path);
  assert.equal(first.record, first.path + '.acq.json');
  assert.equal(first.verified, 'sha256');
  assert.equal(first.transfer_skipped, false);
  assert.equal(first.reused, false);
  assert.equal(first.bytes, expectedBytes.length);
  assert.deepEqual(await readFile(first.path), expectedBytes, 'Downloaded fixture bytes differ');
  const receiptBytes = await readFile(first.record);
  const receipt = JSON.parse(receiptBytes);
  const digest = createHash('sha256').update(expectedBytes).digest('hex');
  assert.equal(receipt.record_version, 1);
  assert.equal(receipt.state, 'complete');
  assert.equal(receipt.path, first.path);
  assert.equal(receipt.bytes, expectedBytes.length);
  assert.equal(receipt.sha256, digest);
  assert.equal(receipt.source, source);
  assert.equal(receipt.request.ref, row.ref);
  const before = await fake.stats();
  if (zlibrary) {
    assert.equal(before.quotaSpent, 1);
    assert.equal(before.calls['/files/101.txt'], 1);
    assert.equal(receipt.identifiers.id, '101');
  } else {
    // One range probe plus one actual acquisition stream.
    assert.equal(before.calls['/libgen/get.php'], 2);
    assert.equal(before.logins, 0, 'LibGen journey must remain credential-free');
    assert.equal(receipt.identifiers.md5, createHash('md5').update(expectedBytes).digest('hex'));
  }
  const repeat = await call('download', args);
  assert.equal(repeat.transfer_skipped, true, 'Repeated download did not hit its receipt');
  assert.equal(repeat.reused, false);
  assert.equal(repeat.path, first.path);
  assert.equal(repeat.record, first.record);
  assert.equal(repeat.bytes, first.bytes);
  assert.equal(repeat.verified, 'sha256');
  assert.deepEqual(await readFile(first.path), expectedBytes);
  assert.deepEqual(await readFile(first.record), receiptBytes, 'Receipt hit rewrote provenance');
  const after = await fake.stats();
  assert.deepEqual(after.calls, before.calls, 'Receipt hit contacted a fake provider');
  assert.equal(after.quotaSpent, before.quotaSpent);
  process.stdout.write(
    `Installed ${source} journey passed: search -> get_book -> download -> receipt hit; bytes=${first.bytes}; sha256=${digest}; body endpoint requests=${before.calls[zlibrary ? '/files/101.txt' : '/libgen/get.php']}; repeat provider calls=0.\n`,
  );
}

export async function main(args = process.argv.slice(2)) {
  assert(
    args.includes('--offline-providers'),
    'Required: --offline-providers (live providers are not supported)',
  );
  const control = args.find((arg) => arg.startsWith('--control='))?.split('=')[1];
  assert(
    !control || ['missing-bridge', 'missing-script', 'drop-transfer'].includes(control),
    'Unknown failure control',
  );
  assert(
    args.every((arg) => arg === '--offline-providers' || arg === `--control=${control}`),
    'Unknown argument',
  );
  // macOS exposes /var via /private/var; ESM resolves the physical path.
  const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'zlib-packed-')));
  try {
    const home = path.join(temporary, 'home');
    const prefix = path.join(temporary, 'prefix');
    await mkdir(home);
    await mkdir(prefix);
    // Do not inherit credentials, source defaults, Python paths, Node preloads,
    // user npm config, or account/session state from the operator's environment.
    const env = {
      PATH: `${path.dirname(process.execPath)}${path.delimiter}${process.env.PATH}`,
      HOME: home,
      TMPDIR: temporary,
      XDG_CONFIG_HOME: home,
      XDG_CACHE_HOME: home,
      XDG_STATE_HOME: home,
      HUSKY: '0',
      npm_config_cache: path.join(temporary, 'npm-cache'),
      npm_config_userconfig: path.join(temporary, 'npmrc'),
      npm_config_globalconfig: path.join(temporary, 'global-npmrc'),
      UV_CACHE_DIR: path.join(temporary, 'uv-cache'),
      UV_PYTHON_INSTALL_DIR: path.join(temporary, 'python'),
      // Optional diagnostic selection only; CI/default installs remain unpinned.
      ...(process.env.UV_PYTHON ? { UV_PYTHON: process.env.UV_PYTHON } : {}),
      PYTHONDONTWRITEBYTECODE: '1',
    };
    const tarball = await packArtifact(temporary, env);
    if (control === 'missing-bridge' || control === 'missing-script') {
      const scratch = path.join(temporary, 'damaged');
      await mkdir(scratch);
      await run('tar', ['xzf', tarball, '-C', scratch], { env });
      const omitted =
        control === 'missing-bridge' ? 'lib/python_bridge.py' : 'scripts/check_upstream.py';
      await rm(path.join(scratch, 'package', omitted));
      await run('tar', ['czf', tarball, '-C', scratch, 'package'], { env });
      process.stdout.write(
        `Failure control: removed ${omitted} from the tarball before install.\n`,
      );
    }
    process.stdout.write(
      `Tarball SHA-256: ${createHash('sha256')
        .update(await readFile(tarball))
        .digest('hex')}\n`,
    );
    await run(
      'npm',
      [
        'install',
        '--prefix',
        prefix,
        '--ignore-scripts',
        '--no-audit',
        '--no-fund',
        '--package-lock=false',
        tarball,
      ],
      { cwd: prefix, env },
    );
    const installed = path.join(prefix, 'node_modules/zlibrary-mcp');
    const { manifest, targets } = await inspectInstalledPackage(installed);
    const buildInfo = await inspectBuildIdentity(installed);
    process.stdout.write(`Installed target checks: ${JSON.stringify(targets)}\n`);
    // README Quick Start, with --no-dev: no test/processing extras or hand-added deps.
    await run('bash', ['setup-uv.sh', '--no-dev'], { cwd: installed, env });
    const python = path.join(installed, '.venv/bin/python');
    const trap = path.join(temporary, 'egress.txt');
    const runtime = {
      ...env,
      PYTHONPATH: rehearsal,
      NODE_OPTIONS: `--import=${path.join(rehearsal, 'node-isolation.mjs')}`,
      REHEARSAL_EGRESS_TRAP: trap,
      REHEARSAL_LOG_DIR: path.join(home, 'logs'),
      ZLIBRARY_SESSION_DIR: path.join(home, 'sessions'),
      LOG_LEVEL: 'info',
    };
    await mkdir(runtime.REHEARSAL_LOG_DIR);
    // Fail if either startup fence did not load: both attempts must be blocked
    // and counted. No allowed fake port => all IP/TCP destinations are refused.
    await writeFile(trap, '');
    await run(
      process.execPath,
      [
        '--input-type=module',
        '-e',
        "import net from 'node:net'; try { net.connect(443, '192.0.2.1'); process.exitCode=1; } catch (e) { if (!e.message.includes('rehearsal blocks')) throw e; }",
      ],
      { cwd: home, env: runtime },
    );
    await run(
      python,
      [
        '-c',
        "import socket\ntry:\n socket.create_connection(('192.0.2.1', 443))\n raise AssertionError('Python fence not loaded')\nexcept RuntimeError as e:\n assert 'rehearsal blocked' in str(e)\n",
      ],
      { cwd: home, env: runtime },
    );
    assert.equal(
      (await readFile(trap, 'utf8')).trim().split('\n').length,
      2,
      'Both network fences must fire',
    );
    await writeFile(trap, '');
    await run(
      python,
      [
        '-c',
        "import importlib.util, pathlib, sys, zlibrary\nroot = pathlib.Path(sys.argv[1]).resolve()\nassert root in pathlib.Path(zlibrary.__file__).resolve().parents\nassert importlib.util.find_spec('pytest') is None\nsys.path.insert(0, str(root / 'lib'))\nimport python_bridge\nassert pathlib.Path(python_bridge.__file__).resolve() == root / 'lib/python_bridge.py'\nprint('Installed core-only Python bridge imports passed')",
        installed,
      ],
      { cwd: home, env: runtime },
    );
    await run(
      process.execPath,
      [
        '--input-type=module',
        '-e',
        "import { pathToFileURL } from 'node:url'; import assert from 'node:assert/strict'; const root = process.argv[1]; const paths = await import(pathToFileURL(root + '/dist/lib/paths.js')); const venv = await import(pathToFileURL(root + '/dist/lib/venv-manager.js')); assert.equal(paths.getPythonScriptPath('python_bridge.py'), root + '/lib/python_bridge.py'); assert.equal(await venv.getManagedPythonPath(), root + '/.venv/bin/python');",
        installed,
      ],
      { cwd: home, env: runtime },
    );
    await run('npm', ['run', 'validate'], { cwd: installed, env: runtime });
    const bin = path.join(prefix, 'node_modules/.bin/zlibrary-mcp');
    assert.equal(await realpath(bin), path.join(installed, manifest.bin['zlibrary-mcp']));
    const expected = JSON.parse(
      await readFile(path.join(root, '__tests__/fixtures/tools-surface-v2.json'), 'utf8'),
    );
    // The same startup/certificate/fence wiring is used by source rehearsal.
    // Its fake runs using the installed core Python, not the contributor venv.
    const fake = await Rehearsal.start('packed-install', { python: { executable: python, env } });
    try {
      for (const source of ['libgen', 'zlibrary']) {
        await fake.mode({ mode: 'healthy', reset: true, remaining: 7 });
        const output = path.join(home, 'outputs', source);
        await mkdir(output, { recursive: true });
        const providerEnv = {
          ...runtime,
          ...fake.providerEnvironment({ libgen: true, credentials: source === 'zlibrary' }),
          ZLIBRARY_OUTPUT_ROOT: output,
          RETRY_MAX_RETRIES: '0',
          PYTHON_BRIDGE_TIMEOUT: '15000',
        };
        await protocolSmoke(bin, home, providerEnv, expected, manifest.version, (call) =>
          acquisitionJourney(call, fake, source, output, control === 'drop-transfer'),
          { buildInfo },
        );
        assert.equal(await readFile(trap, 'utf8'), '', 'Journey attempted non-loopback egress');
      }
    } finally {
      try {
        process.stdout.write(`Final fake counters: ${JSON.stringify(await fake.stats())}\n`);
        assert.equal(await readFile(trap, 'utf8'), '', 'Journey attempted non-loopback egress');
      } finally {
        await fake.close();
      }
    }
    assert.equal(await readFile(trap, 'utf8'), '', 'Installed runtime attempted provider egress');
    process.stdout.write(
      'PASS: exact packed package installed clean; offline runtime had zero non-loopback egress attempts.\n',
    );
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`Packed artifact check failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
