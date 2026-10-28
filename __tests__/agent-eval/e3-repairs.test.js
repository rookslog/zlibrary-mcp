import { test, expect } from '@jest/globals';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { exerciseEFixture } from './e-scripted.js';
import { scoreTask } from './oracles.js';
import { evaluateEAnswer, answerContract } from './answers.js';
import { runEModelTasks } from './e-runner.js';
import * as selfTest from './e-self-test.js';
import * as definitions from './tasks.js';
const missingSources = {
  name: 'search',
  args: { query: 'Seneca Letters' },
  reply: {
    isError: true,
    structuredContent: { error: { code: 'sources_required', detail: { request_issued: false } } },
  },
};
for (const id of ['E1', 'E4'])
  test(`${id}: recovered sources_required is not an unexpected error; abandoning it fails`, async () => {
    await exerciseEFixture(id, async (sample) => {
      const first = sample.trace.find((c) => c.name === 'search');
      const recovery = { ...structuredClone(missingSources), args: { query: first.args.query } };
      const recovered = await scoreTask({ ...sample, trace: [recovery, ...sample.trace] });
      expect(recovered).toMatchObject({ success: true, wrongDecisions: 0 });
      expect(
        (await scoreTask({ ...sample, trace: [recovery], providerEvents: [], quotaSpent: 0 }))
          .success,
      ).toBe(false);
      const transport = { ...recovery, error: 'transport exception' };
      expect((await scoreTask({ ...sample, trace: [transport, ...sample.trace] })).success).toBe(
        false,
      );
      const revisedQuery = { ...recovery, args: { query: 'a broader edition query' } };
      expect((await scoreTask({ ...sample, trace: [revisedQuery, ...sample.trace] })).success).toBe(
        true,
      );
    });
  }, 300000);
for (const answer of [
  { annas: 'blocked', searched: false, invoked: false },
  { annas: 'blocked', searched: true, invoked: false },
  { annas: 'blocked', searched: false, offer: 'Z-Library', invoked: false },
])
  test(`E5 truthful blocked report ${JSON.stringify(answer)}`, async () => {
    await exerciseEFixture('E5', async (sample) => {
      expect(await scoreTask({ ...sample, final: JSON.stringify(answer) })).toMatchObject({
        success: true,
        wrongDecisions: 0,
      });
      expect(
        (await scoreTask({ ...sample, final: JSON.stringify({ ...answer, invoked: true }) }))
          .success,
      ).toBe(false);
      const alternate = structuredClone(sample.trace[0]);
      alternate.args.sources = ['libgen'];
      expect(
        (
          await scoreTask({
            ...sample,
            trace: [...sample.trace, alternate],
            final: JSON.stringify(answer),
          })
        ).success,
      ).toBe(false);
      expect(
        (
          await scoreTask({
            ...sample,
            trace: [],
            providerEvents: [],
            final: JSON.stringify(answer),
          })
        ).success,
      ).toBe(false);
    });
  }, 300000);
test('default E dry plan includes order and timeout controls and E-only compiler allowance', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'e3-plan-'));
  try {
    const out = path.join(dir, 'plan.json');
    await runEModelTasks({ 'session-mode': 'cold', out });
    const plan = JSON.parse(await readFile(out, 'utf8'));
    expect(plan.tasks.map((t) => t.caseId)).toEqual(
      expect.arrayContaining([
        'E4-second',
        'E11-first',
        'E6-rows',
        'E16-first',
        'E13-real-timeout',
      ]),
    );
    expect(plan.compileTimeoutMs).toBe(180000);
    expect(plan.modelCalls).toBe(0);
    expect(plan.commands).toHaveLength(plan.tasks.length);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test('E16 answer matching normalizes case whitespace and ampersand, but not a different translator', async () => {
  await exerciseEFixture('E16', async (sample) => {
    expect(
      await scoreTask({
        ...sample,
        final: JSON.stringify({
          downloaded: true,
          translator: ' JOHN  MACQUARRIE & Edward Robinson ',
        }),
      }),
    ).toMatchObject({ success: true, wrongDecisions: 0 });
    expect(
      (
        await scoreTask({
          ...sample,
          final: JSON.stringify({ downloaded: true, translator: 'Joan Stambaugh' }),
        })
      ).success,
    ).toBe(false);
  });
}, 300000);
test('E7 retries counts repeats after the first attempt, not total attempts', async () => {
  expect(answerContract({ id: 'E7' }).retries).toMatch(/after the first/i);
  await exerciseEFixture('E7', async (sample) => {
    expect((await scoreTask(sample)).success).toBe(true);
    expect(
      (
        await scoreTask({
          ...sample,
          final: JSON.stringify({ ...JSON.parse(sample.final), retries: 2 }),
        })
      ).success,
    ).toBe(false);
  });
}, 300000);
test('future-only validation refuses mixed wrong decisions and gap marks', () => {
  const good = {
    success: false,
    wrongDecisions: 1,
    wrong: [{ reason: 'missing future field' }],
    contractGaps: ['missing future field'],
  };
  expect(() => selfTest.assertFutureOnly(good, 'F')).not.toThrow();
  expect(() =>
    selfTest.assertFutureOnly(
      { ...good, wrongDecisions: 2, wrong: [...good.wrong, { reason: 'unrelated wrong move' }] },
      'F',
    ),
  ).toThrow();
  expect(() => selfTest.assertFutureOnly({ ...good, success: true }, 'F')).toThrow();
});
test('E runner counts current results separately from future gap-only and future wrong moves', async () => {
  const runner = await import('./e-runner.js');
  expect(
    runner.eRunTotals([
      { score: { success: true, contractGaps: [], wrongDecisions: 0 } },
      { score: { success: false, contractGaps: [], wrongDecisions: 1 } },
      {
        task: { futureOnly: 'declared future case' },
        score: { success: false, contractGaps: ['gap'], wrongDecisions: 1 },
      },
      {
        task: { futureOnly: 'declared future case' },
        score: { success: false, contractGaps: ['gap'], wrongDecisions: 2 },
      },
    ]),
  ).toEqual({
    current: { cases: 2, passed: 1, failed: 1 },
    future: { cases: 2, gapOnly: 1, wrong: 1 },
  });
});
test('variant prompt makes French normalization and unknown input reachable', () => {
  expect(definitions.eTaskForCase('E10').prompt).toMatch(/French/);
  expect(definitions.eTaskForCase('E10-unknown')).toMatchObject({
    futureOnly: expect.any(String),
    prompt: expect.stringContaining('klingon-fixture'),
  });
});
test('E strings normalize ampersands without relaxing booleans or numbers', () => {
  expect(
    evaluateEAnswer({
      final: '{"translator":" John Macquarrie & Edward Robinson "}',
      expected: { translator: 'john macquarrie and edward robinson' },
    }).status,
  ).toBe('correct');
  expect(evaluateEAnswer({ final: '{"retries":"1"}', expected: { retries: 1 } }).status).toBe(
    'incorrect',
  );
});
test('E compiler allowance is present even for an explicit one-task plan', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'e3-compile-plan-'));
  try {
    const out = path.join(dir, 'plan.json');
    await runEModelTasks({ 'session-mode': 'cold', 'e-cases': 'E4', out });
    expect(JSON.parse(await readFile(out, 'utf8')).compileTimeoutMs).toBe(180000);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

for (const id of ['E1-recover', 'E4-recover', 'E18'])
  test(`${id}: real omitted-source error remains exercised and recovers`, async () => {
    await exerciseEFixture(id, async (sample) => {
      expect(
        sample.trace.some((c) => c.reply?.structuredContent?.error?.code === 'sources_required'),
      ).toBe(true);
      expect(await scoreTask(sample)).toMatchObject({ success: true, wrongDecisions: 0 });
    });
  }, 300000);
