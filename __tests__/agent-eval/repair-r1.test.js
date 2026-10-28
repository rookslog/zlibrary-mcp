import { driverEnvironmentRecord } from './driver-environment.js';
import { test, expect } from '@jest/globals';
import { readFile } from 'node:fs/promises';
import { tasks, arms } from './tasks.js';
import { scoreTask } from './oracles.js';
import { summarize } from './report.js';
import * as drivers from './drivers.js';
import { EvaluationServer } from './server.js';
import { measured } from './receipt-controls.js';
const controls = JSON.parse(
  await readFile(new URL('./regression-controls.json', import.meta.url), 'utf8'),
);
const task = (id) => tasks.find((t) => t.id === id);
const score = (id, final, overrides = {}) =>
  scoreTask({
    ...controls.readonly.find((r) => r.task === id),
    task: task(id),
    arm: arms[0],
    final,
    downloads: '/private/tmp',
    ...overrides,
  });
const compare = (b) => summarize([measured(arms[0]), b]).comparisons[0];
for (const [id, bad] of [
  ['T2', 'Graver translated the 1969 edition; Campbell translated the 2015 edition.'],
  ['T4', 'You have 1 Z-Library download remaining, not 2. LibGen has no daily cap.'],
  ['T7', 'No, you have never downloaded Being and Time; it is absent from account history.'],
  ['T12', 'No books by Hannah Arendt were found.'],
])
  test(`EVAL-R1-01 ${id} opposite answer refused with actual A trace`, async () => {
    const positive = await score(id, controls.readonly.find((r) => r.task === id).final);
    expect(positive.success).toBe(true);
    expect(positive.pathCorrect).toBe(true);
    expect(positive.answer.status).toBe('correct');
    const negative = await score(id, bad);
    expect(negative.success).not.toBe(true);
    expect(negative.pathCorrect).toBe(true);
    expect(['incorrect', 'manual-review']).toContain(negative.answer.status);
  });
test('EVAL-R1-01 uncertain or contradictory extra prose is never automatic success, history needs observed membership', async () => {
  const uncertain = await score('T7', 'It might be there; Being and Time has a download history.');
  expect(uncertain.success).toBeNull();
  expect(uncertain.answer.status).toBe('manual-review');
  const extra = await score(
    'T7',
    controls.readonly.find((r) => r.task === 'T7').final + ' However, it was never downloaded.',
  );
  expect(extra.success).not.toBe(true);
  const absent = await score('T7', controls.readonly.find((r) => r.task === 'T7').final, {
    trace: [
      { name: 'get_download_history', args: {}, reply: { structuredContent: { items: [] } } },
    ],
  });
  expect(absent.success).not.toBe(true);
});
test('EVAL-R1-02 complete compatible measured positive control can meet the bar', () =>
  expect(compare(measured(arms[1], 50)).status).toBe('bar met'));
for (const [name, mutate] of [
  [
    'failed egress',
    (b) => {
      b.status = 'failed';
      b.error = 'server attempted non-fixture egress';
      b.sourceStable = false;
    },
  ],
  [
    'unstable only',
    (b) => {
      b.sourceStable = false;
    },
  ],
  [
    'fence not established',
    (b) => {
      delete b.fencePassed;
    },
  ],
  [
    'duplicate T8 / missing T7',
    (b) => {
      for (const x of b.results) if (x.task === 'T7') x.task = 'T8';
    },
  ],
  [
    'missing warm phaseIndex',
    (b) => {
      delete b.results[12].phaseIndex;
    },
  ],
  [
    'duplicate run',
    (b) => {
      b.results[24].run = 0;
    },
  ],
  [
    'missing usage',
    (b) => {
      delete b.results[0].usage;
    },
  ],
  [
    'negative token count',
    (b) => {
      b.results[0].usage.inputTotal = -1;
    },
  ],
  [
    'driver error',
    (b) => {
      b.results[0].driverError = 'non-evaluation tool';
    },
  ],
  [
    'model mismatch',
    (b) => {
      b.settings.model = 'another-model';
    },
  ],
  [
    'CLI mismatch',
    (b) => {
      b.results[0].driverVersion = 'other';
    },
  ],
  [
    'source SHA mismatch',
    (b) => {
      b.build.source.sha = 'c'.repeat(40);
    },
  ],
  [
    'session changes',
    (b) => {
      b.results[1].session = 'unexpected-session';
    },
  ],
  [
    'unscored answer',
    (b) => {
      b.results[0].success = null;
      b.results[0].answer.status = 'manual-review';
    },
  ],
])
  test(`EVAL-R1-02 rejects ${name}`, () => {
    const b = measured(arms[1], 50);
    mutate(b);
    expect(compare(b).status).not.toBe('bar met');
  });
test('EVAL-R1-02 incompatible same-key receipts remain diagnostic, never silently mixed', () => {
  const b = measured(arms[1], 50),
    changed = structuredClone(b);
  changed.build.sha = 'c'.repeat(40);
  changed.build.source.sha = changed.build.sha;
  const r = summarize([measured(arms[0]), b, changed]);
  expect(r.comparisons.every((c) => c.status !== 'bar met')).toBe(true);
  expect(r.diagnostics.length).toBeGreaterThan(0);
});
test('EVAL-R1-03 two failed replicates cannot hide behind success medians', () => {
  const b = measured(arms[1], 50);
  for (const x of b.results)
    if (x.run < 2) {
      x.success = false;
      x.pathCorrect = false;
      x.wrongDecisions = 1;
    }
  const r = compare(b);
  expect(r.status).toBe('bar not met');
  expect(r.checks[0].successAtLeastA).toBe(false);
  expect(r.checks[0].success.B).toMatchObject({ successes: 27, total: 45, fraction: 0.6 });
});
test('EVAL-R1-03 ALL B observations must be zero where A median wrong is zero', () => {
  const b = measured(arms[1], 50);
  Object.assign(
    b.results.find((x) => x.task === 'T1'),
    { success: false, pathCorrect: false, wrongDecisions: 1 },
  );
  const r = compare(b);
  expect(r.status).toBe('bar not met');
  expect(r.checks[0].perTask.T1.zeroWhereAZero).toBe(false);
});
test('EVAL-R1-04 rejected unknown/disabled broker calls remain visible without forwarding', async () => {
  const s = new EvaluationServer();
  s.tools = [];
  s.trace = [];
  await expect(s.call('get_history', { limit: 10 })).rejects.toThrow('disabled or unknown');
  const r = await scoreTask({
    task: task('T7'),
    arm: arms.find((a) => !a.history),
    trace: s.trace,
    final: 'Account history is not available in this configuration.',
    downloads: '/private/tmp',
  });
  expect(r).toMatchObject({ success: false, calls: 1, invalidCalls: 1 });
  expect(s.trace[0]).toMatchObject({ name: 'get_history', args: { limit: 10 }, forwarded: false });
  expect(r.wrong.some((w) => w.reason === 'off-oracle tool')).toBe(true);
});
test('EVAL-R1-04 CLI rejected attempts counted once, started/completed and forwarded broker calls deduplicated', () => {
  const item = (id, status) => ({
    id,
    type: 'mcp_tool_call',
    server: 'books',
    tool: 'get_history',
    arguments: { limit: 10 },
    status,
    ...(status === 'failed' ? { error: { message: 'unknown tool' } } : {}),
  });
  const events = [
    { type: 'item.started', item: item('one', 'in_progress') },
    { type: 'item.completed', item: item('one', 'completed') },
    { type: 'item.completed', item: item('two', 'failed') },
  ];
  const r = drivers.reconcileAttempts(events, [
    { name: 'get_history', args: { limit: 10 }, origin: 'broker', forwarded: true, reply: {} },
  ]);
  expect(r.trace).toHaveLength(2);
  expect(r.trace[1]).toMatchObject({ origin: 'driver', forwarded: false, clientRejected: true });
  expect(drivers.reconcileAttempts(events, []).trace).toHaveLength(2);
});
test('EVAL-R1-05 B T5 is an absolute negative guard although A stays outside common totals', () => {
  const b = measured(arms[1], 50);
  for (const x of b.results)
    if (x.task === 'T5') {
      x.success = false;
      x.pathCorrect = false;
      x.wrongDecisions = 1;
      x.trace = [{ name: 'search', args: { query: 'the owl of Minerva' } }];
      x.calls = 1;
    }
  const r = compare(b);
  expect(r.status).toBe('bar not met');
  expect(r.absoluteViolations.some((x) => x.includes('T5'))).toBe(true);
  const hidden = measured(arms[1], 50);
  hidden.results.find((x) => x.task === 'T5').trace = [
    { name: 'download', args: { ref: 'invented' } },
  ];
  expect(compare(hidden).status).not.toBe('bar met');
});
test('EVAL-R1-06 root recorded three-turn counters normalize to session deltas, not sums', () => {
  const tracker = drivers.createUsageTracker('codex', { version: '0.162.1' });
  const values = controls.codexProbe.totals.map((usage, index) =>
    tracker.consume(
      drivers.usageFrom('codex', [{ type: 'turn.completed', usage }]),
      'root-session',
      index ? 'root-session' : undefined,
    ),
  );
  expect(values.map((u) => u.inputTotal)).toEqual([39031, 39574, 22520]);
  expect(values.map((u) => u.inputCacheRead)).toEqual([19328, 38912, 19712]);
  expect(values.map((u) => u.output)).toEqual([118, 118, 5]);
  expect(values.map((u) => u.inputUncached)).toEqual([19703, 662, 2808]);
  expect(values[1].rawCounters).toEqual(controls.codexProbe.totals[1]);
  expect(values.reduce((n, u) => n + u.inputTotal, 0)).toBe(101125);
  expect(values.reduce((n, u) => n + u.output, 0)).toBe(241);
  expect(values[0].cacheFirstRequest).toBeNull();
});
test('EVAL-R1-06 negative/incomplete counters, changed session and unobserved CLI scope fail closed', () => {
  const tracker = drivers.createUsageTracker('codex', { version: '0.162.1' });
  const usage = drivers.usageFrom('codex', [
    { type: 'turn.completed', usage: controls.codexProbe.totals[1] },
  ]);
  tracker.consume(usage, 'id');
  expect(() =>
    tracker.consume(
      drivers.usageFrom('codex', [
        { type: 'turn.completed', usage: controls.codexProbe.totals[0] },
      ]),
      'id',
      'id',
    ),
  ).toThrow();
  expect(() => tracker.consume(usage, 'other', 'id')).toThrow();
  expect(() =>
    drivers.usageFrom('codex', [
      { type: 'turn.completed', usage: { input_tokens: 1, output_tokens: 1 } },
    ]),
  ).toThrow();
  expect(() => drivers.createUsageTracker('codex', { version: 'other' })).toThrow();
});

test('EVAL-R1-01 explicit correct JSON facts pass, swapped/duplicated associations and contradictions do not', async () => {
  const facts = {
    translations: [
      { translator: 'Margaret Graver and A. A. Long', year: 2015 },
      { translator: 'Robin Campbell', year: 1969 },
    ],
  };
  expect((await score('T2', JSON.stringify(facts))).success).toBe(true);
  const duplicate = { translations: [facts.translations[0], facts.translations[0]] };
  expect((await score('T2', JSON.stringify(duplicate))).success).toBe(false);
  const swapped = {
    translations: [
      { ...facts.translations[0], year: 1969 },
      { ...facts.translations[1], year: 2015 },
    ],
  };
  expect((await score('T2', JSON.stringify(swapped))).success).toBe(false);
  expect((await score('T4', JSON.stringify({ remaining: 2, libgen_cap: 'none' }))).success).toBe(
    true,
  );
  expect(
    (await score('T4', JSON.stringify({ remaining: 1, libgen_cap: 'none', not: 2 }))).success,
  ).toBe(false);
  const original = controls.readonly.find((r) => r.task === 'T7');
  const bTrace = original.trace.map((c) => ({ ...c, name: 'get_history' }));
  expect(
    (
      await score(
        'T7',
        'No, you have never downloaded Being and Time; it is absent from account history.',
        { arm: arms[1], trace: bTrace },
      )
    ).success,
  ).not.toBe(true);
  expect(
    (
      await score('T7', JSON.stringify({ title: 'Being and Time', in_account_history: true }), {
        arm: arms[1],
        trace: bTrace,
      })
    ).success,
  ).toBe(true);
});
test('EVAL-R1-02 malformed and partial receipts stay diagnostic instead of throwing or being admitted', () => {
  expect(() => summarize([null, {}, { status: 'measured', results: [null] }])).not.toThrow();
  const b = measured(arms[1], 50);
  b.results[0] = null;
  expect(compare(b).status).not.toBe('bar met');
});
test('EVAL-R1-02 exact repeated warm episode positions are validated, not just aggregate counts', () => {
  const addWarm = (r) => {
    r.settings.warmRuns = 2;
    r.results.push(
      ...r.results
        .filter((x) => x.phaseIndex === 1)
        .map((x) => ({ ...structuredClone(x), phaseIndex: 2 })),
    );
    return r;
  };
  const a = addWarm(measured(arms[0])),
    b = addWarm(measured(arms[1], 50));
  expect(summarize([a, b]).comparisons[0].status).toBe('bar met');
  b.results.find((x) => x.phaseIndex === 2).phaseIndex = 1;
  expect(summarize([a, b]).comparisons[0].status).not.toBe('bar met');
});
test('EVAL-R1-04 partial completed events use earlier arguments; otherwise known rejected attempt is retained with unavailable args', () => {
  const started = {
    type: 'item.started',
    item: {
      id: 'x',
      type: 'mcp_tool_call',
      server: 'books',
      tool: 'get_history',
      arguments: { limit: 10 },
    },
  };
  const ended = {
    type: 'item.completed',
    item: {
      id: 'x',
      type: 'mcp_tool_call',
      server: 'books',
      tool: 'get_history',
      status: 'failed',
      error: { message: 'disabled' },
    },
  };
  const merged = drivers.reconcileAttempts([started, ended], []);
  expect(merged.trace).toHaveLength(1);
  expect(merged.trace[0].args).toEqual({ limit: 10 });
  expect(merged.coverage.unavailable).toEqual([]);
  const partial = drivers.reconcileAttempts([ended], []);
  expect(partial.trace).toHaveLength(1);
  expect(partial.trace[0].clientRejected).toBe(true);
  expect(partial.coverage.unavailable.length).toBeGreaterThan(0);
});
test('EVAL-R1-04 rejected broker call plus matching CLI rejection is still ONE invalid call', async () => {
  const s = new EvaluationServer();
  s.trace = [];
  s.tools = [];
  try {
    await s.call('get_history', {});
  } catch {}
  const event = {
    type: 'item.completed',
    item: {
      id: 'no-history',
      type: 'mcp_tool_call',
      server: 'books',
      tool: 'get_history',
      arguments: {},
      status: 'failed',
      error: { message: 'disabled' },
    },
  };
  const attempts = drivers.reconcileAttempts([event], s.trace);
  expect(attempts.trace).toHaveLength(1);
  const r = await scoreTask({
    task: task('T7'),
    arm: arms.find((a) => !a.history),
    trace: attempts.trace,
    final: 'Account history is not available in this configuration.',
    downloads: '/private/tmp',
  });
  expect(r).toMatchObject({ calls: 1, invalidCalls: 1, success: false });
});
test('EVAL-R1-06 multiple raw snapshots use the last cumulative total and preserve them all; cache deltas fail on resets', () => {
  const raw = drivers.usageFrom(
    'codex',
    controls.codexProbe.totals.map((usage) => ({ type: 'turn.completed', usage })),
  );
  expect(raw.inputTotal).toBe(101125);
  expect(raw.rawSnapshots).toHaveLength(3);
  const tracker = drivers.createUsageTracker('codex', { version: '0.162.1' });
  tracker.consume(
    drivers.usageFrom('codex', [{ type: 'turn.completed', usage: controls.codexProbe.totals[0] }]),
    'id',
  );
  const reset = { ...controls.codexProbe.totals[1], cached_input_tokens: 1 };
  expect(() =>
    tracker.consume(
      drivers.usageFrom('codex', [{ type: 'turn.completed', usage: reset }]),
      'id',
      'id',
    ),
  ).toThrow(/negative/);
  const missing = { ...controls.codexProbe.totals[1] };
  delete missing.cache_write_input_tokens;
  expect(() =>
    tracker.consume(
      drivers.usageFrom('codex', [{ type: 'turn.completed', usage: missing }]),
      'id',
      'id',
    ),
  ).toThrow(/continuity/);
});

test('EVAL-R1-01 duplicate contradictory JSON keys cannot silently become a correct answer', async () => {
  expect((await score('T4', '{"remaining":1,"remaining":2,"libgen_cap":"none"}')).success).not.toBe(
    true,
  );
});
test('EVAL-R1-02 contradictory correctness metrics are not admissible evidence', () => {
  const a = measured(arms[0]),
    b = measured(arms[1], 50);
  a.results[0].success = false;
  expect(summarize([a, b]).comparisons[0].status).not.toBe('bar met');
});
test('EVAL-R1-06 normalized complete Codex receipts are admitted but never strict-cold eligible; tampered deltas are refused', async () => {
  const { admitReceipt } = await import('./admission.js');
  const codex = (arm) => {
    const r = measured(arm);
    r.driver = 'codex';
    r.driverEnvironment = driverEnvironmentRecord('codex', {});
    for (const x of r.results) x.driverEnvironment = structuredClone(r.driverEnvironment);
    r.driverVersion = 'codex-cli 0.162.1';
    Object.assign(r.settings, drivers.driverPins.codex, { version: '0.162.1' });
    for (let run = 0; run < 5; run++) {
      const tracker = drivers.createUsageTracker('codex', { version: '0.162.1' });
      let input = 0,
        cache = 0,
        output = 0;
      for (const x of r.results.filter((x) => x.run === run)) {
        input += 100;
        cache += 20;
        output += 5;
        const counters = { input_tokens: input, cached_input_tokens: cache, output_tokens: output };
        x.usage = tracker.consume(
          drivers.usageFrom('codex', [{ type: 'turn.completed', usage: counters }]),
          x.session,
        );
        x.driverVersion = r.driverVersion;
        x.requestedModel = r.settings.model;
      }
    }
    return r;
  };
  const a = codex(arms[0]),
    b = codex(arms[1]);
  expect(admitReceipt(a).eligible).toBe(true);
  expect(summarize([a, b]).comparisons[0].status).not.toBe('bar met');
  b.results[1].usage.inputTotal = 200;
  b.results[1].usage.inputUncached = 180;
  expect(admitReceipt(b).eligible).toBe(false);
});

test('EVAL-R1-06 an intermediate resumed counter reset cannot be hidden by a later larger total', () => {
  const tracker = drivers.createUsageTracker('codex', { version: '0.162.1' });
  tracker.consume(
    drivers.usageFrom('codex', [{ type: 'turn.completed', usage: controls.codexProbe.totals[1] }]),
    'id',
  );
  const next = drivers.usageFrom('codex', [
    { type: 'turn.completed', usage: controls.codexProbe.totals[0] },
    { type: 'turn.completed', usage: controls.codexProbe.totals[2] },
  ]);
  expect(() => tracker.consume(next, 'id', 'id')).toThrow(/negative/);
});
