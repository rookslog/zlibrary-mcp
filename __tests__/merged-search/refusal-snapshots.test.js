import { expect, test } from '@jest/globals';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { ToolFailure } from '../../dist/lib/tool-errors.js';

test('shared admission refusals keep their original retry across searches', async () => {
  const refusal = new ToolFailure('breaker_open', 'Search admission is closed.', {
    source: 'zlibrary',
    retry: 'safe',
    retry_after_s: 30,
    detail: { reason: 'auth_backoff', request_issued: false },
  });
  const originalInfo = refusal.info;
  const originalJSON = JSON.stringify(refusal);
  let breakerWait = 600;
  const tools = new AcquisitionTools(
    async () => {
      throw new Error('Unexpected source dispatch.');
    },
    {
      ZLIBRARY_EMAIL: 'fixture.invalid',
      ZLIBRARY_PASSWORD: 'synthetic-only',
    },
    {
      localState: () => ({
        zlibrary: {
          breaker: 'open',
          breakerBlocked: true,
          breakerRetryAfterS: breakerWait,
          admission: 'backoff',
          admissionRetryAfterS: 30,
          refusal,
        },
      }),
    },
  );
  const first = await tools.call('search', { query: 'Solaris', sources: ['zlibrary'] });
  expect(first.error.retry_after_s).toBe(600);
  const afterFirstJSON = JSON.stringify(refusal);
  breakerWait = 40;
  const second = await tools.call('search', { query: 'Solaris', sources: ['zlibrary'] });
  expect(second.error.retry_after_s).toBe(40);
  expect(afterFirstJSON).toBe(originalJSON);
  expect(refusal.info).toBe(originalInfo);
  expect(JSON.stringify(refusal)).toBe(originalJSON);
});
