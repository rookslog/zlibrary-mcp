import { expect, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { ErrorEnvelopeSchema, ToolFailure, errorEnvelope } from '../dist/lib/tool-errors.js';

test.each([
  ['http_error', 'provider_error'],
  ['browser_check', 'provider_error'],
  ['protocol_error', 'provider_error'],
  ['connect_timeout', 'provider_unreachable'],
  ['read_timeout', 'provider_timeout'],
  ['timeout', 'provider_timeout'],
  ['internal', 'internal'],
])(
  'valid search with %s names source/helper failure and retains its envelope',
  async (reason, code) => {
    const tools = new AcquisitionTools(async () => {
      throw { context: { details: { reason, status_code: 403 } } };
    }, {});
    const reply = await tools.call('search', { query: 'book', sources: ['annas'] });
    expect(ErrorEnvelopeSchema.parse(reply)).toEqual(reply);
    const { message, ...fields } = reply.error;
    expect(message).toMatch(/request was valid; the (source|helper) failed/i);
    expect(fields).toEqual({
      code,
      source: 'annas',
      operation: 'search',
      retry: code === 'internal' ? 'no' : 'safe',
      recovery:
        reason === 'browser_check'
          ? "Anna's Archive is behind a DDoS-Guard browser check; this connector does not pass it. Use another source."
          : 'Try another source or candidate; report persistent provider drift.',
      attempts: [
        {
          source: 'annas',
          outcome: 'failed',
          code,
          reason,
          ...(reason === 'browser_check' ? { http_status: 403 } : {}),
        },
      ],
      detail: { reason, ...(reason === 'browser_check' ? { http_status: 403 } : {}) },
    });
  },
);

test.each([false, true])(
  'aggregate failure with an empty source=%s still identifies a valid request',
  async (empty) => {
    const tools = new AcquisitionTools(async (_op, args) => {
      if (empty && args.source === 'libgen') return { books: [] };
      throw { context: { details: { reason: 'read_timeout' } } };
    }, {});
    const reply = await tools.call('search', { query: 'book', sources: ['libgen', 'annas'] });
    expect(reply.error.code).toBe(empty ? 'partial_failure' : 'all_sources_failed');
    expect(reply.error.message).toMatch(/request was valid; the source failed/i);
  },
);

test('download timeout retains unknown outcome and quota-aware retry', () => {
  const reply = errorEnvelope({ context: { reason: 'timeout' } }, 'download', 'annas');
  expect(reply.error.message).toMatch(/request was valid; the source failed/i);
  expect(reply.error).toMatchObject({ code: 'outcome_unknown', retry: 'may_spend_quota' });
});

test('invalid requests keep their argument diagnosis without claiming validity', async () => {
  const reply = await new AcquisitionTools(async () => {
    throw new Error('must not run');
  }, {}).call('search', { query: 'book', sources: ['annas'], limit: 0 });
  expect(reply.error.code).toBe('invalid_argument');
  expect(reply.error.message).not.toMatch(/request was valid/i);
  const direct = errorEnvelope(new ToolFailure('invalid_argument', 'Invalid range.'), 'search');
  expect(direct.error.message).toBe('Invalid range.');
});
