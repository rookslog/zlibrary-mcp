import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  symlink,
  writeFile,
} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { baselinePin } from './tasks.js';

const scopes = [
  'src',
  'lib',
  'zlibrary',
  'tsconfig.json',
  'package.json',
  'package-lock.json',
  'pyproject.toml',
  'uv.lock',
];
const pythonConfigs = ['pyproject.toml', 'uv.lock'];
const configs = ['tsconfig.json', 'package.json', 'package-lock.json'];
export const artifactProofMethod = 'clean-pinned-tsc-runtime-byte-comparison';
const sha256 = (value) => createHash('sha256').update(value).digest('hex');
export const manifestDigest = (files) => sha256(JSON.stringify(files));
const git = (dir, args) =>
  execFileSync(
    'git',
    ['-C', dir, '-c', 'filter.lfs.process=', '-c', 'filter.lfs.required=false', ...args],
    { env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' }, maxBuffer: 16 * 1024 * 1024 },
  );
export const gitRead = (dir, ...args) => git(dir, args).toString('utf8').trim();

// Compare actual bytes with Git blobs, not only status (which can hide assume-unchanged files).
export async function pinnedInputs(buildRoot, pin) {
  const sha = gitRead(buildRoot, 'rev-parse', 'HEAD');
  if (pin && sha !== gitRead(buildRoot, 'rev-parse', `${pin}^{commit}`))
    throw new Error('server checkout does not match pinned commit');
  if (gitRead(buildRoot, 'status', '--porcelain', '--untracked-files=all', '--', ...scopes))
    throw new Error('dirty production source/config inputs');
  const files = [],
    contents = new Map();
  for (const item of git(buildRoot, ['ls-tree', '-rz', sha, '--', ...scopes])
    .toString('utf8')
    .split('\0')
    .filter(Boolean)) {
    const match = /^(100644|100755) blob ([a-f0-9]{40})\t(.+)$/.exec(item);
    if (!match) throw new Error('unsupported pinned source entry');
    const [, , blob, name] = match,
      file = path.join(buildRoot, name);
    if (!(await lstat(file)).isFile()) throw new Error('source/config symlink or non-file');
    const bytes = await readFile(file);
    const actual = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (actual !== blob) throw new Error('dirty pinned source/config bytes: ' + name);
    files.push({ path: name, sha256: sha256(bytes) });
    contents.set(name, bytes);
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  for (const name of [...configs, ...pythonConfigs, 'src/index.ts'])
    if (!contents.has(name)) throw new Error('missing pinned input: ' + name);
  return { sha, inputs: { sha256: manifestDigest(files), files }, contents };
}

export async function resolveBaselinePin(buildRoot, requestedPin) {
  if (requestedPin)
    return {
      pin: gitRead(buildRoot, 'rev-parse', `${requestedPin}^{commit}`),
      requestedPin,
      root: path.resolve(buildRoot),
      mode: 'explicit',
    };
  const before = await pinnedInputs(buildRoot);
  if (gitRead(buildRoot, 'diff', '--name-only', baselinePin, before.sha, '--', ...scopes))
    throw new Error(
      'default A checkout is not source-equivalent to settled baseline; select a compatible --a-root or explicit --a-pin',
    );
  const after = await pinnedInputs(buildRoot);
  if (before.sha !== after.sha || before.inputs.sha256 !== after.inputs.sha256)
    throw new Error('baseline identity changed while resolving');
  return {
    pin: before.sha,
    root: path.resolve(buildRoot),
    mode: 'source-equivalent-head',
    settledBaseline: baselinePin,
    inputsSha256: before.inputs.sha256,
  };
}

const runtimeFile = (name) => /\.(?:js|mjs|cjs|json|node|wasm)$/.test(name);
const metadataFile = (name) => /(?:\.map|\.d\.(?:ts|mts|cts)|\.tsbuildinfo)$/.test(name);
export async function distSnapshot(directory) {
  const full = createHash('sha256'),
    files = [];
  let count = 0;
  const visit = async (relative = '') => {
    for (const entry of (
      await readdir(path.join(directory, relative), { withFileTypes: true })
    ).sort((a, b) => a.name.localeCompare(b.name))) {
      const name = path.join(relative, entry.name),
        file = path.join(directory, name);
      if (entry.isSymbolicLink()) throw new Error('runtime artifact symlink: ' + name);
      if (entry.isDirectory()) {
        await visit(name);
        continue;
      }
      if (!entry.isFile()) throw new Error('unsupported runtime artifact: ' + name);
      const bytes = await readFile(file);
      full.update(JSON.stringify([name, (await lstat(file)).mode]));
      full.update(bytes);
      full.update('\0');
      count++;
      if (runtimeFile(name)) files.push({ path: name, sha256: sha256(bytes) });
      else if (!metadataFile(name)) throw new Error('unclassified runtime artifact: ' + name);
    }
  };
  await visit();
  files.sort((a, b) => a.path.localeCompare(b.path));
  return {
    runtime: { sha256: manifestDigest(files), files },
    fullDist: { sha256: full.digest('hex'), files: count },
  };
}

async function compilerIdentity(buildRoot) {
  const directory = await realpath(path.join(buildRoot, 'node_modules/typescript'));
  const files = [];
  const visit = async (relative) => {
    const file = path.join(directory, relative),
      stat = await lstat(file);
    if (stat.isSymbolicLink()) throw new Error('compiler symlink within installed package');
    if (stat.isDirectory())
      for (const entry of (await readdir(file)).sort()) await visit(path.join(relative, entry));
    else files.push({ path: relative, sha256: sha256(await readFile(file)) });
  };
  await visit('package.json');
  await visit('bin');
  await visit('lib');
  files.sort((a, b) => a.path.localeCompare(b.path));
  const version = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8')).version;
  return { directory, identity: { version, sha256: manifestDigest(files), files: files.length } };
}

export async function verifyBuildArtifacts({
  buildRoot,
  distRoot = path.join(buildRoot, 'dist'),
  pin,
  compileTimeoutMs = 60000,
}) {
  const before = await pinnedInputs(buildRoot, pin),
    compiler = await compilerIdentity(buildRoot),
    selected = await distSnapshot(distRoot);
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'eval-pinned-build-'));
  const args = [
    '--project',
    'tsconfig.json',
    '--outDir',
    'dist',
    '--incremental',
    'false',
    '--noEmitOnError',
    'true',
  ];
  try {
    // These buffers were independently checked against the selected commit's blobs.
    for (const [name, bytes] of before.contents)
      if (name.startsWith('src/') || configs.includes(name)) {
        await mkdir(path.dirname(path.join(scratch, name)), { recursive: true });
        await writeFile(path.join(scratch, name), bytes);
      }
    await symlink(
      await realpath(path.join(buildRoot, 'node_modules')),
      path.join(scratch, 'node_modules'),
      'dir',
    );
    try {
      execFileSync(process.execPath, [path.join(compiler.directory, 'bin/tsc'), ...args], {
        cwd: scratch,
        encoding: 'utf8',
        stdio: 'pipe',
        timeout: compileTimeoutMs,
        maxBuffer: 8 * 1024 * 1024,
      });
    } catch (error) {
      throw new Error(
        'pinned TypeScript compilation failed: ' +
          [error.message, error.stdout, error.stderr].filter(Boolean).join('\n'),
      );
    }
    const compiled = await distSnapshot(path.join(scratch, 'dist'));
    if (
      !compiled.runtime.files.some((f) => f.path === 'index.js') ||
      JSON.stringify(compiled.runtime) !== JSON.stringify(selected.runtime)
    )
      throw new Error('selected runtime artifact set/bytes differ from clean pinned compilation');
    const after = await pinnedInputs(buildRoot, pin),
      compilerAfter = await compilerIdentity(buildRoot),
      selectedAfter = await distSnapshot(distRoot);
    if (
      before.sha !== after.sha ||
      before.inputs.sha256 !== after.inputs.sha256 ||
      JSON.stringify(compiler.identity) !== JSON.stringify(compilerAfter.identity) ||
      JSON.stringify(selected) !== JSON.stringify(selectedAfter)
    )
      throw new Error('source/config/compiler/runtime identity changed during build verification');
    return {
      version: 1,
      method: artifactProofMethod,
      status: 'verified',
      identityStable: true,
      sha: before.sha,
      inputs: before.inputs,
      compiler: compiler.identity,
      command: { binary: 'node', compiler: 'typescript/bin/tsc', args },
      compiledRuntime: compiled.runtime,
      selectedRuntime: selected.runtime,
      fullDist: selected.fullDist,
      boundary:
        'Exact runtime executable/data files only; maps/declarations/build metadata excluded from byte comparison, full selected dist digest retained. Existing local compiler/dependencies; no install or package script.',
    };
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
}

// Structural admission is not a signature: it requires the producer's concrete
// reproducibility proof, not a syntactically valid arbitrary dist digest.
export function artifactProofErrors(proof, build) {
  const errors = [],
    hash = /^[a-f0-9]{64}$/;
  const fail = (message) => errors.push('artifact proof: ' + message);
  const manifest = (value, runtime = false) => {
    if (!Array.isArray(value?.files) || !value.files.length || !hash.test(value.sha256 ?? ''))
      return false;
    const names = value.files.map((f) => f.path);
    if (
      new Set(names).size !== names.length ||
      names.some(
        (n) =>
          typeof n !== 'string' ||
          n.startsWith('/') ||
          n.split(/[\\/]/).some((p) => p === '..' || p === '.' || !p) ||
          (runtime && !runtimeFile(n)),
      )
    )
      return false;
    if (value.files.some((f) => !hash.test(f.sha256 ?? ''))) return false;
    return (
      value.sha256 === manifestDigest(value.files) &&
      names.join('\0') === [...names].sort((a, b) => a.localeCompare(b)).join('\0')
    );
  };
  if (
    !proof ||
    proof.version !== 1 ||
    proof.method !== artifactProofMethod ||
    proof.status !== 'verified' ||
    proof.identityStable !== true ||
    proof.sha !== build?.sha
  )
    fail('missing/unverified or inconsistent revision/identity');
  if (
    !manifest(proof?.inputs) ||
    ![...configs, ...pythonConfigs, 'src/index.ts'].every((n) =>
      proof?.inputs?.files?.some((f) => f.path === n),
    )
  )
    fail('missing/inconsistent pinned inputs/config/lock');
  if (
    !hash.test(proof?.compiler?.sha256 ?? '') ||
    typeof proof?.compiler?.version !== 'string' ||
    !proof.compiler.version ||
    !Number.isSafeInteger(proof?.compiler?.files) ||
    proof.compiler.files < 1
  )
    fail('missing compiler identity');
  if (
    !manifest(proof?.compiledRuntime, true) ||
    !manifest(proof?.selectedRuntime, true) ||
    !proof?.selectedRuntime?.files?.some((f) => f.path === 'index.js') ||
    JSON.stringify(proof?.compiledRuntime) !== JSON.stringify(proof?.selectedRuntime)
  )
    fail('runtime manifests missing/mismatched');
  if (
    !hash.test(proof?.fullDist?.sha256 ?? '') ||
    proof.fullDist.sha256 !== build?.source?.trees?.dist?.sha256 ||
    proof.fullDist.files !== build?.source?.trees?.dist?.files
  )
    fail('full dist/source binding mismatch');
  if (proof?.inputs?.sha256 !== build?.source?.trees?.productionInputs?.sha256)
    fail('pinned source/config binding mismatch');
  if (
    proof?.command?.compiler !== 'typescript/bin/tsc' ||
    proof?.command?.binary !== 'node' ||
    JSON.stringify(proof?.command?.args) !==
      JSON.stringify([
        '--project',
        'tsconfig.json',
        '--outDir',
        'dist',
        '--incremental',
        'false',
        '--noEmitOnError',
        'true',
      ])
  )
    fail('unexpected compilation recipe');
  return errors;
}
