import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { format } from 'node:util';

jest.setTimeout(30000);
const saved = { ...process.env };
const sentinels = [
  'i251-password',
  'i251-cookie',
  'i251-token',
  'i251@example.test',
  'i251-body',
  'i251-book',
];
const poison = sentinels.join(' ');
let temporary, modeFile, callsFile, captured, logs, api, envelope, offsets, pythonExecutable;
const listeners = new Map(
  ['exit', 'SIGINT', 'SIGTERM'].map((n) => [n, new Set(process.rawListeners(n))]),
);
beforeEach(async () => {
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'i251-12345678-1234-4234-8234-123456789abc-'));
  modeFile = path.join(temporary, 'mode');
  callsFile = path.join(temporary, 'calls');
  fs.writeFileSync(modeFile, 'success');
  const root = process.cwd();
  pythonExecutable = path.join(root, '.venv/bin/python');
  offsets = ['logs/zlibrary_debug.log', 'logs/nodejs_debug.log'].map((file) => [
    file,
    fs.existsSync(file) ? fs.statSync(file).size : 0,
  ]);
  fs.writeFileSync(
    path.join(temporary, 'offline.py'),
    `import sys, os, runpy, json, socket
sys.path[:0]=[${JSON.stringify(path.join(root, 'lib'))},${JSON.stringify(path.join(root, 'zlibrary/src'))},${JSON.stringify(root)}]
def forbidden(*a,**k): raise AssertionError('real network forbidden')
socket.socket.connect=forbidden
socket.getaddrinfo=forbidden
import httpx
import ssl
from zlibrary import eapi
async def resolve():
    with open(${JSON.stringify(modeFile)}) as f: mode=f.read()
    if mode=='discovery-transport': raise httpx.ConnectTimeout(${JSON.stringify(poison)})
    return 'fake.example.test'
eapi.resolve_eapi_domain=resolve
async def get_client(self):
    with open(${JSON.stringify(modeFile)}) as f: mode=f.read()
    if mode=='local-budget':
        from lib.acquisition import AcquisitionError
        raise AcquisitionError('walk_budget_exhausted','synthetic local refusal',issued=False)
    if self._client is None or self._client.is_closed:
        self._client=httpx.AsyncClient(transport=httpx.MockTransport(handle),cookies=self._cookies)
    return self._client
eapi.EAPIClient._get_client=get_client
def handle(request):
    with open(${JSON.stringify(callsFile)},'a') as f: f.write(request.url.path+'\\n')
    with open(${JSON.stringify(modeFile)}) as f: mode=f.read()
    login=request.url.path=='/eapi/user/login'
    if mode in ('login-tls','login-tls-context'):
        try: raise ssl.SSLCertVerificationError(1,${JSON.stringify(poison)})
        except ssl.SSLCertVerificationError as cause:
            if mode=='login-tls-context': raise httpx.ConnectError(${JSON.stringify(poison)},request=request) from None
            raise httpx.ConnectError(${JSON.stringify(poison)},request=request) from cause
    if login and not mode.startswith('login-'):
        return httpx.Response(200,json={'success':1,'user':{'id':'17','remix_userkey':'i251-cookie'}})
    if mode=='validate-refresh-400':
        marker=${JSON.stringify(path.join(temporary, 'validation-expired'))}
        if request.url.path=='/eapi/user/profile':
            if not os.path.exists(marker):
                with open(marker,'w') as f: f.write('expired')
                return httpx.Response(401,json={'error':${JSON.stringify(poison)}})
            return httpx.Response(200,json={'success':1,'user':{'id':'17'}})
        return httpx.Response(400,json={'error':${JSON.stringify(poison)}})
    if mode=='refresh-then-400':
        if request.url.path=='/eapi/user/profile': return httpx.Response(200,json={'success':1,'user':{'id':'17'}})
        marker=${JSON.stringify(path.join(temporary, 'expired'))}
        if not os.path.exists(marker):
            with open(marker,'w') as f: f.write('expired')
            return httpx.Response(401,json={'error':${JSON.stringify(poison)}})
        return httpx.Response(400,json={'error':${JSON.stringify(poison)}})
    if mode=='success': return httpx.Response(200,json={'success':1,'books':[],'user':{'id':'17'}})
    if mode=='transport': raise httpx.ReadTimeout(${JSON.stringify(poison)},request=request)
    if mode=='read-error': raise httpx.ReadError(${JSON.stringify(poison)},request=request)
    if mode=='write-error': raise httpx.WriteError(${JSON.stringify(poison)},request=request)
    if mode=='write-timeout': raise httpx.WriteTimeout(${JSON.stringify(poison)},request=request)
    if mode=='pool-timeout': raise httpx.PoolTimeout(${JSON.stringify(poison)},request=request)
    if mode=='remote-protocol': raise httpx.RemoteProtocolError(${JSON.stringify(poison)},request=request)
    if mode=='malformed': return httpx.Response(200,text=${JSON.stringify(poison)})
    if mode=='wall': return httpx.Response(403,text='diamwall '+${JSON.stringify(poison)})
    if mode=='oversized': return httpx.Response(500,text=(${JSON.stringify(poison)}+'\\n')*10000)
    status=401 if mode in ('auth','login-auth') else 400
    return httpx.Response(status,json={'success':0,'error':${JSON.stringify(poison)},'book':{'title':'i251-book'},'url':'https://i251-token:i251-password@fake.example.test/'})
runpy.run_path(${JSON.stringify(path.join(root, 'lib/python_bridge.py'))},run_name='__main__')
`,
  );
  Object.assign(process.env, {
    // Acquisition diagnostics must reach the provider inside this test's root.
    ZLIBRARY_OUTPUT_ROOT: temporary,
    ZLIBRARY_EMAIL: 'i251@example.test',
    ZLIBRARY_PASSWORD: 'i251-password',
    ZLIBRARY_EAPI_DOMAIN: 'fake.example.test',
    ZLIBRARY_SESSION_MODE: 'per-call',
    ZLIBRARY_DEBUG: '1',
    LOG_LEVEL: 'debug',
    RETRY_MAX_RETRIES: '0',
    CIRCUIT_BREAKER_THRESHOLD: '2',
  });
  jest.resetModules();
  jest.unstable_unmockModule('../lib/python-runner.js');
  const runner = await import('../lib/python-runner.js');
  captured = [];
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => pythonExecutable,
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    LONG_BRIDGE_TIMEOUT_MS: runner.LONG_BRIDGE_TIMEOUT_MS,
    runPythonBridge: async (_script, options, runOptions) => {
      try {
        const result = await runner.runPythonBridge(
          'offline.py',
          { ...options, scriptPath: temporary },
          runOptions,
        );
        captured.push(JSON.stringify(result));
        return result;
      } catch (error) {
        captured.push(String(error.stderr), JSON.stringify(error), String(error.stack));
        throw error;
      }
    },
  }));
  logs = jest.spyOn(console, 'error').mockImplementation(() => {});
  api = await import('../lib/zlibrary-api.js');
  ({ errorEnvelope: envelope } = await import('../lib/tool-errors.js'));
});
afterEach(() => {
  process.env = { ...saved };
  fs.rmSync(temporary, { recursive: true, force: true });
  for (const [name, previous] of listeners)
    for (const listener of process.rawListeners(name))
      if (!previous.has(listener)) process.removeListener(name, listener);
  jest.restoreAllMocks();
});

// Catches raw exception/argument logging or diagnostics lost at either language boundary.
test.each([
  ['400', 'ambiguous_status', 400, 'provider_error'],
  ['auth', 'auth_rejected', 401, 'auth_failed'],
  ['wall', 'wall', 403, 'provider_error'],
  ['malformed', 'ambiguous_status', 200, 'provider_error'],
  ['oversized', 'ambiguous_status', 500, 'provider_error'],
  ['transport', 'transport', undefined, 'provider_timeout'],
  ['login-400', 'ambiguous_status', 400, 'provider_error'],
  ['login-auth', 'auth_rejected', 401, 'auth_failed'],
  ['login-tls', 'transport', undefined, 'provider_unreachable'],
  ['login-tls-context', 'transport', undefined, 'provider_unreachable'],
])(
  'offline %s keeps sentinels out of bridge, Node, logs and tool error',
  async (mode, kind, status, code) => {
    if (mode.startsWith('login-')) process.env.ZLIBRARY_SESSION_MODE = 'memory';
    fs.writeFileSync(modeFile, mode);
    const failure = await api.searchBooks({ query: poison }).catch((e) => e);
    expect(failure).toBeInstanceOf(Error);
    const result = envelope(failure, 'search', 'zlibrary');
    const files = offsets
      .map(([file, offset]) =>
        fs.existsSync(file) ? fs.readFileSync(file).subarray(offset).toString() : '',
      )
      .join('');
    const all =
      captured.join('') +
      JSON.stringify(failure) +
      String(failure.stack) +
      JSON.stringify(logs.mock.calls) +
      files +
      JSON.stringify(result);
    for (const sentinel of sentinels) expect(all).not.toContain(sentinel);
    expect(result.error.code).toBe(code);
    if (mode.startsWith('login-tls')) expect(result.error.detail.reason).toBe('tls_error');
    expect(result.error.detail.diagnostics).toEqual({
      failure_kind: kind,
      ...(status === undefined ? {} : { http_status: status }),
      attempt: 1,
      refresh_count: 0,
      request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
    expect(JSON.stringify(logs.mock.calls)).toContain(result.error.detail.diagnostics.request_id);
    for (const call of logs.mock.calls.filter((call) => call[1] === 'EAPI failure'))
      expect(format(...call).split('\n')).toHaveLength(1);
    expect(Buffer.byteLength(JSON.stringify(result.error.detail.diagnostics))).toBeLessThanOrEqual(
      512,
    );
    for (const value of captured) expect(Buffer.byteLength(value)).toBeLessThanOrEqual(8192);
  },
);

// Catches blanket 400 invalidation, breaker poisoning and automatic transfer replay.
test('generic 400 preserves a warm session and breaker without login/download replay', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.RETRY_MAX_RETRIES = '3';
  await api.searchBooks({ query: 'seed' });
  fs.writeFileSync(modeFile, '400');
  for (let i = 0; i < 4; i++) {
    const failure = await api
      .downloadBookToFile({ bookDetails: { id: '17', book_hash: 'hash', title: 'i251-book' } })
      .catch((e) => e);
    expect(envelope(failure, 'download', 'zlibrary').error).toMatchObject({
      code: 'provider_error',
      detail: {
        diagnostics: { failure_kind: 'ambiguous_status', http_status: 400, refresh_count: 0 },
      },
    });
  }
  fs.writeFileSync(modeFile, 'success');
  await expect(api.searchBooks({ query: 'after' })).resolves.toMatchObject({ books: [] });
  const calls = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
  expect(calls.filter((x) => x === '/eapi/user/login')).toHaveLength(1);
  expect(calls.filter((x) => x === '/eapi/book/17/hash/file')).toHaveLength(4);
  expect(calls.filter((x) => x === '/eapi/user/profile')).toHaveLength(1);
});

test.each([
  {
    failure_kind: 'auth_rejected',
    attempt: 1,
    refresh_count: 0,
    request_id: 'arbitrary-provider-token',
  },
  {
    failure_kind: 'wall',
    attempt: 1,
    refresh_count: 0,
    request_id: '00000000-0000-4000-8000-000000000001',
    body: poison,
  },
  {
    failure_kind: 'wall',
    attempt: 65536,
    refresh_count: 0,
    request_id: '00000000-0000-4000-8000-000000000001',
  },
])('malformed diagnostic records are rejected, not partially copied: %j', (diagnostics) => {
  const result = envelope(
    { context: { pythonErrorType: 'HTTPStatusError', diagnostics } },
    'search',
    'zlibrary',
  );
  expect(result.error.detail).not.toHaveProperty('diagnostics');
});

test('one owner refresh keeps correlation and reports its actual refresh count', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  await api.searchBooks({ query: 'seed' });
  fs.writeFileSync(modeFile, 'refresh-then-400');
  const failure = await api.searchBooks({ query: 'offline' }).catch((e) => e);
  const result = envelope(failure, 'search', 'zlibrary');
  expect(result.error.detail.diagnostics).toMatchObject({
    failure_kind: 'ambiguous_status',
    http_status: 400,
    attempt: 1,
    refresh_count: 1,
  });
  const calls = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
  expect(calls.filter((x) => x === '/eapi/user/login')).toHaveLength(2);
  // A checkout frame can contain a UUID unrelated to the correlation field.
  captured.push('at /tmp/checkout-12345678-1234-4234-8234-123456789abc/dist/index.js:1:1');
  const ids = [
    ...captured.join('').matchAll(/\\?"request_id\\?"\s*:\s*\\?"([0-9a-f-]{36})\\?"/g),
  ].map((match) => match[1]);
  expect(ids.length).toBeGreaterThan(0);
  expect(new Set(ids)).toEqual(new Set([result.error.detail.diagnostics.request_id]));
});

test('real SDK tool error keeps diagnostics and the tools/list contract unchanged', async () => {
  const { Client } = await import('@modelcontextprotocol/sdk/client/index.js');
  const { InMemoryTransport } = await import('@modelcontextprotocol/sdk/inMemory.js');
  const { createServer } = await import('../dist/index.js');
  const server = createServer();
  const client = new Client({ name: 'i251-offline', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([client.connect(a), server.connect(b)]);
    const listed = await client.listTools();
    expect(listed.tools).toHaveLength(5);
    expect(JSON.stringify(listed).length).toBe(6493);
    fs.writeFileSync(modeFile, '400');
    const result = await client.callTool({
      name: 'search',
      arguments: { query: poison, sources: ['zlibrary'] },
    });
    expect(result.isError).toBe(true);
    expect(result.structuredContent.error.detail.diagnostics).toMatchObject({
      failure_kind: 'ambiguous_status',
      http_status: 400,
    });
    expect(JSON.parse(result.content[0].text)).toEqual(result.structuredContent);
    for (const sentinel of sentinels) expect(JSON.stringify(result)).not.toContain(sentinel);
  } finally {
    await client.close();
    await server.close();
  }
});

test('aggregate tool errors retain the EAPI diagnostic rather than discarding it', async () => {
  const { AcquisitionTools } = await import('../lib/acquisition-tools.js');
  const { ToolFailure } = await import('../lib/tool-errors.js');
  const tools = new AcquisitionTools(async (operation, args, options) => {
    if (operation === 'search_multi_source')
      throw new ToolFailure('provider_error', 'Offline source failure');
    return api.invokeAcquisitionBridge(operation, args, options);
  });
  fs.writeFileSync(modeFile, '400');
  const result = await tools.call('search', { query: poison, sources: ['libgen', 'zlibrary'] });
  expect(result.error.code).toBe('all_sources_failed');
  expect(result.error.detail.diagnostics).toMatchObject({
    failure_kind: 'ambiguous_status',
    http_status: 400,
  });
});

test('generic 400 neither heals nor increments a partially failed breaker', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  await api.searchBooks({ query: 'seed' });
  fs.writeFileSync(modeFile, 'transport');
  await expect(api.searchBooks({ query: 'first-outage' })).rejects.toBeInstanceOf(Error);
  fs.writeFileSync(modeFile, '400');
  await expect(api.searchBooks({ query: 'ambiguous' })).rejects.toBeInstanceOf(Error);
  fs.writeFileSync(modeFile, 'transport');
  await expect(api.searchBooks({ query: 'second-outage' })).rejects.toBeInstanceOf(Error);
  fs.writeFileSync(modeFile, 'success');
  await expect(api.searchBooks({ query: 'must-be-open' })).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  const calls = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
  expect(calls.filter((x) => x === '/eapi/book/search')).toHaveLength(4);
  expect(calls.filter((x) => x === '/eapi/user/login')).toHaveLength(1);
});

test('lazy transfer logs and tool error agree after a pre-grant owner refresh', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.ZLIBRARY_SESSION_IDLE_MS = '1';
  await api.searchBooks({ query: 'seed' });
  await new Promise((resolve) => setTimeout(resolve, 5));
  fs.writeFileSync(modeFile, 'validate-refresh-400');
  const failure = await api
    .invokeAcquisitionBridge('acquire_book', {
      book_details: {
        source: 'zlibrary',
        id: '17',
        book_hash: 'hash',
        title: 'offline',
        extension: 'epub',
      },
      output_dir: temporary,
      filename: 'offline.epub',
    })
    .catch((e) => e);
  expect(envelope(failure, 'download', 'zlibrary').error.detail.diagnostics).toMatchObject({
    failure_kind: 'ambiguous_status',
    http_status: 400,
    refresh_count: 1,
  });
  const final = JSON.parse(captured.filter((x) => x.startsWith('{"error":')).at(-1));
  expect(final.diagnostics).toMatchObject({
    failure_kind: 'ambiguous_status',
    http_status: 400,
    refresh_count: 1,
  });
});

// Per-call acquisition authenticates only inside the real transfer callback.
// A rejected login has not assigned either Python session/client global yet.
test.each([
  ['login-auth', 'auth_rejected', 'auth_failed'],
  ['login-tls', 'transport', 'provider_unreachable'],
  ['discovery-transport', 'transport', 'provider_unreachable'],
])('per-call acquisition retains an early %s diagnosis', async (mode, kind, code) => {
  fs.writeFileSync(modeFile, mode);
  const failure = await api
    .invokeAcquisitionBridge('acquire_book', {
      book_details: {
        source: 'zlibrary',
        id: '17',
        book_hash: 'hash',
        title: 'offline',
        extension: 'epub',
      },
      output_dir: temporary,
      filename: 'offline.epub',
    })
    .catch((e) => e);
  const result = envelope(failure, 'download', 'zlibrary');
  expect(result.error.code).toBe(code);
  expect(result.error.detail.diagnostics.failure_kind).toBe(kind);
  for (const sentinel of sentinels)
    expect(
      captured.join('') + JSON.stringify(result) + JSON.stringify(logs.mock.calls),
    ).not.toContain(sentinel);
  const calls = fs.existsSync(callsFile)
    ? fs.readFileSync(callsFile, 'utf8').trim().split('\n')
    : [];
  expect(calls).toEqual(mode === 'discovery-transport' ? [] : ['/eapi/user/login']);
});

test.each([
  ['read-error', 'ReadError'],
  ['write-error', 'WriteError'],
])(
  '%s survives both bridge projections without Node replay and counts on the breaker',
  async (mode, type) => {
    process.env.RETRY_MAX_RETRIES = '3';
    process.env.CIRCUIT_BREAKER_THRESHOLD = '2';
    process.env.RETRY_INITIAL_DELAY = '1';
    fs.writeFileSync(modeFile, mode);
    const { isRetryableError, isTransientDependencyError } =
      await import('../lib/retry-manager.js');
    for (let verdict = 1; verdict <= 2; verdict++) {
      const failure = await api.searchBooks({ query: 'offline' }).catch((e) => e);
      expect(failure.context.pythonErrorType).toBe(type);
      expect(failure.context.diagnostics).toMatchObject({ failure_kind: 'transport', attempt: 1 });
      expect(failure.context.details).toEqual({});
      expect(isRetryableError(failure)).toBe(false);
      // Breaker health alone strips empty/issuance-only details for typed evidence.
      expect(isTransientDependencyError({ context: { pythonErrorType: type } })).toBe(true);
      const issued = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
      expect(issued.filter((route) => route === '/eapi/book/search')).toHaveLength(verdict);
    }
    const calls = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
    expect(calls.filter((route) => route === '/eapi/book/search')).toHaveLength(2);
    fs.writeFileSync(modeFile, 'success');
    await expect(api.searchBooks({ query: 'breaker-is-open' })).rejects.toMatchObject({
      code: 'CIRCUIT_BREAKER_OPEN',
    });
    expect(fs.readFileSync(callsFile, 'utf8').trim().split('\n')).toEqual(calls);
  },
);

// Catches session presence overriding Python's explicit pre-request verdict.
test('warm memory session preserves explicit pre-issuance budget refusal', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.RETRY_MAX_RETRIES = '3';
  await api.searchBooks({ query: 'warm-session' });
  const before = fs.readFileSync(callsFile, 'utf8');
  fs.writeFileSync(modeFile, 'local-budget');
  for (let i = 0; i < 3; i++) {
    const failure = await api
      .downloadBookToFile({
        bookDetails: { id: '17', book_hash: 'hash', title: 'offline' },
      })
      .catch((error) => error);
    expect(JSON.parse(failure.context.stderr).details.request_issued).toBe(false);
    expect(envelope(failure, 'download', 'zlibrary').error.detail).not.toHaveProperty(
      'diagnostics',
    );
    expect(failure.context.details).toMatchObject({
      reason: 'walk_budget_exhausted',
      request_issued: false,
    });
    expect(envelope(failure, 'download', 'zlibrary').error).toMatchObject({
      code: 'provider_timeout',
      retry: 'safe',
      detail: { reason: 'walk_budget_exhausted', request_issued: false },
    });
  }
  expect(fs.readFileSync(callsFile, 'utf8')).toBe(before);
  fs.writeFileSync(modeFile, 'success');
  await expect(api.searchBooks({ query: 'still-warm-and-not-open' })).resolves.toMatchObject({
    books: [],
  });
  const calls = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
  expect(calls.filter((route) => route === '/eapi/user/login')).toHaveLength(1);
});

// Bare Python envelopes remain Python-owned even when a typed outage counts
// for dependency health. Losing active issuance metadata must never permit replay.
test('active transport bare ReadError keeps issuance, does not replay, and counts on the breaker', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.RETRY_MAX_RETRIES = '3';
  process.env.RETRY_INITIAL_DELAY = '1';
  await api.searchBooks({ query: 'warm-session' });
  fs.writeFileSync(modeFile, 'read-error');
  const routes = () => fs.readFileSync(callsFile, 'utf8').trim().split('\n');
  let previous = routes();
  const invoke = () =>
    api
      .downloadBookToFile({
        bookDetails: { id: '17', book_hash: 'hash', title: 'offline' },
      })
      .catch((error) => error);
  const { isRetryableError } = await import('../lib/retry-manager.js');
  for (let verdict = 1; verdict <= 2; verdict++) {
    const failure = await invoke();
    const current = routes();
    expect(current.length - previous.length).toBe(1);
    expect(failure.context.pythonErrorType).toBe('ReadError');
    expect(JSON.parse(failure.context.stderr).details).toBeUndefined();
    expect(failure.context.details).toEqual({ request_issued: true });
    expect(isRetryableError(failure)).toBe(false);
    expect(envelope(failure, 'download', 'zlibrary').error.detail).toMatchObject({
      request_issued: true,
      diagnostics: { failure_kind: 'transport', attempt: 1 },
    });
    previous = current;
  }
  // Two independent logical failures (not retries) open the threshold-two breaker.
  await expect(api.searchBooks({ query: 'breaker-is-open' })).rejects.toMatchObject({
    code: 'CIRCUIT_BREAKER_OPEN',
  });
  expect(routes()).toEqual(previous);
  expect(previous.filter((route) => route === '/eapi/user/login')).toHaveLength(1);
});

// A no-PID spawn failure used to retain Node's argument-bearing originalError
// and a configured interpreter path in both its message and context.
test.each([
  ['missing', 'ENOENT'],
  ['not-executable', 'EACCES'],
])(
  'failed %s interpreter spawn never exposes arguments or configured paths',
  async (mode, errnoCode) => {
    const pathSentinel = 'i251-spawn-path';
    pythonExecutable = path.join(temporary, pathSentinel);
    if (mode === 'not-executable')
      fs.writeFileSync(pythonExecutable, 'not executable', { mode: 0o600 });
    const failure = await api.searchBooks({ query: poison }).catch((error) => error);
    const result = envelope(failure, 'search', 'zlibrary');
    const files = offsets
      .map(([file, offset]) =>
        fs.existsSync(file) ? fs.readFileSync(file).subarray(offset).toString() : '',
      )
      .join('');
    const all =
      captured.join('') +
      JSON.stringify(failure) +
      String(failure.stack) +
      JSON.stringify(logs.mock.calls) +
      files +
      JSON.stringify(result);
    for (const sentinel of [...sentinels, pathSentinel]) expect(all).not.toContain(sentinel);
    expect(failure).toMatchObject({
      code: 'BRIDGE_SPAWN',
      errnoCode,
      context: { label: 'python_bridge.search', errno: expect.any(Number), errnoCode },
    });
    expect(failure.context.originalError).toBeUndefined();
    expect(failure.context.path).toBeUndefined();
    expect(JSON.stringify(failure)).not.toContain('spawnargs');
    expect(fs.existsSync(callsFile)).toBe(false);
    expect(result.error.detail).not.toHaveProperty('diagnostics');
  },
);

const transportCases = [
  ['WriteTimeout', 'write-timeout', 'provider_timeout', 'read_timeout'],
  ['PoolTimeout', 'pool-timeout', 'provider_unreachable', 'connect_timeout'],
  ['RemoteProtocolError', 'remote-protocol', 'provider_error', 'protocol_error'],
  ['ReadError', 'read-error', 'provider_unreachable', 'connect_error'],
  ['WriteError', 'write-error', 'provider_unreachable', 'connect_error'],
].flatMap(([type, mode, code, reason]) =>
  [
    ['search', 'safe'],
    ['download', 'may_spend_quota'],
  ].map(([operation, retry]) => ({ type, mode, code, reason, operation, retry })),
);
// Typed transport diagnostics must drive actionable public errors on ordinary
// operations, without changing Python-owned replay or issuance evidence.
test.each(transportCases)(
  'ordinary $type $operation has a transport code and retry hint without replay',
  async ({ type, mode, code, reason, operation, retry }) => {
    process.env.ZLIBRARY_SESSION_MODE = 'memory';
    process.env.RETRY_MAX_RETRIES = '3';
    await api.searchBooks({ query: 'warm-session' });
    const before = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
    fs.writeFileSync(modeFile, mode);
    const failure = await (
      operation === 'search'
        ? api.searchBooks({ query: poison })
        : api.downloadBookToFile({ bookDetails: { id: '17', book_hash: 'hash', title: 'offline' } })
    ).catch((error) => error);
    expect(failure.context.pythonErrorType).toBe(type);
    const result = envelope(failure, operation, 'zlibrary');
    expect(result.error).toMatchObject({
      code,
      retry,
      detail: {
        reason,
        request_issued: true,
        diagnostics: { failure_kind: 'transport', attempt: 1 },
      },
    });
    const { isRetryableError } = await import('../lib/retry-manager.js');
    expect(isRetryableError(failure)).toBe(false);
    const after = fs.readFileSync(callsFile, 'utf8').trim().split('\n');
    expect(after.length - before.length).toBe(1);
    expect(after.filter((route) => route === '/eapi/user/login')).toHaveLength(1);
  },
);

// Real Python validation occurs before the offline provider transport is reached.
test('pre-dispatch argument ValueError retains invalid_argument without provider contact', async () => {
  const failure = await api.searchBooks({ query: 'offline', count: 0 }).catch((error) => error);
  expect(envelope(failure, 'search', 'zlibrary').error.code).toBe('invalid_argument');
  expect(fs.existsSync(callsFile)).toBe(false);
});

test('pre-dispatch local argument refusal has no EAPI request diagnostic', async () => {
  const failure = await api.searchBooks({ query: 'offline', count: 0 }).catch((error) => error);
  expect(failure).toBeInstanceOf(Error);
  expect(envelope(failure, 'search', 'zlibrary').error.detail).not.toHaveProperty('diagnostics');
  expect(fs.existsSync(callsFile)).toBe(false);
});

test('missing credentials are a local refusal with no provider diagnostic', async () => {
  delete process.env.ZLIBRARY_EMAIL;
  delete process.env.ZLIBRARY_PASSWORD;
  const failure = await api.searchBooks({ query: 'offline' }).catch((error) => error);
  expect(failure).toBeInstanceOf(Error);
  expect(envelope(failure, 'search', 'zlibrary').error.detail).not.toHaveProperty('diagnostics');
  expect(fs.existsSync(callsFile)).toBe(false);
});
