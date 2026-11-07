import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { ToolFailure } from '../../dist/lib/tool-errors.js';
import { windowFixture } from './window-fixture.js';
export const names = ['libgen', 'zlibrary', 'annas', 'held'];
export const metadata = {
  title: 'Solaris',
  author: 'Lem',
  year: 2001,
  language: 'French',
  format: 'pdf',
  size_mb: 1,
  pages: 100,
  publisher: 'Fixture',
};
export function extensionFixture({
  count = 1,
  groups = false,
  failed,
  delay,
  env = {},
  runtime = {},
} = {}) {
  const events = { search: [], detail: [], acquire: [], aborted: [] };
  const specs = names.map((name) => ({
    name,
    capabilities: () => ({
      configured: true,
      search_available: true,
      download_available: true,
      operations: ['search', 'details', 'download'],
      limit: { kind: 'none' },
    }),
    defaultSearch: () => (name === 'annas' ? { on: false, reason: 'route_off' } : { on: true }),
    md5: (book) => book.digest ?? book.md5,
    searchWindow: async (request, signal) => {
      events.search.push(name);
      signal.addEventListener('abort', () => events.aborted.push([name, signal.reason]), {
        once: true,
      });
      if (delay) return delay(name, request, signal);
      if (name === failed)
        throw new ToolFailure('provider_error', 'Synthetic source failure.', {
          source: name,
          retry: 'safe',
          detail: { reason: 'future_reason' },
        });
      const rows = Array.from({ length: count }, (_, i) => {
        const digest = (groups ? i + 1 : names.indexOf(name) * 50 + i + 1)
          .toString(16)
          .padStart(32, '0');
        const book = {
          id: `${name}-${i}`,
          title: 'Solaris',
          source: name,
          description: 'Synthetic verbose catalogue note. '.repeat(40),
          ...(name === 'held' ? { digest } : { md5: digest }),
        };
        return {
          id: book.id,
          book,
          metadata: { ...metadata, title: `Solaris ${i}` },
          ...(name === 'held' ? {} : { md5: digest }),
        };
      });
      return windowFixture(rows, {}, request);
    },
    details: async (book, options) => {
      events.detail.push([name, book.id, options?.signal]);
      return { edition: 'Registered detail' };
    },
    acquire: async (args, options) => {
      events.acquire.push([name, args, options?.signal]);
      return {
        path: '/synthetic/registered.pdf',
        bytes: 1,
        reused: false,
        transfer_skipped: false,
        verified: 'sha256',
        record: '/synthetic/registered.acq.json',
      };
    },
  }));
  const tools = new AcquisitionTools(
    async () => {
      throw new Error('Unregistered bridge dispatch');
    },
    env,
    { registry: new SourceRegistry(specs), ...runtime },
  );
  return { tools, events, specs };
}
/** Future opt-in worker only exists in this guard fixture. Ordinary calls use the real tools. */
export function futureHarness(tools) {
  let starts = 0;
  const jobs = new Map();
  return {
    get starts() {
      return starts;
    },
    async call(name, args, options) {
      if (name === 'download' && args.background === true) {
        starts++;
        const job = `jfixture${starts}`;
        const result = {
          job,
          state: 'running',
          ref: args.ref,
          source: 'libgen',
          bytes_done: 0,
          poll_after_s: 1,
        };
        jobs.set(job, result);
        return result;
      }
      if (name === 'download' && args.job) return jobs.get(args.job);
      return tools.call(name, args, options);
    },
  };
}

export function budgetFixture(kind) {
  if (kind === 'plain_public')
    return {
      tools: new AcquisitionTools(
        async () => ({
          books: Array.from({ length: 10 }, (_, i) => ({
            ...metadata,
            title: `Solaris ${i}`,
            id: String(i),
            extension: 'pdf',
            md5: (i + 1).toString(16).padStart(32, '0'),
            description: 'Synthetic verbose catalogue note. '.repeat(40),
          })),
        }),
        {},
        { nonce: 1n, now: () => 0 },
      ),
      names: names.slice(0, 3),
      sources: ['libgen'],
      options: {},
    };
  const options = {
    count: 10,
    groups: true,
    ...(kind === 'mixed_registered' ? { failed: 'annas' } : {}),
  };
  const fixture = extensionFixture({ ...options, runtime: { nonce: 1n, now: () => 0 } });
  return {
    ...fixture,
    names,
    sources: options.failed ? names : ['libgen', 'zlibrary', 'held'],
    options,
  };
}

export function futureDownloadSchema(publicSchema) {
  const extension = structuredClone(publicSchema);
  extension.oneOf.push(
    {
      required: ['job', 'state', 'ref', 'source'],
      not: { required: ['record'] },
      properties: { job: { type: 'string' }, state: { enum: ['running', 'done', 'failed'] } },
    },
    {
      required: ['path', 'bytes', 'fulfilled', 'verified', 'source'],
      not: { required: ['record'] },
      properties: { fulfilled: { const: 'available_local' }, verified: { const: 'sha256' } },
    },
  );
  return extension;
}
