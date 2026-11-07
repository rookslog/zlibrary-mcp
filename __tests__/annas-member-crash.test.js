import { test, expect } from '@jest/globals';
import { spawn } from 'node:child_process';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';

test.each(['slow-signin', 'reject-slow'])('pending worker failure keeps sign-in cooldown during %s', async mode => {
  let child, closed, signal;
  let trace = '';
  let launches = 0;
  const reached = new Promise(resolve => { signal = resolve; });
  const marker = mode === 'slow-signin' ? 'SIGNIN_POST' : 'GET_STARTED';
  const worker = new AnnasMemberWorker(async () => {
    launches += 1;
    const fixtureMode = launches === 1 ? mode : 'reject-slow';
    child = spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', fixtureMode], { stdio: ['pipe', 'pipe', 'pipe'] });
    closed = new Promise(resolve => child.once('close', resolve));
    child.stderr.on('data', chunk => { trace += chunk.toString(); if (trace.includes(marker)) signal(); });
    return child;
  });
  let timer;
  try {
    const first = worker.search({ query: 'wait' }, { timeoutMs: 4000 }).catch(error => error);
    await Promise.race([reached, new Promise((_, reject) => { timer = setTimeout(() => reject(Error('No fixture stage signal')), 3000); })]);
    child.kill('SIGKILL');
    expect((await first).code).toBe('PYTHON_ERROR');
    await closed;
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 0');
    expect(launches).toBe(2);
    expect((trace.match(/SIGNIN_POST/g) ?? []).length).toBe(1);
  } finally { clearTimeout(timer); await worker.close(); }
});

test('oversized reply cannot reset the sign-in cooldown', async () => {
  let closed;
  let trace = '';
  let launches = 0;
  const worker = new AnnasMemberWorker(async () => {
    launches += 1;
    const child = spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py', 'reject-large'], { stdio: ['pipe', 'pipe', 'pipe'] });
    closed = new Promise(resolve => child.once('close', resolve));
    child.stderr.on('data', chunk => { trace += chunk.toString(); });
    return child;
  });
  try {
    await expect(worker.search({ query: 'oversize', count: 50 })).rejects.toMatchObject({ code: 'PYTHON_ERROR' });
    await closed;
    expect((await worker.search({ query: 'books' })).books[0].title).toBe('Sign-ins 0');
    expect(launches).toBe(2);
    expect((trace.match(/SIGNIN_POST/g) ?? []).length).toBe(1);
  } finally { await worker.close(); }
}, 10000);
