import { test, expect } from '@jest/globals';
import { spawn } from 'node:child_process';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { TOOL_DEFINITIONS } from '../dist/index.js';

const owner = (mode = 'ok') => new AnnasMemberWorker(async () => spawn(
  '.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', mode],
  { stdio: ['pipe', 'pipe', 'pipe'] },
));

test('S1 resident preflight recovers after transient DNS failure and rechecks cached success', async () => {
  const worker = owner('preflight');
  try {
    await expect(worker.search({ query: 'books' })).rejects.toMatchObject({ context: { details: { reason: 'dns_failure' } } });
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 1 DNS 2');
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 1 DNS 3');
  } finally { await worker.close(); }
});

test('S1 resident HTTP DNS uses the bounded daemon resolver', async () => {
  const worker = owner('dns-hang');
  try {
    await expect(worker.search({ query: 'dns-hang' })).rejects.toMatchObject({ context: { details: { reason: 'dns_timeout' } } });
  } finally { await worker.close(); }
});

test('resident member searches share one sign-in across calls and local limits expose only coarse state', async () => {
  const worker = owner();
  const tools = new AcquisitionTools(() => { throw Error('Unexpected bridge'); }, { ANNAS_SECRET_KEY: 'worker-key-canary' }, () => worker.status());
  try {
    expect((await tools.call('get_limits', { sources: ['annas'] })).sources.annas).toMatchObject({ member_key_configured: true, member_session: 'none' });
    const replies = await Promise.all(Array.from({ length: 8 }, () => worker.search({ query: 'books', count: 50 })));
    replies.push(await worker.search({ query: 'books', count: 50 }));
    expect(replies.every(r => r.books[0].title === 'Sign-ins 1')).toBe(true);
    const limits = await tools.call('get_limits', {});
    expect(limits.sources.annas.member_session).toBe('active');
    expect(limits.sources.libgen).not.toHaveProperty('member_session');
    expect(limits.sources.zlibrary).not.toHaveProperty('member_key_configured');
    expect(JSON.stringify([replies, limits])).not.toMatch(/worker-key-canary|worker-cookie-canary/);
    const sources = TOOL_DEFINITIONS.find(t => t.name === 'get_limits').outputSchema.oneOf[0].properties.sources;
    expect(sources.properties.annas.properties.member_session.enum).toEqual(['none', 'active', 'rejected']);
  } finally { await worker.close(); }
  expect(worker.status()).toBe('none');
});

test('rejected member key backs off across public calls', async () => {
  const worker = owner('reject');
  const tools = new AcquisitionTools(() => {}, { ANNAS_SECRET_KEY: 'worker-key-canary' }, () => worker.status());
  try {
    await worker.search({ query: 'books' });
    const next = await worker.search({ query: 'books' });
    expect(next.books[0].title).toBe('Sign-ins 1');
    expect((await tools.call('get_limits', { sources: ['annas'] })).sources.annas.member_session).toBe('rejected');
  } finally { await worker.close(); }
});

test('member challenge error is structured and excludes key and cookie canaries', async () => {
  const worker = owner();
  try {
    await expect(worker.search({ query: 'check' })).rejects.toMatchObject({ context: { details: { reason: 'http_error' } } });
    expect(worker.status()).toBe('rejected');
  } finally { await worker.close(); }
});

test('aborting one resident search cancels it without losing another search or session', async () => {
  const worker = owner();
  try {
    await worker.search({ query: 'books' });
    const controller = new AbortController();
    const waiting = worker.search({ query: 'wait' }, { signal: controller.signal });
    const normal = worker.search({ query: 'books' });
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ code: 'TIMEOUT' });
    expect((await normal).books[0].title).toBe('Sign-ins 1');
    expect(worker.status()).toBe('active');
  } finally { await worker.close(); }
});

test('absent member key is reported without invoking a worker', async () => {
  const tools = new AcquisitionTools(() => { throw Error('Unexpected bridge'); }, {});
  expect((await tools.call('get_limits', { sources: ['annas'] })).sources.annas).toMatchObject({ member_key_configured: false, member_session: 'none' });
});

test('worker exit drops session state and the next search starts a new owner', async () => {
  let child;
  const worker = new AnnasMemberWorker(async () => {
    child = spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py'], { stdio: ['pipe', 'pipe', 'pipe'] });
    return child;
  });
  try {
    await worker.search({ query: 'books' });
    await new Promise(resolve => { child.once('close', resolve); child.kill('SIGKILL'); });
    expect(worker.status()).toBe('none');
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 1');
  } finally { await worker.close(); }
});

test('resident search timeout cancels the task and leaves the owner usable', async () => {
  const worker = owner();
  try {
    await worker.search({ query: 'books' });
    await expect(worker.search({ query: 'wait' }, { timeoutMs: 50 })).rejects.toMatchObject({ code: 'TIMEOUT' });
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 1');
  } finally { await worker.close(); }
});

test('malformed worker output is never relayed as an error or retained as session state', async () => {
  const worker = new AnnasMemberWorker(async () => spawn('.venv/bin/python', ['-c', 'print("worker-key-canary worker-cookie-canary", flush=True)'], { stdio: ['pipe', 'pipe', 'pipe'] }));
  try {
    const error = await worker.search({ query: 'books' }).catch(error => error);
    expect(error.code).toBe('PYTHON_ERROR');
    expect(JSON.stringify(error)).not.toMatch(/worker-key-canary|worker-cookie-canary/);
    expect(worker.status()).toBe('none');
  } finally { await worker.close(); }
});

test('S7 generic worker exception never crosses pipes with canaries', async () => {
  const worker = owner('generic');
  try {
    const error = await worker.search({ query: 'generic-canary' }).catch(e => e);
    expect(error.message).toBe('Anna member search failed');
    expect(JSON.stringify(error)).not.toMatch(/worker-key-canary|worker-cookie-canary/);
    expect(error.context.details.reason).toBe('protocol_error');
  } finally { await worker.close(); }
});

test('S8 stdin is UTF-8 even under a legacy Python stdio encoding', async () => {
  const worker = new AnnasMemberWorker(async () => spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', 'unicode'], { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'cp1252' } }));
  try {
    for (const query of ['Müller', 'Ángel']) {
      expect((await worker.search({ query })).books[0].title).toBe(`Sign-ins 1 Query ${query}`);
    }
  } finally { await worker.close(); }
});

test('S9 rejection cooldown survives a worker crash in the Node owner', async () => {
  let child;
  const worker = new AnnasMemberWorker(async () => {
    child = spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', 'reject'], { stdio: ['pipe', 'pipe', 'pipe'] });
    return child;
  });
  try {
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 1');
    await new Promise(resolve => { child.once('close', resolve); child.kill('SIGKILL'); });
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 0');
  } finally { await worker.close(); }
});

test('S10 cancelled refresh publishes none and retains cooldown', async () => {
  let started, status;
  const signinStarted = new Promise(resolve => { started = resolve; });
  const statusSeen = new Promise(resolve => { status = resolve; });
  const worker = new AnnasMemberWorker(async () => {
    const child = spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', 'refresh-slow'], { stdio: ['pipe', 'pipe', 'pipe'] });
    child.stderr.on('data', chunk => { if (chunk.toString().includes('SIGNIN_STARTED')) started(); });
    let buffer = '';
    child.stdout.on('data', chunk => {
      buffer += chunk.toString();
      for (let end; (end = buffer.indexOf('\n')) >= 0;) {
        const message = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1);
        if (message.status_only) status();
      }
    });
    return child;
  });
  let timer;
  try {
    await worker.search({ query: 'books' });
    expect(worker.status()).toBe('active');
    const controller = new AbortController();
    const pending = worker.search({ query: 'refresh' }, { signal: controller.signal }).catch(e => e);
    await signinStarted;
    controller.abort();
    expect((await pending).code).toBe('TIMEOUT');
    await Promise.race([statusSeen, new Promise((_, reject) => { timer = setTimeout(() => reject(Error('No cancellation status')), 1500); })]);
    expect(worker.status()).toBe('none');
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 2');
  } finally { clearTimeout(timer); await worker.close(); }
});

test('N1 overload returns immediately instead of dropping request 65', async () => {
  const worker = owner();
  const controllers = Array.from({ length: 64 }, () => new AbortController());
  let pending = [];
  try {
    await worker.search({ query: 'books' });
    pending = controllers.map(c => worker.search({ query: 'wait' }, { signal: c.signal }).catch(e => e));
    await expect(worker.search({ query: 'books' }, { timeoutMs: 2000 })).rejects.toMatchObject({ code: 'PYTHON_ERROR', message: 'Anna member worker busy' });
  } finally { controllers.forEach(c => c.abort()); await Promise.all(pending); await worker.close(); }
});
