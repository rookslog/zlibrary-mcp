import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { createServer } from 'node:net';
import { createInterface } from 'node:readline';
import { mkdir, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { Rehearsal } from '../rehearsal/harness.js';
import { snapshotSource } from '../rehearsal/source-identity.js';
import { dataOf, selectableRows, identity } from './oracles.js';
import { gitRead, pinnedInputs, verifyBuildArtifacts, artifactProofErrors } from './build-proof.js';
export { gitRead, verifyBuildArtifacts } from './build-proof.js';
import { verifyNodeRuntime, nodeProofErrors } from './node-runtime.js';
import { verifyPythonRuntime, pythonProofErrors } from './python-runtime.js';
import { verifyEvaluatorIdentity } from './evaluator-identity.js';
export { verifyEvaluatorIdentity } from './evaluator-identity.js';

export const here = path.dirname(fileURLToPath(import.meta.url));
export const root = path.resolve(here, '../..');

export const canonicalRow = (value) =>
  JSON.stringify(value, (_key, item) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
      : item,
  );

export async function evalSnapshot(buildRoot) {
  const snapshot = await snapshotSource(buildRoot);
  const hash = createHash('sha256');
  const visit = async (dir, relative = '') => {
    for (const entry of (await readdir(dir, { withFileTypes: true })).sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      if (entry.name === '__pycache__' || entry.name.endsWith('.pyc')) continue;
      const file = path.join(dir, entry.name),
        name = path.join(relative, entry.name);
      if (entry.isDirectory()) await visit(file, name);
      else {
        hash.update(name);
        hash.update(await readFile(file));
      }
    }
  };
  await visit(path.join(buildRoot, 'zlibrary/src'));
  snapshot.trees.vendoredZlibrary = { sha256: hash.digest('hex') };
  const inputs = await pinnedInputs(buildRoot);
  if (inputs.sha !== snapshot.sha) throw new Error('HEAD changed while snapshotting source');
  snapshot.trees.productionInputs = {
    sha256: inputs.inputs.sha256,
    files: inputs.inputs.files.length,
  };
  return snapshot;
}

export class EvaluationServer {
  static async start({
    buildRoot = root,
    pin,
    arm,
    defaultOrderEnv = 'BOOK_SOURCE_DEFAULT_ORDER',
  }) {
    const s = new EvaluationServer();
    s.arm = arm;
    s.trace = [];
    s.observed = new Map();
    s.rows = new Map();
    s.refRows = new Map();
    s.buildRoot = path.resolve(buildRoot);
    s.sha = gitRead(s.buildRoot, 'rev-parse', 'HEAD');
    try {
      // No fixture server, MCP server or model may start without reproducible bytes.
      if (pin && s.sha !== gitRead(s.buildRoot, 'rev-parse', `${pin}^{commit}`))
        throw new Error('server checkout does not match pinned commit');
      s.evaluator = await verifyEvaluatorIdentity();
      s.artifactProof = await verifyBuildArtifacts({ buildRoot: s.buildRoot, pin });
      s.sourceStart = await evalSnapshot(s.buildRoot);
      const proofErrors = artifactProofErrors(s.artifactProof, {
        sha: s.sha,
        source: s.sourceStart,
      });
      if (proofErrors.length) throw new Error(proofErrors.join('; '));
      s.node = await verifyNodeRuntime({ root });
      s.buildNode = await verifyNodeRuntime({ root: s.buildRoot });
      const nodeErrors = [
        ...nodeProofErrors(s.node, s.evaluator.files),
        ...nodeProofErrors(s.buildNode, s.artifactProof.inputs.files),
      ];
      if (nodeErrors.length) throw new Error(nodeErrors.join('; '));
      s.python = await verifyPythonRuntime({ buildRoot: s.buildRoot });
      const pythonErrors = pythonProofErrors(s.python, { artifactProof: s.artifactProof });
      if (pythonErrors.length) throw new Error(pythonErrors.join('; '));
      await s.assertNodeIdentity();
      s.fake = await Rehearsal.start('agent-eval', { python: s.python.descriptor });
      s.home = path.join(s.fake.temp, 'server');
      s.downloads = path.join(s.home, 'downloads');
      s.artifactRoot = s.downloads;
      await mkdir(path.join(s.downloads, 'inbox'), { recursive: true });
      const env = {
        PATH: process.env.PATH,
        HOME: s.home,
        TMPDIR: s.home,
        ...s.python.descriptor.env,
        SSL_CERT_FILE: s.fake.cert,
        REHEARSAL_ALLOWED_PORT: String(new URL(s.fake.origin).port),
        REHEARSAL_EGRESS_TRAP: path.join(s.home, 'egress-trap.txt'),
        REHEARSAL_LIBGEN_ORIGIN: s.fake.origin,
        REHEARSAL_LOG_DIR: path.join(s.home, 'logs'),
        ZLIBRARY_EAPI_DOMAIN: s.fake.domain,
        ZLIBRARY_EMAIL: 'rehearsal-account@example.invalid',
        ZLIBRARY_PASSWORD: 'rehearsal-password-canary',
        ZLIBRARY_SESSION_DIR: path.join(s.home, 'session'),
        XDG_STATE_HOME: s.home,
        XDG_CACHE_HOME: s.home,
        XDG_CONFIG_HOME: s.home,
        MCP_READABLE_ROOTS: s.downloads,
        BOOK_SOURCE_PREFLIGHT: 'false',
        BOOK_SOURCE_FALLBACK_ENABLED: 'false',
        ANNAS_BASE_URL: s.fake.origin + '/annas',
        RETRY_MAX_RETRIES: '0',
        PYTHON_BRIDGE_TIMEOUT: '15000',
        LOG_LEVEL: 'error',
        // B may choose a different env spelling: --default-order-env records it.
        [defaultOrderEnv]: arm.defaultOrder ? arm.defaultOrder.join(',') : '',
        ZLIBRARY_OUTPUT_ROOT: s.downloads,
      };
      s.env = env;
      await s.fake.mode({ mode: 'healthy', reset: true, remaining: 2 });
      const launchPython = await verifyPythonRuntime({ buildRoot: s.buildRoot });
      if (
        launchPython.sha256 !== s.python.sha256 ||
        JSON.stringify(launchPython.descriptor) !== JSON.stringify(s.python.descriptor)
      )
        throw new Error('Python identity changed before server launch');
      const launchEvaluator = await verifyEvaluatorIdentity();
      if (launchEvaluator.sha256 !== s.evaluator.sha256)
        throw new Error('evaluator source changed before server launch');
      const launchSnapshot = await evalSnapshot(s.buildRoot);
      if (
        launchSnapshot.sha !== s.sourceStart.sha ||
        JSON.stringify(launchSnapshot.trees) !== JSON.stringify(s.sourceStart.trees)
      )
        throw new Error('source/runtime identity changed before server launch');
      await s.assertNodeIdentity();
      s.transport = new StdioClientTransport({
        command: process.execPath,
        args: [
          '--import',
          path.resolve(here, '../rehearsal/node-isolation.mjs'),
          path.join(s.buildRoot, 'dist/index.js'),
        ],
        cwd: s.home,
        env,
        stderr: 'pipe',
      });
      s.stderr = '';
      s.transport.stderr.on('data', (d) => {
        s.stderr += d;
      });
      s.client = new Client({ name: 'agent-evaluation', version: '1.0.0' });
      await s.client.connect(s.transport);
      s.rawTools = (await s.client.listTools()).tools;
      if (
        arm.build === 'B' &&
        s.rawTools
          .map((t) => t.name)
          .sort()
          .join(',') !==
          ['search', 'get_book', 'download', 'get_limits', 'get_history'].sort().join(',')
      )
        throw new Error('B must advertise exactly the five specified tools');
      s.tools = s.listTools();
      s.measurement = {
        count: s.tools.length,
        serializedCharacters: JSON.stringify({ tools: s.tools }).length,
        utf8Bytes: Buffer.byteLength(JSON.stringify({ tools: s.tools })),
      };
      if (arm.build === 'A' && s.tools.length !== 12)
        throw new Error(`expected pinned 12-tool A; observed ${s.tools.length}`);
      // One underlying MCP process survives driver reconnects and warm resumes.
      s.socketPath = path.join(s.fake.temp, 'broker.sock');
      s.broker = createServer((socket) => {
        const lines = createInterface({ input: socket });
        lines.on('line', async (line) => {
          try {
            const req = JSON.parse(line);
            const result =
              req.method === 'list' ? { tools: s.tools } : await s.call(req.name, req.args);
            socket.end(JSON.stringify({ result }) + '\n');
          } catch (e) {
            socket.end(JSON.stringify({ error: String(e.message) }) + '\n');
          }
        });
      });
      await new Promise((resolve, reject) => {
        s.broker.once('error', reject);
        s.broker.listen(s.socketPath, resolve);
      });
      return s;
    } catch (e) {
      await s.close();
      throw e;
    }
  }
  listTools() {
    return this.rawTools.filter(
      (t) => this.arm.history || !['get_history', 'get_download_history'].includes(t.name),
    );
  }
  async assertNodeIdentity() {
    const evaluator = await verifyEvaluatorIdentity(),
      node = await verifyNodeRuntime({ root }),
      buildNode = await verifyNodeRuntime({ root: this.buildRoot });
    if (
      evaluator.sha256 !== this.evaluator.sha256 ||
      node.sha256 !== this.node.sha256 ||
      buildNode.sha256 !== this.buildNode.sha256 ||
      JSON.stringify(node.local) !== JSON.stringify(this.node.local) ||
      JSON.stringify(buildNode.local) !== JSON.stringify(this.buildNode.local)
    )
      throw new Error('Node common/selected runtime identity drift');
    return { node, buildNode };
  }
  async finishIdentity() {
    const nodes = await this.assertNodeIdentity();
    const evaluator = await verifyEvaluatorIdentity(),
      python = await verifyPythonRuntime({ buildRoot: this.buildRoot });
    if (
      evaluator.sha256 !== this.evaluator.sha256 ||
      python.sha256 !== this.python.sha256 ||
      JSON.stringify(python.descriptor) !== JSON.stringify(this.python.descriptor)
    )
      throw new Error('evaluator/Python metadata drift during evaluation');
    return { evaluator, python, ...nodes };
  }
  async call(name, args = {}) {
    const c = { name, args: structuredClone(args), origin: 'broker', forwarded: false };
    this.trace.push(c);
    if (!this.tools.some((t) => t.name === name)) {
      c.error = `tool disabled or unknown: ${name}`;
      c.rejected = true;
      throw new Error(c.error);
    }
    const row =
      args.bookDetails ?? this.refRows.get(args.ref) ?? this.rows.get(String(args.bookId));
    c.selected = row;
    const fixtureTitles = {
      Persuasion: row?.source === 'libgen' ? 301 : 201,
      'Letters on Ethics: To Lucilius': 202,
      'Letters from a Stoic': 203,
      'The Glass Orchard': 302,
      'Pride and Prejudice': row?.source === 'libgen' ? 303 : 209,
      Middlemarch: row?.source === 'libgen' ? 304 : 210,
      'On Liberty': row?.source === 'libgen' ? 305 : 211,
    };
    c.fixtureId = row?.id ?? fixtureTitles[row?.title];
    c.observedSelection = args.ref
      ? this.refRows.has(args.ref)
      : args.bookDetails
        ? this.observed.has(canonicalRow(args.bookDetails))
        : !!row;
    try {
      c.forwarded = true;
      c.reply = await this.client.callTool({ name, arguments: args }, undefined, {
        timeout: 25000,
      });
      if (!this.arm.history && name === 'get_limits') {
        // This ablation belongs to the evaluator. Keep the product reply intact,
        // and filter both MCP representations before tracing or returning it.
        const hideHistory = (data) => {
          const capability = data?.sources?.zlibrary;
          if (Array.isArray(capability?.operations))
            capability.operations = capability.operations.filter((op) => op !== 'history');
          return data;
        };
        c.reply = structuredClone(c.reply);
        if (c.reply.structuredContent) hideHistory(c.reply.structuredContent);
        c.reply.content = c.reply.content?.map((block) => {
          if (block.type !== 'text') return block;
          try {
            return { ...block, text: JSON.stringify(hideHistory(JSON.parse(block.text))) };
          } catch {
            // Diagnostic text is not a capability payload.
            return block;
          }
        });
      }
      const data = dataOf(c.reply);
      for (const r of selectableRows(data)) {
        this.observed.set(canonicalRow(r), r);
        this.rows.set(identity(r), r);
        if (r.ref) this.refRows.set(r.ref, r);
      }
      if (!this.arm.textCopy && c.reply.structuredContent) c.reply = { ...c.reply, content: [] };
      return c.reply;
    } catch (e) {
      c.error = String(e.message);
      throw e;
    }
  }
  async beginTask(task) {
    this.trace = [];
    await this.fake.mode({
      mode: 'healthy',
      remaining: task.id === 'T11' ? 0 : task.id === 'T9' ? 3 : 2,
    });
    this.before = await this.fake.stats();
  }
  async finishTask() {
    const after = await this.fake.stats();
    return {
      trace: this.trace,
      quotaSpent: after.quotaSpent - this.before.quotaSpent,
      providerEvents: after.events.slice(this.before.events.length),
      remainingBefore: this.before.remaining,
    };
  }
  async assertFence() {
    const visit = async (dir) => {
      for (const entry of await readdir(dir, { withFileTypes: true })) {
        const file = path.join(dir, entry.name);
        if (entry.isDirectory()) await visit(file);
        else if (entry.name === 'egress-trap.txt' && (await readFile(file, 'utf8')).length)
          throw new Error('server attempted non-fixture egress');
      }
    };
    await visit(this.home);
  }
  async close() {
    await this.client?.close();
    await this.transport?.close();
    if (this.broker) await new Promise((resolve) => this.broker.close(resolve));
    await this.fake?.close();
  }
}
