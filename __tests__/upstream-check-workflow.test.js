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

async function reportFor(data, laterPages = []) {
  const pages = [data, ...laterPages];
  const issues = {
    listForRepo: jest.fn(async ({ page = 1 }) => ({ data: pages[page - 1] || [] })),
    createComment: jest.fn().mockResolvedValue({}),
    create: jest.fn().mockResolvedValue({}),
  };
  const paginate = jest.fn(async (method, parameters) => {
    const rows = [];
    for (let page = 1; page <= pages.length; page += 1) {
      const response = await method({ ...parameters, page });
      rows.push(...response.data);
    }
    return rows;
  });
  await new AsyncFunction('github', 'context', script)(
    { rest: { issues }, paginate },
    { repo: { owner: 'owner', repo: 'repo' }, serverUrl: '', runId: 1 },
  );
  return { ...issues, paginate };
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

// The REST endpoint also returns pull requests; pagination must retain the filter.
test('paginates open upstream-drift issues with 100 results per page', async () => {
  const issues = await reportFor([]);
  expect(issues.paginate).toHaveBeenCalledTimes(1);
  expect(issues.paginate).toHaveBeenCalledWith(issues.listForRepo, {
    owner: 'owner',
    repo: 'repo',
    state: 'open',
    labels: 'upstream-drift',
    per_page: 100,
  });
});

test('comments on the rolling issue on a later page instead of creating a duplicate', async () => {
  const firstPage = Array.from({ length: 99 }, (_, index) => ({
    number: 300 + index,
    title: 'Other upstream drift',
  }));
  firstPage.push({ number: 399, title, pull_request: {} });
  const issues = await reportFor(firstPage, [[
    { number: 400, title: `${title} (different)` },
    { number: 250, title },
  ]]);
  expect(issues.createComment).toHaveBeenCalledTimes(1);
  expect(issues.createComment).toHaveBeenCalledWith(
    expect.objectContaining({ issue_number: 250 }),
  );
  expect(issues.create).not.toHaveBeenCalled();
});
