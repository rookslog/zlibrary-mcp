import { test, expect } from '@jest/globals';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { exerciseEFixture } from './e-scripted.js';
import { scoreTask } from './oracles.js';
import { eRunTotals, runEModelTasks } from './e-runner.js';
import { eTaskForCase } from './tasks.js';
import { answerContract } from './answers.js';
import { driverPins, runDriver, createUsageTracker } from './drivers.js';

const rejection = () => ({
  name: 'search',
  args: { query: 'E Transient Recovery' },
  reply: {
    isError: true,
    structuredContent: { error: { code: 'sources_required', detail: { request_issued: false } } },
  },
});

test('Codex 0.165.0 launch pin reaches pre-spawn admission without a model call', async () => {
  expect(driverPins.codex.version).toBe('0.165.0');
  const stop = new Error('offline pre-spawn sentinel');
  await expect(
    runDriver({ binary: 'codex', args: [], env: {}, unsetEnv: [] }, '', '/tmp', {
      beforeSpawn() {
        throw stop;
      },
    }),
  ).rejects.toBe(stop);
});

test('E10 French wire gap stays a current failure', async () => {
  await exerciseEFixture('E10', async (sample) => {
    const altered = structuredClone(sample);
    altered.trace.find((c) => c.name === 'search').args.languages = ['French'];
    altered.providerEvents.find((e) => e.route === '/eapi/book/search').arguments['languages[]'] = [
      'French',
    ];
    const score = await scoreTask(altered);
    expect(score.success).toBe(false);
    expect(score.contractGaps.length).toBeGreaterThan(0);
    expect(eRunTotals([{ ...altered, score }])).toEqual({
      current: { cases: 1, passed: 0, failed: 1 },
      future: { cases: 0, gapOnly: 0, wrong: 0 },
    });
  });
}, 300000);

test('future declarations stay future even when gap marks are absent', () => {
  expect(
    eRunTotals([
      {
        task: eTaskForCase('E10-fr'),
        score: { success: false, contractGaps: [], wrongDecisions: 1 },
      },
      {
        expectsFutureContract: true,
        score: { success: true, contractGaps: [], wrongDecisions: 0 },
      },
    ]),
  ).toEqual({
    current: { cases: 0, passed: 0, failed: 0 },
    future: { cases: 2, gapOnly: 0, wrong: 2 },
  });
});

for (const [id, facts, wrongFacts] of [
  [
    'E4',
    [202, '202', 'Margaret Graver and A. A. Long', 'Letters on Ethics: To Lucilius'],
    [203, 'Robin Campbell', 'graver'],
  ],
  ['E11', [402, '402', 'Crystal Forms'], [401, 'niggli-1981']],
]) {
  test(`${id} accepts typed observed identity facts without mnemonic wording`, async () => {
    await exerciseEFixture(id, async (sample) => {
      const outcomes = [];
      for (const downloaded of facts)
        outcomes.push({
          downloaded,
          success: (await scoreTask({ ...sample, final: JSON.stringify({ downloaded }) })).success,
        });
      expect(outcomes).toEqual(facts.map((downloaded) => ({ downloaded, success: true })));
      expect(answerContract({ id }).downloaded).toMatch(/INTEGER/);
      for (const downloaded of wrongFacts)
        expect(
          (await scoreTask({ ...sample, final: JSON.stringify({ downloaded }) })).success,
        ).toBe(false);
      expect(
        (
          await scoreTask({
            ...sample,
            trace: [],
            providerEvents: [],
            final: JSON.stringify({ downloaded: facts[0] }),
          })
        ).success,
      ).toBe(false);
    });
  }, 300000);
  test(`${id} executed wrong selection fails even with every truthful target answer`, async () => {
    await exerciseEFixture(
      id,
      async (sample) => {
        for (const downloaded of facts)
          expect(
            (await scoreTask({ ...sample, final: JSON.stringify({ downloaded }) })).success,
          ).toBe(false);
      },
      { wrongMove: true },
    );
  }, 300000);
}

test('E5 rejects undeclared has_it even when required fields are correct', async () => {
  await exerciseEFixture('E5', async (sample) => {
    expect((await scoreTask(sample)).success).toBe(true);
    for (const has_it of [true, false])
      expect(
        (
          await scoreTask({
            ...sample,
            final: JSON.stringify({ ...JSON.parse(sample.final), has_it }),
          })
        ).success,
      ).toBe(false);
  });
}, 300000);

test('E7 excludes one pre-request rejection from retries and bounds recovery', async () => {
  await exerciseEFixture('E7', async (sample) => {
    const recovered = { ...sample, trace: [rejection(), ...sample.trace] };
    expect((await scoreTask(recovered)).success).toBe(true);
    expect(
      (
        await scoreTask({
          ...recovered,
          final: JSON.stringify({ ...JSON.parse(sample.final), retries: 2 }),
        })
      ).success,
    ).toBe(false);
    expect(
      (await scoreTask({ ...sample, trace: [rejection(), rejection(), ...sample.trace] })).success,
    ).toBe(false);
    expect(
      (
        await scoreTask({
          ...sample,
          trace: [sample.trace[0], rejection(), ...sample.trace.slice(1)],
        })
      ).success,
    ).toBe(false);
    expect((await scoreTask({ ...sample, trace: [rejection()] })).success).toBe(false);
  });
}, 300000);

test('E1 dry plan records one cold command, model and zero calls', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'e4-plan-'));
  try {
    const out = path.join(dir, 'pilot.json');
    await runEModelTasks({
      'session-mode': 'cold',
      'cold-session': true,
      'dry-run': true,
      'e-cases': 'E1',
      driver: 'codex',
      runs: '1',
      model: 'gpt-6.1-sol',
      out,
    });
    const plan = JSON.parse(await readFile(out, 'utf8'));
    expect(plan.modelCalls).toBe(0);
    expect(plan.coldSession).toBe(true);
    expect(plan.commands).toHaveLength(1);
    expect(plan.commands[0]).toMatchObject({ binary: 'codex', env: {}, unsetEnv: [] });
    expect(plan.commands[0].args).toEqual(
      expect.arrayContaining([
        'exec',
        '--model',
        'gpt-6.1-sol',
        '--ignore-user-config',
        '--ignore-rules',
      ]),
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('Codex version mismatch refuses before spawn', async () => {
  const original = driverPins.codex.version;
  try {
    driverPins.codex.version = '0.162.1';
    await expect(
      runDriver({ binary: 'codex', args: [], env: {}, unsetEnv: [] }, '', '/tmp', {
        beforeSpawn() {
          throw new Error('must not reach spawn admission');
        },
      }),
    ).rejects.toThrow(/CLI version mismatch/);
  } finally {
    driverPins.codex.version = original;
  }
});

test('launch repin does not claim cumulative telemetry qualification for 0.165.0', () => {
  expect(() => createUsageTracker('codex', { version: '0.165.0' })).toThrow(/uncorroborated/);
});
