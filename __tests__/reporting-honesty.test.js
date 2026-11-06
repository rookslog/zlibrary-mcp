import { expect, jest, test } from '@jest/globals';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const runFile = promisify(execFile);
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import * as acquisitionModule from '../dist/lib/acquisition-tools.js';

// These checks exercise reporting, with only provider calls replaced.
const credentials = {
  ZLIBRARY_EMAIL: 'offline@example.invalid',
  ZLIBRARY_PASSWORD: 'synthetic-password',
};

test('cap-only Python profile leaves public remaining and subsequent quota estimates absent', async () => {
  const { stdout } = await runFile('.venv/bin/python', ['-c', `
import asyncio, json, sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, 'lib')
import python_bridge
client = SimpleNamespace(get_profile=AsyncMock(return_value={'user': {'downloads_limit': 10}}))
python_bridge.get_eapi_client = AsyncMock(return_value=client)
print(json.dumps(asyncio.run(python_bridge.get_download_limits())))
`], { encoding: 'utf8' });
  const profile = JSON.parse(stdout);
  const invoke = jest.fn(async op => {
    if (op === 'get_download_limits') return profile;
    if (op === 'search') return { books: [{ id: '1', hash: 'abc', source: 'zlibrary' }] };
    if (op === 'acquire_book') return {
      path: '/offline/book.pdf', bytes: 12, reused: false, transfer_skipped: false,
      verified: 'sha256', record: '/offline/book.pdf.acq.json',
    };
    throw new Error('Unexpected operation');
  });
  const tools = new AcquisitionTools(invoke, credentials);
  const result = await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  expect(result.sources.zlibrary.limit).toMatchObject({ kind: 'unparsed', per_day: 10 });
  expect(result.sources.zlibrary.limit).not.toHaveProperty('remaining');
  const ref = (await tools.call('search', { query: 'book', sources: ['zlibrary'] })).rows[0].ref;
  expect(await tools.call('download', { ref })).not.toHaveProperty('quota_estimate');
  expect(invoke.mock.calls.filter(([op]) => op === 'get_download_limits')).toHaveLength(1);
});

// Both keyed and search-only reports must reject unusable configuration.
const badBases = [
  '', '   ', '\t\n', 'http://', 'https://', '/path/only', 'urn:isbn:0451450523',
  'ftp://annas-archive.gl', 'annas-archive.gl', 'http://[', 'http://]',
  'https://[::1', 'https://annas-archive.gl:notaport', 'https://annas-archive.gl:0',
  'https://annas-archive.gl:65536', 'https://annas-archive.gl:-1',
  'https://annas-archive.gl\n', 'https://annas-archive.gl\r', 'https://annas-archive.gl\t',
  'https://annas-archive.gl\x1f', 'https://annas-archive.gl\x7f',
  'https://annas-archive.gl?x=1', 'https://annas-archive.gl#x',
  'https://annas-archive.gl?', 'https://annas-archive.gl#',
  'https://annas-archive.%67l', 'https://%61nnas-archive.gl',
  'https://annas-archive.gl@evil.invalid', 'https://user:synthetic-secret@annas-archive.gl',
  'https://annas-archive.gl\\@evil.invalid',
];
for (const key of ['', 'synthetic-secret']) {
  test.each(badBases)('Anna local capability rejects base %j with key ' + Boolean(key), async base => {
    const invoke = jest.fn(() => { throw new Error('No bridge allowed'); });
    const tools = new AcquisitionTools(invoke, { ANNAS_BASE_URL: base, ANNAS_SECRET_KEY: key });
    for (const mode of ['local', 'live']) {
      const result = await tools.call('get_limits', { mode, sources: ['annas'] });
      expect(result.sources.annas).toMatchObject({
        configured: false, search_available: false, download_available: false,
      });
      expect(result.sources.annas.note).toMatch(/base.*unusable/i);
      expect(JSON.stringify(result)).not.toMatch(/synthetic-secret|evil\.invalid/);
    }
    const search = await tools.call('search', { query: 'book', sources: ['annas'] });
    expect(search.error.code).toBe('credentials_missing');
    expect(invoke).not.toHaveBeenCalled();
  });
}

// Trust protects the download key; a usable search URL need not be allowlisted.
for (const key of ['', 'synthetic-secret']) {
  test.each([
    'https://annas-archive.li', 'https://annas-archive.gl.evil.invalid',
    'http://localhost:8080', 'http://localhost:8080/annas',
    'http://[::1]:8080/prefix/',
  ])('Anna searches usable untrusted base %j with key ' + Boolean(key), async base => {
    const invoke = jest.fn(async () => ({ books: [] }));
    const tools = new AcquisitionTools(invoke, { ANNAS_BASE_URL: base, ANNAS_SECRET_KEY: key });
    for (const mode of ['local', 'live']) {
      const result = await tools.call('get_limits', { mode, sources: ['annas'] });
      expect(result.sources.annas).toMatchObject({
        configured: false, search_available: true, download_available: false,
      });
      expect(result.sources.annas.note).toMatch(/base.*not trusted.*key/i);
      expect(JSON.stringify(result)).not.toMatch(/synthetic-secret|evil\.invalid/);
    }
    expect(invoke).not.toHaveBeenCalled();
    const search = await tools.call('search', { query: 'book', sources: ['annas'] });
    expect(search).not.toHaveProperty('error');
    expect(search.rows).toEqual([]);
    expect(search.sources.annas).toMatchObject({state:'empty',rows:0});expect(search.all_answered).toBe(true);
    expect(invoke).toHaveBeenCalledWith('search_multi_source',
      { query: 'book', source: 'annas', count: 50, multi_source:false }, {signal:expect.any(AbortSignal)});
  });
}

test('supported operations are symmetric across configured and unconfigured sources', async () => {
  const invoke = jest.fn();
  const empty = (await new AcquisitionTools(invoke, {}).call('get_limits')).sources;
  const configured = (await new AcquisitionTools(invoke, {
    ...credentials, ANNAS_SECRET_KEY: 'synthetic-key',
  }).call('get_limits')).sources;
  expect(empty.annas.operations).toEqual(['search', 'details', 'download']);
  expect(empty.libgen.operations).toEqual(['search', 'details', 'download']);
  expect(empty.zlibrary.operations).toEqual(['search', 'details', 'download', 'history']);
  for (const source of ['libgen', 'annas', 'zlibrary']) {
    expect(empty[source].operations).toEqual(configured[source].operations);
    expect(Object.keys(empty[source]).filter(key=>key!=='off_reason').sort()).toEqual(
      Object.keys(empty.libgen).concat(source === 'libgen' ? [] : ['note']).sort(),
    );
  }
  expect(empty.annas.download_available).toBe(false);
  expect(configured.annas.download_available).toBe(true);
  expect(empty.zlibrary.download_available).toBe(false);
  expect(configured.zlibrary.download_available).toBe(true);
  expect(invoke).not.toHaveBeenCalled();
  await checkTrustedBases();
});

test.each([['libgen'],['annas'],['libgen','annas'],['annas','libgen'],['zlibrary','libgen']])('empty source-set %j preserves intent and reports unavailable members',async(...sources)=>{
 const invoke=jest.fn(async()=>({books:[]}));const result=await new AcquisitionTools(invoke,{}).call('search',{query:'book',sources});
 if(sources.includes('zlibrary')) {expect(result.error.code).toBe('partial_failure');expect(result.error.attempts.find(a=>a.source==='zlibrary')).toMatchObject({state:'not_asked',reason:'credentials_missing'});}
 else {expect(result.rows).toEqual([]);expect(result.all_answered).toBe(true);for(const source of sources)expect(result.sources[source]).toMatchObject({state:'empty',rows:0});expect(result).not.toHaveProperty('routing');}
});

test('an exhausted single-source refill reports emptiness rather than a stale serving source',async()=>{
 const invoke=jest.fn(async(op,args)=>({books:op==='search'&&args.page===1?Array.from({length:50},(_,i)=>({id:String(i),hash:'abc'})):[]}));const tools=new AcquisitionTools(invoke,credentials);const first=await tools.call('search',{query:'book',sources:['zlibrary'],limit:50});const next=await tools.call('search',{cursor:first.next_cursor});expect(next.rows).toEqual([]);expect(next.sources.zlibrary.state).toBe('empty');expect(next).not.toHaveProperty('routing');expect(next.all_answered).toBe(true);await checkNonemptyRouting();
});

// Positive controls embedded in the regressions constrain valid configurations.
async function checkTrustedBases() {
  const { stdout } = await runFile('.venv/bin/python', ['-c', `
import json
from lib.sources.config import ANNAS_TRUSTED_HOSTS, DEFAULT_ANNAS_BASE_URL
print(json.dumps({'hosts': sorted(ANNAS_TRUSTED_HOSTS), 'default': DEFAULT_ANNAS_BASE_URL}))
`], { encoding: 'utf8' });
  const config = JSON.parse(stdout);
  expect(acquisitionModule.annasTrustedHosts).toEqual(new Set(config.hosts));
  for (const base of [undefined, config.default, ...config.hosts.map(h => 'https://' + h),
    'https://ANNAS-ARCHIVE.GL/', 'http://annas-archive.gl:8080/prefix/',
    'https://annas-archive.pk:8443/prefix/nested']) {
    for (const key of ['', 'synthetic-key']) {
      const invoke = jest.fn();
      const tools = new AcquisitionTools(invoke, { ANNAS_BASE_URL: base, ANNAS_SECRET_KEY: key });
      for (const mode of ['local', 'live']) {
        const result = await tools.call('get_limits', { mode, sources: ['annas', 'libgen'] });
        expect(result.sources.annas).toMatchObject({
          configured: Boolean(key), search_available: true, download_available: Boolean(key),
        });
      }
      expect(invoke).not.toHaveBeenCalled();
    }
  }
}

async function checkNonemptyRouting() {
  for (const [sources, served, fellBack] of [
  [['libgen', 'annas'], 'libgen', false],
  [['libgen', 'annas'], 'annas', true],
  [['annas', 'libgen'], 'libgen', true],
]) {
  const invoke = async (_op, args) => ({
    books: args.source === served ? [{ md5: 'a'.repeat(32), source: served }] : [],
  });
  const result = await new AcquisitionTools(invoke, {}).call('search', { query: 'book', sources });
  expect(result.rows).toHaveLength(1);
  expect(result.rows[0].source).toBe(served);expect(result.sources[served].state).toBe('rows');expect(result.all_answered).toBe(true);expect(result).not.toHaveProperty('routing');
  }
}
