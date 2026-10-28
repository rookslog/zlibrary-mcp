import { test, expect } from '@jest/globals';
import path from 'node:path';
import { commandFor } from './drivers.js';
import { here } from './server.js';
import { evaluateEAnswer } from './answers.js';
import { exerciseEFixture } from './e-scripted.js';
import { scoreTask } from './oracles.js';
import { eRunTotals } from './e-runner.js';

// Catches a missing/misspelled books override or accidental widening of tool access.
for (const session of [undefined, 'saved-session']) {
  test(`Codex books approval argv stays isolated (${session ?? 'cold'})`, () => {
    const command = commandFor({
      driver: 'codex',
      directory: '/tmp/e5',
      socketPath: '/tmp/e5/broker.sock',
      nonce: 'e5',
      session,
    });
    expect(command.args).toEqual([
      'exec',
      ...(session ? ['resume'] : []),
      '--ignore-user-config',
      '--ignore-rules',
      '--skip-git-repo-check',
      '--json',
      '--model',
      'gpt-6.1-sol',
      '--disable',
      'code_mode_tool_search',
      '--disable',
      'code_mode',
      '--disable',
      'multi_agent',
      '--disable',
      'memories',
      '--disable',
      'plugins',
      '--disable',
      'shell_tool',
      '-c',
      'model_reasoning_effort="high"',
      '-c',
      'approval_policy="never"',
      '-c',
      'sandbox_mode="read-only"',
      '-c',
      'web_search="disabled"',
      '-c',
      `mcp_servers.books.command=${JSON.stringify(process.execPath)}`,
      '-c',
      `mcp_servers.books.args=${JSON.stringify([path.join(here, 'proxy.mjs'), '/tmp/e5/broker.sock'])}`,
      '-c',
      'mcp_servers.books.required=true',
      '-c',
      'mcp_servers.books.default_tools_approval_mode="approve"',
      '-c',
      'developer_instructions="Evaluation e5. Use only the books MCP tools. Do not run commands, read files, use web, or delegate."',
      ...(session ? [session] : []),
    ]);
  });
}

// Catches loss of recoverable facts or accidental acceptance of malformed answers.
for (const [final, factStatus, formatStatus] of [
  ['{"retries":1}', 'correct', 'correct'],
  ['I observed this.\n{"retries":1}', 'correct', 'incorrect'],
  ['{"retries":"1"}', 'correct', 'incorrect'],
  ['{"retries":2}', 'incorrect', 'correct'],
  ['{"retries":true}', 'incorrect', 'incorrect'],
  ['```json\n{"retries":1}\n```', 'correct', 'incorrect'],
  ['{"retries":1,"note":"extra"}', 'correct', 'incorrect'],
  ['{"retries":2,"retries":1}', 'unavailable', 'incorrect'],
  ['{"retries":2}\n{"retries":1}', 'unavailable', 'incorrect'],
  ['no JSON', 'unavailable', 'incorrect'],
]) {
  test(`answer facts and format are independent: ${final}`, () => {
    const score = evaluateEAnswer({ final, expected: { retries: 1 } });
    expect(score.facts?.status).toBe(factStatus);
    expect(score.format?.status).toBe(formatStatus);
    expect(score.status).toBe(
      factStatus === 'correct' && formatStatus === 'correct' ? 'correct' : 'incorrect',
    );
  });
}

test('E1 accepts integer/numeric-string identity, keeps format and acquisition strict', async () => {
  await exerciseEFixture('E1', async (sample) => {
    for (const downloaded of [502, '502']) {
      const score = await scoreTask({ ...sample, final: JSON.stringify({ downloaded }) });
      expect(score.success).toBe(true);
      expect(score.answer.facts.status).toBe('correct');
    }
    const malformed = await scoreTask({ ...sample, final: 'I selected it.\n{"downloaded":"502"}' });
    expect(malformed).toMatchObject({ success: false, wrongDecisions: 0, formatFailures: 1 });
    expect(malformed.answer).toMatchObject({
      facts: { status: 'correct', actual: { downloaded: '502' } },
      format: { status: 'incorrect' },
    });
    const wrong = await scoreTask({ ...sample, final: '{"downloaded":"501"}' });
    expect(wrong.success).toBe(false);
    expect(wrong.answer.facts.status).toBe('incorrect');
    expect(wrong.wrongDecisions).toBeGreaterThan(0);
    expect(
      (await scoreTask({ ...sample, trace: [], providerEvents: [], final: '{"downloaded":"502"}' }))
        .success,
    ).toBe(false);
  });
}, 300000);

test('E4 also separates malformed truthful facts from wrong decisions', async () => {
  await exerciseEFixture('E4', async (sample) => {
    const score = await scoreTask({ ...sample, final: 'Done.\n{"downloaded":"202"}' });
    expect(score).toMatchObject({ success: false, wrongDecisions: 0, formatFailures: 1 });
    expect(score.answer.facts.status).toBe('correct');
  });
}, 300000);

test('diagnostic numeric normalization cannot bypass explicitly accepted identity strings', () => {
  const score = evaluateEAnswer({
    task: { id: 'E1' },
    final: '{"downloaded":"0502"}',
    expected: { downloaded: 502 },
    acceptedFacts: { downloaded: ['502'] },
  });
  expect(score.status).toBe('incorrect');
  expect(score.facts.status).toBe('incorrect');
});

test('future contract totals do not conceal a separate format failure', () => {
  expect(
    eRunTotals([
      {
        task: { futureOnly: true },
        score: { success: false, contractGaps: ['future'], wrongDecisions: 1, formatFailures: 1 },
      },
    ]).future,
  ).toEqual({ cases: 1, gapOnly: 0, wrong: 1 });
});
