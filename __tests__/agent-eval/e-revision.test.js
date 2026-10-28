import { test, expect } from '@jest/globals';
import * as answers from './answers.js';
import { expectedError } from './oracles.js';
import { eTasks } from './tasks.js';
test('E tasks publish answer fields, including boolean recovery decisions', () => {
  for (const task of eTasks) expect(answers.answerContract(task)).toBeDefined();
  expect(answers.answerContract({ id: 'E13' })).toHaveProperty('check_receipt');
});
test('E answer fields accept declared facts and reject unknown explanations or wrong facts', () => {
  expect(
    answers.evaluateEAnswer({
      final: '{"note":"observed","downloaded":502}',
      expected: { downloaded: 502 },
    }).status,
  ).toBe('incorrect');
  expect(
    answers.evaluateEAnswer({ final: '{"downloaded":501}', expected: { downloaded: 502 } }).status,
  ).toBe('incorrect');
});
test('expected errors cannot hide missing issuance declarations or a contradictory issued flag', () => {
  const call = {
    name: 'download',
    reply: {
      isError: true,
      structuredContent: { error: { code: 'outcome_unknown', detail: { request_issued: false } } },
    },
  };
  expect(
    expectedError({ expectedErrors: [{ tool: 'download', code: 'outcome_unknown' }] }, call),
  ).toBe(false);
  expect(
    expectedError(
      { expectedErrors: [{ tool: 'download', code: 'outcome_unknown', requestIssued: true }] },
      call,
    ),
  ).toBe(false);
});

test('E recovery scoring uses boolean fields and cannot accept an unsafe retry sentence', () => {
  const expected = {
    outcome: 'unknown',
    check_destination: true,
    check_receipt: true,
    fresh_intent_required: true,
    automatic_retry: false,
  };
  expect(
    answers.evaluateEAnswer({
      final: JSON.stringify({
        automatic_retry: false,
        fresh_intent_required: true,
        check_receipt: true,
        check_destination: true,
        outcome: 'UNKNOWN',
      }),
      expected,
    }).status,
  ).toBe('correct');
  expect(
    answers.evaluateEAnswer({
      final: JSON.stringify({ ...expected, check_receipt: false, automatic_retry: true }),
      expected,
    }).status,
  ).toBe('incorrect');
});
