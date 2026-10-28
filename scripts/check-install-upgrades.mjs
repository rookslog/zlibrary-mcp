#!/usr/bin/env node
// Actual registry 1.4.0 -> local packed candidate -> registry 1.4.0, never global.
import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import process from 'node:process';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  packArtifact,
  inspectInstalledPackage,
  inspectBuildIdentity,
  protocolSmoke,
  acquisitionJourney,
  run,
} from './check-packed-artifact.mjs';
import { Rehearsal } from '../__tests__/rehearsal/harness.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rehearsal = path.join(root, '__tests__/rehearsal');
const digest = (data) => createHash('sha256').update(data).digest('hex');

// Snapshot user artifacts, not the replaceable installation's dependency tree.
export async function snapshotState(directory) {
  const files = {};
  async function visit(relative = '') {
    for (const entry of await readdir(path.join(directory, relative), { withFileTypes: true })) {
      const name = path.join(relative, entry.name);
      if (entry.isDirectory()) await visit(name);
      else {
        assert(entry.isFile(), `Unexpected non-file in artifact tree: ${name}`);
        const bytes = await readFile(path.join(directory, name));
        files[name] = { bytes: bytes.length, sha256: digest(bytes) };
      }
    }
  }
  await visit();
  return files;
}

export async function assertPreserved(directory, before) {
  const after = await snapshotState(directory);
  for (const [name, value] of Object.entries(before)) {
    assert(Object.hasOwn(after, name), `Existing file missing: ${name}`);
    assert.deepEqual(after[name], value, `Existing bytes changed: ${name}`);
  }
}

// End-to-end controls simulate destructive upgrade side effects after real npm replacement.
export async function mutatePreservedState(control, directory, book, state) {
  if (control === 'delete-output') await rm(path.join(directory, book));
  if (control === 'rewrite-output') {
    const file = path.join(directory, book);
    const bytes = await readFile(file);
    bytes[0] ^= 1; // same-size corruption must not evade the checksum oracle
    await writeFile(file, bytes);
  }
  if (control === 'delete-state') await rm(path.join(directory, state));
}

const savedCalls = [
  ['search_books', { query: 'sequential' }],
  ['full_text_search', { query: 'two words' }],
  ['get_download_history', {}],
  ['get_download_limits', {}],
  ['get_recent_books', {}],
  [
    'download_book_to_file',
    { bookDetails: { id: '101', hash: 'hash101' }, process_for_rag: false },
  ],
  ['download_book_to_file', { bookDetails: { id: '101', hash: 'hash101' }, process_for_rag: true }],
  ['get_book_metadata', { bookId: '101', bookHash: 'hash101' }],
  ['search_by_term', { term: 'test' }],
  ['search_by_author', { author: 'test' }],
  ['fetch_booklist', { booklistId: '1', booklistHash: 'hash', topic: 'test' }],
  ['search_advanced', { query: 'test' }],
  ['search_multi_source', { query: 'test', source: 'libgen' }],
  ['process_document_for_rag', { file_path: '/unused-saved-call.txt' }],
];

export async function main(args = process.argv.slice(2)) {
  if (args.includes('--matrix')) {
    const i = args.indexOf('--matrix');
    assert.equal(args[i + 1], 'all', 'Only --matrix all is supported');
    args = [...args.slice(0, i), '--matrix=all', ...args.slice(i + 2)];
  }
  const control = args.find((x) => x.startsWith('--control='))?.slice('--control='.length);
  assert(args.includes('--offline-providers'), 'Required: --offline-providers');
  assert(
    control === undefined || ['delete-output', 'rewrite-output', 'delete-state'].includes(control),
    'Unknown failure control',
  );
  assert(
    args.every(
      (x) => x === '--offline-providers' || x === '--matrix=all' || x === `--control=${control}`,
    ),
    'Unknown argument',
  );
  const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'zlib-upgrade-')));
  const started = Date.now();
  try {
    const home = path.join(temporary, 'home');
    const prefix = path.join(temporary, 'prefix');
    const artifacts = path.join(temporary, 'artifacts');
    const library = path.join(home, 'library');
    for (const directory of [home, prefix, artifacts, library]) await mkdir(directory);
    const env = {
      PATH: `${path.dirname(process.execPath)}${path.delimiter}${process.env.PATH}`,
      HOME: home,
      TMPDIR: temporary,
      XDG_CONFIG_HOME: home,
      XDG_CACHE_HOME: home,
      XDG_STATE_HOME: home,
      HUSKY: '0',
      npm_config_ignore_scripts: 'true',
      npm_config_cache: path.join(temporary, 'npm-cache'),
      npm_config_userconfig: path.join(temporary, 'npmrc'),
      npm_config_globalconfig: path.join(temporary, 'global-npmrc'),
      UV_CACHE_DIR: path.join(temporary, 'uv-cache'),
      UV_PYTHON_INSTALL_DIR: path.join(temporary, 'python'),
      // Legacy processing dependencies are exercised on their supported 3.13 lane.
      // This does not change the separate default-Python packed-install check.
      UV_PYTHON: process.env.UV_PYTHON || '3.13',
      PYTHONDONTWRITEBYTECODE: '1',
    };
    const tarball = await packArtifact(artifacts, env);
    const report = { candidateSha256: digest(await readFile(tarball)), stages: {} };
    const installed = path.join(prefix, 'node_modules/zlibrary-mcp');
    const python = path.join(installed, '.venv/bin/python');
    const bin = path.join(prefix, 'node_modules/.bin/zlibrary-mcp');
    async function install(target) {
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
          target,
        ],
        { cwd: prefix, env },
      );
    }
    await install('zlibrary-mcp@1.4.0');
    const oldEntry = digest(await readFile(path.join(installed, 'dist/index.js')));
    report.legacyRuntimeSha256 = oldEntry;
    assert.equal(JSON.parse(await readFile(path.join(installed, 'package.json'))).version, '1.4.0');
    await run('bash', ['setup-uv.sh'], { cwd: installed, env });
    // Published 1.4.0 lazily downloads Punkt on bridge import. Prepare this public
    // dependency before the runtime fence, never by allowing provider egress.
    const nltkData = path.join(temporary, 'nltk-data');
    await run(
      python,
      [
        '-c',
        "import nltk,sys;assert all(nltk.download(name,download_dir=sys.argv[1],raise_on_error=True) for name in ('punkt','punkt_tab'))",
        nltkData,
      ],
      {
        cwd: home,
        env,
      },
    );
    const oldPython = (await run(python, ['--version'], { env })).trim();
    await writeFile(
      path.join(installed, '.venv/t20-environment-choice'),
      'manual-extra-adoption-canary',
    );
    const fake = await Rehearsal.start('upgrade', { python: { executable: python, env } });
    try {
      const trap = path.join(home, 'egress.txt');
      await writeFile(trap, '');
      const runtime = {
        ...env,
        ...fake.providerEnvironment({ libgen: true }),
        NODE_OPTIONS: `--import=${path.join(rehearsal, 'node-isolation.mjs')}`,
        REHEARSAL_EGRESS_TRAP: trap,
        REHEARSAL_LOG_DIR: path.join(home, 'logs'),
        ZLIBRARY_SESSION_DIR: path.join(home, 'sessions'),
        ZLIBRARY_OUTPUT_ROOT: library,
        RETRY_MAX_RETRIES: '0',
        PYTHON_BRIDGE_TIMEOUT: '15000',
        LOG_LEVEL: 'warn',
        RAG_QUALITY_STRATEGY: 'technical',
        NLTK_DATA: nltkData,
      };
      await mkdir(runtime.REHEARSAL_LOG_DIR);
      // Both startup fences must be installed, not merely declared in env.
      await run(
        process.execPath,
        [
          '--input-type=module',
          '-e',
          "import net from 'node:net';try{net.connect(443,'192.0.2.1');process.exitCode=1}catch(e){if(!e.message.includes('rehearsal blocks'))throw e}",
        ],
        { cwd: home, env: runtime },
      );
      await run(
        python,
        [
          '-c',
          "import socket\ntry:\n socket.create_connection(('192.0.2.1',443))\n raise AssertionError('fence absent')\nexcept RuntimeError as e:\n assert 'rehearsal blocked' in str(e)",
        ],
        { cwd: home, env: runtime },
      );
      assert.equal((await readFile(trap, 'utf8')).trim().split('\n').length, 2);
      await writeFile(trap, '');
      await run(
        python,
        [
          '-c',
          "import sys;sys.path.insert(0,sys.argv[1]);from lib.rag.quality.pipeline import QualityPipelineConfig;assert QualityPipelineConfig.from_env().strategy=='technical';print('1.4.0 RAG_QUALITY_STRATEGY adopted')",
          installed,
        ],
        { cwd: home, env: runtime },
      );
      await fake.mode({ mode: 'healthy', reset: true, remaining: 7 });
      let original;
      let processing;
      await protocolSmoke(
        bin,
        home,
        runtime,
        null,
        '1.4.0',
        async (call) => {
          const search = await call('search_books', { query: 'sequential', count: 1 });
          assert.equal(search.books.length, 1);
          const result = await call('download_book_to_file', {
            bookDetails: search.books[0],
            outputDir: library,
            process_for_rag: false,
          });
          original = result.file_path;
          assert.deepEqual(await readFile(original), Buffer.from('Offline rehearsal book 101.\n'));
          processing = await call('process_document_for_rag', {
            file_path: original,
            output_format: 'txt',
          });
          assert(
            processing.processed_file_path && processing.metadata_file_path,
            'Legacy processing must write real text and metadata',
          );
          for (const field of ['processed_file_path', 'metadata_file_path']) {
            const file = path.resolve(home, processing[field]);
            processing[field] = file;
            assert(
              (await realpath(file)).startsWith(home + path.sep),
              'Legacy output escaped disposable home',
            );
            assert((await readFile(file)).length > 0);
          }
        },
        { legacy: true },
      );
      assert(
        (await fake.stats()).calls['/files/101.txt'] > 0,
        'Legacy download must reach fake file endpoint',
      );
      const before = await snapshotState(home);
      // Egress/logs are mutable diagnostics; outputs and metadata are never excluded.
      for (const name of Object.keys(before))
        if (name === 'egress.txt' || name.startsWith('logs' + path.sep)) delete before[name];
      assert(
        Object.keys(before).some((x) => x.endsWith('.json')),
        'No legacy sidecar state captured',
      );
      report.stages.legacy = {
        python: oldPython,
        files: before,
        processing,
        provider: await fake.stats(),
      };
      await install(tarball);
      assert.notEqual(
        digest(await readFile(path.join(installed, 'dist/index.js'))),
        oldEntry,
        'npm did not replace the legacy runtime',
      );
      const envChoice = path.join(installed, '.venv/t20-environment-choice');
      report.environmentChoiceAfterNpmReplacement = await readFile(envChoice, 'utf8').then(
        () => 'retained',
        () => 'removed',
      );
      if (control) {
        process.stdout.write(`Failure control: ${control} after in-place npm replacement\n`);
        await mutatePreservedState(
          control,
          home,
          path.relative(home, original),
          path.relative(home, processing.metadata_file_path),
        );
      }
      await assertPreserved(home, before);
      const { manifest } = await inspectInstalledPackage(installed);
      // The candidate may carry a build stamp; its server version must match it.
      const buildInfo = await inspectBuildIdentity(installed);
      await run('bash', ['setup-uv.sh', '--no-dev'], { cwd: installed, env });
      await run(
        python,
        [
          '-c',
          "import importlib.util;assert importlib.util.find_spec('nltk') is None;print('Candidate core has no NLTK processing dependency')",
        ],
        { cwd: home, env: runtime },
      );
      report.processingDependencyAfterUpgrade = 'absent';
      await assertPreserved(home, before);
      const expected = JSON.parse(
        await readFile(path.join(root, '__tests__/fixtures/tools-surface-v2.json'), 'utf8'),
      );
      await fake.mode({ mode: 'healthy', reset: true, remaining: 7 });
      const baseline = await fake.stats();
      await protocolSmoke(bin, home, runtime, expected, manifest.version, async (call, request) => {
        let rejectedCalls = 0;
        async function refuse(name, arguments_) {
          const noContact = await fake.stats();
          const filesBefore = await snapshotState(library);
          const reply = await request('tools/call', { name, arguments: arguments_ });
          const data = JSON.parse(reply.content[0].text);
          assert.equal(reply.isError, true, `Saved call unexpectedly accepted: ${name}`);
          assert.equal(data.error.code, 'invalid_argument');
          assert.equal(data.error.retry, 'no');
          assert(data.error.message.length > 0);
          assert(data.error.recovery.length > 0);
          assert.deepEqual(
            await fake.stats(),
            noContact,
            `Rejected call contacted provider: ${name}`,
          );
          assert.deepEqual(
            await snapshotState(library),
            filesBefore,
            `Rejected call changed outputs: ${name}`,
          );
          rejectedCalls++;
        }
        // Old calls are exercised cold, before login, search or any quota use.
        for (const [name, arguments_] of savedCalls) await refuse(name, arguments_);
        assert.deepEqual(await fake.stats(), baseline);
        // Use a real current ref so a stale-ref error cannot mask schema rejection.
        const searched = await call('search', {
          query: 'sequential',
          sources: ['zlibrary'],
          limit: 1,
        });
        const ref = searched.rows[0].ref;
        for (const value of [false, true, 'false', null])
          await refuse('download', { ref, process_for_rag: value });
        for (const value of ['txt', 'markdown', false, null])
          await refuse('download', { ref, processed_output_format: value });
        report.stages.migration = {
          rejectedCalls,
          providerDelta: 0,
          outputDelta: 0,
          droppedEnvironment:
            'RAG_QUALITY_STRATEGY has no processing effect; old processing calls refused',
        };
      }, { buildInfo });
      assert((await fake.stats()).logins > baseline.logins);
      for (const source of ['libgen', 'zlibrary']) {
        await fake.mode({ mode: 'healthy', reset: true, remaining: 7 });
        const output = path.join(library, 'v2-' + source);
        await mkdir(output);
        await protocolSmoke(
          bin,
          home,
          {
            ...runtime,
            ZLIBRARY_OUTPUT_ROOT: output,
            ...(source === 'libgen' ? { ZLIBRARY_EMAIL: '', ZLIBRARY_PASSWORD: '' } : {}),
          },
          expected,
          manifest.version,
          (call) => acquisitionJourney(call, fake, source, output),
          { buildInfo },
        );
      }
      await assertPreserved(home, before);
      report.stages.upgraded = {
        filesPreserved: Object.keys(before).length,
        candidateVersion: manifest.version,
      };
      const rollbackFiles = await snapshotState(home);
      for (const name of Object.keys(rollbackFiles))
        if (name === 'egress.txt' || name.startsWith('logs' + path.sep)) delete rollbackFiles[name];
      await install('zlibrary-mcp@1.4.0');
      assert.equal(
        digest(await readFile(path.join(installed, 'dist/index.js'))),
        oldEntry,
        'Rollback did not restore published runtime',
      );
      await assertPreserved(home, rollbackFiles);
      await run('bash', ['setup-uv.sh'], { cwd: installed, env });
      await protocolSmoke(
        bin,
        home,
        runtime,
        null,
        '1.4.0',
        async (call) => {
          const search = await call('search_books', { query: 'sequential', count: 1 });
          assert.equal(search.books.length, 1);
        },
        { legacy: true },
      );
      await assertPreserved(home, rollbackFiles);
      report.stages.rollback = {
        legacyStartsAndSearches: true,
        filesPreserved: Object.keys(rollbackFiles).length,
      };
      assert.equal(await readFile(trap, 'utf8'), '', 'Installed runtime attempted provider egress');
      report.elapsedMs = Date.now() - started;
      process.stdout.write(
        `UPGRADE RECEIPT ${JSON.stringify(report)}\nPASS: published 1.4.0 -> packed candidate -> 1.4.0; preserved artifacts, refused saved calls, new journeys, rollback.\n`,
      );
    } finally {
      await fake.close();
    }
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`Upgrade check failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
