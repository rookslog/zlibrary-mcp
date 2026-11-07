import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import os from 'node:os';
import { readFileSync } from 'node:fs';

let original, metadata, runner, login, api, createServer, AcquisitionTools;
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
beforeEach(async () => {
  original = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_EMAIL: 'environment@example.test',
    ZLIBRARY_PASSWORD: 'fake-password',
    ZLIBRARY_SESSION_MODE: 'memory',
    RETRY_MAX_RETRIES: '0',
    CIRCUIT_BREAKER_THRESHOLD: '100',
    ZLIBRARY_OUTPUT_ROOT: os.tmpdir(),
    LOG_LEVEL: 'error',
  });
  for (const key of [
    'BOOK_SOURCE_DEFAULT_ORDER',
    'BOOK_SOURCE_SEARCH_WAIT_MS',
    'BOOK_FORMAT_PREFERENCE',
    'ANNAS_SECRET_KEY',
  ])
    delete process.env[key];
  login = {
    configured: true,
    identity: 'environment@example.test',
    origin: 'environment',
    backend: 'file',
    expiry: 'unknown',
    generation: 0,
    notice: '',
  };
  metadata = jest.fn(async () => login);
  runner = jest.fn();
  jest.resetModules();
  jest.unstable_mockModule('../dist/lib/login.js', () => ({
    readLoginMetadata: metadata,
    runLoginCommand: async () => 0,
  }));
  jest.unstable_mockModule('../dist/lib/python-runner.js', () => ({
    runPythonBridge: runner,
    LONG_BRIDGE_TIMEOUT_MS: 3900000,
  }));
  api = await import('../dist/lib/zlibrary-api.js');
  ({ createServer } = await import('../dist/index.js'));
  ({ AcquisitionTools } = await import('../dist/lib/acquisition-tools.js'));
});
afterEach(() => {
  process.env = original;
});

test.each(['environment@example.test', 'saved@example.test'])(
  'saved identity %s uses comparison search without environment-session backoff',
  async (identity) => {
    runner.mockRejectedValue(
      Object.assign(new Error('fake rejected environment login'), {
        stderr: JSON.stringify({
          type: 'EAPIAuthenticationError',
          error: 'Fake authentication rejected',
        }),
      }),
    );
    await api.invokeAcquisitionBridge('search', { query: 'fixture' }).catch(() => {});
    expect(api.getSearchState().zlibrary.admission).toBe('backoff');
    login = { ...login, identity, origin: 'saved', generation: 1 };
    runner.mockReset().mockImplementation(async (_script, options, control) => {
      expect(control.sessionInput).toBeUndefined();
      expect(options.env.ZLIBRARY_LOGIN_EXPECTED_ORIGIN).toBe('saved');
      expect(options.env.ZLIBRARY_LOGIN_EXPECTED_IDENTITY).toBe(identity);
      return envelope({
        books: [{ id: '42', hash: 'fake-hash', title: 'Saved book', md5: 'a'.repeat(32) }],
      });
    });
    const server = createServer();
    try {
      const limits = await server.tools.call('get_limits');
      expect(limits.search.default_sources).toEqual(['libgen', 'zlibrary']);
      expect(limits.sources.zlibrary).toMatchObject({ configured: true, admission: 'open' });
      expect(limits.sources.zlibrary.retry_after_s).toBeUndefined();
      const reply = await server.tools.call('search', {
        query: 'Saved book',
        sources: ['zlibrary'],
      });
      expect(reply.error).toBeUndefined();
      expect(reply.rows[0].title).toBe('Saved book');
      expect(reply.sources.zlibrary.state).toBe('rows');
      expect(reply.all_answered).toBe(true);
      expect(runner).toHaveBeenCalledTimes(1);
    } finally {
      await server.close();
    }
  },
);

test('metadata refusal preserves the limits search block and refuses account search before dispatch', async () => {
  metadata.mockRejectedValue(new Error('fake metadata deadline'));
  const invoke = jest.fn(async () => ({ books: [{ id: '42', title: 'Fixture' }] }));
  const tools = new AcquisitionTools(invoke);
  const limits = await tools.call('get_limits', { sources: ['zlibrary'] });
  expect(limits.search).toMatchObject({
    default_sources: ['libgen', 'zlibrary'],
    ordering: 'fit-v1',
  });
  expect(limits.sources.zlibrary).toMatchObject({
    configured: true,
    error: {
      code: 'internal',
      detail: { reason: 'login_metadata_unavailable', request_issued: false },
    },
  });
  const reply = await tools.call('search', { query: 'Fixture', sources: ['zlibrary'] });
  expect(invoke).not.toHaveBeenCalled();
  expect(reply).toMatchObject({
    error: {
      code: 'internal',
      detail: { reason: 'login_metadata_unavailable', request_issued: false },
    },
  });
});

test('unconfigured comparison limits retain the saved-login setup note', async () => {
  const recorded = JSON.parse(
    readFileSync(new URL('./merged-search/legacy-boundary.json', import.meta.url), 'utf8'),
  );
  login = { ...login, configured: false, identity: '', origin: 'none' };
  const limits = await new AcquisitionTools(jest.fn(), {
    ...process.env,
    ZLIBRARY_EMAIL: '',
    ZLIBRARY_PASSWORD: '',
  }).call('get_limits');
  expect(limits.sources.zlibrary.note).toBe(recorded.limits.sources.zlibrary.note);
  expect(limits.sources.zlibrary.note).toBe(
    'Run zlibrary login or set ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD.',
  );
  expect(limits.search.default_sources).toEqual(['libgen']);
});
