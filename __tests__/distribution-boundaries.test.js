/**
 * Distribution contracts for the optional Python dependency tiers.
 *
 * The npm tarball must contain every document linked by the README, while the
 * Docker image deliberately includes the lightweight RAG tier without making
 * that tier mandatory for source or npm users.
 */

import { execFileSync } from 'child_process';
import { readFileSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

describe('distribution boundaries', () => {
  test('npm package includes the optional-dependency guide linked from the README', () => {
    const output = execFileSync(
      'npm',
      ['pack', '--dry-run', '--ignore-scripts', '--json'],
      {
        cwd: projectRoot,
        encoding: 'utf8',
        env: { ...process.env, npm_config_cache: '/tmp/zlibrary-mcp-npm-cache' },
      },
    );
    const [{ files }] = JSON.parse(output);

    expect(files.map(({ path: filePath }) => filePath)).toContain(
      'docs/optional-dependencies.md',
    );
  });

  test('Docker installs the RAG extra without making source installs non-core', () => {
    const dockerfile = readFileSync(path.join(projectRoot, 'docker', 'Dockerfile'), 'utf8');

    expect(dockerfile).toMatch(/uv sync --frozen --no-dev --extra rag/);
    expect(dockerfile).not.toMatch(/--all-extras/);
  });
});
