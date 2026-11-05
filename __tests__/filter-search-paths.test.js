import { afterEach, expect, jest, test } from '@jest/globals';
const run = jest.fn(async () => [JSON.stringify({ content: [{ type: 'text', text: JSON.stringify({ books: [] }) }] })]);
jest.unstable_mockModule('../lib/venv-manager.js', () => ({ getManagedPythonPath: async () => '/offline/python' }));
jest.unstable_mockModule('../lib/python-runner.js', () => ({ runPythonBridge: run, LONG_BRIDGE_TIMEOUT_MS: 2400000 }));
const api = await import('../dist/lib/zlibrary-api.js');
const previous = process.env.ZLIBRARY_SESSION_MODE;
process.env.ZLIBRARY_SESSION_MODE = 'per-call';
afterEach(() => run.mockClear());
// Restore only this fixture's setting; no account values are set in the process.
const { afterAll } = await import('@jest/globals');
afterAll(() => { if (previous === undefined) delete process.env.ZLIBRARY_SESSION_MODE; else process.env.ZLIBRARY_SESSION_MODE = previous; });

test.each([
  ['searchBooks', { query: 'book' }, 'search'],
  ['fullTextSearch', { query: 'book' }, 'full_text_search'],
  ['searchByTerm', { term: 'book' }, 'search_by_term_bridge'],
  ['searchByAuthor', { author: 'writer' }, 'search_by_author_bridge'],
  ['invokeAcquisitionBridge', { query: 'book' }, 'search'],
])('%s normalizes language aliases at the shared subprocess seam', async (method, args, op) => {
  args = { ...args, languages: ['en', 'english', 'nb', 'nn', 'ca', ' français '] };
  if (method === 'invokeAcquisitionBridge') await api[method](op, args);
  else await api[method](args);
  expect(run).toHaveBeenCalledTimes(1);
  const payload = JSON.parse(run.mock.calls[0][1].args[1]);
  expect(payload.languages).toEqual(['english', 'norwegian', 'catalan', ' français ']);
  expect(args.languages).toEqual(['en', 'english', 'nb', 'nn', 'ca', ' français ']);
});
