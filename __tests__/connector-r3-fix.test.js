import { expect, test } from '@jest/globals';
import { readFileSync } from 'node:fs';
import { BridgeKilledError } from '../dist/lib/errors.js';
import { errorEnvelope, ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';

test.each([
  ['zlibrary', 'may_spend_quota', /may spend quota/],
  ['libgen', 'safe', /quota-free/],
])(
  'signal-killed %s download retains unknown outcome and source advice',
  (source, retry, advice) => {
    const killed = new BridgeKilledError('PRIVATE_KILL_MESSAGE', 'SIGKILL');
    const reply = errorEnvelope(killed, 'download', source);
    expect(reply.error).toMatchObject({
      code: 'outcome_unknown',
      source,
      operation: 'download',
      retry,
    });
    expect(reply.error.recovery).toContain('The request was valid; its outcome is unknown.');
    expect(reply.error.recovery).not.toContain('the source failed');
    expect(reply.error.recovery).toMatch(/disk receipts are checked first/);
    expect(reply.error.recovery).toMatch(advice);
    expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
    expect(JSON.stringify(reply)).not.toMatch(/PRIVATE_KILL_MESSAGE|SIGKILL/);
  },
);

test('signal-killed non-download keeps its existing internal/no-retry mapping', () => {
  const killed = new BridgeKilledError('PRIVATE_KILL_MESSAGE', 'SIGKILL');
  const reply = errorEnvelope(killed, 'get_book', 'zlibrary');
  expect(reply.error).toMatchObject({
    code: 'internal',
    source: 'zlibrary',
    operation: 'get_book',
    retry: 'no',
  });
  expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
  expect(JSON.stringify(reply)).not.toMatch(/PRIVATE_KILL_MESSAGE|SIGKILL/);
});

const api = readFileSync(new URL('../docs/api.md', import.meta.url), 'utf8');
const statusRow = (code) => api.split('\n').find((line) => line.startsWith(`| \`${code}\` |`));

test('quota documentation distinguishes the Anna router from unnormalized EAPI download failures', () => {
  const row = statusRow('quota_exhausted');
  expect(row).toMatch(/Anna's keyed-download router/);
  expect(row).toMatch(/zero remaining/);
  expect(row).toMatch(/Z-Library EAPI/);
  expect(row).toContain('`provider_error`');
  expect(row).toContain('`internal`');
});

test('absence documentation does not promise a business-status mapping no current adapter emits', () => {
  const row = statusRow('not_found');
  expect(row).toMatch(/explicit structured `not_found` reason/);
  expect(row).toMatch(/no current source adapter emits it/i);
  expect(row).toMatch(/Z-Library EAPI/);
  expect(row).toContain('`provider_error`');
  expect(row).toContain('`internal`');
  expect(row).toMatch(/empty search is not absence/);
});

test.each([
  ['zlibrary', true],
  ['libgen', false],
])('issued occupied-destination refusal on %s words quota use by source', (source, warns) => {
  const refused = {
    context: {
      details: { reason: 'output_refused', destination_occupied: true, request_issued: true },
    },
  };
  const reply = errorEnvelope(refused, 'download', source);
  expect(reply.error).toMatchObject({ code: 'output_refused', source, retry: 'no' });
  expect(reply.error.recovery).toMatch(/move or rename/);
  if (warns) expect(reply.error.recovery).toMatch(/may already have used a download/);
  else expect(reply.error.recovery).not.toMatch(/used a download/);
  expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
});
