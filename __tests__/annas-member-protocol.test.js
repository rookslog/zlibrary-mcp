import { test, expect } from '@jest/globals';
import { spawn } from 'node:child_process';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';

// The peer only reads/writes protocol frames; it has no provider client.
const fixture = '__tests__/fixtures/annas-member-protocol.py';

test.each(['pending', 'cancel', 'malformed', 'malformed-late'])(
  'worker failure after %s preserves about sixty seconds on the next request',
  async mode => {
    let child, closed, received;
    let launches = 0;
    const requestReceived = new Promise(resolve => { received = resolve; });
    const worker = new AnnasMemberWorker(async () => {
      launches += 1;
      const peerMode = launches > 1 ? 'inspect' : mode === 'cancel' ? 'pending' : mode;
      child = spawn('.venv/bin/python', [fixture, peerMode], { stdio: ['pipe', 'pipe', 'pipe'] });
      closed = new Promise(resolve => child.once('close', resolve));
      child.stderr.on('data', chunk => { if (chunk.toString().includes('REQUEST_RECEIVED')) received(); });
      return child;
    });
    let timer;
    try {
      const controller = new AbortController();
      const beforeFailure = Date.now();
      const first = worker.search({ query: 'books' }, { signal: controller.signal }).catch(error => error);
      if (!mode.startsWith('malformed')) {
        await Promise.race([requestReceived, new Promise((_, reject) => {
          timer = setTimeout(() => reject(Error('No request receipt')), 3000);
        })]);
        if (mode === 'cancel') controller.abort();
        else child.kill('SIGKILL');
      }
      expect((await first).code).toBe(mode === 'cancel' ? 'TIMEOUT' : 'PYTHON_ERROR');
      await closed;
      const next = await worker.search({ query: 'books' });
      expect(launches).toBe(2);
      // Assert the deadline actually sent through the next child's stdin.
      expect(next.received_retry_at).toBeGreaterThanOrEqual(beforeFailure + 55_000);
      expect(next.received_retry_at).toBeLessThanOrEqual(Date.now() + 61_000);
      // The completed reply clears the deadline; no abandoned cancellation
      // may turn a later idle exit into another failed active call.
      child.kill('SIGKILL');
      await closed;
      expect((await worker.search({ query: 'books' })).received_retry_at).toBe(0);
      expect(launches).toBe(3);
    } finally { clearTimeout(timer); await worker.close(); }
  },
);

test('acknowledged cancellation does not add cooldown on a later idle exit', async () => {
  let child, closed, received;
  let launches = 0;
  const requestReceived = new Promise(resolve => { received = resolve; });
  const worker = new AnnasMemberWorker(async () => {
    launches += 1;
    child = spawn('.venv/bin/python', [fixture, launches === 1 ? 'ack' : 'inspect'], { stdio: ['pipe', 'pipe', 'pipe'] });
    closed = new Promise(resolve => child.once('close', resolve));
    child.stderr.on('data', chunk => { if (chunk.toString().includes('REQUEST_RECEIVED')) received(); });
    return child;
  });
  let timer, ackTimer;
  try {
    const controller = new AbortController();
    const first = worker.search({ query: 'books' }, { signal: controller.signal }).catch(error => error);
    await Promise.race([requestReceived, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error('No request receipt')), 3000);
    })]);
    const ack = new Promise(resolve => {
      let buffer = '';
      const onData = chunk => {
        buffer += chunk;
        if (buffer.includes('\n')) {
          child.stdout.removeListener('data', onData);
          resolve();
        }
      };
      child.stdout.on('data', onData);
    });
    controller.abort();
    expect((await first).code).toBe('TIMEOUT');
    await Promise.race([ack, new Promise((_, reject) => {
      ackTimer = setTimeout(() => reject(Error('No cancellation acknowledgement')), 3000);
    })]);
    child.kill('SIGKILL');
    await closed;
    expect((await worker.search({ query: 'books' })).received_retry_at).toBe(0);
    expect(launches).toBe(2);
  } finally { clearTimeout(timer); clearTimeout(ackTimer); await worker.close(); }
});
