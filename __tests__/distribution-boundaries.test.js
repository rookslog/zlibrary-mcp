/**
 * Distribution contracts for the optional Python dependency tiers.
 *
 * The npm tarball must contain every document linked by the README, while the
 * Docker image deliberately includes the lightweight RAG tier without making
 * that tier mandatory for source or npm users.
 */

import { execFileSync } from 'child_process';
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

describe('distribution boundaries', () => {
  function runSetupUv(args) {
    const tempDir = mkdtempSync(path.join(os.tmpdir(), 'zlibrary-setup-uv-'));
    const binDir = path.join(tempDir, 'bin');
    const callLog = path.join(tempDir, 'uv-calls.log');

    try {
      mkdirSync(binDir, { recursive: true });
      const pythonStub = path.join(binDir, 'python3');
      writeFileSync(pythonStub, '#!/bin/sh\nprintf "3.10\\n"\n');
      chmodSync(pythonStub, 0o755);

      const uvStub = path.join(binDir, 'uv');
      writeFileSync(
        uvStub,
        [
          '#!/bin/sh',
          'if [ "$1" = "--version" ]; then printf "uv 0.8.22\\n"; exit 0; fi',
          'printf "%s\\n" "$*" >> "$UV_CALL_LOG"',
          'mkdir -p .venv/bin',
          'printf "#!/bin/sh\\nexit 0\\n" > .venv/bin/python',
          'chmod +x .venv/bin/python',
          '',
        ].join('\n'),
      );
      chmodSync(uvStub, 0o755);

      execFileSync('bash', [path.join(projectRoot, 'setup-uv.sh'), ...args], {
        cwd: tempDir,
        encoding: 'utf8',
        env: {
          ...process.env,
          PATH: `${binDir}:${process.env.PATH}`,
          UV_CALL_LOG: callLog,
        },
      });

      return readFileSync(callLog, 'utf8').trim();
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  }

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

  test('setup keeps end-user core separate from the contributor dev group', () => {
    expect(runSetupUv(['--no-dev'])).toBe('sync --no-dev');
    expect(runSetupUv([])).toBe('sync --group dev');
  });
});
