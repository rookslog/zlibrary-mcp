import { expect, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { errorEnvelope } from '../dist/lib/tool-errors.js';

const check = { context: { details: { reason: 'browser_check' } } };
const failure = (reason) => ({ context: { details: { reason } } });

test.each(['true', '1', 'yes', ' TRUE '])('browser check with flag %s names the host browser and person', async (flag) => {
  const tools = new AcquisitionTools(async () => { throw check; }, { ANNAS_BROWSER_ENABLED: flag });
  const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
  expect(result.error).toMatchObject({ code: 'provider_error', retry: 'safe', detail: { reason: 'browser_check' } });
  expect(result.error.recovery).toMatch(/visible browser/i);
  expect(result.error.recovery).toMatch(/person.*host/i);
  expect(result.error.recovery).toMatch(/backoff/i);
  expect(result.error.recovery).not.toMatch(/does not pass it|Use another source/);
});

test.each(['false', '0', 'on', undefined])('flag off preserves default recovery (%s)', async (flag) => {
  const baseline = new AcquisitionTools(async () => { throw check; }, {});
  const tools = new AcquisitionTools(async () => { throw check; }, { ANNAS_BROWSER_ENABLED: flag });
  const args = { query: 'Book', sources: ['annas'] };
  expect(await tools.call('search', args)).toEqual(await baseline.call('search', args));
});

test('explicit flag off overrides the process flag', async () => {
  const before = process.env.ANNAS_BROWSER_ENABLED;
  process.env.ANNAS_BROWSER_ENABLED = 'true';
  try {
    const tools = new AcquisitionTools(async () => { throw check; }, { ANNAS_BROWSER_ENABLED: 'false' });
    const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
    expect(result.error.recovery).not.toMatch(/visible browser|person.*host/i);
  } finally {
    if (before === undefined) delete process.env.ANNAS_BROWSER_ENABLED;
    else process.env.ANNAS_BROWSER_ENABLED = before;
  }
});

test('browser download refusal carries the same recovery', async () => {
  const tools = new AcquisitionTools(async (operation) => {
    if (operation === 'search_multi_source') return { books: [{ md5: 'a'.repeat(32), source: 'annas_archive', title: 'Book' }] };
    throw check;
  }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
  const download = await tools.call('download', { ref: result.rows[0].ref });
  expect(download.error.detail.reason).toBe('browser_check');
  expect(download.error.recovery).toMatch(/visible browser/i);
});

test('read timeout is retry safe rather than empty', async () => {
  const tools = new AcquisitionTools(async () => { throw failure('read_timeout'); }, { ANNAS_BROWSER_ENABLED: 'true' });
  const result = await tools.call('search', { query: 'Book', sources: ['annas'] });
  expect(result.error).toMatchObject({ code: 'provider_timeout', retry: 'safe', detail: { reason: 'read_timeout' } });
  expect(result.rows).toBeUndefined();
});

test('nested browser failures keep browser recovery', () => {
  const before = process.env.ANNAS_BROWSER_ENABLED;
  process.env.ANNAS_BROWSER_ENABLED = 'true';
  try {
    const envelope = errorEnvelope({ context: { details: { failures: [{ provider: 'annas', reason: 'browser_check' }] } } }, 'search', 'annas');
    expect(envelope.error.recovery).toMatch(/visible browser/i);
  } finally {
    if (before === undefined) delete process.env.ANNAS_BROWSER_ENABLED;
    else process.env.ANNAS_BROWSER_ENABLED = before;
  }
});
