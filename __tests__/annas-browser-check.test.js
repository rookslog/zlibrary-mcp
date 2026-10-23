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

// Review of 6ad5cfb, F2: the public source loop rebuilt attempts without the
// status. These go through AcquisitionTools.call, as a client sees it.
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
const bridge = (answers) => async (op, args) => {
  const answer = answers[args.source];
  if (answer instanceof Error || answer.context) throw answer;
  return answer;
};
const libgenDown = { context: { details: { provider: 'libgen', reason: 'http_error', detail: 'HTTP 502' } } };
const annasChallenge = { context: { details: challenge } };
const row = { md5: 'a'.repeat(32), title: 'Grid systems', source: 'libgen' };

test('all sources failed: the Anna attempt keeps http_status (reviewer red assertion)', async () => {
  const tools = new AcquisitionTools(bridge({ libgen: libgenDown, annas: annasChallenge }), {});
  const r = await tools.call('search', { query: 'grid', sources: ['libgen', 'annas'] });
  expect(r.error.code).toBe('all_sources_failed');
  expect(r.error.attempts.find((a) => a.source === 'annas').http_status).toBe(403);
  expect(r.error.attempts.find((a) => a.source === 'annas').reason).toBe('browser_check');
  expect(r.error.attempts.find((a) => a.source === 'libgen')).not.toHaveProperty('http_status');
});
test('empty plus failed: partial_failure keeps the Anna status', async () => {
  const tools = new AcquisitionTools(bridge({ libgen: { books: [] }, annas: annasChallenge }), {});
  const r = await tools.call('search', { query: 'grid', sources: ['libgen', 'annas'] });
  expect(r.error.code).toBe('partial_failure');
  expect(r.error.attempts).toEqual([
    { source: 'libgen', outcome: 'empty' },
    { source: 'annas', outcome: 'failed', code: 'provider_error', reason: 'browser_check', http_status: 403 },
  ]);
});
test('successful fallback: routing attempts keep the Anna status', async () => {
  const tools = new AcquisitionTools(bridge({ annas: annasChallenge, libgen: { books: [row] } }), {});
  const r = await tools.call('search', { query: 'grid', sources: ['annas', 'libgen'] });
  expect(r.rows).toHaveLength(1);
  expect(r.routing.fell_back).toBe(true);
  expect(r.routing.attempts[0]).toMatchObject({ source: 'annas', reason: 'browser_check', http_status: 403 });
  expect(r.routing.attempts[1]).toEqual({ source: 'libgen', outcome: 'rows' });
});
test('single-source challenge keeps detail.http_status and the plain http_error has none', async () => {
  const one = await new AcquisitionTools(bridge({ annas: annasChallenge }), {}).call('search', {
    query: 'grid',
    sources: ['annas'],
  });
  expect(one.error.detail).toEqual({ reason: 'browser_check', http_status: 403 });
  const plain = await new AcquisitionTools(bridge({ libgen: libgenDown }), {}).call('search', {
    query: 'grid',
    sources: ['libgen'],
  });
  expect(plain.error.detail).toEqual({ reason: 'http_error' });
  expect(plain.error.attempts[0]).not.toHaveProperty('http_status');
});
