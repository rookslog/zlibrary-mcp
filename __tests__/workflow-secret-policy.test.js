import { test, expect } from '@jest/globals';
import { mkdtempSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { stringify } from 'yaml';
import { pathToFileURL } from 'node:url';

function check(document, filename = 'fixture.yml') {
  const directory = mkdtempSync(join(tmpdir(), 'workflow-policy-'));
  try {
    writeFileSync(join(directory, filename), stringify(document));
    return spawnSync(process.execPath, ['scripts/check-workflow-secrets.mjs', directory], { encoding: 'utf8' });
  } finally { rmSync(directory, { recursive: true, force: true }); }
}
const filter = { id: 'filter_output', run: 'python3 scripts/filter_live_test_output.py' };
const upload = { uses: 'actions/upload-artifact@v7', with: { path: 'live.log' } };
function workflow(secret, condition) {
  return { jobs: { test: { env: { VALUE: secret }, steps: [{ ...filter }, { ...upload, with: { ...upload.with }, ...(condition === undefined ? {} : { if: condition }) }] } } };
}

test.each([undefined, 'always()', "always() || steps.filter_output.outcome == 'success'", "! (steps.filter_output.outcome == 'success')", "'steps.filter_output.outcome == success'"])
('rejects an upload without a necessary success condition: %s', condition => {
  expect(check(workflow('${{ secrets.TEST_VALUE }}', condition)).status).toBe(1);
});

test.each(["${{ format('}}{0}', secrets.TEST_VALUE) }}", '${{ toJSON(secrets) }}', "${{ secrets['TEST_VALUE'] }}", '${{ secrets[matrix.name] }}', '${{ SECRETS.TEST_VALUE }}'])
('catches indirect or aggregate secret use: %s', secret => {
  expect(check(workflow(secret, 'always()')).status).toBe(1);
});

test.each(['other/repo/.github/workflows/x.yml@v1', './.github/workflows/x.yml'])
('rejects inherited secrets in a legal reusable caller: %s', uses => {
  expect(check({ jobs: { test: { uses, secrets: 'inherit' } } }).status).toBe(1);
});

test('rejects an explicit secret map in a reusable caller', () => {
  expect(check({ jobs: { test: { uses: 'other/repo/.github/workflows/x.yml@v1', secrets: { VALUE: '${{ secrets.TEST_VALUE }}' } } } }).status).toBe(1);
});

test('includes workflow-level environment in each job', () => {
  const shared = workflow('', 'always()');
  shared.env = { VALUE: '${{ secrets.TEST_VALUE }}' };
  expect(check(shared, 'fixture.yaml').status).toBe(1);
});

test.each(["steps.filter_output.outcome == 'success'", "${{ always() && steps.filter_output.outcome == 'success' }}"])
('accepts required successful filtering: %s', condition => {
  expect(check(workflow('${{ secrets.TEST_VALUE }}', condition)).status).toBe(0);
});

test('allows only GITHUB_TOKEN without filtering and ignores ordinary shell identifiers', () => {
  expect(check(workflow('${{ secrets.GITHUB_TOKEN }}', 'always()')).status).toBe(0);
  expect(check(workflow('import secrets; print(secrets.token_hex(32))', 'always()')).status).toBe(0);
});

test('requires the real filter before every upload', () => {
  const doc = workflow('${{ secrets.TEST_VALUE }}', "steps.filter_output.outcome == 'success'");
  doc.jobs.test.steps.reverse();
  expect(check(doc).status).toBe(1);
  doc.jobs.test.steps.reverse();
  doc.jobs.test.steps[0] = { id: 'filter_output', run: 'true' };
  expect(check(doc).status).toBe(1);
});

test('rejects malformed YAML', () => {
  const directory = mkdtempSync(join(tmpdir(), 'workflow-policy-'));
  try {
    writeFileSync(join(directory, 'bad.yml'), 'jobs: [');
    expect(spawnSync(process.execPath, ['scripts/check-workflow-secrets.mjs', directory]).status).toBe(1);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('repository workflows satisfy policy', () => {
  const result = spawnSync(process.execPath, ['scripts/check-workflow-secrets.mjs'], { encoding: 'utf8' });
  expect(result.stdout + result.stderr).toContain('Workflow secret policy passed');
  expect(result.status).toBe(0);
});

test('allows an explicitly indexed GITHUB_TOKEN without filtering', () => {
  expect(check(workflow("${{ secrets['GITHUB_TOKEN'] }}", 'always()')).status).toBe(0);
});

test('checks native if expressions as well as interpolation', () => {
  const document = workflow('', 'always()');
  document.jobs.test.steps[0].if = 'toJSON(secrets)';
  expect(check(document).status).toBe(1);
  delete document.jobs.test.steps[0].if;
});


const success = "always() && steps.filter_output.outcome == 'success'";
test.each(['dump.txt', '.', 'live.log\n**/*.txt', 'live.log\n../live.xml', '${{ inputs.path }}', ''])
('rejects upload paths the filter never prepared: %s', path => {
  const document = workflow('${{ secrets.TEST_VALUE }}', success);
  document.jobs.test.steps[1].with.path = path;
  expect(check(document).status).toBe(1);
});

test.each(['step', 'job', 'workflow'])('rejects effective filter working-directory at %s scope', scope => {
  const document = workflow('${{ secrets.TEST_VALUE }}', success);
  if (scope === 'step') document.jobs.test.steps[0]['working-directory'] = 'empty';
  else (scope === 'job' ? document.jobs.test : document).defaults = { run: { 'working-directory': 'empty' } };
  expect(check(document).status).toBe(1);
});

test.each([{ run: 'echo fake >> live.log' }, { uses: 'example/writer@v1' }])
('rejects a non-upload step between filtering and the last upload: %j', step => {
  const document = workflow('${{ secrets.TEST_VALUE }}', success);
  document.jobs.test.steps.splice(1, 0, step);
  expect(check(document).status).toBe(1);
});

test('checks the entire interval through the last upload', () => {
  const document = workflow('${{ secrets.TEST_VALUE }}', success);
  document.jobs.test.steps.push({ run: 'echo fake >> live.log' }, { ...upload, if: success });
  expect(check(document).status).toBe(1);
});

test('accepts adjacent prepared uploads and a summary after the last upload', () => {
  const document = workflow('${{ secrets.TEST_VALUE }}', success);
  document.jobs.test.steps[1].with.path = 'live.log\nlive.xml';
  document.jobs.test.steps.push({ ...upload, if: success }, { run: 'echo summary' });
  expect(check(document).status).toBe(0);
});

async function inspect(document, exceptions) {
  const module = await import(pathToFileURL(join(process.cwd(), 'scripts/check-workflow-secrets.mjs')).href);
  expect(typeof module.checkWorkflow).toBe('function');
  return module.checkWorkflow(document, 'fixture.yml', exceptions);
}

test.each(['job', 'step'])('exceptions require a nonempty reason at %s scope', async scope => {
  const document = scope === 'job'
    ? { jobs: { test: { uses: 'other/repo/.github/workflows/x.yml@v1', secrets: 'inherit' } } }
    : workflow('${{ secrets.TEST_VALUE }}', 'always()');
  const key = scope === 'job' ? 'fixture.yml/test' : 'fixture.yml/test/1';
  expect((await inspect(document, new Map([[key, '   ']]))).length).toBeGreaterThan(0);
  expect(await inspect(document, new Map([[key, 'Reviewed separate output preparation']]))).toEqual([]);
  expect((await inspect(document, new Map([['unrelated/name', 'Valid reason']]))).length).toBeGreaterThan(0);
});

test('rejects duplicate YAML job keys instead of silently replacing the secret job', () => {
  const directory = mkdtempSync(join(tmpdir(), 'workflow-policy-'));
  try {
    writeFileSync(join(directory, 'duplicate.yml'), `jobs:\n  test:\n    uses: other/repo/.github/workflows/x.yml@v1\n    secrets: inherit\n  test:\n    steps: []\n`);
    expect(spawnSync(process.execPath, ['scripts/check-workflow-secrets.mjs', directory]).status).toBe(1);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});


test('symlinked checker rejects an unsafe workflow and reports its result', () => {
  const directory = mkdtempSync(join(tmpdir(), 'workflow-cli-link-'));
  try {
    const cli = join(directory, 'check.mjs');
    symlinkSync(resolve('scripts/check-workflow-secrets.mjs'), cli);
    writeFileSync(join(directory, 'unsafe.yml'), stringify(workflow('${{ secrets.TEST_VALUE }}', 'always()')));
    const result = spawnSync(process.execPath, [cli, directory], { encoding: 'utf8' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('Workflow secret policy:');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

const countsSuccess = success + " && steps.live_counts.outcome == 'success'";
const counts = {
  id: 'live_counts',
  run: 'python3 scripts/live_test_counts.py',
  if: success,
  'continue-on-error': true,
};
function withCounts() {
  const doc = workflow('${{ secrets.TEST_VALUE }}', success);
  doc.jobs.test.steps.splice(1, 0, { ...counts });
  doc.jobs.test.steps.push({ ...upload, if: countsSuccess, with: { path: 'live-counts.json' } });
  return doc;
}
test('accepts counts derived immediately after successful filtering and adjacent uploads', () => {
  expect(check(withCounts()).status).toBe(0);
});
test.each([
  { if: 'always()' },
  { if: "always() || steps.filter_output.outcome == 'success'" },
  { run: 'python3 scripts/live_test_counts.py --output live.log' },
  { 'working-directory': 'other' },
  { env: { PYTHONPATH: 'other' } },
  { shell: 'other' },
  { id: 'other' },
  { 'continue-on-error': false },
])('rejects an untrusted counts preparation step: %j', change => {
  const doc = withCounts();
  Object.assign(doc.jobs.test.steps[1], change);
  expect(check(doc).status).toBe(1);
});
test.each(['absent', 'before-filter', 'after-upload', 'intervening-write'])('rejects counts prepared in the wrong position: %s', location => {
  const doc = withCounts();
  const step = doc.jobs.test.steps.splice(1, 1)[0];
  if (location === 'before-filter') doc.jobs.test.steps.unshift(step);
  if (location === 'after-upload') doc.jobs.test.steps.push(step);
  if (location === 'intervening-write') doc.jobs.test.steps.splice(1, 0, { run: 'echo extra >> live.xml' }, step);
  expect(check(doc).status).toBe(1);
});
test('counts derivation cannot excuse an unguarded or unrelated upload', () => {
  for (const change of [{ if: 'always()' }, { with: { path: 'other.json' } }]) {
    const doc = withCounts();
    Object.assign(doc.jobs.test.steps.at(-1), change);
    expect(check(doc).status).toBe(1);
  }
});

test('rejects a writer after counts preparation and before the final upload', () => {
  const doc = withCounts();
  doc.jobs.test.steps.splice(3, 0, { run: 'echo extra >> live.xml' });
  expect(check(doc).status).toBe(1);
});


test.each([
  success,
  success + " && steps.live_counts.conclusion == 'success'",
  success + " || steps.live_counts.outcome == 'success'",
  success + " && steps.other_counts.outcome == 'success'",
  success + " && steps.live_counts.outcome != 'failure'",
])('counts uploads require the producer outcome: %s', condition => {
  const doc = withCounts();
  doc.jobs.test.steps.at(-1).if = condition;
  expect(check(doc).status).toBe(1);
});

test.each(['live.log\nlive-counts.json', 'live.xml\nlive-counts.json'])('rejects mixing counts with diagnostic paths: %s', path => {
  const doc = withCounts();
  doc.jobs.test.steps.at(-1).with.path = path;
  expect(check(doc).status).toBe(1);
});

test('rejects a planted counts directory without a producer-success condition', () => {
  const doc = withCounts();
  doc.jobs.test.steps.unshift({ id: 'live', run: 'mkdir -p live-counts.json\nenv > live-counts.json/env.txt' });
  doc.jobs.test.steps.at(-1).if = success;
  expect(check(doc).status).toBe(1);
});
