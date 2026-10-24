import { jest } from '@jest/globals';
import { readFileSync, mkdtempSync, rmSync, existsSync } from 'node:fs';
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

function api(issues) {
  return {
    paginate: jest.fn(async () => issues),
    rest: { issues: {
      listForRepo: jest.fn(), update: jest.fn(), create: jest.fn(), createComment: jest.fn(),
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
});
afterEach(() => {
  rmSync(directory, { recursive: true, force: true });
  for (const name of ['PREVIOUS_ISSUE_PATH', 'PROBE_REPORT', 'REACHABILITY_RESULT', 'PROBE_FAILED', 'LIVE_RESULT']) delete process.env[name];
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

test('healthy run clears the previous failure state in the issue body', async () => {
  process.env.REACHABILITY_RESULT = 'success';
  process.env.PROBE_FAILED = 'false';
  process.env.PROBE_REPORT = 'all passing\n<!-- upstream-source-status: {} -->';
  const github = api([{ number: 250, title: 'Upstream contract check failing', body: 'old failure' }]);
  await run(script('report', 'issue'), github);
  expect(github.rest.issues.update.mock.calls[0][0].body).toContain('upstream-source-status: {}');
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
