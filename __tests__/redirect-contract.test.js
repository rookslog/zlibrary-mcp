import { expect, test } from '@jest/globals';
import { authDiagnosticsFor } from '../lib/auth-diagnostics.js';
import { errorEnvelope } from '../lib/tool-errors.js';

test('download refusal without child diagnostics keeps status, host and HTTP reason', () => {
  const error = {
    context: {
      pythonErrorType: 'RedirectRefusedError',
      httpStatus: 302,
      redirectHost: 'cdn.example.test',
      details: { request_issued: true },
    },
  };
  const diagnostics = authDiagnosticsFor(error, '00000000-0000-4000-8000-000000000001', 1, 0);
  expect(diagnostics.failure_kind).toBe('transport');
  expect(diagnostics.http_status).toBe(302);
  error.context.diagnostics = diagnostics;
  const reply = errorEnvelope(error, 'download', 'zlibrary').error;
  expect(reply).toMatchObject({
    code: 'provider_unreachable',
    retry: 'may_spend_quota',
    detail: { reason: 'http_error', diagnostics: { failure_kind: 'transport', http_status: 302 } },
  });
  expect(reply.message).toContain('Redirect refused (HTTP 302) to host cdn.example.test');
});
