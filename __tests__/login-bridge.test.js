import { jest } from '@jest/globals';
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

jest.setTimeout(30000);
const run = jest.fn();
const envelope = (value) => [
  JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] }),
];
let directory, original, api;
beforeEach(async () => {
  original = { ...process.env };
  directory = mkdtempSync(path.join(tmpdir(), 'saved-bridge-'));
  Object.assign(process.env, {
    XDG_STATE_HOME: directory,
    ZLIBRARY_EMAIL: 'environment@example.test',
    ZLIBRARY_PASSWORD: 'fake-env-password',
    ZLIBRARY_SESSION_MODE: 'memory',
    RETRY_MAX_RETRIES: '0',
  });
  delete process.env.ZLIBRARY_PREFER_ENV;
  execFileSync(
    path.resolve('.venv/bin/python'),
    [
      '-c',
      'from lib.login_store import save; save(identity="saved@example.test", user_id="42", user_key="fake-private-bridge-key", domain="fake.example.test")',
    ],
    { env: process.env },
  );
  jest.resetModules();
  run.mockReset();
  jest.unstable_mockModule('../lib/python-runner.js', () => ({
    runPythonBridge: run,
    LONG_BRIDGE_TIMEOUT_MS: 3900000,
  }));
  api = await import('../lib/zlibrary-api.js');
});
afterEach(() => {
  process.env = original;
  rmSync(directory, { recursive: true, force: true });
});

test('saved metadata selects Python-owned authentication despite environment password', async () => {
  run.mockImplementation(async (_script, options, control) => {
    expect(control.sessionInput).toBeUndefined();
    expect(options.env.ZLIBRARY_LOGIN_EXPECTED_ORIGIN).toBe('saved');
    expect(options.env.ZLIBRARY_LOGIN_EXPECTED_IDENTITY).toBe('saved@example.test');
    expect(JSON.stringify(options.args)).not.toContain('fake-private-bridge-key');
    return envelope({ books: [], retrieved_from_url: 'fake' });
  });
  await expect(api.searchBooks({ query: 'fake' })).resolves.toMatchObject({ books: [] });
  expect(run).toHaveBeenCalledTimes(1);
});

test('a refused login store does not block an admitted local acquisition receipt', async () => {
  chmodSync(path.join(directory, 'zlibrary/logins/zlibrary.json'), 0o644);
  const receipt = {
    path: '/synthetic/book.pdf',
    transfer_skipped: true,
    reused: false,
    bytes: 100,
    verified: 'sha256',
    record: '/synthetic/book.pdf.acq.json',
  };
  run.mockImplementation(async (_script, options, control) => {
    expect(options.args[0]).toBe('acquire_book');
    expect(control.sessionInput).toBeUndefined();
    return envelope(receipt);
  });
  await expect(
    api.invokeAcquisitionBridge('acquire_book', {
      book_details: { source: 'zlibrary', id: '123', hash: 'fake' },
    }),
  ).resolves.toEqual(receipt);
  expect(run).toHaveBeenCalledTimes(1);
});

test('cancelled metadata never reaches the provider bridge', async () => {
  await expect(
    api.searchBooks({ query: 'fake' }, { signal: AbortSignal.abort() }),
  ).rejects.toMatchObject({ context: { reason: 'aborted' } });
  expect(run).not.toHaveBeenCalled();
});

test('corrupt saved content allows the existing environment session path', async () => {
  const { writeFileSync } = await import('node:fs');
  writeFileSync(path.join(directory, 'zlibrary/logins/zlibrary.json'), '{invalid');
  run.mockImplementation(async (_script, options, control) => {
    expect(control.sessionInput.identity).toBe('environment@example.test');
    expect(options.env.ZLIBRARY_LOGIN_EXPECTED_ORIGIN).toBeUndefined();
    if (control.sessionInput.action === 'login') {
      control.onSession({
        ...control.sessionInput,
        domain: 'fake.example.test',
        remix_userid: '42',
        remix_userkey: 'fake-environment-cookie',
      });
      return envelope({ authenticated: true });
    }
    return envelope({ books: [], retrieved_from_url: 'fake' });
  });
  await expect(api.searchBooks({ query: 'fake' })).resolves.toMatchObject({ books: [] });
  expect(run).toHaveBeenCalledTimes(2);
});
