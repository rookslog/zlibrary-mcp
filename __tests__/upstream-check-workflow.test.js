import { jest } from '@jest/globals';
import { readFileSync, mkdtempSync, rmSync, existsSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { parse } from 'yaml';

const workflow = parse(readFileSync(new URL('../.github/workflows/upstream-check.yml', import.meta.url), 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const context = { serverUrl: 'https://github.com', repo: { owner: 'example', repo: 'books' }, runId: 123 };
const require = createRequire(import.meta.url);

function script(job, id) {
  return workflow.jobs[job].steps.find((step) => step.id === id)?.with.script;
}

function api(issues, laterPages = []) {
  const pages = [issues, ...laterPages];
  for (const rows of pages) for (const row of rows) {
    row.state ??= 'open';
    row.labels ??= [{ name: 'upstream-drift' }];
    row.created_at ??= '2026-11-01T00:00:00Z';
  }
  return {
    paginate: jest.fn(async (method, parameters) => {
      const rows = [];
      for (let page = 1; ; page += 1) {
        const { data } = await method({ ...parameters, page });
        rows.push(...data);
        if (data.length < 100) return rows;
      }
    }),
    rest: { issues: {
      listForRepo: jest.fn(async ({ page = 1 }) => ({ data: pages[page - 1] || [] })),
      listComments: jest.fn(async () => ({ data: [] })),
      update: jest.fn(), create: jest.fn(), createComment: jest.fn(),
    } },
  };
}

async function run(code, github) {
  expect(typeof code).toBe('string');
  await new AsyncFunction('github', 'context', 'core', 'require', code)(
    github, context, { setOutput: jest.fn() }, require,
  );
}

let directory;
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'canary-workflow-'));
  process.env.PREVIOUS_ISSUE_PATH = join(directory, 'previous.md');
  process.env.PROBE_REPORT = '### Source blocked\nAnna blocked\n<!-- upstream-source-status: {"annas-archive:search":"2026-10-20"} -->';
  process.env.REACHABILITY_RESULT = 'failure';
  process.env.PROBE_FAILED = 'true';
  process.env.LIVE_RESULT = 'success';
  process.env.LIVE_STATUS = 'passed';
});
afterEach(() => {
  rmSync(directory, { recursive: true, force: true });
  for (const name of ['PREVIOUS_ISSUE_PATH', 'PROBE_REPORT', 'REACHABILITY_RESULT', 'PROBE_FAILED', 'LIVE_RESULT', 'LIVE_STATUS', 'ZLIB_BLOCKED', 'ZLIB_PROBE_DOMAIN']) delete process.env[name];
});

test('previous rolling issue body reaches the next probe without inventing dates', async () => {
  const body = 'old body\n<!-- upstream-source-status: {"annas-archive:search":"2026-10-20"} -->';
  const github = api([{ number: 250, title: 'Upstream contract check failing', body }]);
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe(body);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
});

test('first run supplies an empty previous body', async () => {
  await run(script('reachability', 'previous'), api([]));
  expect(existsSync(process.env.PREVIOUS_ISSUE_PATH)).toBe(true);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('');
});

test('a blocked source updates the existing issue body and its machine state', async () => {
  const github = api([{ number: 250, title: 'Upstream contract check failing', body: 'old' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).toHaveBeenCalledWith(expect.objectContaining({
    issue_number: 250,
    body: expect.stringContaining('### Source blocked'),
  }));
  const body = github.rest.issues.update.mock.calls[0][0].body;
  expect(body).toContain('"2026-10-20"');
  expect(github.rest.issues.create).not.toHaveBeenCalled();
});

test('healthy run closes the issue and clears the previous failure state', async () => {
  process.env.REACHABILITY_RESULT = 'success';
  process.env.PROBE_FAILED = 'false';
  process.env.PROBE_REPORT = 'all passing\n<!-- upstream-source-status: {} -->';
  const github = api([{ number: 250, title: 'Upstream contract check failing', body: 'old failure' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update.mock.calls[0][0].body).toContain('upstream-source-status: {}');
  expect(github.rest.issues.update.mock.calls[0][0].state).toBe('closed');
});

test('healthy run does not create a drift issue', async () => {
  process.env.REACHABILITY_RESULT = 'success';
  process.env.PROBE_FAILED = 'false';
  const github = api([]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.create).not.toHaveBeenCalled();
});

test('failing first run creates an issue with source sections', async () => {
  const github = api([]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.create).toHaveBeenCalledWith(expect.objectContaining({
    body: expect.stringContaining('### Source blocked'), labels: ['upstream-drift'],
  }));
});

test('failed probes still upload both reports and allow independent live diagnosis', () => {
  const steps = workflow.jobs.reachability.steps;
  const artifact = steps.find((step) => step.uses?.startsWith('actions/upload-artifact@'));
  expect(artifact?.if).toBe('always()');
  expect(artifact?.with.path.split('\n').filter(Boolean)).toEqual(['upstream-report.txt', 'upstream-report.json']);
  expect(workflow.jobs.live_tests.if).toContain('always()');
  expect(workflow.jobs.report.if).toContain('always()');
  expect(workflow.jobs.report.if).not.toContain("result == 'failure'");
});

test('previous-body lookup ignores unrelated labelled issues', async () => {
  const github = api([{ number: 7, title: 'Anna mirror list triage (maintainer notes)', body: 'Keep these notes' }]);
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('');
});

test.each([true, false])('unrelated labelled issue is untouched when failure=%s', async (failed) => {
  process.env.REACHABILITY_RESULT = failed ? 'failure' : 'success';
  process.env.PROBE_FAILED = String(failed);
  const unrelated = { number: 7, title: 'Anna mirror list triage (maintainer notes)', body: 'Keep these notes' };
  const github = api([unrelated]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
  expect(github.rest.issues.createComment).not.toHaveBeenCalled();
  expect(unrelated.body).toBe('Keep these notes');
  expect(github.rest.issues.create).toHaveBeenCalledTimes(failed ? 1 : 0);
});


const rollingTitle = 'Upstream contract check failing';

test('updates the exact rolling title, ignoring other drift issues and pull requests', async () => {
  const github = api([
    { number: 300, title: 'Other upstream drift' },
    { number: 301, title: rollingTitle, pull_request: {} },
    { number: 250, title: rollingTitle },
  ]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).toHaveBeenCalledTimes(1);
  expect(github.rest.issues.update).toHaveBeenCalledWith(
    expect.objectContaining({ issue_number: 250, body: expect.stringContaining('### Source blocked') }),
  );
  expect(github.rest.issues.createComment).toHaveBeenCalledWith(expect.objectContaining({ issue_number: 250 }));
  expect(github.rest.issues.create).not.toHaveBeenCalled();
});

test.each([
  { data: [] },
  { data: [{ number: 300, title: `${rollingTitle} (different)` }] },
  { data: [{ number: 301, title: rollingTitle, pull_request: {} }] },
])('creates the rolling issue when no exact-title non-PR issue exists: %j', async ({ data }) => {
  const github = api(data);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
  expect(github.rest.issues.createComment).not.toHaveBeenCalled();
  expect(github.rest.issues.create).toHaveBeenCalledTimes(1);
  expect(github.rest.issues.create).toHaveBeenCalledWith(
    expect.objectContaining({ title: rollingTitle, labels: ['upstream-drift'] }),
  );
});

test('previous-body lookup skips an exact-title PR and reads the real rolling issue', async () => {
  const github = api([
    { number: 301, title: rollingTitle, pull_request: {}, body: 'PR body must not become probe state' },
    { number: 250, title: rollingTitle, body: 'real previous probe state' },
  ]);
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('real previous probe state');
});

test('previous-body lookup supplies empty state when only an exact-title PR exists', async () => {
  await run(script('reachability', 'previous'), api([
    { number: 301, title: rollingTitle, pull_request: {}, body: 'PR body must not become probe state' },
  ]));
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('');
});


test.each(['reachability', 'report'])('paginates %s lookup and finds issue250 after a full page', async (job) => {
  const firstPage = Array.from({ length: 99 }, (_, i) => ({ number: 400 + i, title: 'Other upstream drift' }));
  firstPage.push({ number: 399, title: rollingTitle, pull_request: {} });
  const github = api(firstPage, [[{ number: 250, title: rollingTitle, body: 'later page state' }]]);
  await run(script(job, job === 'report' ? 'issue' : 'previous'), github);
  expect(github.paginate).toHaveBeenCalledWith(github.rest.issues.listForRepo, {
    owner: 'example', repo: 'books', state: 'all', labels: 'upstream-drift', per_page: 100, sort: 'created', direction: 'desc',
  });
  expect(github.paginate.mock.calls.filter(([method]) => method === github.rest.issues.listForRepo)).toHaveLength(1);
  expect(github.rest.issues.listForRepo).toHaveBeenCalledTimes(2);
  if (job === 'report') {
    expect(github.rest.issues.update).toHaveBeenCalledWith(expect.objectContaining({ issue_number: 250 }));
    expect(github.rest.issues.create).not.toHaveBeenCalled();
  } else {
    expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('later page state');
  }
});

test('failed recovery issue update retains its date and cleared state on the next run', async () => {
  const issue = { number: 250, title: rollingTitle, state: 'open', body: 'old failure\n<!-- upstream-source-status: {"zlibrary:search":"2026-10-20"} -->' };
  const github = api([issue]);
  const comments = [];
  github.rest.issues.createComment.mockImplementation(async ({ body }) => { comments.push({ body, user: { login: 'github-actions[bot]' } }); });
  github.rest.issues.listComments.mockImplementation(async () => ({ data: comments }));
  github.rest.issues.update.mockRejectedValueOnce(new Error('update unavailable'));
  process.env.REACHABILITY_RESULT = 'success';
  process.env.PROBE_FAILED = 'false';
  process.env.PROBE_REPORT = 'all passing\n<!-- upstream-source-status: {} -->';
  jest.useFakeTimers().setSystemTime(new Date('2026-11-05T06:20:00Z'));
  try {
    await expect(run(script('report', 'issue'), github)).rejects.toThrow('update unavailable');
    expect(issue.state).toBe('open');
    expect(issue.body).toContain('2026-10-20');
    expect(comments).toHaveLength(1);
    expect(comments[0].body).toContain('Recovered: 2026-11-05 (UTC)');
    jest.setSystemTime(new Date('2026-11-06T06:20:00Z'));
    await run(script('reachability', 'previous'), github);
    const previous = readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8');
    expect(previous).toContain('upstream-source-status: {}');
    expect(previous).toContain('Recovered: 2026-11-05 (UTC)');
    github.rest.issues.update.mockImplementationOnce(async ({ body, state }) => Object.assign(issue, { body, state }));
    await run(script('report', 'issue'), github);
    expect(issue.state).toBe('closed');
    expect(issue.body).toContain('Recovered: 2026-11-05 (UTC)');
    expect(issue.body).not.toContain('Recovered: 2026-11-06');
  } finally { jest.useRealTimers(); }
});

test('snapshot write failure leaves the original issue body and state untouched', async () => {
  const github = api([{ number: 250, title: rollingTitle, state: 'open', body: 'old failure' }]);
  github.rest.issues.createComment.mockRejectedValueOnce(new Error('snapshot unavailable'));
  await expect(run(script('report', 'issue'), github)).rejects.toThrow('snapshot unavailable');
  expect(github.rest.issues.update).not.toHaveBeenCalled();
});

test('a new failure reopens the closed exact-title rolling issue', async () => {
  const github = api([{ number: 250, title: rollingTitle, state: 'closed', body: 'old recovery' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).toHaveBeenCalledWith(expect.objectContaining({ issue_number: 250, state: 'open' }));
  expect(github.rest.issues.create).not.toHaveBeenCalled();
});


test.each([
  [undefined, 'zlibrary,libgen'],
  ['', 'zlibrary,libgen'],
  ['zlibrary,annas-archive,libgen', 'zlibrary,annas-archive,libgen'],
])('workflow passes default or override reliance %s to the probe', (value, expected) => {
  const expression = workflow.jobs.reachability.steps.find(step => step.id === 'probe').env.UPSTREAM_RELIED_SOURCES;
  const evaluate = new Function('vars', 'return ' + expression.slice(3, -2).trim());
  expect(evaluate({ UPSTREAM_RELIED_SOURCES: value })).toBe(expected);
});

test('next probe reads the latest snapshot on a later comments page', async () => {
  const github = api([{ number: 250, title: rollingTitle, body: 'stale failure' }]);
  const firstPage = Array.from({ length: 100 }, () => ({ body: '<!-- upstream-report-snapshot -->\nold state', user: { login: 'github-actions[bot]' } }));
  github.rest.issues.listComments.mockImplementation(async ({ page }) => ({ data: page === 1 ? firstPage : [
    { body: '<!-- upstream-report-snapshot -->\nlatest recovery\n<!-- upstream-source-status: {} -->', user: { login: 'github-actions[bot]' } },
    { body: 'ordinary maintainer comment' },
  ] }));
  await run(script('reachability', 'previous'), github);
  expect(github.rest.issues.listComments).toHaveBeenCalledTimes(2);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('<!-- upstream-report-snapshot -->\nlatest recovery\n<!-- upstream-source-status: {} -->');
});


test.each(['reachability', 'report'])('selects the open labelled issue in %s despite an older updated duplicate', async (job) => {
  const github = api([
    { number: 71, title: rollingTitle, state: 'closed', body: 'old report', created_at: '2026-08-01T00:00:00Z', updated_at: '2026-11-06T00:00:00Z' },
    { number: 250, title: rollingTitle, state: 'open', body: 'current report', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z' },
  ]);
  await run(script(job, job === 'report' ? 'issue' : 'previous'), github);
  expect(github.paginate).toHaveBeenCalledWith(github.rest.issues.listForRepo, {
    owner: 'example', repo: 'books', state: 'all', labels: 'upstream-drift', per_page: 100, sort: 'created', direction: 'desc',
  });
  expect(github.rest.issues.listForRepo).toHaveBeenCalledTimes(1);
  if (job === 'report') expect(github.rest.issues.update).toHaveBeenCalledWith(expect.objectContaining({ issue_number: 250 }));
  else expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('current report');
});

test.each(['reachability', 'report'])('selects the newest created closed issue in %s', async (job) => {
  const github = api([
    { number: 71, title: rollingTitle, state: 'closed', body: 'old report', created_at: '2026-08-01T00:00:00Z', updated_at: '2026-11-06T00:00:00Z' },
    { number: 250, title: rollingTitle, state: 'closed', body: 'newest report', created_at: '2026-10-01T00:00:00Z' },
  ]);
  await run(script(job, job === 'report' ? 'issue' : 'previous'), github);
  if (job === 'report') expect(github.rest.issues.update).toHaveBeenCalledWith(expect.objectContaining({ issue_number: 250 }));
  else expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('newest report');
});

test('healthy closed issue receives no writes or comment lookup', async () => {
  process.env.REACHABILITY_RESULT = 'success';
  process.env.PROBE_FAILED = 'false';
  const github = api([{ number: 250, title: rollingTitle, state: 'closed' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
  expect(github.rest.issues.createComment).not.toHaveBeenCalled();
  expect(github.rest.issues.listComments).not.toHaveBeenCalled();
});

test.each([
  ['cancelled', 'success', 'unknown', 'false'],
  ['success', 'cancelled', 'unknown', 'false'],
  ['success', 'success', 'skipped', 'false'],
  ['success', 'success', 'blocked', 'false'],
  ['success', 'success', 'unknown', 'false'],
  ['success', 'success', 'passed', ''],
  ['success', 'success', 'passed', 'False'],
])('inconclusive evidence leaves issue state unchanged: %s %s %s %s', async (reachability, live, status, failed) => {
  Object.assign(process.env, { REACHABILITY_RESULT: reachability, LIVE_RESULT: live, LIVE_STATUS: status, PROBE_FAILED: failed });
  const github = api([{ number: 250, title: rollingTitle, state: 'open' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
  expect(github.rest.issues.createComment).not.toHaveBeenCalled();
});

const sourceMarker = '<!-- upstream-source-status: {"zlibrary:search":"2026-10-20"} -->';
const snapshot = (body, login = 'github-actions[bot]') => ({ body: '<!-- upstream-report-snapshot -->\n' + body, user: { login } });

test.each(['reachability', 'report'])('reads only bot snapshot dates in %s', async (job) => {
  const github = api([{ number: 250, title: rollingTitle, body: sourceMarker }]);
  github.rest.issues.listComments.mockResolvedValue({ data: [
    snapshot(sourceMarker),
    snapshot('<!-- upstream-source-status: {} -->', 'someone-else'),
  ] });
  process.env.PROBE_REPORT = '';
  await run(script(job, job === 'report' ? 'issue' : 'previous'), github);
  const body = job === 'report' ? github.rest.issues.update.mock.calls[0][0].body : readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8');
  expect(body).toContain(sourceMarker);
});

test.each(['9999-99-99', '2099-01-01', '2026-02-30'])('validates snapshot recovery date %s in both lookups', async (date) => {
  const github = api([{ number: 250, title: rollingTitle, body: sourceMarker }]);
  github.rest.issues.listComments.mockResolvedValue({ data: [snapshot(sourceMarker + '\n<!-- upstream-recovered-at: ' + date + ' -->')] });
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).not.toContain(date);
  Object.assign(process.env, { REACHABILITY_RESULT: 'success', PROBE_FAILED: 'false', PROBE_REPORT: '<!-- upstream-source-status: {} -->' });
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update.mock.calls[0][0].body).not.toContain(date);
});

test('validates source dates before selecting a snapshot', async () => {
  const github = api([{ number: 250, title: rollingTitle, body: sourceMarker }]);
  github.rest.issues.listComments.mockResolvedValue({ data: [snapshot('<!-- upstream-source-status: {"zlibrary:search":"2026-02-30"} -->')] });
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe(sourceMarker);
});

test('a missing report preserves source dates across the next probe', async () => {
  const issue = { number: 250, title: rollingTitle, state: 'open', body: sourceMarker };
  const github = api([issue]);
  const comments = [];
  github.rest.issues.listComments.mockImplementation(async () => ({ data: comments }));
  github.rest.issues.createComment.mockImplementation(async ({ body }) => comments.push(snapshot(body.replace('<!-- upstream-report-snapshot -->\n', ''))));
  github.rest.issues.update.mockImplementation(async ({ body, state }) => Object.assign(issue, { body, state }));
  process.env.PROBE_REPORT = '';
  await run(script('report', 'issue'), github);
  expect(issue.body).toContain(sourceMarker);
  await run(script('reachability', 'previous'), github);
  expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toContain(sourceMarker);
});

test.each(['reachability', 'report'])('requires the rolling label in %s', async (job) => {
  const github = api([{ number: 71, title: rollingTitle, body: 'unlabelled', labels: [] }]);
  await run(script(job, job === 'report' ? 'issue' : 'previous'), github);
  expect(github.rest.issues.update).not.toHaveBeenCalled();
  if (job === 'reachability') expect(readFileSync(process.env.PREVIOUS_ISSUE_PATH, 'utf8')).toBe('');
});

test.each([[1, 0], [0, 1], [0, 0]])('doctor retains audit execution for probe=%i audit=%i', (probeCode, auditCode) => {
  const command = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).scripts.doctor;
  const calls = join(directory, 'calls');
  mkdirSync(join(directory, 'scripts'));
  writeFileSync(join(directory, 'uv'), '#!' + process.execPath + '\nrequire("node:fs").appendFileSync(' + JSON.stringify(calls) + ', "probe\\n");process.exit(' + probeCode + ');', { mode: 0o755 });
  writeFileSync(join(directory, 'scripts', 'release-audit.mjs'), 'import fs from "node:fs";fs.appendFileSync(' + JSON.stringify(calls) + ', "audit\\n");process.exit(' + auditCode + ');');
  const result = require('node:child_process').spawnSync('bash', ['-c', command], { cwd: directory, env: { ...process.env, PATH: directory + ':' + process.env.PATH }, encoding: 'utf8' });
  expect(result.status).toBe(probeCode || auditCode ? 1 : 0);
  expect(readFileSync(calls, 'utf8')).toBe('probe\naudit\n');
});
