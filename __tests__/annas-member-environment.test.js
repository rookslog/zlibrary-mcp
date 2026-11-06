import { test, expect, jest } from '@jest/globals';
import * as native from 'node:child_process';

let environment;
jest.unstable_mockModule('node:child_process', () => ({
  ...native,
  spawn(python, args, options) {
    environment = options.env;
    return native.spawn(python, ['__tests__/fixtures/annas-member-worker.py'], options);
  },
}));
const { AnnasMemberWorker } = await import('../dist/lib/annas-member-worker.js');

test('member child retains its key but excludes unrelated Z-Library credentials', async () => {
  const prior = { ...process.env };
  Object.assign(process.env, { ZLIBRARY_EMAIL: 'fixture-zlib-email', ZLIBRARY_PASSWORD: 'fixture-zlib-password', ANNAS_SECRET_KEY: 'fixture-member-key' });
  const worker = new AnnasMemberWorker();
  try {
    await worker.search({ query: 'books' });
    expect(environment).not.toHaveProperty('ZLIBRARY_EMAIL');
    expect(environment).not.toHaveProperty('ZLIBRARY_PASSWORD');
    expect(environment.ANNAS_SECRET_KEY).toBe('fixture-member-key');
  } finally { await worker.close(); process.env = prior; }
});
