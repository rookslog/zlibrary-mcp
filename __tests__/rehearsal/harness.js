import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile, rm, readdir, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import https from 'node:https';
import { fileURLToPath } from 'node:url';
import { classifySentinels } from './sentinels.js';
import { expectedFixes } from './expectations.js';
import { waitForFakeReady } from './fake-startup.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const serverRoot = path.resolve(process.env.REHEARSAL_SERVER_ROOT || root);
const fakeEmail = 'rehearsal-account@example.invalid';
const fakePassword = 'rehearsal-password-canary';
export class Rehearsal {
  static async start(suite = 'base', { python } = {}) {
    const h = new Rehearsal();
    h.temp = await mkdtemp(
      path.join(process.env.REHEARSAL_TEMP_ROOT || tmpdir(), 'zlib-rehearsal-'),
    );
    h.records = {};
    h.suite = suite;
    h.leaks = 0;
    h.knownFindings = 0;
    h.findings = [];

    try {
      h.cert = path.join(h.temp, 'ca.crt');
      h.key = path.join(h.temp, 'ca.key');
      execFileSync(
        'openssl',
        [
          'req',
          '-x509',
          '-newkey',
          'rsa:2048',
          '-nodes',
          '-days',
          '1',
          '-subj',
          '/CN=Offline rehearsal',
          '-addext',
          'subjectAltName=IP:127.0.0.1',
          '-keyout',
          h.key,
          '-out',
          h.cert,
        ],
        { stdio: 'ignore' },
      );
      h.keyText = await readFile(h.key, 'utf8');
      const fakeStarted = Date.now();
      h.fake = spawn(
        python?.executable ?? path.join(serverRoot, '.venv/bin/python'),
        [path.join(here, 'fake_eapi.py'), h.cert, h.key],
        {
          env: {
            PATH: process.env.PATH,
            PYTHONDONTWRITEBYTECODE: '1',
            ...python?.env,
            ZLIBRARY_OUTPUT_ROOT: h.temp,
          },
          stdio: ['pipe', 'pipe', 'pipe'],
        },
      );
      h.fakeStderr = '';
      h.fake.stderr.on('data', (d) => {
        h.fakeStderr += d;
      });
      const port = await waitForFakeReady(h.fake, { stderr: () => h.fakeStderr });
      h.fakeStartup = {
        elapsedMs: Date.now() - fakeStarted,
        deadlineMs: 5000,
        phases: h.fakeStderr,
      };
      if (!Number.isInteger(port) || port < 1) throw new Error('invalid fake port');
      h.origin = `https://127.0.0.1:${port}`;
      h.domain = `127.0.0.1:${port}`;
      await writeFile(
        path.join(h.temp, 'fake-lifetime.json'),
        JSON.stringify({ pid: h.fake.pid, port }),
      );
      h.agent = new https.Agent({ ca: await readFile(h.cert) });
      return h;
    } catch (e) {
      await h.close();
      throw e;
    }
  }
  request(route, body, includeStatus = false) {
    return new Promise((resolve, reject) => {
      const r = https.request(
        this.origin + route,
        {
          agent: this.agent,
          method: body ? 'POST' : 'GET',
          headers: { 'Content-Type': 'application/json' },
        },
        (res) => {
          let data = '';
          res.on('data', (d) => {
            data += d;
          });
          res.on('end', () => {
            try {
              resolve(
                includeStatus
                  ? { status: res.statusCode, body: JSON.parse(data) }
                  : JSON.parse(data),
              );
            } catch {
              reject(new Error('invalid fake response'));
            }
          });
        },
      );
      r.on('error', reject);
      r.setTimeout(3000, () => r.destroy(new Error('fake control timeout')));
      r.end(body ? JSON.stringify(body) : undefined);
    });
  }
  stats() {
    return this.request('/__stats');
  }
  mode(body) {
    return this.request('/__mode', body);
  }
  // Shared endpoint, certificate and fence wiring for real source and packed bins.
  providerEnvironment({ libgen = false, credentials = true } = {}) {
    return {
      SSL_CERT_FILE: this.cert,
      PYTHONPATH: here,
      PYTHONDONTWRITEBYTECODE: '1',
      REHEARSAL_ALLOWED_PORT: String(new URL(this.origin).port),
      ZLIBRARY_EAPI_DOMAIN: this.domain,
      ...(libgen ? {
        REHEARSAL_LIBGEN_ORIGIN: this.origin,
        BOOK_SOURCE_PREFLIGHT: 'false',
        BOOK_SOURCE_FALLBACK_ENABLED: 'false',
      } : {}),
      ...(credentials ? {
        ZLIBRARY_EMAIL: fakeEmail,
        ZLIBRARY_PASSWORD: fakePassword,
      } : {}),
    };
  }
  scan(value, source = 'reply') {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    const found = classifySentinels(text, source, this.keyText, {
      redaction: expectedFixes.has('redaction'),
    });
    this.leaks += found.unexpected.length;
    this.knownFindings += found.knownLogCount;
    this.findings.push(
      ...found.unexpected.map((f) => ({ ...f, expected: false })),
      ...found.expected.map((f) => ({ ...f, expected: true })),
    );
  }
  async call(c, name, args = {}, allowError = false) {
    const reply = await c.callTool({ name, arguments: args }, undefined, { timeout: 25000 });
    const previousLeaks = this.leaks;
    this.scan(reply);
    if (this.leaks !== previousLeaks)
      throw new Error('Unexpected reply sentinel finding (value suppressed)');
    const data = reply.isError
      ? (reply.structuredContent ?? { error: { message: reply.content[0].text } })
      : JSON.parse(reply.content[0].text);
    if (!allowError && (reply.isError || data.error))
      throw new Error(
        `Unexpected tool error for ${name}: ${String(data.error?.message).replaceAll(this.domain, '[loopback]')}`,
      );
    return data;
  }
  note(name, fields) {
    Object.assign(this.records[name], fields);
  }
  async scenario(name, run, options = {}) {
    await this.mode({
      mode: 'healthy',
      reset: true,
      drop_cookies: process.env.REHEARSAL_DROP_COOKIES === '1',
    });
    const state = path.join(this.temp, name);
    this.downloads = path.join(state, 'downloads');
    await mkdir(this.downloads, { recursive: true });
    const initialLeaks = this.leaks;
    const sessions = [];
    let clientIndex = 0;
    this.records[name] = {};
    const connect = async () => {
      const home = path.join(state, `client-${clientIndex++}`);
      await mkdir(home, { recursive: true });
      const transport = new StdioClientTransport({
        command: process.execPath,
        args: [
          '--import',
          path.join(here, 'node-isolation.mjs'),
          path.join(serverRoot, 'dist/index.js'),
        ],
        cwd: home,
        env: {
          PATH: process.env.PATH,
          HOME: home,
          TMPDIR: home,
          ...this.providerEnvironment({ libgen: options.libgen }),
          ...(options.untrusted ? { SSL_CERT_FILE: undefined } : {}),
          REHEARSAL_EGRESS_TRAP: path.join(home, 'egress-trap.txt'),
          ZLIBRARY_SESSION_DIR: path.join(home, 'session'),
          XDG_STATE_HOME: home,
          XDG_CACHE_HOME: home,
          XDG_CONFIG_HOME: home,
          REHEARSAL_LOG_DIR: path.join(home, 'logs'),
          MCP_READABLE_ROOTS: this.downloads,
          ZLIBRARY_OUTPUT_ROOT: this.downloads,
          RETRY_MAX_RETRIES: '0',
          CIRCUIT_BREAKER_THRESHOLD: '5',
          CIRCUIT_BREAKER_TIMEOUT: '60000',
          PYTHON_BRIDGE_TIMEOUT: String(options.budgetMs ?? 15000),
          LOG_LEVEL: 'info',
        },
        stderr: 'pipe',
      });
      const c = new Client({ name: 'offline-rehearsal', version: '1.0.0' });
      const session = { c, transport, stderr: '' };
      sessions.push(session);
      transport.stderr.on('data', (d) => {
        session.stderr += d;
      });
      await c.connect(transport);
      session.pid = transport.pid;
      return c;
    };
    this.sessions = sessions;
    let failure;
    try {
      await run(connect);
    } catch (error) {
      failure = error;
    }
    {
      const changedPid = sessions.some((s) => s.pid !== undefined && s.transport.pid !== s.pid);
      const closed = await Promise.allSettled(
        sessions.map(async (s) => {
          try {
            await s.c.close();
          } finally {
            await s.transport.close();
          }
        }),
      );
      if (changedPid) failure ??= new Error('Node child PID changed');
      if (closed.some((r) => r.status === 'rejected'))
        failure ??= new Error('One or more client cleanup operations failed');
      const stats = await this.stats();
      Object.assign(this.records[name], stats, {
        childPids: sessions.map((s) => s.pid),
        childCount: sessions.length,
      });
      for (const s of sessions) this.scan(s.stderr, 'stderr');
      const beforeLeaks = this.leaks;
      this.records[name].protectedSessionFiles = 0;
      const visit = async (dir) => {
        for (const entry of await readdir(dir, { withFileTypes: true })) {
          const file = path.join(dir, entry.name);
          if (entry.isDirectory()) await visit(file);
          else if (entry.isFile()) {
            const data = await readFile(file, 'utf8');
            if (
              entry.name === 'eapi-session.json' &&
              path.basename(path.dirname(file)) === 'session'
            ) {
              if ((await stat(file)).mode & 0o077)
                throw new Error('synthetic session cache is not private');
              const cache = JSON.parse(data);
              if (
                cache.identity !== fakeEmail ||
                cache.remix_userid !== 'rehearsal-cookie-id' ||
                !/^rehearsal-cookie-key-epoch-\d+$/.test(cache.remix_userkey)
              )
                throw new Error('Unexpected synthetic cache fields');
              const rest = { ...cache };
              delete rest.identity;
              delete rest.remix_userid;
              delete rest.remix_userkey;
              this.scan(rest, 'protected-cache-other-fields');
              this.records[name].protectedSessionFiles++;
            } else {
              this.scan(data, path.relative(state, file).split(path.sep).join('/'));
              if (entry.name === 'egress-trap.txt' && data)
                throw new Error('unintended egress attempt detected');
            }
          }
        }
      };
      await visit(state);
      this.records[name].fileSentinelLeaks = this.leaks - beforeLeaks;
      this.records[name].sentinelFindings = this.leaks - initialLeaks;
      this.records[name].secretScan =
        this.leaks - initialLeaks ? 'sentinel finding (value suppressed)' : 'passed';
      this.scan(this.fakeStderr, 'fake-stderr');
    }
    if (failure) throw failure;
  }
  async captureSurface(surface) {
    if (process.env.REHEARSAL_SURFACE_FILE)
      await writeFile(process.env.REHEARSAL_SURFACE_FILE, JSON.stringify(surface, null, 2) + '\n');
    this.records.S5.surface = surface;
  }
  async close() {
    try {
      if (process.env.REHEARSAL_RECORD_DIR && this.records) {
        await writeFile(
          path.join(process.env.REHEARSAL_RECORD_DIR, `${this.suite}.json`),
          JSON.stringify(
            {
              serverRoot,
              fakeStartup: this.fakeStartup,
              records: this.records,
              sentinelFindings: this.leaks,
              knownCookieIdFindings: this.knownFindings,
              findings: this.findings,
            },
            null,
            2,
          ) + '\n',
        );
      }
    } finally {
      this.agent?.destroy();
      try {
        if (this.fake?.pid && this.fake.exitCode === null && this.fake.signalCode === null) {
          const exited = new Promise((resolve) => this.fake.once('exit', resolve));
          this.fake.stdin.end();
          const killTimer = setTimeout(() => this.fake.kill('SIGKILL'), 2000);
          let deadline;
          try {
            await Promise.race([
              exited,
              new Promise((_, reject) => {
                deadline = setTimeout(
                  () => reject(new Error('fake did not exit during cleanup')),
                  3000,
                );
              }),
            ]);
          } finally {
            clearTimeout(killTimer);
            clearTimeout(deadline);
          }
        }
      } finally {
        if (this.temp) await rm(this.temp, { recursive: true, force: true });
      }
    }
  }
}
