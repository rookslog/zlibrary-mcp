import { expect, test } from '@jest/globals';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { SearchRecords } from '../../dist/lib/search-records.js';
import { planSearch, prepareSearch } from '../../dist/lib/search-assembly.js';
import { ToolFailure, classifyFailure } from '../../dist/lib/tool-errors.js';

const views = () => ['libgen', 'annas'].map(name => ({ name,
  capabilities: { configured: true, search_available: true, download_available: true },
  defaultSearch: { on: true },
}));
const copy = (source, id) => ({ source, id, ref: `r-${id}`, md5: 'a'.repeat(32), metadata: { title: 'Solaris' } });
const answer = (source, copies = [copy(source, source)]) => ({ source, state: 'answered', ms: 1, copies });

test('G5 public handler import graph joins activated search models transitively', () => {
  const root = fileURLToPath(new URL('../../dist/', import.meta.url));
  const seen = new Set();
  function visit(file) {
    if (seen.has(file)) return;
    seen.add(file);
    const source = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    function scan(node) {
      const spec = (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) ? node.moduleSpecifier
        : ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || node.expression.getText(source) === 'require') ? node.arguments[0] : undefined;
      if (spec && ts.isStringLiteral(spec) && spec.text.startsWith('.'))
        visit(path.resolve(path.dirname(file), spec.text));
      ts.forEachChild(node, scan);
    }
    scan(source);
  }
  visit(path.join(root, 'index.js'));
  visit(path.join(root, 'lib/acquisition-tools.js'));
  expect([...seen].map(file => path.relative(root, file))).toEqual(expect.arrayContaining(['lib/search-limits.js']));
  expect([...seen].map(file => path.relative(root, file))).toEqual(expect.arrayContaining(['lib/search-assembly.js']));
});

test('G5 E1 eviction releases backing row and cursor aliases rather than only hiding them', () => {
  const store = new SearchRecords({ capacity: 1, nonce: 1n });
  const plan = planSearch({ query: 'Solaris', sources: ['libgen'], limit: 1 }, views(), { waitMs: 12000 });
  for (let generation = 0; generation < 12; generation++) {
    store.retain(plan, [answer('libgen', [copy('libgen', `${generation}-a`), { ...copy('libgen', `${generation}-b`), md5: 'b'.repeat(32) }])]);
    // TypeScript privacy is deliberately inspected here: stale references being
    // inaccessible does not demonstrate release of their retained record graphs.
    expect(store.refs.values.size).toBe(3);
    expect(store.records.size).toBe(1);
  }
});

test.each([['safe', 'no', 'safe'], ['no', 'safe', 'safe'], ['no', 'no', 'no']])(
  'G5 A12 aggregate retry for %s and %s is %s', (a, b, expected) => {
    const plan = planSearch({ query: 'Solaris' }, views(), { waitMs: 12000 });
    const prepared = prepareSearch(plan, [a, b].map((retry, i) => ({ source: views()[i].name, state: 'failed', ms: 1,
      failure: new ToolFailure('provider_error', 'Synthetic failure.', { retry }),
    })));
    expect(prepared.error).toMatchObject({ code: 'all_sources_failed', retry: expected });
  },
);

test('G5 A13 assembly carries operator route preference through file grouping', () => {
  const plan = planSearch({ query: 'Solaris' }, views(), { waitMs: 12000, operatorSources: ['annas', 'libgen'] });
  const prepared = prepareSearch(plan, [answer('libgen'), answer('annas')]);
  expect(prepared.rows[0]).toMatchObject({ ref: 'r-annas', source: 'annas', route_basis: 'operator_order' });
});

test.each(['capability', 'copy'])('G5 A16 assembly carries unavailable download from %s', kind => {
  const sourceViews = views();
  if (kind === 'capability') sourceViews[1].capabilities.download_available = false;
  const plan = planSearch({ query: 'Solaris' }, sourceViews, { waitMs: 12000, operatorSources: ['annas', 'libgen'] });
  const annas = copy('annas', 'annas');
  if (kind === 'copy') annas.downloadAvailable = false;
  const prepared = prepareSearch(plan, [answer('libgen'), answer('annas', [annas])]);
  expect(prepared.rows[0]).toMatchObject({ ref: 'r-libgen', route_basis: 'only_usable',
    other_copies: [expect.objectContaining({ ref: 'r-annas', download_available: false })],
  });
});


test('N2 mirror slice reason survives bridge classification without blaming a searched host', () => {
  const result = classifyFailure({ context: { details: { reason: 'mirror_slice_exhausted' } } }, 'search', 'libgen');
  expect(result).toMatchObject({ code: 'provider_timeout', info: { retry: 'safe', detail: { reason: 'mirror_slice_exhausted' } } });
});
