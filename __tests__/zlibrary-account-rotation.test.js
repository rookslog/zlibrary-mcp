import { expect, test } from '@jest/globals';
import { inspectZlibraryAccounts, ZlibraryDownloadAccountSelector } from '../dist/lib/zlibrary-accounts.js';
import { EapiSessionRegistry } from '../dist/lib/eapi-session.js';

test('rotates complete distinct accounts and ignores incomplete or duplicate slots', () => {
  const config = inspectZlibraryAccounts({
    ZLIBRARY_EMAIL: 'one@example.test', ZLIBRARY_PASSWORD: 'fake-1',
    ZLIBRARY_EMAIL_2: 'two@example.test', ZLIBRARY_PASSWORD_2: 'fake-2',
    ZLIBRARY_EMAIL_3: 'ONE@example.test', ZLIBRARY_PASSWORD_3: 'fake-3',
    ZLIBRARY_EMAIL_4: 'partial@example.test',
  });
  expect(config.invalidSlots).toEqual([3, 4]);
  const selector = new ZlibraryDownloadAccountSelector();
  expect(Array.from({ length: 4 }, () => selector.next(config.accounts).slot)).toEqual([1, 2, 1, 2]);
});

test('one account session does not replace another account session', () => {
  const registry = new EapiSessionRegistry();
  for (const identity of ['one@example.test', 'two@example.test']) {
    const generation = registry.generationFor(identity);
    registry.consume({ identity, generation, domain: 'example.test', remix_userid: identity, remix_userkey: 'fake-key' }, identity, generation);
  }
  expect(registry.get('one@example.test').remix_userid).toBe('one@example.test');
  expect(registry.get('two@example.test').remix_userid).toBe('two@example.test');
});
