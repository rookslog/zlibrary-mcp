import { beforeAll, afterAll, test, expect } from '@jest/globals';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, symlinkSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { protocolSmoke, assertServerVersion, inspectBuildIdentity } from '../scripts/check-packed-artifact.mjs';
import { getServerVersion } from '../dist/index.js';

const source = fileURLToPath(new URL('../', import.meta.url));
const pkg = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8')).version;
let temporary, clean, nested, foreign, longCommit;
const env = { PATH: process.env.PATH, HUSKY: '0', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' };
function run(cwd, command, args, extra = {}) {
  return execFileSync(command, args, { cwd, env: { ...env, ...extra }, encoding: 'utf8', timeout: 300000, stdio: ['pipe', 'pipe', 'pipe'] }).trim();
}
function git(cwd, ...args) { return run(cwd, 'git', args); }
function initialize(root) {
  git(root, 'init', '-q');
  git(root, 'config', 'user.name', 'Build fixture');
  git(root, 'config', 'user.email', 'build@example.invalid');
  git(root, 'add', '.');
  git(root, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture');
}
function layout(root) {
  mkdirSync(root, { recursive: true });
  for (const file of ['package.json', 'tsconfig.json', 'pyproject.toml', 'uv.lock']) cpSync(join(source, file), join(root, file));
  for (const directory of ['src', 'lib']) cpSync(join(source, directory), join(root, directory), { recursive: true, filter: p => !p.includes('__pycache__') });
  mkdirSync(join(root, 'scripts'));
  for (const file of ['write-build-info.mjs', 'validate-python-bridge.js']) cpSync(join(source, 'scripts', file), join(root, 'scripts', file));
  writeFileSync(join(root, '.gitignore'), 'node_modules/\n.venv/\ndist/\nbuild-info.json\ntsconfig.tsbuildinfo\n');
  symlinkSync(join(source, 'node_modules'), join(root, 'node_modules'), 'dir');
  symlinkSync(join(source, '.venv'), join(root, '.venv'), 'dir');
}
function build(root) { run(root, 'npm', ['run', 'build']); chmodSync(join(root, 'dist/index.js'), 0o755); }
function info(root) { return JSON.parse(readFileSync(join(root, 'build-info.json'), 'utf8')); }
function version(root) {
  return run(root, process.execPath, ['--input-type=module', '-e', `const m = await import(${JSON.stringify(pathToFileURL(join(root, 'dist/index.js')).href)}); process.stdout.write(m.getServerVersion());`]);
}
// Find a real seven-hex collision, then materialize both valid commit objects in Git.
function collide(root) {
  const tree = git(root, 'rev-parse', 'HEAD^{tree}');
  const seen = new Map();
  for (let n = 0; n < 200000; n++) {
    const body = `tree ${tree}\nauthor Build fixture <build@example.invalid> 1 +0000\ncommitter Build fixture <build@example.invalid> 1 +0000\n\ncollision ${n}\n`;
    const sha = createHash('sha1').update(`commit ${Buffer.byteLength(body)}\0${body}`).digest('hex');
    const previous = seen.get(sha.slice(0, 7));
    if (previous) {
      for (const content of [previous, body]) execFileSync('git', ['hash-object', '-t', 'commit', '-w', '--stdin'], { cwd: root, env, input: content });
      git(root, 'update-ref', 'HEAD', sha);
      return git(root, 'rev-parse', '--short=7', 'HEAD');
    }
    seen.set(sha.slice(0, 7), body);
  }
  throw new Error('No collision found within fixture bound');
}
beforeAll(() => {
  temporary = mkdtempSync(join(tmpdir(), 'build-identity-'));
  clean = join(temporary, 'clean'); layout(clean); initialize(clean); longCommit = collide(clean); build(clean);
  foreign = join(temporary, 'foreign'); mkdirSync(foreign); writeFileSync(join(foreign, 'file'), 'foreign'); initialize(foreign);
  nested = join(foreign, 'unpacked'); layout(nested); build(nested);
}, 600000);
afterAll(() => { if (temporary) rmSync(temporary, { recursive: true, force: true }); });

test('T1 writer uses real Git output with a collision requiring more than seven hex characters', () => {
  expect(longCommit.length).toBeGreaterThan(7);
  expect(info(clean).commit).toBe(longCommit);
  expect(info(clean).commit).toMatch(/^[0-9a-f]{7,40}$/);
  expect(version(clean)).toBe(`${pkg}+${longCommit}`);
});
test('C2 unpacked package under an ancestor repository gets no commit', () => {
  expect(info(nested).commit).toBeNull();
  expect(version(nested)).toBe(pkg);
});
test('C2 writer ignores foreign cwd and inherited Git routing', () => {
  run(foreign, process.execPath, [join(clean, 'scripts/write-build-info.mjs')], { GIT_DIR: join(foreign, '.git'), GIT_WORK_TREE: foreign });
  expect(info(clean).commit).toBe(longCommit);
});
test('P1 packed gate accepts actual built identity independently of package version', async () => {
  await expect(inspectBuildIdentity(clean)).resolves.toEqual(info(clean));
  await protocolSmoke(join(clean, 'dist/index.js'), clean, env, null, pkg, undefined, { buildInfo: info(clean) });
}, 60000);
test('P1 rejects wrong releases, wrong identities and malformed metadata from a real built version', () => {
  const actual = version(clean), stamp = info(clean);
  expect(() => assertServerVersion(actual, '9.9.9', stamp)).toThrow('release');
  expect(() => assertServerVersion(actual, pkg, { ...stamp, commit: 'abcdef0' })).toThrow('identity');
  for (const actual of [`${pkg}oops`, `${pkg}+`, `${pkg}+abc+def`, `${pkg}+abc..def`])
    expect(() => assertServerVersion(actual, pkg, stamp)).toThrow();
  expect(() => assertServerVersion(pkg, pkg)).not.toThrow();
});
test('C1 old stamp cannot name A after plain tsc emits changed code at B', () => {
  run(clean, process.execPath, [join(clean, 'scripts/write-build-info.mjs')]);
  const stamp = readFileSync(join(clean, 'build-info.json'), 'utf8');
  const file = join(clean, 'src/index.ts');
  writeFileSync(file, readFileSync(file, 'utf8') + '\nexport const buildIdentityFixture = "B";\n');
  git(clean, 'add', 'src/index.ts'); git(clean, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'B');
  run(clean, process.execPath, [join(source, 'node_modules/typescript/bin/tsc')]);
  expect(readFileSync(join(clean, 'dist/index.js'), 'utf8')).toContain('buildIdentityFixture');
  expect(readFileSync(join(clean, 'build-info.json'), 'utf8')).toBe(stamp);
  expect(version(clean)).toBe(pkg);
});
test('C1 changed nested runtime bytes, added/removed files, missing digest and malformed stamps fall back', async () => {
  run(clean, process.execPath, [join(clean, 'scripts/write-build-info.mjs')]);
  const original = info(clean);
  const file = join(clean, 'dist/lib/eapi-session.js');
  const bytes = readFileSync(file);
  writeFileSync(file, Buffer.concat([bytes, Buffer.from('\n// changed\n')]));
  expect(version(clean)).toBe(pkg);
  await expect(inspectBuildIdentity(clean)).rejects.toThrow('digest mismatch');
  writeFileSync(file, bytes);
  for (const extension of ['json', 'node', 'wasm', 'txt']) {
    const asset = join(clean, `dist/lib/extra.${extension}`); writeFileSync(asset, 'changed runtime');
    expect(version(clean)).toBe(pkg); rmSync(asset);
  }
  const extra = join(clean, 'dist/lib/extra.json');
  writeFileSync(extra, '{}');
  run(clean, process.execPath, [join(clean, 'scripts/write-build-info.mjs')]);
  rmSync(extra); expect(version(clean)).toBe(pkg);
  for (const body of [null, 'not json', { ...original, runtime_sha256: undefined }, { ...original, commit: 'z' }, { ...original, commit: 7 }]) {
    writeFileSync(join(clean, 'build-info.json'), typeof body === 'string' ? body : JSON.stringify(body));
    expect(version(clean)).toBe(pkg);
  }
  rmSync(join(clean, 'build-info.json')); expect(version(clean)).toBe(pkg);
  writeFileSync(join(clean, 'build-info.json'), JSON.stringify(original));
}, 120000);
test('no-Git writer replaces stale identity and dirty tracked/untracked runtime changes are marked', () => {
  writeFileSync(join(clean, 'src/dirty.ts'), 'export const dirty = true;\n');
  run(clean, process.execPath, [join(clean, 'scripts/write-build-info.mjs')]); expect(info(clean).dirty).toBe(true); expect(version(clean)).toMatch(/\.dirty$/);
  rmSync(join(clean, '.git'), { recursive: true, force: true });
  run(clean, process.execPath, [join(clean, 'scripts/write-build-info.mjs')]); expect(info(clean).commit).toBeNull(); expect(version(clean)).toBe(pkg);
}, 120000);

test('default reader checks the generated runtime binding and accepts actual 7-40 hex Git forms', () => {
  const built = JSON.parse(readFileSync(join(source, 'build-info.json'), 'utf8'));
  expect(built.commit).toBe(run(source, 'git', ['rev-parse', '--short=7', 'HEAD']));
  expect(built.runtime_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(getServerVersion()).toBe(`${pkg}+${built.commit}${built.dirty ? '.dirty' : ''}`);
  const file = join(temporary, 'reader-info.json');
  for (const commit of [built.commit.slice(0, 7), run(source, 'git', ['rev-parse', 'HEAD'])]) {
    writeFileSync(file, JSON.stringify({ ...built, commit }));
    expect(getServerVersion(pathToFileURL(file))).toBe(`${pkg}+${commit}${built.dirty ? '.dirty' : ''}`);
  }
  for (const body of [null, 'not json', { ...built, runtime_sha256: undefined }, { ...built, runtime_sha256: '0'.repeat(64) }, { ...built, commit: 7 }]) {
    writeFileSync(file, typeof body === 'string' ? body : JSON.stringify(body));
    expect(getServerVersion(pathToFileURL(file))).toBe(pkg);
  }
  rmSync(file); expect(getServerVersion(pathToFileURL(file))).toBe(pkg);
});
