import { expect, jest, test } from '@jest/globals';

const environments = [];
jest.unstable_mockModule('../lib/venv-manager.js', () => ({ getManagedPythonPath: async () => '/fake/python' }));
jest.unstable_mockModule('../lib/python-runner.js', () => ({
  LONG_BRIDGE_TIMEOUT_MS: 2400000,
  runPythonBridge: async (_script, options) => {
    environments.push(options.env);
    return [JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({ path: '/fake/book.pdf', bytes: 1, reused: false, transfer_skipped: false, verified: 'sha256', record: '/fake/book.json' }) }] })];
  },
}));

test('separate downloads rotate credentials and do not pass other accounts to Python', async () => {
  const previous = { ...process.env };
  Object.assign(process.env, {
    ZLIBRARY_EMAIL: 'one@example.test', ZLIBRARY_PASSWORD: 'fake-one',
    ZLIBRARY_EMAIL_2: 'two@example.test', ZLIBRARY_PASSWORD_2: 'fake-two',
    ZLIBRARY_SESSION_MODE: 'per-call', RETRY_MAX_RETRIES: '0',
  });
  try {
    const { invokeAcquisitionBridge } = await import('../lib/zlibrary-api.js');
    const args = { book_details: { source: 'zlibrary', id: '1', hash: 'fake' } };
    const first = await invokeAcquisitionBridge('acquire_book', args);
    const second = await invokeAcquisitionBridge('acquire_book', args);
    expect([first.account_slot, second.account_slot]).toEqual([1, 2]);
    expect(environments.map((env) => env.ZLIBRARY_EMAIL)).toEqual(['one@example.test', 'two@example.test']);
    for (const env of environments) {
      expect(env).not.toHaveProperty('ZLIBRARY_EMAIL_2');
      expect(env).not.toHaveProperty('ZLIBRARY_PASSWORD_2');
    }
  } finally { process.env = previous; }
});
