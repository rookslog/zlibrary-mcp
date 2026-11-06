import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { RefStore } from '../../dist/lib/refs.js';
export async function historyScenario(isolate = false) {
  const calls = []; let refill = 0;
  const invoke = async (operation, args) => {
    calls.push({ operation, args });
    if (operation === 'get_download_history') return args.page === 1 ? [
      { id: '101', hash: 'hash-101', title: 'Downloadable', author: 'Synthetic', extension: 'pdf', downloaded_at: '2001-01-02' },
      { id: '102', title: 'No hash', author: 'Synthetic', downloaded_at: '2001-01-03' },
    ] : [{ id: '103', hash: 'hash-103', title: `Refill ${++refill}`, downloaded_at: '2001-01-04' }];
    if (operation === 'get_book_metadata_complete') return { title: 'Detail', publisher: 'Synthetic' };
    if (operation === 'acquire_book') return { path: '/synthetic/history.pdf', bytes: 1, reused: false, transfer_skipped: false, verified: 'sha256', record: '/synthetic/history.record.json' };
    throw new Error('Unexpected synthetic operation');
  };
  const tools = new AcquisitionTools(invoke, { ZLIBRARY_EMAIL: 'fixture', ZLIBRARY_PASSWORD: 'fixture' });
  tools.refs = new RefStore({ nonce: 1n });
  const searchPage = tools.page;
  if (isolate) tools.page = async () => { throw new Error('Search page was called'); };
  const first = await tools.call('get_history', { limit: 2 });
  if (!first.rows) return { first, calls };
  const detail = await tools.call('get_book', { ref: first.rows[0].ref });
  const download = await tools.call('download', { ref: first.rows[0].ref });
  const next = await tools.call('get_history', { cursor: first.next_cursor });
  const replay = await tools.call('get_history', { cursor: first.next_cursor });
  const unsupported = await tools.call('get_history', { source: 'annas' });
  if (isolate) tools.page = searchPage;
  const wrongOperation = await tools.call('search', { cursor: first.next_cursor });
  const missingCredentials = await new AcquisitionTools(invoke, {}).call('get_history', {});
  return { first, detail, download, next, replay, unsupported, wrongOperation, missingCredentials, calls };
}
