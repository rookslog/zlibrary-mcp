import { test, expect, jest } from '@jest/globals';
import { spawn } from 'node:child_process';
import { AnnasMemberWorker } from '../dist/lib/annas-member-worker.js';
import { isTransientDependencyError } from '../dist/lib/retry-manager.js';

test.each([[429, true], [503, true], [404, false]])('member HTTP %i keeps structured status and counts only transient source failures', async (status, transient) => {
  const prior = { ...process.env };
  const worker = new AnnasMemberWorker(async () => spawn('.venv/bin/python', ['__tests__/fixtures/annas-member-worker.py'], { stdio: ['pipe', 'pipe', 'pipe'] }));
  process.env.ANNAS_SECRET_KEY = 'worker-key-canary';
  process.env.CIRCUIT_BREAKER_THRESHOLD = '1';
  jest.resetModules();
  jest.unstable_mockModule('../dist/lib/annas-member-worker.js', () => ({ annasMemberWorker: worker }));
  try {
    const { invokeAcquisitionBridge } = await import('../dist/lib/zlibrary-api.js');
    const args = { query: 'books', source: 'annas' };
    expect((await invokeAcquisitionBridge('search_multi_source', args)).books[0].title).toBe('Sign-ins 1');
    const error = await invokeAcquisitionBridge('search_multi_source', { ...args, query: `http-status-${status}` }).catch(e => e);
    const next = await invokeAcquisitionBridge('search_multi_source', args).then(
      result => ({ outcome: 'accepted', title: result.books[0].title }),
      failure => ({ outcome: failure.code }),
    );
    expect({
      reason: error.context.details.reason,
      detail: error.context.details.detail,
      transient: isTransientDependencyError(error),
      next,
    }).toEqual({
      reason: 'http_error',
      detail: `HTTP ${status}`,
      transient,
      next: transient ? { outcome: 'CIRCUIT_BREAKER_OPEN' } : { outcome: 'accepted', title: 'Sign-ins 1' },
    });
    expect(JSON.stringify(error)).not.toMatch(/worker-key-canary|worker-cookie-canary/);
  } finally {
    await worker.close();
    process.env = prior;
    jest.unstable_unmockModule('../dist/lib/annas-member-worker.js');
  }
});
