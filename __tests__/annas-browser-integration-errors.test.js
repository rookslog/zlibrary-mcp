import { expect, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { errorEnvelope } from '../dist/lib/tool-errors.js';

const check = {
  context: {
    retryAfterMs: 1201,
    details: { reason: 'browser_check', status_code: 403, request_issued: false },
  },
};
const enabledRecovery = "Anna's Archive's visible browser could not clear the check, or its pacing/backoff is still active. A person may need to clear the check on the host; wait for any backoff before retrying.";
const disabledRecovery = "Anna's Archive is behind a DDoS-Guard browser check; this connector does not pass it. Use another source.";

test('integration search exposes the enabled browser recovery with HTTP status and backoff', async () => {
  const tools = new AcquisitionTools(async () => { throw check; }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
  expect(result.error).toMatchObject({
    code: 'provider_error', retry: 'safe', retry_after_s: 2,
    detail: { reason: 'browser_check', http_status: 403, request_issued: false },
    recovery: `The request was valid; the source failed. annas: ${enabledRecovery}`,
  });
});

test('integration keeps explicit flag-off recovery even when the process flag is enabled', async () => {
  const before = process.env.ANNAS_BROWSER_ENABLED;
  process.env.ANNAS_BROWSER_ENABLED = 'true';
  try {
    const tools = new AcquisitionTools(async () => { throw check; }, { ANNAS_BROWSER_ENABLED: 'false' });
    const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
    expect(result.error.recovery).toBe(`The request was valid; the source failed. annas: ${disabledRecovery}`);
  } finally {
    if (before === undefined) delete process.env.ANNAS_BROWSER_ENABLED;
    else process.env.ANNAS_BROWSER_ENABLED = before;
  }
});

test('integration enabled recovery reaches nested browser-check failures', () => {
  const result = errorEnvelope({ context: { details: { failures: [
    { provider: 'annas', reason: 'browser_check', status_code: 429 },
  ] } } }, 'search', 'annas', { ANNAS_BROWSER_ENABLED: 'true' });
  expect(result.error.recovery).toBe(`The request was valid; the source failed. annas: ${enabledRecovery}`);
  expect(result.error.detail).toEqual({ reason: 'browser_check', http_status: 429 });
});
