import { expect, test } from '@jest/globals';
import { ErrorEnvelopeSchema, errorEnvelope } from '../dist/lib/tool-errors.js';

// The bridge forwards SourceError.to_dict() as context.details.
const challenge = {
  provider: 'annas',
  host: 'annas-archive.gl',
  reason: 'browser_check',
  detail: 'HTTP 403',
  status_code: 403,
};

test('a DDoS-Guard challenge is reported as browser_check with its HTTP status', () => {
  const envelope = errorEnvelope({ context: { details: challenge } }, 'search', 'annas');
  expect(ErrorEnvelopeSchema.parse(envelope)).toBeTruthy();
  expect(envelope.error.code).toBe('provider_error');
  expect(envelope.error.detail).toEqual({ reason: 'browser_check', http_status: 403 });
  expect(envelope.error.recovery).toMatch(/DDoS-Guard browser check/);
});
test('a fallback walk keeps browser_check and its status per attempt', () => {
  const envelope = errorEnvelope(
    {
      context: {
        details: {
          operation: 'search',
          failures: [{ provider: 'libgen', reason: 'connect_timeout' }, challenge],
        },
      },
    },
    'search',
  );
  expect(envelope.error.detail).toEqual({ reason: 'browser_check', http_status: 403 });
  expect(envelope.error.attempts[1]).toMatchObject({ source: 'annas', reason: 'browser_check', http_status: 403 });
  expect(envelope.error.attempts[0]).not.toHaveProperty('http_status');
});
test('a plain HTTP error keeps http_error and no browser-check wording', () => {
  const envelope = errorEnvelope(
    { context: { details: { provider: 'annas', reason: 'http_error', detail: 'HTTP 403' } } },
    'search',
    'annas',
  );
  expect(envelope.error.detail).toEqual({ reason: 'http_error' });
  expect(envelope.error.recovery).not.toMatch(/DDoS/);
});
