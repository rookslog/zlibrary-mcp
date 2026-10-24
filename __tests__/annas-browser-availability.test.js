import { expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';

test.each([
  [undefined, false], ['false', false], ['0', false], ['on', false],
  ['true', true], ['1', true], ['yes', true], [' TRUE ', true],
])('Anna download availability follows the same opt-in parser (%s)', async (flag, expected) => {
  const invoke = jest.fn();
  const tools = new AcquisitionTools(invoke, { ANNAS_BROWSER_ENABLED: flag });
  const local = await tools.call('get_limits', { sources: ['annas'] });
  expect(local.sources.annas.download_available).toBe(expected);
  expect(local.sources.annas.search_available).toBe(true);
  expect(local.sources.annas.limit).toEqual({ kind: 'unknown' });
  expect(invoke).not.toHaveBeenCalled();
});

test.each([
  [{ ANNAS_SECRET_KEY: 'offline-key' }, true],
  [{ ANNAS_BROWSER_ENABLED: 'true', ANNAS_BASE_URL: 'https://mirror.example.invalid' }, true],
  [{ ANNAS_BROWSER_ENABLED: 'true', ANNAS_BASE_URL: 'https://mirror.example.invalid', ANNAS_SECRET_KEY: 'offline-key' }, false],
  [{ ANNAS_BROWSER_ENABLED: 'true', ANNAS_BASE_URL: 'not a URL' }, false],
])('Anna availability keeps trusted-key and usable-base boundaries (%j)', async (env, expected) => {
  const tools = new AcquisitionTools(jest.fn(), env);
  const local = await tools.call('get_limits', { sources: ['annas'] });
  expect(local.sources.annas.download_available).toBe(expected);
});

test.each([
  ['browser', false, 'true', undefined],
  ['browser', true, 'true', undefined],
  ['keyed', false, 'true', 3],
  ['keyed', false, 'false', 3],
])('cached membership quota: %s reuse=%s flag=%s', async (route, reuse, flag, remaining) => {
  let downloads = 0;
  const tools = new AcquisitionTools(async (op) => {
    if (op === 'search_multi_source') return { books: [{ md5: '0'.repeat(32), title: 'Book', source: 'annas_archive' }] };
    downloads++;
    const result = { path: '/offline/book.pdf', record: '/offline/book.pdf.acq.json', bytes: 12, reused: false, transfer_skipped: downloads > 1 && reuse, verified: 'sha256' };
    if (downloads === 1) result.quota_estimate = { source: 'annas', remaining: 4, as_of: '2026-10-24T00:00:00Z', basis: 'observed' };
    else if (route === 'browser') result.quota_estimate = null; // Private explicit unknown, never a public null.
    return result;
  }, { ANNAS_SECRET_KEY: 'offline-key', ANNAS_BROWSER_ENABLED: flag });
  const ref = (await tools.call('search', { query: 'Book', sources: ['annas'] })).rows[0].ref;
  const first = await tools.call('download', { ref });
  expect(first.quota_estimate.remaining).toBe(4);
  const second = await tools.call('download', { ref });
  expect(second.error).toBeUndefined();
  expect(second.path).toBe('/offline/book.pdf');
  if (remaining === undefined) expect(second).not.toHaveProperty('quota_estimate');
  else expect(second.quota_estimate).toMatchObject({ remaining, basis: 'decremented' });
});
