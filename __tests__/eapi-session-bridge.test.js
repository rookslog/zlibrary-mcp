import { afterEach, expect, jest, test } from '@jest/globals';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

jest.setTimeout(30000);
let temporary;
const originalEnv = { ...process.env };
const originalListeners = new Map(
  ['exit', 'SIGINT', 'SIGTERM'].map((name) => [name, new Set(process.rawListeners(name))]),
);
afterEach(() => {
  process.env = { ...originalEnv };
  if (temporary) fs.rmSync(temporary, { recursive: true, force: true });
  for (const [name, previous] of originalListeners) {
    for (const listener of process.rawListeners(name))
      if (!previous.has(listener)) process.removeListener(name, listener);
  }
  jest.restoreAllMocks();
});

test('real Node runner and Python main reuse private cookies with fake HTTP only', async () => {
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'eapi-private-'));
  const root = process.cwd(),
    countFile = path.join(temporary, 'login-count'),
    modeFile = path.join(temporary, 'mode');
  fs.writeFileSync(modeFile, 'success');
  const wrapper = `import sys, os, json, runpy
sys.path[:0]=[${JSON.stringify(path.join(root, 'lib'))}, ${JSON.stringify(path.join(root, 'zlibrary/src'))}, ${JSON.stringify(root)}]
import httpx
from zlibrary import eapi
async def resolve(): return 'fake.example.test'
eapi.resolve_eapi_domain=resolve
async def get_client(self):
    if self._client is None or self._client.is_closed:
        self._client=httpx.AsyncClient(transport=httpx.MockTransport(handle),cookies=self._cookies)
    return self._client
eapi.EAPIClient._get_client=get_client
def handle(request):
    assert not os.get_inheritable(3)
    assert 'ZLIBRARY_EAPI_SESSION' not in os.environ
    route=request.url.path
    with open(${JSON.stringify(modeFile)}) as record: mode=record.read()
    if route=='/eapi/user/login':
        with open(${JSON.stringify(countFile)},'a') as record: record.write('login\\n')
        if mode=='login-failure': return httpx.Response(401,json={'success':0,'error':'Invalid credentials #1'})
        return httpx.Response(200,json={'success':1,'user':{'id':'7','remix_userkey':'fake-end-to-end-bearer'}})
    if route=='/eapi/user/profile': return httpx.Response(200,json={'success':1,'user':{'id':'7'}})
    with open(${JSON.stringify(modeFile)}) as record: mode=record.read()
    if mode=='rejected': return httpx.Response(401,json={'error':'expired'})
    if mode=='failure': raise RuntimeError('fake-end-to-end-bearer fake-password-marker')
    assert 'fake-end-to-end-bearer' in request.headers.get('cookie','')
    return httpx.Response(200,json={'success':1,'books':[]})
runpy.run_path(${JSON.stringify(path.join(root, 'lib/python_bridge.py'))},run_name='__main__')
`;
  fs.writeFileSync(path.join(temporary, 'offline.py'), wrapper);
  process.env.ZLIBRARY_EMAIL = 'offline-reader@example.test';
  process.env.ZLIBRARY_PASSWORD = 'fake-password-marker';
  process.env.ZLIBRARY_EAPI_DOMAIN = 'fake.example.test';
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  process.env.ZLIBRARY_DEBUG = '1';
  process.env.RETRY_MAX_RETRIES = '0';
  delete process.env.ZLIBRARY_EAPI_SESSION;
  jest.resetModules();
  const runner = await import('../lib/python-runner.js');
  const outputs = [];
  jest.unstable_mockModule('../lib/venv-manager.js', () => ({
    getManagedPythonPath: async () => path.join(root, '.venv/bin/python'),
  }));
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    LONG_BRIDGE_TIMEOUT_MS: runner.LONG_BRIDGE_TIMEOUT_MS,
    runPythonBridge: async (_script, options, runOptions) => {
      const result = await runner.runPythonBridge(
        'offline.py',
        { ...options, scriptPath: temporary },
        runOptions,
      );
      outputs.push(result);
      return result;
    },
  }));
  const logs = jest.spyOn(console, 'error').mockImplementation(() => {});
  const api = await import('../lib/zlibrary-api.js');
  await expect(
    Promise.all(Array.from({ length: 6 }, () => api.searchBooks({ query: 'offline' }))),
  ).resolves.toEqual(
    Array.from({ length: 6 }, () => ({
      books: [],
      retrieved_from_url: 'EAPI search: offline',
      via: [{ source: 'zlibrary', route: 'api', ms: expect.any(Number) }],
    })),
  );
  await expect(api.searchBooks({ query: 'offline' })).resolves.toEqual({
    books: [],
    retrieved_from_url: 'EAPI search: offline',
    via: [{ source: 'zlibrary', route: 'api', ms: expect.any(Number) }],
  });
  expect(fs.readFileSync(countFile, 'utf8')).toBe('login\n');
  fs.writeFileSync(modeFile, 'failure');
  const failure = await api.searchBooks({ query: 'offline' }).catch((error) => error);
  expect(failure).toBeInstanceOf(Error);
  fs.writeFileSync(modeFile, 'rejected');
  await expect(api.searchBooks({ query: 'offline' })).rejects.toThrow(/authentication/i);
  expect(fs.readFileSync(countFile, 'utf8')).toBe('login\nlogin\n');
  fs.writeFileSync(modeFile, 'login-failure');
  jest.resetModules();
  const failedApi = await import('../lib/zlibrary-api.js');
  await expect(failedApi.searchBooks({ query: 'offline' })).rejects.toThrow(
    /rejected the login.*HTTP 401/,
  );
  await expect(failedApi.searchBooks({ query: 'offline' })).rejects.toThrow(
    /HTTP 401.*Authentication rejected.*next attempt allowed in/,
  );
  expect(fs.readFileSync(countFile, 'utf8')).toBe('login\nlogin\nlogin\n');
  const publicValues =
    JSON.stringify(outputs) +
    JSON.stringify(failure) +
    String(failure.stack) +
    JSON.stringify(logs.mock.calls);
  expect(publicValues).not.toContain('fake-end-to-end-bearer');
  expect(publicValues).not.toContain('fake-password-marker');
});

test('server startup rejects an unknown session mode before accepting requests', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'disk';
  const { createServer } = await import('../dist/index.js');
  expect(() => createServer()).toThrow('ZLIBRARY_SESSION_MODE must be memory or per-call');
});
