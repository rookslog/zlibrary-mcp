import { test, expect, jest } from '@jest/globals';
import { spawn } from 'node:child_process';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';

test('public Anna searches and limits use the resident owner through retry/breaker admission', async () => {
  const worker = new AnnasMemberWorker(async () => spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py'], { stdio: ['pipe', 'pipe', 'pipe'] }));
  jest.resetModules();
  jest.unstable_mockModule('../dist/lib/annas-member-worker.js', () => ({ annasMemberWorker: worker }));
  const oldKey = process.env.ANNAS_SECRET_KEY;
  process.env.ANNAS_SECRET_KEY = 'worker-key-canary';
  try {
    const { AcquisitionTools } = await import('../dist/lib/acquisition-tools.js');
    const { invokeAcquisitionBridge } = await import('../dist/lib/zlibrary-api.js');
    const tools = new AcquisitionTools(invokeAcquisitionBridge);
    for (let i = 0; i < 2; i++) {
      const reply = await tools.call('search', { query: 'books', sources: ['annas'] });
      expect(reply.rows[0].title).toBe('Sign-ins 1');
    }
    expect((await tools.call('get_limits', { sources: ['annas'] })).sources.annas.member_session).toBe('active');
    const refused = await tools.call('search', { query: 'check', sources: ['annas'] });
    expect(refused.error).toBeDefined();
    expect(JSON.stringify(refused)).not.toMatch(/worker-key-canary|worker-cookie-canary/);
  } finally {
    await worker.close();
    if (oldKey === undefined) delete process.env.ANNAS_SECRET_KEY; else process.env.ANNAS_SECRET_KEY = oldKey;
    jest.unstable_unmockModule('../dist/lib/annas-member-worker.js');
  }
});
