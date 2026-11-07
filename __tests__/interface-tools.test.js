import { test, expect, jest, afterEach } from '@jest/globals';
import { AcquisitionTools, OLD_TOOLS } from '../dist/lib/acquisition-tools.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createServer, TOOL_DEFINITIONS } from '../dist/index.js';
import { ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';
const env = { ZLIBRARY_EMAIL: 'offline@example.invalid', ZLIBRARY_PASSWORD: 'password' };
const book = { id: '1', hash: 'abc', title: 'A book', author: 'An Author', extension: 'pdf', source: 'zlibrary' };
const clients = [];
afterEach(async () => { for (const c of clients.splice(0)) await c.close(); });

test('local limits and LibGen-only live limits never invoke a bridge or acquire an EAPI session', async () => {
  const invoke = jest.fn();
  const tools = new AcquisitionTools(invoke, {});
  const local = await tools.call('get_limits', {});
  expect(local.sources.libgen.limit.kind).toBe('none');
  expect(local.sources.annas.search_available).toBe(true);
  expect(local.sources.annas.download_available).toBe(false);
  await tools.call('get_limits', { sources: ['libgen'], mode: 'live' });
  expect(invoke).not.toHaveBeenCalled();
  invoke.mockResolvedValueOnce({books:[]});
  const search=await tools.call('search',{query:'books'});expect(search.rows).toEqual([]);expect(search.all_answered).toBe(true);expect(search.sources.libgen.state).toBe('empty');
});

test('lost-reply refill replay is memoized and extra arguments cost no request', async () => {
 const calls=[];const tools=new AcquisitionTools(async(op,args)=>{calls.push([op,args]);return {books:Array.from({length:50},(_,i)=>({...book,id:`${args.page}-${i}`,title:`Page ${args.page}`}))};},env);
 const first=await tools.call('search',{query:'books',sources:['zlibrary'],limit:50});const cursor=first.next_cursor;
 const lost=await tools.call('search',{cursor}),replay=await tools.call('search',{cursor});expect(replay.rows[0].title).toBe('Page 2');expect(replay).toEqual(lost);expect(calls.map(c=>c[1].page)).toEqual([1,2]);
 const before=calls.length;expect((await tools.call('search',{cursor,query:'different'})).error.code).toBe('invalid_argument');expect(calls.length).toBe(before);expect((await tools.call('get_history',{cursor})).error.code).toBe('invalid_argument');
});

test('held non-EAPI page is sliced and cursor replay survives row eviction', async () => {
  const invoke = jest.fn().mockResolvedValue({ books: Array.from({ length: 4 }, (_, i) => ({ md5: String(i).padStart(32, '0'), title: 'Book ' + i, source: 'libgen' })) });
  const tools = new AcquisitionTools(invoke, {});
  const first = await tools.call('search', { query: 'books', sources: ['libgen'], limit: 2 });
  const next = await tools.call('search', { cursor: first.next_cursor });
  expect(next.rows.map(r => r.title)).toEqual(['Book 2', 'Book 3']);
  expect((await tools.call('search', { cursor: first.next_cursor })).rows.map(r => r.title)).toEqual(['Book 2', 'Book 3']);
  expect(invoke).toHaveBeenCalledTimes(1);
});

test.each([0, -1, 51, 1.5])('invalid limit %s is rejected before any provider call', async limit => {
  const invoke = jest.fn();
  expect((await new AcquisitionTools(invoke, env).call('search', { query: 'book', sources: ['zlibrary'], limit })).error.code).toBe('invalid_argument');
  expect(invoke).not.toHaveBeenCalled();
});

test('routing distinguishes empty, failed, skipped configuration and all failures', async () => {
  const failure = { context: { details: { reason: 'dns_failure' } } };
  const invoke = jest.fn(async (_op, args) => { if (args.source === 'libgen') return { books: [] }; throw failure; });
  const tools = new AcquisitionTools(invoke, env);
  expect((await tools.call('search', { query: 'book', sources: ['libgen', 'annas'] })).error.code).toBe('partial_failure');
  expect((await tools.call('search', { query: 'book', sources: ['annas'] })).error.code).toBe('provider_unreachable');
  const missing = await new AcquisitionTools(invoke, {}).call('search', { query: 'book', sources: ['zlibrary'] });
  expect(missing.error.code).toBe('credentials_missing');
  const empty = await new AcquisitionTools(async () => ({ books: [] }), {}).call('search', { query: 'book', sources: ['zlibrary', 'libgen'] });
  expect(empty.error.code).toBe('partial_failure');expect(empty.error.attempts.map(a=>[a.source,a.state])).toEqual([['libgen','empty'],['zlibrary','not_asked'],['annas','not_asked']]);
});

test('get_book includes bounded description and field-origin evidence; history only mints downloadable refs', async () => {
  const invoke = jest.fn(async op => op === 'get_book_metadata_complete' ? { description: 'x'.repeat(1100), isbn_13: '123', edition: 'Second' } : op === 'get_download_history' ? [book, { title: 'No identity' }] : { books: [book] });
  const tools = new AcquisitionTools(invoke, env);
  const result = await tools.call('search', { query: 'book', sources: ['zlibrary'] });
  const details = await tools.call('get_book', { ref: result.rows[0].ref });
  expect(details.description.length).toBe(1000);
  expect(details.description_truncated).toBe(true);
  expect(details.evidence.detail).toContain('edition');
  const history = await tools.call('get_history', {});
  expect(history.rows[0].ref).toBeDefined();
  expect(history.rows[1].ref).toBeUndefined();
});

test('quota estimates decrement only on transfer and invalidate on ambiguous outcome', async () => {
  let outcome = 'ok';
  const tools = new AcquisitionTools(async op => {
    if (op === 'get_download_limits') return { daily_limit: 10, downloads_today: 3, daily_remaining: 7 };
    if (op === 'search') return { books: [book] };
    if (outcome === 'unknown') throw { context: { reason: 'timeout' } };
    return { path: '/book.pdf', bytes: 12, reused: false, transfer_skipped: outcome === 'skip', verified: 'sha256', record: '/book.pdf.acq.json' };
  }, env);
  await tools.call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  const ref = (await tools.call('search', { query: 'book', sources: ['zlibrary'] })).rows[0].ref;
  expect((await tools.call('download', { ref })).quota_estimate.remaining).toBe(6);
  outcome = 'skip';
  expect((await tools.call('download', { ref })).quota_estimate.remaining).toBe(6);
  outcome = 'unknown';
  expect((await tools.call('download', { ref })).error.retry).toBe('may_spend_quota');
  outcome = 'ok';
  expect((await tools.call('download', { ref })).quota_estimate).toBeUndefined();
});

for (const name of Object.keys(OLD_TOOLS)) test(`migration: ${name} is typed, helpful, and costs no provider call`, async () => {
  const invoke = jest.fn();
  const result = await new AcquisitionTools(invoke, {}).call(name, {});
  expect(result.error.code).toBe('invalid_argument');
  expect(result.error.recovery.length).toBeGreaterThan(10);
  expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
  expect(invoke).not.toHaveBeenCalled();
});

test('offline SDK tools/list advertises exactly five tools and independently validates errors', async () => {
  const server = createServer();
  const client = new Client({ name: 'offline-interface', version: '1' });
  clients.push(client, server);
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(b), client.connect(a)]);
  const list = await client.listTools();
  expect(list.tools.map(t => t.name)).toEqual(['search', 'get_book', 'download', 'get_limits', 'get_history']);
  expect(list.tools).toEqual(TOOL_DEFINITIONS);
  const result = await client.callTool({ name: 'search', arguments: { limit: 0 } });
  expect(result.isError).toBe(true);
  expect(ErrorEnvelopeSchema.safeParse(result.structuredContent).success).toBe(true);
  expect(JSON.parse(result.content[0].text)).toEqual(result.structuredContent);
});

test('provider rows cannot silently substitute a source selected by the caller', async () => {
  const tools = new AcquisitionTools(async () => ({ books: [{ ...book, source: 'libgen' }] }), env);
  const result = await tools.call('search', { query: 'book', sources: ['zlibrary'] });
  expect(result.error.code).toBe('provider_error');
});

test('whole-search LRU evicts row and cursor together without a hidden refill', async () => {
 let calls=0;const tools=new AcquisitionTools(async()=>{calls++;return {books:[{...book,id:'a'},{...book,id:'b'}]};},env,{recordCapacity:1});const first=await tools.call('search',{query:'book',sources:['zlibrary'],limit:1});await tools.call('search',{query:'other',sources:['zlibrary'],limit:1});
 expect((await tools.call('get_book',{ref:first.rows[0].ref})).error.detail.reason).toBe('expired');expect((await tools.call('search',{cursor:first.next_cursor})).error.code).toBe('ref_unavailable');expect(calls).toBe(2);
});

test('download projects compact replies and retains an observed daily reading from the provider reply', async () => {
  let transferred = false;
  const tools = new AcquisitionTools(async op => {
    if (op === 'search_multi_source') return { books: [{ md5: '0'.repeat(32), title: 'Book', source: 'annas_archive' }] };
    const result = { path: '/book.pdf', bytes: 12, reused: false, transfer_skipped: transferred, verified: 'sha256', record: '/book.pdf.acq.json', unexpected_payload: 'must not enter context' };
    if (!transferred) result.quota_estimate = { source: 'annas', remaining: 4, as_of: '2026-10-19T12:00:00Z', basis: 'observed' };
    transferred = true;
    return result;
  }, { ANNAS_SECRET_KEY: 'offline' });
  const ref = (await tools.call('search', { query: 'Book', sources: ['annas'] })).rows[0].ref;
  const first = await tools.call('download', { ref });
  expect(first.unexpected_payload).toBeUndefined();
  expect(first.quota_estimate).toMatchObject({ source: 'annas', remaining: 4, basis: 'observed' });
  expect((await tools.call('download', { ref })).quota_estimate).toEqual(first.quota_estimate);
});
