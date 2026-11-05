import { readFileSync } from 'node:fs';
import { jest } from '@jest/globals';
import { parse } from 'yaml';

const workflow = parse(
  readFileSync(new URL('../.github/workflows/upstream-check.yml', import.meta.url), 'utf8'),
);
const script = workflow.jobs.report.steps.find((step) =>
  step.uses?.startsWith('actions/github-script@'),
).with.script;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const title = 'Upstream contract check failing';

async function reportFor(data) {
  const issues = {
    listForRepo: jest.fn().mockResolvedValue({ data }),
    createComment: jest.fn().mockResolvedValue({}),
    create: jest.fn().mockResolvedValue({}),
  };
  await new AsyncFunction('github', 'context', script)(
    { rest: { issues } },
    { repo: { owner: 'owner', repo: 'repo' }, serverUrl: '', runId: 1 },
  );
  return issues;
}

test('comments on the exact rolling title, ignoring other drift issues and pull requests', async () => {
  const issues = await reportFor([
    { number: 300, title: 'Other upstream drift' },
    { number: 301, title, pull_request: {} },
    { number: 250, title },
  ]);
  expect(issues.createComment).toHaveBeenCalledTimes(1);
  expect(issues.createComment).toHaveBeenCalledWith(
    expect.objectContaining({ issue_number: 250 }),
  );
  expect(issues.create).not.toHaveBeenCalled();
});

test.each([
  { data: [] },
  { data: [{ number: 300, title: `${title} (different)` }] },
  { data: [{ number: 301, title, pull_request: {} }] },
])('creates the rolling issue when no exact-title issue exists: %j', async ({ data }) => {
  const issues = await reportFor(data);
  expect(issues.createComment).not.toHaveBeenCalled();
  expect(issues.create).toHaveBeenCalledTimes(1);
  expect(issues.create).toHaveBeenCalledWith(
    expect.objectContaining({ title, labels: ['upstream-drift'] }),
  );
});
