import { jest, beforeEach, afterEach, test, expect } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { CallToolResultSchema } from '@modelcontextprotocol/sdk/types.js';

// The result tests keep the API facade and SDK real, replacing only the
// subprocess/environment boundary. The recent-books wiring spy delegates to
// the real facade, including its literal Python argument contract.
const runBridge = jest.fn();
jest.unstable_mockModule('../lib/venv-manager.js', () => ({
  getManagedPythonPath: jest.fn().mockResolvedValue('/offline/python'),
}));
jest.unstable_mockModule('../lib/python-runner.js', () => ({
  runPythonBridge: runBridge,
  LONG_BRIDGE_TIMEOUT_MS: 2400000,
}));

// Offline synthetic contract fixtures, not provider recordings.
const syntheticProvenance = {
  source: 'zlibrary', route: 'eapi', mirror: null, host: 'synthetic-transfer-host',
};
const syntheticLimits = {
  requested: ['annas_archive', 'libgen', 'zlibrary'],
  sources: {
    annas_archive: {
      available: true, routes: ['search'],
      daily_limit: { state: 'not_applicable', total: null, used: null, remaining: null, note: 'synthetic search-only configuration' },
      note: 'synthetic search-only configuration', details: {},
    },
    libgen: {
      available: true, routes: ['search', 'download'],
      daily_limit: { state: 'none', total: null, used: null, remaining: null, note: 'synthetic unlimited source' },
      note: 'synthetic unlimited source', details: {},
    },
    zlibrary: {
      available: true, routes: ['search', 'download'],
      daily_limit: { state: 'known', total: 10, used: 2, remaining: 8, note: '' },
      note: 'synthetic authenticated configuration', details: { is_premium: false },
    },
  },
};

const cases = {
  search_books: [{ query: 'offline' }, 'search', { books: [] }],
  full_text_search: [{ query: 'offline phrase' }, 'full_text_search', { books: [] }],
  get_download_history: [{ count: 1 }, 'get_download_history', []],
  get_download_limits: [{}, 'get_download_limits', syntheticLimits],
  get_recent_books: [{ count: 1 }, 'get_recent_books', { books: [] }],
  download_book_to_file: [
    { bookDetails: { id: '1', hash: 'offline' } },
    'download_book',
    { file_path: '/offline/book.pdf', provenance: syntheticProvenance },
  ],
  process_document_for_rag: [
    { file_path: '/offline/book.pdf' },
    'process_document',
    { processed_file_path: '/offline/book.txt' },
  ],
  get_book_metadata: [
    { bookId: '1', bookHash: 'offline' },
    'get_book_metadata_complete',
    { title: 'Offline' },
  ],
  search_by_term: [{ term: 'offline' }, 'search_by_term_bridge', { books: [] }],
  search_by_author: [{ author: 'offline' }, 'search_by_author_bridge', { books: [] }],
  fetch_booklist: [
    { booklistId: '1', booklistHash: 'offline', topic: 'offline' },
    'fetch_booklist_bridge',
    { books: [] },
  ],
  search_advanced: [
    { query: 'offline' },
    'search_advanced',
    { exact_matches: [], fuzzy_matches: [] },
  ],
  search_multi_source: [
    { query: 'offline', source: 'libgen' },
    'search_multi_source',
    { books: [] },
  ],
};

// Literal expectations describe the Python boundary, independently of the
// schemas/facade under test. JSON normalization omits undefined year filters.
const expectedBridgeArgs = {
  search_books: {
    query: 'offline', exact: false, from_year: null, to_year: null,
    languages: [], extensions: [], content_types: [], count: 10,
  },
  full_text_search: {
    query: 'offline phrase', exact: false, phrase: true, words: false,
    languages: [], extensions: [], content_types: [], count: 10,
  },
  get_download_history: { count: 1 },
  get_download_limits: {},
  get_recent_books: { count: 1 },
  download_book_to_file: {
    book_details: { id: '1', hash: 'offline' }, output_dir: './downloads',
    process_for_rag: false, processed_output_format: 'txt',
  },
  process_document_for_rag: { file_path_str: '/offline/book.pdf', output_format: 'txt' },
  get_book_metadata: { book_id: '1', book_hash: 'offline' },
  search_by_term: { term: 'offline', languages: [], extensions: [], limit: 25 },
  search_by_author: {
    author: 'offline', exact: false, languages: [], extensions: [], limit: 25,
  },
  fetch_booklist: { booklist_id: '1', booklist_hash: 'offline', topic: 'offline', page: 1 },
  search_advanced: { query: 'offline', exact: false, count: 10 },
  search_multi_source: { query: 'offline', source: 'libgen', count: 10 },
};

// These are facade-supplied options, not the runner's eventual default budget.
// Pin literal labels and budgets independently of the implementation constants.
const expectedBridgeOptions = {
  search_books: { timeoutMs: undefined, label: 'python_bridge.search' },
  full_text_search: { timeoutMs: undefined, label: 'python_bridge.full_text_search' },
  get_download_history: { timeoutMs: undefined, label: 'python_bridge.get_download_history' },
  get_download_limits: { timeoutMs: undefined, label: 'python_bridge.get_download_limits' },
  get_recent_books: { timeoutMs: undefined, label: 'python_bridge.get_recent_books' },
  download_book_to_file: { timeoutMs: 2400000, label: 'python_bridge.download_book' },
  process_document_for_rag: { timeoutMs: 2400000, label: 'python_bridge.process_document' },
  get_book_metadata: { timeoutMs: undefined, label: 'python_bridge.get_book_metadata_complete' },
  search_by_term: { timeoutMs: undefined, label: 'python_bridge.search_by_term_bridge' },
  search_by_author: { timeoutMs: undefined, label: 'python_bridge.search_by_author_bridge' },
  fetch_booklist: { timeoutMs: undefined, label: 'python_bridge.fetch_booklist_bridge' },
  search_advanced: { timeoutMs: undefined, label: 'python_bridge.search_advanced' },
  search_multi_source: { timeoutMs: undefined, label: 'python_bridge.search_multi_source' },
};

// Independent literal wire contract, not imported from production metadata.
// Recent books shares search annotations today; title prose is not pinned.
const expectedAnnotations = {
  search_books: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  full_text_search: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  get_download_history: { readOnlyHint: true, idempotentHint: false, openWorldHint: true },
  get_download_limits: { readOnlyHint: true, idempotentHint: false, openWorldHint: true },
  get_recent_books: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  download_book_to_file: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
  process_document_for_rag: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  get_book_metadata: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  search_by_term: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  search_by_author: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  fetch_booklist: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  search_advanced: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  search_multi_source: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
};

// Non-default values make dropped optional fields observable even where the
// facade would otherwise reconstruct a missing field's default value.
const forwardingCases = [
  {
    name: 'get_recent_books',
    args: { count: 3, format: 'pdf' },
    // Format is applied by the real facade, not forwarded to Python.
    expectedArgs: { count: 3 },
    payload: { books: [
      { id: 'synthetic-pdf', title: 'Synthetic PDF', extension: 'pdf' },
      { id: 'synthetic-epub', title: 'Synthetic EPUB', extension: 'epub' },
    ] },
    expectedPayload: { books: [
      { id: 'synthetic-pdf', title: 'Synthetic PDF', extension: 'pdf' },
    ] },
  },
  {
    name: 'get_book_metadata',
    args: { bookId: '2', bookHash: 'other', include: ['terms'] },
    // Include selects fields in TypeScript; Python receives only the identity.
    expectedArgs: { book_id: '2', book_hash: 'other' },
    payload: { title: 'Synthetic metadata', terms: ['synthetic term'] },
  },
  {
    name: 'get_download_limits',
    args: { sources: ['libgen'] },
    expectedArgs: { sources: ['libgen'] },
    payload: {
      requested: ['libgen'],
      sources: {
        libgen: {
          available: true, routes: ['search', 'download'],
          daily_limit: { state: 'none', total: null, used: null, remaining: null, note: 'synthetic unlimited source' },
          note: 'synthetic unlimited source', details: {},
        },
      },
    },
  },
  {
    name: 'search_books',
    args: {
      query: 'filtered books', exact: true, fromYear: 1900, toYear: 1950,
      languages: ['english'], extensions: ['pdf'], content_types: ['book'], count: 3,
    },
    expectedArgs: {
      query: 'filtered books', exact: true, from_year: 1900, to_year: 1950,
      languages: ['english'], extensions: ['pdf'], content_types: ['book'], count: 3,
    },
  },
  {
    name: 'full_text_search',
    args: {
      query: 'filtered phrase', exact: true, phrase: false, words: true,
      languages: ['french'], extensions: ['epub'], content_types: ['article'], count: 4,
    },
    expectedArgs: {
      query: 'filtered phrase', exact: true, phrase: false, words: true,
      languages: ['french'], extensions: ['epub'], content_types: ['article'], count: 4,
    },
  },
  {
    name: 'download_book_to_file',
    args: {
      bookDetails: { id: '2', hash: 'other', title: 'Preserved details' },
      outputDir: '/offline/downloads', process_for_rag: true,
      processed_output_format: 'markdown',
    },
    expectedArgs: {
      book_details: { id: '2', hash: 'other', title: 'Preserved details' },
      output_dir: '/offline/downloads', process_for_rag: true,
      processed_output_format: 'markdown',
    },
    payload: { file_path: '/offline/book.pdf', provenance: syntheticProvenance, processed_file_path: '/offline/book.md' },
  },
  {
    name: 'process_document_for_rag',
    args: { file_path: '/offline/other.pdf', output_format: 'markdown' },
    expectedArgs: { file_path_str: '/offline/other.pdf', output_format: 'markdown' },
  },
  {
    name: 'search_by_term',
    args: {
      term: 'filtered term', yearFrom: 1800, yearTo: 1899,
      languages: ['german'], extensions: ['pdf'], count: 2,
    },
    expectedArgs: {
      term: 'filtered term', year_from: 1800, year_to: 1899,
      languages: ['german'], extensions: ['pdf'], limit: 2,
    },
  },
  {
    name: 'search_by_author',
    args: {
      author: 'Filtered Author', exact: true, yearFrom: 1980, yearTo: 2020,
      languages: ['english'], extensions: ['epub'], count: 3,
    },
    expectedArgs: {
      author: 'Filtered Author', exact: true, year_from: 1980, year_to: 2020,
      languages: ['english'], extensions: ['epub'], limit: 3,
    },
  },
  {
    name: 'fetch_booklist',
    args: { booklistId: '2', booklistHash: 'other', topic: 'other topic', page: 2 },
    expectedArgs: { booklist_id: '2', booklist_hash: 'other', topic: 'other topic', page: 2 },
  },
  {
    name: 'search_advanced',
    args: { query: 'filtered advanced', exact: true, yearFrom: 2001, yearTo: 2021, count: 4 },
    expectedArgs: { query: 'filtered advanced', exact: true, from_year: 2001, to_year: 2021, count: 4 },
  },
  {
    name: 'search_multi_source',
    args: { query: 'other sources', source: 'auto', count: 5 },
    expectedArgs: { query: 'other sources', source: 'auto', count: 5 },
  },
];

// Measured at ec91a32 with SDK 1.30.0; each tool gets 10% headroom.
const characterBaselines = {
  search_books: 1588,
  full_text_search: 1517,
  get_download_history: 542,
  // #157 adds the optional sources schema; measured SDK serialization is 764 chars.
  get_download_limits: 764,
  get_recent_books: 585,
  download_book_to_file: 1097,
  process_document_for_rag: 733,
  get_book_metadata: 1323,
  search_by_term: 1265,
  search_by_author: 1346,
  fetch_booklist: 848,
  search_advanced: 1092,
  search_multi_source: 993,
};

let client;
let server;
let advertised;
async function connectServer(apiOverrides) {
  await client?.close();
  await server?.close();
  jest.resetModules();
  jest.unstable_unmockModule('../lib/zlibrary-api.js');
  if (apiOverrides) {
    const actualApi = await import('../dist/lib/zlibrary-api.js');
    jest.unstable_mockModule('../lib/zlibrary-api.js', () => ({ ...actualApi, ...apiOverrides }));
  }
  const { createServer } = await import('../dist/index.js');
  expect(typeof createServer).toBe('function');
  server = createServer();
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  client = new Client({ name: 'surface-oracle', version: '1.0.0' });
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  advertised = await client.listTools();
}
beforeEach(async () => {
  runBridge.mockReset();
  await connectServer();
});
afterEach(async () => {
  await client?.close();
  await server?.close();
  client = server = undefined;
});

test('tools/list has a fixture for every advertised tool and bounded schema cost', () => {
  // Catches new unexercised tools and schema/description bloat. Baseline from
  // #196: 13,346 total characters at f779ee3; ±10% is the accepted envelope.
  expect(advertised.tools.map((tool) => tool.name).sort()).toEqual(Object.keys(cases).sort());
  const total = JSON.stringify(advertised).length;
  expect(total).toBeGreaterThanOrEqual(Math.floor(13346 * 0.9));
  expect(total).toBeLessThanOrEqual(Math.ceil(13346 * 1.1));
  for (const tool of advertised.tools) {
    expect(JSON.stringify(tool).length).toBeLessThanOrEqual(
      Math.ceil(characterBaselines[tool.name] * 1.1),
    );
  }
});

test('tools/list advertises the literal annotation contract for every tool', () => {
  expect(advertised.tools.map((tool) => tool.name).sort()).toEqual(Object.keys(expectedAnnotations).sort());
  for (const tool of advertised.tools) {
    expect(tool.annotations).toMatchObject(expectedAnnotations[tool.name]);
  }
});

test('tools/list titles are nonempty human-readable strings for every tool', () => {
  for (const tool of advertised.tools) {
    expect(tool.annotations?.title).toBeDefined();
    expect(typeof tool.annotations.title).toBe('string');
    expect(tool.annotations.title.trim().length).toBeGreaterThan(0);
    expect(tool.annotations.title).not.toContain('_');
    expect(tool.annotations.title).toMatch(/\p{L}/u);
  }
});

test('tools/list exposes descriptive names and object input schemas for every tool', () => {
  for (const tool of advertised.tools) {
    expect(tool.name).toMatch(/^[a-z][a-z0-9_]*$/);
    expect(tool.name).not.toContain('__');
    expect(typeof tool.description).toBe('string');
    expect(tool.description.length).toBeGreaterThan(10);
    expect(tool.description.toLowerCase()).not.toContain('todo');
    expect(tool.description.toLowerCase()).not.toContain('placeholder');
    expect(tool.inputSchema.type).toBe('object');
    expect(tool.inputSchema.properties).toBeDefined();
  }
});

test('tools/list retains required search and download inputs and search filters', () => {
  const search = advertised.tools.find((tool) => tool.name === 'search_books');
  const download = advertised.tools.find((tool) => tool.name === 'download_book_to_file');
  expect(search.inputSchema.properties.query).toBeDefined();
  expect(search.inputSchema.required).toContain('query');
  expect(download.inputSchema.properties.bookDetails).toBeDefined();
  expect(download.inputSchema.required).toContain('bookDetails');
  expect(search.inputSchema.properties.languages).toBeDefined();
  expect(search.inputSchema.properties.extensions).toBeDefined();
  expect(search.inputSchema.properties.fromYear).toBeDefined();
});

test('every advertised input property is explicitly set by a tool fixture', () => {
  const missing = [];
  for (const tool of advertised.tools) {
    const fixtures = [
      cases[tool.name]?.[0],
      ...forwardingCases.filter((fixture) => fixture.name === tool.name).map((fixture) => fixture.args),
    ].filter(Boolean);
    for (const key of Object.keys(tool.inputSchema.properties ?? {})) {
      if (!fixtures.some((args) => Object.prototype.hasOwnProperty.call(args, key) && args[key] !== undefined)) {
        missing.push(`${tool.name}.${key}`);
      }
    }
  }
  expect(missing).toEqual([]);
});

// A deadline that also clears its timer when the operation wins. Promise.race
// attaches rejection handlers immediately, including to a late losing promise.
async function within(promise, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} exceeded 3000 ms`)), 3000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

test('cancelling a pending MCP request aborts the very signal delivered to the runner', async () => {
  const request = new AbortController();
  let markStarted;
  let releaseRunner;
  let runnerPromise;
  let runnerSettled = false;
  let runnerSignal;
  let onAbort;
  const started = new Promise((resolve) => { markStarted = resolve; });
  const pending = new Promise((resolve) => { releaseRunner = resolve; });
  runBridge.mockImplementationOnce((_script, _options, runOptions) => {
    runnerSignal = runOptions.signal;
    runnerPromise = pending.then(() => {
      runnerSettled = true;
      return [JSON.stringify({ content: [{
        type: 'text', text: JSON.stringify(cases.download_book_to_file[2]),
      }] })];
    });
    markStarted();
    return runnerPromise;
  });
  // Attach both handlers before any abort; a rejection is an expected outcome,
  // not an unhandled promise. The SDK deadline is separate from our wait bounds.
  const outcome = client.callTool(
    { name: 'download_book_to_file', arguments: cases.download_book_to_file[0] },
    CallToolResultSchema,
    { signal: request.signal, timeout: 5000 },
  ).then(
    (value) => ({ status: 'fulfilled', value }),
    (reason) => ({ status: 'rejected', reason }),
  );
  let primaryFailed = false;
  let primaryError;
  const cleanupErrors = [];
  try {
    await within(started, 'runner start');
    expect(runBridge).toHaveBeenCalledTimes(1);
    expect(runBridge.mock.calls[0][1].args[0]).toBe('download_book');
    expect(JSON.parse(runBridge.mock.calls[0][1].args[1])).toEqual(expectedBridgeArgs.download_book_to_file);
    expect(runBridge.mock.calls[0][2]).toEqual({
      timeoutMs: 2400000, label: 'python_bridge.download_book', signal: expect.any(AbortSignal),
    });
    expect(runnerSignal.aborted).toBe(false);
    expect(runnerSettled).toBe(false);
    const aborted = new Promise((resolve) => {
      onAbort = resolve;
      runnerSignal.addEventListener('abort', onAbort, { once: true });
    });
    request.abort();
    await within(aborted, 'runner signal cancellation');
    expect(runnerSignal.aborted).toBe(true);
    expect(runnerSettled).toBe(false);
    expect((await within(outcome, 'cancelled client request')).status).toBe('rejected');
  } catch (error) {
    primaryFailed = true;
    primaryError = error;
  } finally {
    // Attempt each cleanup independently, without replacing a primary error.
    // Release even a mutant's unrelated signal so the mock cannot stay pending.
    for (const cleanup of [
      () => { if (onAbort) runnerSignal.removeEventListener('abort', onAbort); },
      () => request.abort(),
      () => releaseRunner(),
    ]) {
      try {
        cleanup();
      } catch (error) {
        cleanupErrors.push(error);
      }
    }
    try {
      await within(Promise.allSettled([outcome, runnerPromise]), 'request and runner drain');
    } catch (error) {
      cleanupErrors.push(error);
    }
    try {
      // Schedule both closes before waiting. Even a synchronous throw from one
      // close must not prevent the other transport's close from being attempted.
      const closed = await within(Promise.allSettled([
        Promise.resolve().then(() => client.close()),
        Promise.resolve().then(() => server.close()),
      ]), 'transport close');
      expect(closed.map((result) => result.status)).toEqual(['fulfilled', 'fulfilled']);
    } catch (error) {
      cleanupErrors.push(error);
    } finally {
      // Do not let afterEach retry a failed or non-settling close without a bound.
      client = server = undefined;
    }
  }
  if (primaryFailed) throw primaryError;
  if (cleanupErrors.length > 0) throw new AggregateError(cleanupErrors, 'Cancellation test cleanup failed');
}, 20000);

async function callAdvertised(name, payloadOverride, fixtureOverride) {
  // A missing or miswired registration must not pass merely because an error
  // envelope is schema-valid: require successful dispatch to the real facade.
  expect(advertised.tools.some((tool) => tool.name === name)).toBe(true);
  const [defaultArgs, operation, defaultPayload] = cases[name];
  const args = fixtureOverride?.args ?? defaultArgs;
  const expectedArgs = fixtureOverride?.expectedArgs ?? expectedBridgeArgs[name];
  const payload = payloadOverride ?? defaultPayload;
  const expectedPayload = fixtureOverride?.expectedPayload ?? payload;
  runBridge.mockResolvedValue([
    JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(payload) }] }),
  ]);
  const request = new AbortController();
  const result = await client.callTool(
    { name, arguments: args }, CallToolResultSchema, { signal: request.signal },
  );
  expect(CallToolResultSchema.safeParse(result).success).toBe(true);
  expect(result.isError).not.toBe(true);
  expect(result.content).toEqual([{ type: 'text', text: JSON.stringify(expectedPayload) }]);
  if (name === 'get_download_limits') {
    expect(result.structuredContent.sources.libgen.daily_limit.state).toBe('none');
    if (args.sources) {
      expect(result.structuredContent).toEqual(payload);
      expect(result.structuredContent.requested).toEqual(['libgen']);
      expect(Object.keys(result.structuredContent.sources)).toEqual(['libgen']);
    } else {
      expect(result.structuredContent).toEqual(syntheticLimits);
      expect(result.structuredContent.sources.annas_archive.daily_limit.state).toBe('not_applicable');
      expect(result.structuredContent.sources.zlibrary.daily_limit).toEqual({
        state: 'known', total: 10, used: 2, remaining: 8, note: '',
      });
    }
  }
  expect(runBridge).toHaveBeenCalledTimes(1);
  expect(runBridge.mock.calls[0][1].args[0]).toBe(operation);
  expect(JSON.parse(runBridge.mock.calls[0][1].args[1])).toEqual(expectedArgs);
  // The SDK creates a server-side request signal across the transport. It must
  // reach the real runner options; it is not the client's object identity.
  expect(runBridge.mock.calls[0][2]).toEqual(
    { ...expectedBridgeOptions[name], signal: expect.any(AbortSignal) },
  );
  expect(runBridge.mock.calls[0][2].signal.aborted).toBe(false);
}

for (const name of Object.keys(cases)) {
  if (name !== 'get_recent_books' && name !== 'get_download_history') {
    test(`${name} dispatches and returns an SDK-valid result`, () => callAdvertised(name));
  }
}

for (const fixture of forwardingCases) {
  test(`${fixture.name} forwards non-default fields`, () =>
    callAdvertised(fixture.name, fixture.payload, fixture));
}

test('get_recent_books registration reaches the intended handler exactly once', async () => {
  // Observe the real API export. A miswired/removed production
  // registration must fail this ordinary test, never be inverted by .failing.
  const actualApi = await import('../dist/lib/zlibrary-api.js');
  const recentProbe = jest.fn(actualApi.getRecentBooks);
  await connectServer({ getRecentBooks: recentProbe });
  await callAdvertised('get_recent_books');
  expect(recentProbe).toHaveBeenCalledTimes(1);
  expect(recentProbe).toHaveBeenCalledWith({ count: 1 }, { signal: expect.any(AbortSignal) });
});

test('get_download_history dispatches exactly once to the correct operation', () =>
  // An object avoids the independent #197 array-result defect. The real facade
  // still has to reach the runner with the history operation, not another one.
  callAdvertised('get_download_history', { books: [] }));

// https://github.com/rookslog/zlibrary-mcp/issues/171 — repaired here. Keep the
// ordinary real-facade result check as well as the independent wiring check.
test('get_recent_books dispatches and returns an SDK-valid result', () =>
  callAdvertised('get_recent_books'));

// https://github.com/rookslog/zlibrary-mcp/issues/197 — repaired here. Arrays
// must now round-trip successfully, with ordinary dispatch checked separately.
test('get_download_history dispatches and returns an SDK-valid result', () =>
  callAdvertised('get_download_history'));

// Mutation caught: publishing scalar/null/array structuredContent causes an
// SDK -32602 rejection before the client can see a valid tool response.
for (const payload of [[], null, false, 0, '', 'backend string']) {
  test(`history round-trip is schema-valid for ${JSON.stringify(payload)}`, async () => {
    runBridge.mockResolvedValueOnce([
      JSON.stringify({
        content: [{ type: 'text', text: JSON.stringify(payload) }],
      }),
    ]);
    const result = await client.callTool({ name: 'get_download_history', arguments: { count: 1 } });
    expect(CallToolResultSchema.safeParse(result).success).toBe(true);
    if (Array.isArray(payload)) {
      expect(result.isError).not.toBe(true);
      expect(result.content).toEqual([{ type: 'text', text: JSON.stringify(payload) }]);
      expect(result.structuredContent).toEqual({ items: payload });
    } else {
      expect(result.isError).toBe(true);
      expect(result.content[0].type).toBe('text');
    }
  });
}

test('object result wire bytes match the pre-fix baseline', async () => {
  const payload = { books: [{ id: '1', title: 'Offline' }], nextPage: null };
  runBridge.mockResolvedValueOnce([
    JSON.stringify({
      content: [{ type: 'text', text: JSON.stringify(payload) }],
    }),
  ]);
  const result = await client.callTool({ name: 'get_download_history', arguments: {} });
  // Literal captured/checked on the #196 base; no implementation helper
  // computes the expectation. This same assertion must pass before and after.
  expect(JSON.stringify(result)).toBe(
    '{"content":[{"type":"text","text":"{\\"books\\":[{\\"id\\":\\"1\\",\\"title\\":\\"Offline\\"}],\\"nextPage\\":null}"}],"structuredContent":{"books":[{"id":"1","title":"Offline"}],"nextPage":null}}',
  );
});
