/**
 * Regression guard: the stdio transport's stdout MUST carry only MCP messages.
 *
 * The MCP stdio specification requires that a server writes nothing to stdout
 * except newline-delimited JSON-RPC messages. A stray `console.log` corrupts the
 * stream and strict clients drop the connection ("server disconnected" —
 * GitHub issue #11). Two layers of defence live here:
 *
 *   1. A static check that no `console.log` creeps back into `src/`.
 *   2. A live handshake against the built server asserting every stdout line
 *      parses as JSON-RPC.
 */

import { spawn } from 'child_process';
import { readFileSync, existsSync, readdirSync, statSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');
const srcDir = path.join(projectRoot, 'src');
const distEntry = path.join(projectRoot, 'dist', 'index.js');

function collectTsFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) return collectTsFiles(full);
    return full.endsWith('.ts') ? [full] : [];
  });
}

describe('stdio purity', () => {
  test('no console.log calls in src/ (stdout is the JSON-RPC channel)', () => {
    const offenders = [];
    for (const file of collectTsFiles(srcDir)) {
      // logger.ts documents the rule in prose; exempt it from the literal scan.
      if (file.endsWith(path.join('lib', 'logger.ts'))) continue;
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, i) => {
        if (/(^|[^.\w])console\.log\s*\(/.test(line)) {
          offenders.push(`${path.relative(projectRoot, file)}:${i + 1}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });

  test.each(['legacy', 'pool'])('every stdout line from a real %s handshake is valid JSON-RPC', async (mode) => {
    if (!existsSync(distEntry)) {
      throw new Error(`dist/index.js missing — run "npm run build" before this test`);
    }

    const initialize = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        protocolVersion: '2025-11-25',
        capabilities: {},
        clientInfo: { name: 'stdio-purity-test', version: '0.0.0' },
      },
    });
    const initialized = JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' });

    const stdout = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [distEntry], {
        env: {
          ...process.env,
          ZLIBRARY_EMAIL: mode === 'legacy' ? 'ci@test.com' : '',
          ZLIBRARY_PASSWORD: mode === 'legacy' ? 'ci-test-password' : '',
          ZLIBRARY_ACCOUNT_CREDENTIALS: mode === 'pool' ? JSON.stringify([{ email: 'ci@test.com', password: 'ci-test-password' }]) : '',
          LOG_LEVEL: 'debug', // strictest case: even debug output must avoid stdout
        },
        stdio: ['pipe', 'pipe', 'pipe'],
      });

      let out = '';
      let err = '';
      child.stderr.on('data', (chunk) => { err += chunk.toString(); });
      child.stdout.on('data', (chunk) => {
        out += chunk.toString();
      });
      child.on('error', reject);

      const done = setTimeout(() => {
        child.kill();
        resolve({ out, err });
      }, 8000);
      child.on('close', () => {
        clearTimeout(done);
        resolve({ out, err });
      });

      child.stdin.write(`${initialize}\n${initialized}\n`);
    });

    expect(stdout.err).not.toContain('Missing environment variable(s)');
    const lines = stdout.out.split('\n').filter((line) => line.trim() !== '');
    expect(lines.length).toBeGreaterThan(0);

    for (const line of lines) {
      let parsed;
      expect(() => {
        parsed = JSON.parse(line);
      }).not.toThrow(); // a non-JSON line here is the exact bug this test guards
      expect(parsed.jsonrpc).toBe('2.0');
    }
  }, 20000);
});
