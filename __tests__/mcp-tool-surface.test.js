import { jest, test, expect, beforeEach, afterEach } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { AjvJsonSchemaValidator } from '@modelcontextprotocol/sdk/validation/ajv';
import { ERROR_CODES, ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';
const run = jest.fn();
jest.unstable_mockModule('../lib/venv-manager.js', () => ({ getManagedPythonPath: async () => '/offline/python' }));
jest.unstable_mockModule('../lib/python-runner.js', () => ({ runPythonBridge: run, LONG_BRIDGE_TIMEOUT_MS: 2400000 }));
const { createServer } = await import('../dist/index.js');
let server, client, previous;
const book = { id: '1', hash: 'abc', title: 'Book', author: 'Author', extension: 'pdf', source: 'zlibrary' };
const encode = value => [JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(value) }] })];
beforeEach(async () => {
  previous = { ...process.env };
  process.env.ZLIBRARY_SESSION_MODE = 'per-call';
  process.env.ZLIBRARY_EMAIL = 'offline@example.invalid'; process.env.ZLIBRARY_PASSWORD = 'offline-password';
  process.env.RETRY_MAX_RETRIES = '3';
  delete process.env.ANNAS_BROWSER_ENABLED;
  run.mockReset();
  server = createServer(); client = new Client({ name: 'boundary', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
});
afterEach(async () => { await client.close(); await server.close(); process.env = previous; });
const call = async (name, args = {}) => (await client.callTool({ name, arguments: args })).structuredContent;
test('tools/list stays under 6600 characters with success/error output schemas when the browser is enabled', async () => {
  await client.close(); await server.close();
  process.env.ANNAS_BROWSER_ENABLED = 'true';
  server = createServer(); client = new Client({ name: 'browser-wire', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
  const list = await client.listTools();
  expect(JSON.stringify(list).length).toBeLessThan(6600);
  expect(JSON.stringify(list).length).toBe(6551);
  expect(ERROR_CODES).toHaveLength(19);
  expect(list.tools.map(t => t.name)).toEqual(['search', 'get_book', 'download', 'get_limits', 'get_history']);
  for (const t of list.tools) expect(t.outputSchema.oneOf).toHaveLength(2);
});
test.each(['false', 'true'])('tools/list bytes and Anna description with browser flag %s', async (flag) => {
  await client.close(); await server.close();
  process.env.ANNAS_BROWSER_ENABLED = flag;
  server = createServer(); client = new Client({ name: 'flag-wire', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(a), server.connect(b)]);
  const list = await client.listTools();
  const wire = JSON.stringify(list);
  expect(list.tools.find((t) => t.name === 'search').description).toContain(
    flag === 'true'
      ? 'annas searches without a key; downloads need a key or the opted-in visible browser (ANNAS_BROWSER_ENABLED)'
      : 'annas searches without a key, downloads with one.',
  );
  expect(Buffer.byteLength(wire, 'utf8')).toBe(flag === 'true' ? 6551 : 6493);
  console.log(`tools/list flag=${flag} characters=${wire.length} bytes=${Buffer.byteLength(wire, 'utf8')}`);
});
test('tools/list retains current annotations, descriptions, schemas and server identity on the wire', async () => {
  expect(client.getServerVersion()).toEqual({
    name: 'book-acquisition',
    version: expect.any(String),
  });
  expect(client.getServerVersion().version.length).toBeGreaterThan(0);
  expect(client.getServerCapabilities()).toEqual({ tools: {} });
  const { tools } = await client.listTools();
  const annotations = {
    search: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
    get_book: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
    download: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
    get_limits: { readOnlyHint: true, idempotentHint: false, openWorldHint: true },
    get_history: { readOnlyHint: true, idempotentHint: false, openWorldHint: true },
  };
  const validator = new AjvJsonSchemaValidator();
  for (const tool of tools) {
    expect(tool.name).toMatch(/^[a-z][a-z0-9_]*$/);
    expect(tool.name).not.toContain('__');
    expect(tool.description.length).toBeGreaterThan(10);
    expect(tool.description.toLowerCase()).not.toMatch(/todo|placeholder/);
    expect(tool.annotations).toEqual(annotations[tool.name]);
    expect(tool.inputSchema.type).toBe('object');
    expect(tool.inputSchema.properties).toEqual(expect.any(Object));
    expect(() => validator.getValidator(tool.inputSchema)).not.toThrow();
    expect(() => validator.getValidator(tool.outputSchema)).not.toThrow();
  }
  expect(tools.find((t) => t.name === 'search').inputSchema.properties).toEqual(
    expect.objectContaining({
      query: expect.any(Object),
      languages: expect.any(Object),
      formats: expect.any(Object),
      year_from: expect.any(Object),
    }),
  );
  for (const name of ['get_book', 'download']) {
    expect(tools.find((t) => t.name === name).inputSchema.required).toContain('ref');
  }
});

test.each([
  ['search', { query: 'Book', sources: ['zlibrary'], limit: 0 }],
  ['get_book', {}],
  ['download', {}],
  ['get_limits', { mode: 'invalid-mode' }],
  ['get_history', { limit: 'not-a-number' }],
])('%s enforces its advertised input schema before bridge work', async (name, args) => {
  const { tools } = await client.listTools();
  const validate = new AjvJsonSchemaValidator().getValidator(
    tools.find((t) => t.name === name).inputSchema,
  );
  expect(validate(args).valid).toBe(false);
  const result = await client.callTool({ name, arguments: args });
  expect(result.isError).toBe(true);
  expect(result.structuredContent.error).toMatchObject({
    code: 'invalid_argument',
    operation: name,
  });
  expect(ErrorEnvelopeSchema.safeParse(result.structuredContent).success).toBe(true);
  expect(JSON.parse(result.content[0].text)).toEqual(result.structuredContent);
  expect(run).not.toHaveBeenCalled();
});

// Only successful SDK calls enter the coverage ledger; listing an argument in a
// disconnected fixture cannot satisfy the advertised-field guard.
function assertInputCoverage(tools, fixtures) {
  for (const tool of tools) {
    const covered = new Set(
      fixtures.filter((f) => f.name === tool.name).flatMap((f) => Object.keys(f.args)),
    );
    const missing = Object.keys(tool.inputSchema.properties).filter((key) => !covered.has(key));
    expect({ tool: tool.name, missing }).toEqual({ tool: tool.name, missing: [] });
  }
}

function assertRunnerOptions(options, operation, timeoutMs) {
  expect(Object.keys(options).sort()).toEqual([
    'label', 'onSession', 'onSessionNeeded', 'sessionInput', 'signal', 'timeoutMs',
  ]);
  expect(options).toEqual({
    timeoutMs,
    signal: expect.any(AbortSignal),
    label: `python_bridge.${operation}`,
    sessionInput: undefined,
    onSession: undefined,
    onSessionNeeded: undefined,
  });
}

test('every advertised input field is exercised through SDK calls with literal bridge payloads and exact options', async () => {
  const downloadReply = {
    path: '/offline/book.pdf',
    bytes: 12,
    verified: 'sha256',
    reused: false,
    transfer_skipped: false,
    record: '/offline/book.pdf.acq.json',
  };
  run.mockImplementation(async (_script, options) => {
    switch (options.args[0]) {
      case 'search':
        return encode({ books: [book] });
      case 'get_book_metadata_complete':
        return encode({ description: 'Details' });
      case 'acquire_book':
        return encode(downloadReply);
      case 'get_download_limits':
        return encode({ daily_limit: 10, downloads_today: 3, daily_remaining: 7 });
      case 'get_download_history':
        return encode([book]);
      default:
        throw new Error(`Unplanned fixture operation: ${options.args[0]}`);
    }
  });
  const fixtures = [];
  const exercise = async (name, args, operation, payload, timeoutMs) => {
    const before = run.mock.calls.length;
    const reply = await client.callTool({ name, arguments: args });
    expect(reply.isError).not.toBe(true);
    expect(JSON.parse(reply.content[0].text)).toEqual(reply.structuredContent);
    expect(run.mock.calls.length).toBe(before + 1);
    const [, bridge, options] = run.mock.calls.at(-1);
    expect(bridge.args[0]).toBe(operation);
    expect(JSON.parse(bridge.args[1])).toEqual(payload);
    assertRunnerOptions(options, operation, timeoutMs);
    fixtures.push({ name, args });
    return reply.structuredContent;
  };
  const searchArgs = {
    query: 'title',
    author: 'Writer',
    sources: ['zlibrary'],
    exact: true,
    year_from: 1900,
    year_to: 2000,
    languages: ['english'],
    formats: ['pdf'],
    limit: 1,
  };
  const searchPayload = {
    query: 'title',
    author: 'Writer',
    exact: true,
    from_year: 1900,
    to_year: 2000,
    languages: ['english'],
    extensions: ['pdf'],
    count: 1,
    page: 1,
  };
  const first = await exercise('search', searchArgs, 'search', searchPayload);
  expect(first.next_cursor).toEqual(expect.any(String));
  await exercise('search', { cursor: first.next_cursor }, 'search', { ...searchPayload, page: 2 });
  const ref = first.rows[0].ref;
  const details = await exercise('get_book', { ref }, 'get_book_metadata_complete', {
    book_id: '1',
    book_hash: 'abc',
  });
  expect(details.description).toBe('Details');
  const receipt = await exercise(
    'download',
    { ref, output_dir: '/offline', filename: 'chosen.epub' },
    'acquire_book',
    {
      book_details: book,
      output_dir: '/offline',
      filename: 'chosen.epub',
      request: { ref, query: 'title Writer' },
    },
    2400000,
  );
  expect(receipt).toEqual(downloadReply);
  const limits = await exercise(
    'get_limits',
    { mode: 'live', sources: ['zlibrary'] },
    'get_download_limits',
    {},
  );
  expect(limits.sources.zlibrary.limit.remaining).toBe(7);
  const history = await exercise(
    'get_history',
    { source: 'zlibrary', limit: 1 },
    'get_download_history',
    { count: 1, page: 1 },
  );
  expect(history.next_cursor).toEqual(expect.any(String));
  await exercise('get_history', { cursor: history.next_cursor }, 'get_download_history', {
    count: 1,
    page: 2,
  });
  const { tools } = await client.listTools();
  assertInputCoverage(tools, fixtures);

  // A schema addition without an executed fixture must trip the same guard.
  const addedField = structuredClone(tools);
  addedField.find((t) => t.name === 'search').inputSchema.properties.unexercised = {
    type: 'string',
  };
  expect(() => assertInputCoverage(addedField, fixtures)).toThrow();
});

test('cancelling an in-flight SDK request aborts that runner signal and leaves the next request live', async () => {
  const started = Promise.withResolvers();
  const bridge = Promise.withResolvers();
  run.mockImplementationOnce((_script, _options, options) => {
    started.resolve(options.signal);
    return bridge.promise;
  });
  const controller = new AbortController();
  const pending = client.callTool(
    { name: 'search', arguments: { query: 'cancel me', sources: ['zlibrary'] } },
    undefined,
    { signal: controller.signal },
  );
  const rejected = expect(pending).rejects.toThrow(/cancel this request/);
  try {
    const signal = await started.promise;
    expect(signal.aborted).toBe(false);
    controller.abort(new Error('cancel this request'));
    await rejected;
    expect(signal.aborted).toBe(true);
  } finally {
    controller.abort();
    bridge.resolve(encode({ books: [] }));
    await rejected;
  }
  run.mockResolvedValue(encode({ books: [book] }));
  expect(
    (await call('search', { query: 'next request', sources: ['zlibrary'] })).rows,
  ).toHaveLength(1);
  expect(run.mock.calls.at(-1)[2].signal.aborted).toBe(false);
});
test('search forwards all normalized filters, author, limit and page through the real facade', async () => {
  run.mockResolvedValue(encode({ books: [book] }));
  const result = await call('search', { query: 'title', author: 'Writer', sources: ['zlibrary'], exact: true, year_from: 1900, year_to: 2000, languages: ['english'], formats: ['pdf'], limit: 3 });
  expect(JSON.parse(run.mock.calls[0][1].args[1])).toEqual({ query: 'title', author: 'Writer', exact: true, from_year: 1900, to_year: 2000, languages: ['english'], extensions: ['pdf'], count: 3, page: 1 });
  expect(result.rows[0]).toMatchObject({ title: 'Book', source: 'zlibrary' });
  expect(result.rows[0].id).toBeUndefined();
});
test('get_book and download rehydrate only held refs, and fresh downloads use the guarded bridge path', async () => {
  run.mockImplementation(async (_script, options) => encode(options.args[0] === 'search' ? { books: [book] } : options.args[0] === 'get_book_metadata_complete' ? { description: 'Details' } : { path: '/offline/book.pdf', bytes: 12, verified: 'sha256', reused: false, transfer_skipped: false, record: '/offline/book.pdf.acq.json' }));
  const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
  expect((await call('get_book', { ref })).description).toBe('Details');
  const download = await call('download', { ref, output_dir: '/offline', filename: 'chosen.epub' });
  expect(download.verified).toBe('sha256');
  expect(run.mock.calls[2][1].args[0]).toBe('acquire_book');
  expect(JSON.parse(run.mock.calls[2][1].args[1])).toMatchObject({ book_details: book, output_dir: '/offline', filename: 'chosen.epub', request: { ref, query: 'Book' } });
});
test('history pagination forwards the page and replay does not consume it', async () => {
  run.mockResolvedValue(encode([book]));
  const first = await call('get_history', { limit: 1 });
  await call('get_history', { cursor: first.next_cursor });
  await call('get_history', { cursor: first.next_cursor });
  expect(run.mock.calls.map(c => JSON.parse(c[1].args[1]))).toEqual([{ count: 1, page: 1 }, { count: 1, page: 2 }, { count: 1, page: 2 }]);
});
test('local mode makes no bridge call, live mode translates quota fields', async () => {
  await call('get_limits'); expect(run).not.toHaveBeenCalled();
  run.mockResolvedValue(encode({ daily_limit: 10, downloads_today: 3, daily_remaining: 7 }));
  expect((await call('get_limits', { mode: 'live', sources: ['zlibrary'] })).sources.zlibrary.limit).toMatchObject({ per_day: 10, used_today: 3, remaining: 7 });
});
test('download bridge failure never automatically retries or exposes stderr secrets', async () => {
  run.mockResolvedValueOnce(encode({ books: [book] }));
  const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
  run.mockRejectedValue(Object.assign(new Error('provider SECRET'), { stderr: JSON.stringify({ error: 'SECRET', details: { provider: 'zlibrary', reason: 'read_timeout' } }) }));
  const result = await call('download', { ref });
  expect(result.error.retry).toBe('may_spend_quota');
  expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
  expect(JSON.stringify(result)).not.toContain('SECRET');
  expect(run).toHaveBeenCalledTimes(2); // one search, one transfer attempt, no automatic quota spend
});

test('error envelopes are validated independently of SDK isError bypass', async () => {
  const validator = new AjvJsonSchemaValidator();
  const list = await client.listTools();
  const result = await client.callTool({ name: 'search', arguments: { limit: 0 } });
  const validate = validator.getValidator(list.tools.find(t => t.name === 'search').outputSchema);
  expect(validate(result.structuredContent).valid).toBe(true);
  expect(validate({ error: { code: 'invalid_argument' } }).valid).toBe(false);
  expect(ErrorEnvelopeSchema.safeParse({ error: { ...result.structuredContent.error, retry: 'probably' } }).success).toBe(false);
});

test('get_book subprocess failure retains selected source and sanitized timeout evidence', async () => {
  process.env.RETRY_MAX_RETRIES = '0';
  run.mockResolvedValueOnce(encode({ books: [book] }));
  const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
  run.mockRejectedValue(Object.assign(new Error('provider SECRET https://private.invalid'), {
    stderr: JSON.stringify({ error: 'SECRET', type: 'ReadTimeout', details: { reason: 'read_timeout' } }),
  }));
  const reply = await client.callTool({ name: 'get_book', arguments: { ref } });
  expect(reply.isError).toBe(true);
  expect(reply.structuredContent.error).toMatchObject({
    source: 'zlibrary', operation: 'get_book', code: 'provider_timeout', retry: 'safe', detail: { reason: 'read_timeout' },
  });
  expect(ErrorEnvelopeSchema.safeParse(reply.structuredContent).success).toBe(true);
  expect(JSON.stringify(reply)).not.toMatch(/SECRET|private\.invalid/);
  expect(run.mock.calls.at(-1)[1].args[0]).toBe('get_book_metadata_complete');
});

test.each(['search', 'get_history'])('%s rejects empty cursor through RefStore before configuration or bridge work', async name => {
  run.mockResolvedValue(encode([]));
  const configured = jest.spyOn(server.tools, 'configured');
  const resolve = jest.spyOn(server.tools.refs, 'get');
  try {
    const reply = await client.callTool({ name, arguments: { cursor: '' } });
    expect(run.mock.calls.length).toBe(0);
    expect(configured).not.toHaveBeenCalled();
    expect(resolve).toHaveBeenCalledWith('', 'c');
    expect(reply.isError).toBe(true);
    expect(reply.structuredContent.error).toMatchObject({
      code: 'invalid_argument', operation: name, message: 'Reference has the wrong kind.', retry: 'no',
    });
    expect(ErrorEnvelopeSchema.safeParse(reply.structuredContent).success).toBe(true);
    expect(JSON.parse(reply.content[0].text)).toEqual(reply.structuredContent);
  } finally {
    configured.mockRestore(); resolve.mockRestore();
  }
});

test.each(['search', 'get_history'])('%s still refuses unknown nonempty cursor without configuration or bridge work', async name => {
  const configured = jest.spyOn(server.tools, 'configured');
  try {
    const result = await call(name, { cursor: 'cmissing' });
    expect(result.error).toMatchObject({
      code: 'ref_unavailable', operation: name, detail: { kind: 'cursor', reason: 'unknown' },
    });
    expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
    expect(configured).not.toHaveBeenCalled();
    expect(run.mock.calls.length).toBe(0);
  } finally { configured.mockRestore(); }
});

test.each(['search', 'get_history'])('%s still rejects empty or nonempty cursor mixed with other arguments', async name => {
  for (const cursor of ['', 'cmissing']) {
    const result = await call(name, { cursor, limit: 2 });
    expect(result.error).toMatchObject({ code: 'invalid_argument', message: 'A cursor must be passed by itself.' });
  }
  expect(run.mock.calls.length).toBe(0);
});

test.each(['search', 'get_history'])('%s retains valid cursor-only page forwarding and exact lost-reply replay', async name => {
  run.mockImplementation(async (_script, options) => {
    const { page } = JSON.parse(options.args[1]);
    const row = { ...book, id: String(page), title: `Page ${page}` };
    return encode(name === 'search' ? { books: [row] } : [row]);
  });
  const first = await call(name, name === 'search' ? { query: 'Book', sources: ['zlibrary'], limit: 1 } : { limit: 1 });
  expect(first.next_cursor).toBeDefined();
  const lost = await call(name, { cursor: first.next_cursor });
  const replay = await call(name, { cursor: first.next_cursor });
  expect(lost.rows[0].title).toBe('Page 2');
  expect(replay).toEqual(lost);
  expect(run.mock.calls.map(c => [c[1].args[0], JSON.parse(c[1].args[1]).page])).toEqual([
    [name === 'search' ? 'search' : 'get_download_history', 1],
    [name === 'search' ? 'search' : 'get_download_history', 2],
    [name === 'search' ? 'search' : 'get_download_history', 2],
  ]);
});

test.each([true, false])('download lease contention issued=%s has bounded retry/estimate semantics and no automatic retry', async issued => {
  let acquisitions = 0;
  run.mockImplementation(async (_script, options) => {
    const operation = options.args[0];
    if (operation === 'get_download_limits') return encode({ daily_limit: 10, downloads_today: 3, daily_remaining: 7 });
    if (operation === 'search') return encode({ books: [book] });
    acquisitions++;
    if (acquisitions === 1) throw Object.assign(new Error('synthetic lease contention'), {
      stderr: JSON.stringify({ error: 'synthetic lease contention', type: 'AcquisitionError', details: { reason: 'breaker_open', request_issued: issued } }),
    });
    return encode({ path: '/offline/book.pdf', bytes: 12, verified: 'sha256', reused: false, transfer_skipped: true, record: '/offline/book.pdf.acq.json' });
  });
  await call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
  const failed = await call('download', { ref });
  expect(failed.error).toMatchObject({ code: 'breaker_open', source: 'zlibrary', retry: issued ? 'may_spend_quota' : 'safe', detail: { reason: 'breaker_open', request_issued: issued } });
  expect(ErrorEnvelopeSchema.safeParse(failed).success).toBe(true);
  expect(acquisitions).toBe(1); // no automatic quota-consuming second transfer
  const explicitRepeat = await call('download', { ref });
  if (issued) expect(explicitRepeat.quota_estimate).toBeUndefined();
  else expect(explicitRepeat.quota_estimate).toMatchObject({ remaining: 7, basis: 'observed' });
  expect(acquisitions).toBe(2);
});

const { classifyFailure } = await import('../dist/lib/tool-errors.js');
const retryReasons = [
  ['dns_failure', 'provider_unreachable'],
  ['connect_timeout', 'provider_unreachable'],
  ['read_timeout', 'provider_timeout'],
  ['http_error', 'provider_error'],
  ['integrity_mismatch', 'integrity_mismatch'],
  ['timeout', 'outcome_unknown'],
];
const quotaSources = [
  ['libgen', 'safe'], ['zlibrary', 'may_spend_quota'], ['annas', 'may_spend_quota'],
  [undefined, 'may_spend_quota'], ['unknown-source', 'may_spend_quota'],
];
test.each(retryReasons.flatMap(([reason, code]) => quotaSources.flatMap(([source, after]) => [
  { reason, code, source, issued: undefined, retry: after },
  { reason, code, source, issued: true, retry: after },
  { reason, code, source, issued: false, retry: 'safe' },
])))('download retry quota contract $source/$reason/issued=$issued', ({ reason, code, source, issued, retry }) => {
  const details = { reason, ...(issued === undefined ? {} : { request_issued: issued }) };
  const failure = classifyFailure({ context: { details } }, 'download', source);
  expect(failure.code).toBe(code);
  expect(failure.info).toMatchObject({ retry });
  expect(failure.info.detail).toEqual(details);
});

test.each(retryReasons)('actual SDK LibGen %s failure is quota-safe without boundary metadata or automatic retry', async (reason, code) => {
  run.mockImplementation(async (_script, options) => {
    if (options.args[0] === 'search_multi_source') return encode({ books: [{ ...book, source: 'libgen', md5: 'a'.repeat(32) }] });
    throw Object.assign(new Error('synthetic source failure'), {
      stderr: JSON.stringify({ error: 'synthetic source failure', type: 'SourceError', details: { provider: 'libgen', reason } }),
    });
  });
  const ref = (await call('search', { query: 'Book', sources: ['libgen'] })).rows[0].ref;
  const failed = await call('download', { ref });
  expect(failed.error).toMatchObject({ source: 'libgen', code, retry: 'safe', detail: { reason } });
  expect(failed.error.detail.request_issued).toBeUndefined();
  expect(ErrorEnvelopeSchema.safeParse(failed).success).toBe(true);
  expect(run.mock.calls.length).toBe(2); // one search, one explicit download; no automatic retry
});

test.each(['invalid_argument', 'quota_exhausted'])('LibGen %s does not become automatically retryable', reason => {
  const failure = classifyFailure({ context: { details: { reason } } }, 'download', 'libgen');
  expect(failure.code).toBe(reason);
  expect(failure.info.retry).toBe('no');
});

test('lazy-v2 actual API/tool corruption is internal/no, issued after handoff, invalidates cached quota and never replays', async () => {
  process.env.ZLIBRARY_SESSION_MODE = 'memory';
  let grants = 0,
    acquisitions = 0,
    logins = 0;
  run.mockImplementation(async (_script, options, ro) => {
    const snapshot = ro.sessionInput;
    if (snapshot?.action === 'login') {
      logins++;
      ro.onSession({
        ...snapshot,
        domain: 'offline.invalid',
        remix_userid: '7',
        remix_userkey: 'original-private-canary',
      });
      return encode({ authenticated: true });
    }
    if (options.args[0] === 'get_download_limits')
      return encode({ daily_limit: 10, downloads_today: 3, daily_remaining: 7 });
    if (options.args[0] === 'search') return encode({ books: [book] });
    acquisitions++;
    if (acquisitions > 1)
      return encode({
        path: '/offline/book.pdf',
        bytes: 12,
        verified: 'sha256',
        reused: false,
        transfer_skipped: true,
        record: '/offline/book.pdf.acq.json',
      });
    return new Promise((_resolve, reject) =>
      ro.onSessionNeeded({
        grant(input) {
          grants++;
          ro.onSession({ ...input, remix_userkey: 'corrupted-private-canary' });
          reject(
            Object.assign(new Error('Invalid private session request'), {
              privateSessionProtocolCorruption: true,
              stderr: JSON.stringify({
                error: 'corrupted-private-canary',
                type: 'ReadTimeout',
                details: { reason: 'read_timeout', request_issued: false },
              }),
            }),
          );
        },
        deny: reject,
      }),
    );
  });
  await call('get_limits', { mode: 'live', sources: ['zlibrary'] });
  const ref = (await call('search', { query: 'Book', sources: ['zlibrary'] })).rows[0].ref;
  const failed = await call('download', { ref });
  expect(failed.error).toMatchObject({
    code: 'internal',
    retry: 'no',
    detail: { request_issued: true },
  });
  expect(acquisitions).toBe(1);
  expect(grants).toBe(1);
  const receipt = await call('download', { ref });
  expect(receipt).not.toHaveProperty('error');
  expect(receipt.transfer_skipped).toBe(true);
  expect(receipt.quota_estimate).toBeUndefined();
  expect(grants).toBe(1);
  expect(logins).toBe(1);
  expect(JSON.stringify(failed)).not.toContain('private-canary');
});
