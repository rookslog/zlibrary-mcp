import { test, expect, jest } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { classifyFailure, errorEnvelope, ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';
const env = { ZLIBRARY_EMAIL: 'offline', ZLIBRARY_PASSWORD: 'offline' };

test.each([
  ['configuration_error', 'credentials_missing', 'no'], ['quota_exhausted', 'quota_exhausted', 'no'],
  ['dns_failure', 'provider_unreachable', 'safe'], ['dns_timeout', 'provider_unreachable', 'safe'],
  ['connect_timeout', 'provider_unreachable', 'safe'], ['connect_refused', 'provider_unreachable', 'safe'],
  ['connect_error', 'provider_unreachable', 'safe'], ['tls_error', 'provider_unreachable', 'safe'],
  ['read_timeout', 'provider_timeout', 'safe'], ['search_timeout', 'provider_timeout', 'safe'],
  ['protocol_error', 'provider_error', 'safe'], ['http_error', 'provider_error', 'safe'],
  ['integrity_mismatch', 'integrity_mismatch', 'safe'], ['auth_rejected', 'auth_failed', 'no'],
  ['login_failed', 'auth_failed', 'no'], ['timeout', 'provider_timeout', 'safe'],
  ['aborted', 'cancelled', 'safe'], ['output_refused', 'output_refused', 'no'],
])('read failure %s preserves closed code %s and retry %s', (reason, code, retry) => {
  const result = errorEnvelope({ context: { details: { reason } } }, 'search', 'libgen');
  expect(result.error).toMatchObject({ code, source: 'libgen', retry, detail: { reason } });
  expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
});
test.each(['aborted', 'timeout', 'read_timeout', 'integrity_mismatch', 'protocol_error', 'connect_timeout'])('download %s never promises quota-free retry after possible issuance', reason => {
  const result = errorEnvelope({ context: { details: { reason } } }, 'download', 'zlibrary');
  expect(result.error.retry).toBe('may_spend_quota');
});
test('download cancellation before issuance is safe; unknown failures do not leak secrets', () => {
  expect(errorEnvelope({ context: { details: { reason: 'aborted', request_issued: false } } }, 'download').error.retry).toBe('safe');
  const result = errorEnvelope(new Error('https://bad.invalid?key=SECRET'), 'search');
  expect(JSON.stringify(result)).not.toContain('SECRET');
  expect(result.error.code).toBe('internal');
});
test('provider aggregate retains integrity verdict and child reasons', () => {
  const failure = classifyFailure({ context: { details: { failures: [
    { provider: 'libgen', reason: 'dns_failure' }, { provider: 'libgen', reason: 'integrity_mismatch' },
  ] } } }, 'download', 'libgen');
  expect(errorEnvelope(failure, 'download').error).toMatchObject({ code: 'integrity_mismatch', retry: 'safe', attempts: [
    { reason: 'dns_failure' }, { reason: 'integrity_mismatch' },
  ] });
});
test('all-empty is success, all failed is aggregate, and source order is not silently restarted', async () => {
  const invoke = jest.fn(async () => ({ books: [] }));
  const tools = new AcquisitionTools(invoke, env);
  const empty = await tools.call('search', { query: 'absent', sources: ['libgen', 'zlibrary'] });
  expect(empty.rows).toEqual([]);
  expect(empty.sources.libgen.state).toBe('empty');expect(empty.sources.zlibrary.state).toBe('empty');expect(empty.all_answered).toBe(true);
  invoke.mockRejectedValue({ context: { details: { reason: 'dns_failure' } } });
  const failed = await tools.call('search', { query: 'absent', sources: ['libgen', 'annas'] });
  expect(failed.error.code).toBe('all_sources_failed');
  expect(failed.error.attempts.map(a=>a.source)).toEqual(['libgen','zlibrary','annas']);expect(failed.error.attempts.filter(a=>a.state==='failed').map(a=>a.source)).toEqual(['libgen','annas']);
});
test('operator default is resolved once and missing credentials are skipped transparently', async () => {
  const tools = new AcquisitionTools(async () => ({ books: [{ md5: '0'.repeat(32), title: 'Book', source: 'libgen' }] }), { BOOK_SOURCE_DEFAULT_ORDER: 'zlibrary,libgen' });
  const result = await tools.call('search', { author: 'Author' });
  expect(result.rows[0].source).toBe('libgen');expect(result.sources.zlibrary).toMatchObject({state:'not_asked',reason:'credentials_missing'});expect(result.all_answered).toBe(false);expect((await tools.call('get_limits',{})).search).toMatchObject({default_sources:['libgen','zlibrary'],route_order:['zlibrary','libgen']});
  expect(() => new AcquisitionTools(jest.fn(), { BOOK_SOURCE_DEFAULT_ORDER: 'auto' })).toThrow(/BOOK_SOURCE_DEFAULT_ORDER/);
});
test('history rejects unsupported sources before network and expired cursors retain typed recovery', async () => {
  const invoke = jest.fn();
  const tools = new AcquisitionTools(invoke, env);
  expect((await tools.call('get_history', { source: 'libgen' })).error.code).toBe('unsupported_operation');
  expect((await tools.call('search', { cursor: 'cmissing' })).error.code).toBe('ref_unavailable');
  expect(invoke).not.toHaveBeenCalled();
});
test('limits isolates source failure and marks unparsed account numbers honestly', async () => {
  const invoke = jest.fn().mockRejectedValue({ context: { details: { reason: 'dns_failure' } } });
  const tools = new AcquisitionTools(invoke, env);
  const failed = await tools.call('get_limits', { mode: 'live' });
  expect(failed.sources.zlibrary.error.code).toBe('provider_unreachable');
  expect(failed.sources.libgen.limit.kind).toBe('none');
  invoke.mockResolvedValue({ daily_limit: 'unknown', daily_remaining: 'unknown' });
  expect((await tools.call('get_limits', { mode: 'live' })).sources.zlibrary.limit.kind).toBe('unparsed');
});
test('pre-cancelled call and invalid year range cost no request', async () => {
  const invoke = jest.fn();
  const tools = new AcquisitionTools(invoke, env);
  const controller = new AbortController(); controller.abort();
  expect((await tools.call('search', { query: 'book', sources: ['libgen'] }, { signal: controller.signal })).error.retry).toBe('safe');
  expect((await tools.call('search', { query: 'book', sources: ['libgen'], year_from: 2000, year_to: 1900 })).error.code).toBe('invalid_argument');
  expect(invoke).not.toHaveBeenCalled();
});
