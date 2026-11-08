import '../helpers/environment-login.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import Ajv from 'ajv';
import fs from 'node:fs';
import { z } from 'zod';
const entry = await import('../../dist/index.js');
const { AcquisitionTools, createInputs } = await import('../../dist/lib/acquisition-tools.js');
import { ErrorEnvelopeSchema } from '../../dist/lib/tool-errors.js';
import {
  extensionFixture,
  futureHarness,
  names,
  budgetFixture,
  futureDownloadSchema,
} from './additive-fixture.js';
import { assertAccounting } from './model-assertions.js';
beforeEach(() => jest.useFakeTimers({ now: 1000000 }));
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});
const validate = (name) =>
  new Ajv({ strict: false }).compile(
    entry.TOOL_DEFINITIONS.find((t) => t.name === name).outputSchema,
  );

test('F46 F47 structural guards allow registered source data, optional held fields and unknown reasons', async () => {
  const f = extensionFixture({ count: 2 });
  const reply = await f.tools.call('search', { query: 'Solaris', sources: ['held'] });
  const extended = structuredClone(reply);
  extended.rows[0].held = {
    status: 'held',
    openable_here: true,
    asset_ref: 'afixture',
    identity: 'sha256',
    index_generation: 'fixture',
  };
  extended.rows[0].held_hint = { ref: extended.rows[0].ref, match: 'likely' };
  extended.sources.annas.reason = 'future_reason';
  expect(validate('search')(extended)).toBe(true);
  expect(Object.keys(reply.sources)).toEqual(names);
  assertAccounting(reply, names, ['held']);
  const bad = structuredClone(extended);
  bad.sources.held.state = 'collecting';
  expect(validate('search')(bad)).toBe(false);
});
test('F46 executable fourth-source digest extraction participates in the same file group', async () => {
  const f = extensionFixture({ groups: true, env: { BOOK_SOURCE_DEFAULT_ORDER: 'held,libgen' } });
  const reply = await f.tools.call('search', { query: 'Solaris', sources: ['held', 'libgen'] });
  expect(reply.rows).toHaveLength(1);
  expect(reply.rows[0].source).toBe('held');
  expect(reply.rows[0].other_copies[0].source).toBe('libgen');
  expect(reply.rows[0].route_basis).toBe('operator_order');
  assertAccounting(reply, names, ['libgen', 'held']);
});
test('registered-source advertised input bounds accept four distinct names and public bounds reject held', () => {
  const definitions = entry.toolDefinitionsFor(names);
  const schema = definitions.find((t) => t.name === 'search').inputSchema;
  const valid = new Ajv({ strict: false }).compile(schema);
  expect(valid({ query: 'Solaris', sources: names })).toBe(true);
  expect(valid({ query: 'Solaris', sources: ['held', 'held'] })).toBe(false);
  const publicInput = z.toJSONSchema(createInputs().search);
  delete publicInput.$schema;
  expect(
    new Ajv({ strict: false }).compile(publicInput)({ query: 'Solaris', sources: ['held'] }),
  ).toBe(false);
  expect(definitions.map((t) => t.name)).toEqual([
    'search',
    'get_book',
    'download',
    'get_limits',
    'get_history',
  ]);
});
test('fourth-source details use its registered owner and keep source identity', async () => {
  const f = extensionFixture();
  const reply = await f.tools.call('search', { query: 'Solaris', sources: ['held'] });
  const detail = await f.tools.call('get_book', { ref: reply.rows[0].ref });
  expect(detail).toMatchObject({
    source: 'held',
    edition: 'Registered detail',
    identifiers: { md5: '0'.repeat(30) + '97' },
  });
  expect(f.events.detail.map((x) => x.slice(0, 2))).toEqual([['held', 'held-0']]);
  expect(f.events.acquire).toEqual([]);
});
test('fourth-source fulfillment uses its registered owner once and never the generic bridge', async () => {
  const f = extensionFixture();
  const reply = await f.tools.call('search', { query: 'Solaris', sources: ['held'] });
  const downloaded = await f.tools.call('download', { ref: reply.rows[0].ref });
  expect(downloaded).toMatchObject({
    source: 'held',
    verified: 'sha256',
    path: '/synthetic/registered.pdf',
  });
  expect(f.events.acquire).toHaveLength(1);
  expect(f.events.acquire[0][0]).toBe('held');
  expect(f.events.acquire[0][1].book_details.source).toBe('held');
  expect(validate('download')(downloaded)).toBe(true);
});
test('registered-source failures pass the executable error validator with open reasons', async () => {
  const f = extensionFixture({ failed: 'held' });
  const reply = await f.tools.call('search', { query: 'Solaris', sources: ['held'] });
  expect(reply.error).toMatchObject({
    source: 'held',
    code: 'provider_error',
    detail: { reason: 'future_reason' },
  });
  expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
  expect(reply.error.attempts.map((a) => a.source)).toEqual(names);
});
test('a registered source without history returns its own unsupported error without a call', async () => {
  const f = extensionFixture();
  const reply = await f.tools.call('get_history', { source: 'held' });
  expect(reply.error).toMatchObject({ code: 'unsupported_operation', source: 'held' });
  expect(ErrorEnvelopeSchema.safeParse(reply).success).toBe(true);
  expect(f.events.search).toEqual([]);
  expect(f.events.detail).toEqual([]);
  expect(f.events.acquire).toEqual([]);
});
test('F48 public registration refuses held before dispatch', async () => {
  let calls = 0;
  const tools = new AcquisitionTools(async () => {
    calls++;
    return { books: [] };
  }, {});
  const reply = await tools.call('search', { query: 'Solaris', sources: ['held'] });
  expect(reply.error.code).toBe('invalid_argument');
  expect(calls).toBe(0);
});
const budgetBaseline = JSON.parse(
  fs.readFileSync(new URL('./byte-budget.json', import.meta.url), 'utf8'),
);
test.each(['plain_public', 'groups_registered', 'mixed_registered'])(
  'F49 ten file rows stay below 6KB and within measured growth budget: %s',
  async (kind) => {
    const f = budgetFixture(kind),
      reply = await f.tools.call('search', { query: 'Solaris', sources: f.sources, limit: 10 });
    expect(reply.rows).toHaveLength(10);
    const bytes = Buffer.byteLength(JSON.stringify(reply));
    expect(bytes).toBeLessThan(6000);
    expect(bytes).toBeLessThanOrEqual(Math.floor(budgetBaseline[kind] * 1.1));
    expect(validate('search')(reply)).toBe(true);
    assertAccounting(reply, f.names, f.sources);
    if (f.options.groups)
      expect(reply.rows.every((row) => row.other_copies.length === 2)).toBe(true);
    if (f.options.failed)
      expect(reply.sources.annas).toMatchObject({ state: 'failed', reason: 'future_reason' });
  },
);
test('M12 ordinary foreground inputs keep receipt arms with optional jobs installed', async () => {
  const f = extensionFixture();
  const harness = futureHarness(f.tools);
  const search = await harness.call('search', { query: 'Solaris', sources: ['libgen'] });
  const ordinary = await harness.call('download', { ref: search.rows[0].ref });
  expect(validate('download')(ordinary)).toBe(true);
  expect(ordinary).toHaveProperty('record');
  expect(ordinary).not.toHaveProperty('job');
  expect(harness.starts).toBe(0);
  const job = await harness.call('download', { ref: search.rows[0].ref, background: true });
  expect(job).toMatchObject({ job: 'jfixture1', state: 'running' });
  expect(validate('download')(job)).toBe(false);
  expect(await harness.call('download', { job: job.job })).toEqual(job);
  expect(harness.starts).toBe(1);
  expect(f.events.acquire).toHaveLength(1);
});
test('M12 optional job and local-file arms are disjoint new schema arms, never ordinary receipt replies', () => {
  const publicSchema = entry.TOOL_DEFINITIONS.find((t) => t.name === 'download').outputSchema;
  const extension = futureDownloadSchema(publicSchema);
  const accepts = new Ajv({ strict: false }).compile(extension),
    ordinary = validate('download');
  const arms = [
    { job: 'jfixture', state: 'running', ref: 'rfixture', source: 'libgen' },
    {
      path: '/synthetic/local.pdf',
      bytes: 1,
      fulfilled: 'available_local',
      verified: 'sha256',
      source: 'held',
    },
  ];
  for (const arm of arms) {
    expect(ordinary(arm)).toBe(false);
    expect(accepts(arm)).toBe(true);
  }
  expect(accepts({ ...arms[0], ...arms[1] })).toBe(false);
});
test('M12 installed optional jobs do not keep ordinary late searches running or mint result handles', async () => {
  let now = 0;
  const f = extensionFixture({
    env: { BOOK_SOURCE_SEARCH_WAIT_MS: '100' },
    runtime: { now: () => now },
    delay: async (name, request) =>
      name === 'libgen' ? { rows: [], accepted: 0 } : new Promise(() => {}),
  });
  const harness = futureHarness(f.tools);
  const pending = harness.call('search', { query: 'Solaris', sources: ['libgen', 'zlibrary'] });
  await jest.advanceTimersByTimeAsync(0);
  now = 100;
  await jest.advanceTimersByTimeAsync(100);
  const reply = await pending;
  expect(reply.error.code).toBe('partial_failure');
  expect(reply.error.attempts.find((a) => a.source === 'zlibrary').state).toBe('late');
  expect(f.events.aborted).toEqual([['zlibrary', 'search_cut']]);
  expect(harness.starts).toBe(0);
  expect(reply).not.toHaveProperty('search_id');
});

test('M12 the public parser does not silently start an unregistered background mode', async () => {
  const f = extensionFixture();
  const search = await f.tools.call('search', { query: 'Solaris', sources: ['libgen'] });
  const reply = await f.tools.call('download', { ref: search.rows[0].ref, background: true });
  expect(reply.error.code).toBe('invalid_argument');
  expect(f.events.acquire).toEqual([]);
});

test.each(['get_book', 'download'])(
  'registered %s errors cannot replace selected provenance with a different source',
  async (operation) => {
    const { SourceRegistry } = await import('../../dist/lib/source-registry.js');
    const { ToolFailure } = await import('../../dist/lib/tool-errors.js');
    const f = extensionFixture();
    const fail = async () => {
      throw new ToolFailure('provider_error', 'Synthetic owner failure.', {
        source: 'zlibrary',
        retry: 'safe',
        detail: { reason: 'http_error' },
      });
    };
    const registry = new SourceRegistry(
      f.specs.map((spec) =>
        spec.name === 'held' ? { ...spec, details: fail, acquire: fail } : spec,
      ),
    );
    const tools = new AcquisitionTools(
      async () => {
        throw new Error('No generic dispatch');
      },
      {},
      { registry },
    );
    const search = await tools.call('search', { query: 'Solaris', sources: ['held'] });
    const reply = await tools.call(operation, { ref: search.rows[0].ref });
    expect(reply.error).toMatchObject({
      source: 'held',
      code: 'provider_error',
      retry: 'safe',
      detail: { reason: 'http_error' },
    });
    expect(reply.error.message.startsWith('held: ')).toBe(true);
  },
);
