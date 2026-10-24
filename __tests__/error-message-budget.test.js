import { expect, test } from '@jest/globals';
import { ErrorEnvelopeSchema, ToolFailure, errorEnvelope } from '../dist/lib/tool-errors.js';

test.each(['provider_error', 'internal'])(
  '%s keeps all 199 message characters with validity outside the budget',
  (code) => {
    const message = 'é'.repeat(196) + 'END';
    const reply = errorEnvelope(new ToolFailure(code, message, {
      retry: 'safe', recovery: 'Retry later.',
    }), 'search', 'libgen');
    expect(reply.error.message).toBe(message);
    expect(reply.error.recovery).toMatch(/request was valid; the (source|helper) failed/i);
    expect(reply.error.recovery).toContain('libgen:');
    expect(reply.error.recovery).toContain('Retry later.');
    expect(reply.error).toMatchObject({ code, source: 'libgen', retry: 'safe' });
    expect(ErrorEnvelopeSchema.parse(reply)).toEqual(reply);
  },
);

test('long messages truncate only their own content at 199 characters', () => {
  const reply = errorEnvelope(new ToolFailure('provider_error', 'x'.repeat(198) + 'YZ'), 'search', 'annas');
  expect(reply.error.message).toBe('x'.repeat(198) + 'Y');
  expect(reply.error.message).toHaveLength(199);
});


test.each([
  [198, 'a'.repeat(198)],
  [197, 'a'.repeat(197) + '😀'],
  [199, 'a'.repeat(199)],
])('199-unit cut with %s prefix units keeps Unicode well formed', (units, expected) => {
  const reply = errorEnvelope(new ToolFailure('provider_error', 'a'.repeat(units) + '😀tail'), 'search', 'libgen');
  expect(reply.error.message).toBe(expected);
  expect(reply.error.message.length).toBeLessThanOrEqual(199);
  expect(reply.error.message.isWellFormed()).toBe(true);
  expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
});
