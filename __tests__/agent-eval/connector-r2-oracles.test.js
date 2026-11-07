import { beforeEach, afterEach, test, expect } from '@jest/globals';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { tasks, arms } from './tasks.js';
import { scoreTask } from './oracles.js';

const inventory = JSON.parse(await readFile(new URL('./inventory.json', import.meta.url), 'utf8'));
let downloads;
beforeEach(async () => {
  downloads = await mkdtemp(path.join(os.tmpdir(), 'eval-r2-oracles-'));
});
afterEach(async () => {
  await rm(downloads, { recursive: true, force: true });
});
const rowFor = (id) => ({
  ...inventory.search.libgen.find((r) => r.id === id),
  source: 'libgen',
  ref: `observed-${id}`,
});
for (const build of ['A', 'B']) {
  const arm = arms.find((a) => a.id === build);
  const search = (rows, source = 'libgen') => ({
    name: build === 'B' ? 'search' : 'search_multi_source',
    args:
      build === 'B'
        ? { query: 'semantic title/author variant', sources: [source] }
        : { query: 'semantic title/author variant', source },
    reply: { structuredContent: { rows, ...(build === 'B' ? {sources:Object.fromEntries(
      ['libgen','zlibrary','annas'].map(name=>[name,name === source
        ? {state:rows.length ? 'rows':'empty',rows:rows.length,ms:1}
        : {state:'not_asked',reason:'not_requested'}])
    )} : {}) } },
  });
  const score = (id, trace) =>
    scoreTask({ task: tasks.find((t) => t.id === id), arm, trace, downloads, remainingBefore: 0 });
  const download = async (row, file = path.join(downloads, 'book.pdf')) => {
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, `%PDF-1.4\nSynthetic fixture ${row.id}\n`);
    return {
      name: build === 'B' ? 'download' : 'download_book_to_file',
      args: build === 'B' ? { ref: row.ref } : { bookDetails: row },
      selected: row,
      observedSelection: true,
      reply: { structuredContent: { path: file } },
    };
  };
  for (const id of ['T1', 'T8']) {
    const row = rowFor(id === 'T1' ? 301 : 302);
    test(`4318353616 ${build}/${id} accepts matching current-task semantic search`, async () => {
      expect(await score(id, [search([row]), await download(row)])).toMatchObject({
        success: true,
        wrong: [],
      });
    });
    for (const kind of [
      'unrelated empty',
      'late',
      'error',
      'different identity',
      'different source',
      ...(build === 'B' ? ['different ref'] : []),
    ])
      test(`4318353616 ${build}/${id} rejects ${kind} search with correct prior row/artifact`, async () => {
        const d = await download(row),
          s = search(
            kind === 'unrelated empty'
              ? []
              : [
                  {
                    ...row,
                    // Public B identity is the opaque ref; A still consumes raw IDs.
                    ...(kind === 'different identity' ? (build === 'B' ? {ref:'other-identity-ref'} : { id: 'other' }) : {}),
                    ...(kind === 'different source' ? { source: 'zlibrary' } : {}),
                    ...(kind === 'different ref' ? { ref: 'other-ref' } : {}),
                  },
                ],
          );
        if (kind === 'error') s.error = 'failed search';
        const result = await score(id, kind === 'late' ? [d, s] : [s, d]);
        expect(result.success).toBe(false);
        expect(result.files).toHaveLength(1);
        expect(result.wrong.some((w) => w.reason.includes('same-task search'))).toBe(true);
      });
  }
  const limits = () => ({
    name: build === 'B' ? 'get_limits' : 'get_download_limits',
    args: {},
    reply: { structuredContent: { daily_remaining: 0 } },
  });
  test(`4318353651 ${build} accepts usable actual fixture LibGen row after limits`, async () => {
    expect(
      await score('T11', [
        limits(),
        search([
          {
            ...rowFor(301),
            download_url: '',
            hash: undefined,
            format: undefined,
            extension: undefined,
          },
        ]),
      ]),
    ).toMatchObject({ success: true, wrong: [] });
  });
  if (build === 'A')
    test('4318353651 A md5-only useful row remains valid even with empty id/URL', async () => {
      expect(
        (
          await score('T11', [
            limits(),
            search([
              {
                source: 'libgen',
                title: 'Any useful book',
                id: '',
                md5: 'fixture-md5',
                download_url: '',
              },
            ]),
          ])
        ).success,
      ).toBe(true);
    });
  if (build === 'B')
    test('4318353651 B compact title identity plus ref does not require an A id/hash', async () => {
      expect(
        (
          await score('T11', [
            limits(),
            search([{ source: 'libgen', title: 'Any useful book', ref: 'observed-row' }]),
          ])
        ).success,
      ).toBe(true);
    });
  for (const kind of [
    'empty annas',
    'empty libgen',
    'error',
    'non-LibGen row',
    'unusable row',
    'missing handle',
    'before limits',
  ])
    test(`4318353651 ${build} rejects ${kind} fallback`, async () => {
      const s = search(
        kind.startsWith('empty')
          ? []
          : [
              {
                ...rowFor(301),
                ...(kind === 'non-LibGen row' ? { source: 'annas' } : {}),
                ...(kind === 'unusable row' ? { title: '' } : {}),
                ...(kind === 'missing handle'
                  ? { id: undefined, md5: undefined, ref: undefined }
                  : {}),
              },
            ],
        kind === 'empty annas' ? 'annas' : 'libgen',
      );
      if (kind === 'error') s.error = 'failed search';
      const result = await score('T11', kind === 'before limits' ? [s, limits()] : [limits(), s]);
      expect(result.success).toBe(false);
      expect(result.wrong.some((w) => w.reason.includes('usable LibGen'))).toBe(true);
    });
  test(`4318353616 ${build} preserves intended T3 prior-task reuse`, async () => {
    expect(
      (
        await score('T3', [
          await download({
            id: '202',
            title: 'Letters on Ethics: To Lucilius',
            extension: 'pdf',
            ref: 'prior-graver',
          }),
        ])
      ).success,
    ).toBe(true);
  });
  if (build === 'B')
    test('4318353616 B preserves intended T10 prior-task reuse and exact destination', async () => {
      const d = await download(
        {
          id: '202',
          title: 'Letters on Ethics: To Lucilius',
          extension: 'pdf',
          ref: 'prior-graver',
        },
        path.join(downloads, 'inbox/seneca.pdf'),
      );
      Object.assign(d.args, { filename: 'seneca.pdf', output_dir: path.join(downloads, 'inbox') });
      expect((await score('T10', [d])).success).toBe(true);
    });
}
