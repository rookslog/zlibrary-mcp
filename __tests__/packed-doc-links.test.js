import { expect, test } from '@jest/globals';
import { spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

function manifestFiles(manifest) {
  const files = new Set(['README.md', 'LICENSE', 'package.json']);
  function add(file) {
    if (statSync(path.join(root, file)).isDirectory()) {
      for (const name of readdirSync(path.join(root, file))) add(path.posix.join(file, name));
    } else files.add(file);
  }
  for (const file of manifest.files.filter((entry) => !entry.startsWith('!'))) add(file);
  for (const entry of manifest.files.filter((file) => file.startsWith('!'))) {
    const pattern = entry.slice(1).replace(/\/$/, '/**');
    for (const file of files) if (path.matchesGlob(file, pattern)) files.delete(file);
  }
  return files;
}

function missingLinks(file, markdown, shipped) {
  // Include inline links/images and reference-link definitions. Ignore examples
  // inside fenced code blocks, URL schemes, network URLs and same-file anchors.
  const prose = markdown.replace(/^\s*(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\s*\1\s*$/gm, '');
  const links = [
    ...prose.matchAll(/!?\[[^\]]*\]\(\s*(<[^>]*>|[^\s)]+)(?:\s+[^)]*)?\)/g),
    ...prose.matchAll(/^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)/gm),
  ];
  return links.flatMap((match) => {
    const link = match[1].replace(/^<|>$/g, '');
    if (/^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(link)) return [];
    const target = path.posix.normalize(
      path.posix.join(path.posix.dirname(file), decodeURIComponent(link.split(/[?#]/)[0])),
    );
    return shipped.has(target) ? [] : [`${file} -> ${target}`];
  });
}

test('published Markdown links resolve within the shipped package, including the interface migration guide', () => {
  const pack = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], {
    cwd: root,
    encoding: 'utf8',
    env: {
      ...process.env,
      npm_config_ignore_scripts: 'true',
      npm_config_cache: path.join(tmpdir(), 'zlibrary-pack-doc-cache'),
      HUSKY: '0',
    },
    timeout: 30000,
  });
  let shipped;
  if (pack.error && ['EPERM', 'EACCES', 'EROFS'].includes(pack.error.code)) {
    console.warn(
      `npm pack unavailable (${pack.error.code}); using package.json files allowlist fallback`,
    );
    shipped = manifestFiles(JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')));
  } else {
    expect(pack.error).toBeUndefined();
    expect(pack.status).toBe(0);
    // npm may emit a prepare diagnostic before the JSON, even with scripts off.
    const [packed] = JSON.parse(pack.stdout.slice(pack.stdout.indexOf('[')));
    shipped = new Set(packed.files.map((file) => file.path));
  }
  expect(
    [...shipped].filter(
      (file) => file.startsWith('.claude/') || file === 'ISSUES.md' || file.startsWith('docs/adr/'),
    ),
  ).toEqual([]);
  const missing = [];
  for (const required of ['docs/interface-migration.md', 'docs/README.md']) {
    if (!shipped.has(required)) missing.push(`${required} is not shipped`);
  }
  for (const file of shipped) {
    if (/\.md$/i.test(file)) {
      missing.push(...missingLinks(file, readFileSync(path.join(root, file), 'utf8'), shipped));
    }
  }
  expect(missing).toEqual([]);
});

test('link check catches missing inline, image and reference targets without requiring external links to ship', () => {
  const markdown =
    '[API](api.md#errors) ![image](images/book.png) [guide][migration]\n[migration]: interface-migration.md\n[remote](https://example.invalid) [here](#section)';
  expect(missingLinks('docs/example.md', markdown, new Set(['docs/api.md']))).toEqual([
    'docs/example.md -> docs/images/book.png',
    'docs/example.md -> docs/interface-migration.md',
  ]);
  expect(
    missingLinks(
      'docs/example.md',
      markdown,
      new Set(['docs/api.md', 'docs/images/book.png', 'docs/interface-migration.md']),
    ),
  ).toEqual([]);
});
