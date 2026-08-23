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
    // Contributors get the dev group AND every extra: part of the fast suite
    // imports scholar-tier modules at collection time, so a core-only
    // contributor environment cannot run the verification CONTRIBUTING.md
    // documents on the very next line (Codex on #114).
    expect(runSetupUv([])).toBe('sync --group dev --all-extras');
    // Deployments validate RAG processing, which core alone cannot do.
    expect(runSetupUv(['--deploy'])).toBe('sync --no-dev --extra rag --extra scholar');
  });

  test('packed CI runs the documented core setup and rejects contributor packages', () => {
    const workflow = readFileSync(
      path.join(projectRoot, '.github', 'workflows', 'ci.yml'),
      'utf8',
    );

    expect(workflow).toMatch(
      /cd "\$EXTRACT\/package"[\s\S]*bash setup-uv\.sh --no-dev[\s\S]*importlib\.util\.find_spec\('pytest'\) is None/,
    );
    expect(workflow).not.toMatch(
      /cd "\$EXTRACT\/package"[\s\S]*\n\s+uv sync\s*(?:\n|$)/,
    );
  });

  test('runtime recovery instructions keep the core tier', () => {
    // Codex on #114: these two messages are the most likely path an npm user
    // takes when the server cannot start, and a bare `uv sync` there installs
    // the whole development group — defeating the boundary this PR builds, at
    // the exact moment the user is following instructions rather than choosing.
    const venvManager = readFileSync(
      path.join(projectRoot, 'src', 'lib', 'venv-manager.ts'),
      'utf8',
    );

    expect(venvManager).toContain('bash setup-uv.sh --no-dev');
    expect(venvManager).toContain('uv sync --no-dev');

    // Scoped to the two thrown messages, not to the file. A doc comment may
    // legitimately mention bare `uv sync` as the contributor variant; what must
    // not survive is an *instruction* that installs the dev group.
    const thrownLines = venvManager
      .split('\n')
      .filter((line) => /^\s*[`'].{0,8}uv sync/.test(line));
    expect(thrownLines.length).toBeGreaterThan(0);
    for (const line of thrownLines) {
      expect(line).toMatch(/uv sync --no-dev/);
    }
  });

  test('every documented setup produces the environment its own steps need', () => {
    // Codex on #114: three guides invoked a tier that could not run the very
    // next thing they told the reader to do. A setup script and the document
    // that calls it have to agree, or the instructions fail on a clean machine
    // at the step after the one that "worked".
    const setup = readFileSync(path.join(projectRoot, 'setup-uv.sh'), 'utf8');
    const contributing = readFileSync(path.join(projectRoot, 'CONTRIBUTING.md'), 'utf8');
    const deployment = readFileSync(
      path.join(projectRoot, 'docs', 'deployment', 'DEPLOYMENT_CHECKLIST.md'),
      'utf8',
    );
    const pkg = JSON.parse(
      readFileSync(path.join(projectRoot, 'package.json'), 'utf8'),
    );

    // Contributor default must match what the fast suite needs to collect.
    expect(setup).toContain('sync --group dev --all-extras');
    expect(contributing).toMatch(/bash setup-uv\.sh\s*$/m);

    // Deployment validates RAG processing, so its tier must carry rag+scholar.
    expect(setup).toContain('--extra rag --extra scholar');
    expect(deployment).toContain('bash setup-uv.sh --deploy');

    // The health check is recommended to core installs; it must not sync dev.
    expect(pkg.scripts.doctor).toContain('uv run --no-dev');
  });

  test('end-user guides keep development tools out of core setup', () => {
    const systemWide = readFileSync(
      path.join(projectRoot, 'docs', 'installation', 'system-wide-setup.md'),
      'utf8',
    );
    const troubleshooting = readFileSync(
      path.join(projectRoot, 'docs', 'TROUBLESHOOTING.md'),
      'utf8',
    );

    expect(systemWide).toContain('bash setup-uv.sh --no-dev');
    expect(troubleshooting).toContain('uv sync --no-dev');
    expect(troubleshooting).toContain('uv sync --no-dev --extra rag');
    expect(troubleshooting).toContain('uv sync --no-dev --extra scholar');
    // The contributor row names the script, not a flag combination: `--group
    // dev` alone fails at collection, because a unit-marked test imports PIL
    // at module scope and Pillow is scholar-only (Codex on #114).
    expect(troubleshooting).toContain('bash setup-uv.sh');
    expect(troubleshooting).not.toContain('uv sync --group dev`');
    expect(troubleshooting).not.toContain(
      'uv sync            # creates .venv/ and installs all Python dependencies',
    );
  });
});
