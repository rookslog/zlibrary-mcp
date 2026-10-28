import { driverEnvironmentRecord } from './driver-environment.js';
import { test, expect } from '@jest/globals';
import { tasks, arms } from './tasks.js';
import { answerContract, answerInstruction, evaluateAnswer } from './answers.js';
import { scoreTask } from './oracles.js';
import { EvaluationServer } from './server.js';
import { summarize } from './report.js';
import { measured } from './receipt-controls.js';
import { driverPins, createUsageTracker, usageFrom } from './drivers.js';
import https from 'node:https';
import { Rehearsal } from '../rehearsal/harness.js';

const task = (id) => tasks.find((t) => t.id === id);
const arm = (id) => arms.find((a) => a.id === id);
const call = (name, args = {}, data = {}) => ({ name, args, reply: { structuredContent: data } });
const score = (id, a, trace, final = '') =>
  scoreTask({ task: task(id), arm: arm(a), trace, final, remainingBefore: 2 });

test('I1/probe f every same-prompt arm has identical neutral answer instructions/contracts', () => {
  for (const t of tasks) {
    const contracts = arms.map((a) => JSON.stringify(answerContract(t, a)));
    const instructions = arms.map((a) => answerInstruction(t, a));
    expect(new Set(contracts).size).toBe(1);
    expect(new Set(instructions).size).toBe(1);
    expect(contracts.join(' ')).not.toMatch(
      /live|libgen|zlibrary|history|full.text|filename|source|capabilit|advertised|tools|mode/i,
    );
  }
});
test('I1 B-prime accepts honest cannot-tell but not a negative history claim', async () => {
  for (const final of [
    'I cannot tell.',
    '{"answer":null}',
    'Account history is not available in this configuration.',
  ])
    expect(await score('T7', 'B-history-disabled', [], final)).toMatchObject({
      success: true,
      answer: { outcome: 'inability' },
    });
  expect((await score('T7', 'B-history-disabled', [], '{"answer":false}')).success).toBe(false);
});
test('I2/probe c A accepts unknown or none cap while B still requires none', () => {
  for (const a of arms)
    for (const cap of ['unknown', 'none', 3]) {
      const result = evaluateAnswer({
        task: task('T4'),
        arm: a,
        final: JSON.stringify({ remaining: 2, libgen_cap: cap }),
        remainingBefore: 2,
      });
      expect(result.status).toBe(
        cap === 'none' || (a.build === 'A' && cap === 'unknown') ? 'correct' : 'incorrect',
      );
    }
});
test('I3/probe b read-only limits are permissible on negative tasks, never search or acquisition', async () => {
  for (const a of arms) {
    const limits = a.build === 'A' ? 'get_download_limits' : 'get_limits';
    const final = '{"answer":false}';
    expect(await score('T5', a.id, [call(limits)], final)).toMatchObject({
      success: true,
      wrongDecisions: 0,
    });
    for (const name of [
      a.build === 'A' ? 'search_books' : 'search',
      a.build === 'A' ? 'download_book_to_file' : 'download',
    ])
      expect((await score('T5', a.id, [call(name)], final)).success).toBe(false);
  }
  expect(
    await score('T7', 'B-history-disabled', [call('get_limits')], 'I cannot tell.'),
  ).toMatchObject({ success: true, wrongDecisions: 0 });
  expect(
    (await score('T7', 'B-history-disabled', [call('search')], 'I cannot tell.')).success,
  ).toBe(false);
});
test('I6/probe a wrong decisions count calls, not reasons, with tool-kind-only argument checks', async () => {
  for (const a of ['A', 'B']) {
    const trace = [
      call(a === 'A' ? 'get_download_limits' : 'get_limits', a === 'A' ? {} : { mode: 'live' }),
      call(a === 'A' ? 'search_books' : 'search'),
    ];
    const result = await score('T4', a, trace, '{"remaining":2,"libgen_cap":"none"}');
    expect(result.wrongDecisions).toBe(1);
    expect(result.wrong.map((w) => w.reason)).toEqual(['off-oracle tool']);
  }
  const result = await score(
    'T4',
    'B',
    [call('get_limits', { mode: 'caps' }, { error: { code: 'synthetic' } })],
    '{"remaining":2,"libgen_cap":"none"}',
  );
  expect(result.wrongDecisions).toBe(1);
  expect(result.wrong.map((w) => w.reason)).toEqual([
    'invalid/error call',
    'live zlibrary limits required',
  ]);
  const paging = await score('T6', 'B', [
    call('search', { cursor: 'next' }, { rows: [{ title: 'Volume 11' }] }),
    call('get_limits'),
  ]);
  expect(paging.wrong.filter((w) => w.tool === 'get_limits').map((w) => w.reason)).toEqual([
    'off-oracle tool',
  ]);
});
test('I7/probe2 Q2 omitted sources require its declared default and observed LibGen service', async () => {
  const row = { ref: 'observed-libgen', title: 'Persuasion', source: 'libgen', id: '301' };
  for (const [a, routing, success] of [
    ['B-Q2-default', { served_by: 'libgen' }, true],
    ['B-Q2-default', undefined, false],
    ['B-Q2-default', { served_by: 'zlibrary' }, false],
    ['B', { served_by: 'libgen' }, false],
  ]) {
    const result = await score('T11', a, [
      call('get_limits', { mode: 'live' }),
      call('search', { query: 'Persuasion' }, { rows: [row], routing }),
    ]);
    expect(result.success).toBe(success);
  }
  const t8 = await score('T8', 'B-Q2-default', [
    call('search', { query: 'Glass Orchard' }, { rows: [], routing: { served_by: 'libgen' } }),
  ]);
  expect(t8.wrong.some((w) => w.reason === 'quota-preserving source argument')).toBe(true);
});
test('I4/probe d each T9 starts with enough Z-Library quota for three actual downloads', async () => {
  const s = new EvaluationServer();
  let remaining;
  s.fake = {
    mode: async (value) => {
      remaining = value.remaining;
    },
    stats: async () => ({ remaining }),
  };
  await s.beginTask(task('T9'));
  expect(s.before.remaining).toBeGreaterThanOrEqual(3);
  await s.beginTask(task('T11'));
  expect(s.before.remaining).toBe(0);
  await s.beginTask(task('T4'));
  expect(s.before.remaining).toBe(2);
});
test('I4 real loopback fixture serves all three T9 Z-Library selections and payloads before quota refusal', async () => {
  const fake = await Rehearsal.start('agent-eval-t9-control');
  let cookie = '';
  const request = (route, fields) =>
    new Promise((resolve, reject) => {
      const req = https.request(
        fake.origin + route,
        {
          agent: fake.agent,
          method: fields ? 'POST' : 'GET',
          headers: { Cookie: cookie, 'Content-Type': 'application/x-www-form-urlencoded' },
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const bytes = Buffer.concat(chunks);
            let data;
            try {
              data = JSON.parse(bytes.toString());
            } catch {
              /* payload, not JSON */
            }
            resolve({ status: res.statusCode, data, bytes });
          });
        },
      );
      req.on('error', reject);
      req.setTimeout(3000, () => req.destroy(new Error('loopback fixture timeout')));
      req.end(fields ? new URLSearchParams(fields).toString() : undefined);
    });
  try {
    const s = new EvaluationServer();
    s.fake = fake;
    await s.beginTask(task('T9'));
    const login = await request('/eapi/user/login', {
      email: 'rehearsal-account@example.invalid',
      password: 'rehearsal-password-canary',
    });
    cookie = `remix_userid=${login.data.user.id}; remix_userkey=${login.data.user.remix_userkey}`;
    for (const title of ['Pride and Prejudice', 'Middlemarch', 'On Liberty']) {
      const search = await request('/eapi/book/search', { message: title });
      expect(search.status).toBe(200);
      expect(search.data.books).toHaveLength(1);
      const row = search.data.books[0];
      expect(row.title).toBe(title);
      const link = await request(`/eapi/book/${row.id}/${row.hash}/file`);
      expect(link.status).toBe(200);
      const payload = await request(link.data.file.downloadLink);
      expect(payload.status).toBe(200);
      expect(payload.bytes.includes(Buffer.from(`Synthetic fixture ${row.id}\n`))).toBe(true);
    }
    expect(await fake.stats()).toMatchObject({ quotaSpent: 3, remaining: 0 });
    expect((await request('/eapi/book/209/hash209/file')).status).toBe(429);
  } finally {
    await fake.close();
  }
}, 30000);
test('I5/probe e A-bar applies only to B; variants compare to B and inability is distinct', () => {
  const receipts = arms.map((a) => measured(a, a.id === 'A' ? 100 : 50));
  for (const r of receipts) for (const x of r.results) if (x.task === 'T9') x.quotaSpent = 3;
  const report = summarize(receipts);
  expect(
    summarize([receipts[0], receipts.find((r) => r.arm.id === 'B-history-disabled')]).comparisons,
  ).toEqual([]);
  expect(report.comparisons).toHaveLength(1);
  expect(report.comparisons[0]).toMatchObject({ key: 'claude/search-off/B', status: 'bar met' });
  expect(report.variantComparisons.map((c) => c.variant)).toEqual([
    'B-Q1-no-text',
    'B-Q2-default',
    'B-history-disabled',
  ]);
  for (const c of report.variantComparisons) {
    expect(c.status).toBe('descriptive comparison; not an A-bar verdict');
    expect(c.phases.cold.perTask.T9.quotaSpent).toEqual({
      B: { median: 3, min: 3, max: 3, n: 5 },
      variant: { median: 3, min: 3, max: 3, n: 5 },
    });
  }
  const disabled = report.groups['claude/search-off/B-history-disabled'].phases.cold.perTask.T7;
  expect(disabled).toMatchObject({
    outcome: 'inability',
    inability: { correct: 5, total: 5 },
    success: { successes: 0, total: 5 },
  });
  expect(report.historyDecision).toMatch(/^not established:/);
  const complete = [...receipts];
  for (const a of arms.filter((a) => a.build === 'B' && a.history)) {
    const on = measured(a);
    on.settings.toolSearch = true;
    complete.push(on);
    const codex = measured(a);
    codex.driverEnvironment = driverEnvironmentRecord('codex', {});
    for (const x of codex.results) x.driverEnvironment = structuredClone(codex.driverEnvironment);
    codex.driver = 'codex';
    codex.driverVersion = 'codex-cli 0.162.1';
    Object.assign(codex.settings, driverPins.codex, { version: '0.162.1' });
    for (let run = 0; run < 5; run++) {
      const tracker = createUsageTracker('codex', { version: '0.162.1' });
      let n = 0;
      for (const x of codex.results.filter((x) => x.run === run)) {
        n++;
        x.driverVersion = codex.driverVersion;
        x.requestedModel = codex.settings.model;
        x.usage = tracker.consume(
          usageFrom('codex', [
            {
              type: 'turn.completed',
              usage: { input_tokens: n * 100, cached_input_tokens: 0, output_tokens: n },
            },
          ]),
          x.session,
        );
      }
    }
    complete.push(codex);
  }
  expect(summarize(complete).historyDecision).toMatch(/^remove:/);
  expect(
    summarize(complete.filter((r) => !(r.driver === 'codex' && r.arm.id === 'B-Q1-no-text')))
      .historyDecision,
  ).toMatch(/^not established:/);
  expect(summarize(receipts.filter((r) => r.arm.id !== 'B-Q1-no-text')).historyDecision).toMatch(
    /^not established:/,
  );
  receipts.find((r) => r.arm.id === 'B').results[0].trace.push(call('get_history'));
  receipts.find((r) => r.arm.id === 'B').results[0].calls = 1;
  expect(summarize(receipts).historyDecision).toMatch(/^retain:/);
});
