// Current-interface scripted diagnostics. No driver, paid admission or settled-A override.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { EvaluationServer, root, here } from './server.js';
import { Rehearsal } from '../rehearsal/harness.js';
import { pythonDescriptor } from './python-runtime.js';

export class EDiagnosticServer extends EvaluationServer {
  static async start(task) {
    const s = new EDiagnosticServer();
    s.arm = {id:'E-current-diagnostic', build:'B', history:true, textCopy:true};
    s.trace = [];
    s.observed = new Map(); s.rows = new Map(); s.refRows = new Map();
    try {
      s.fake = await Rehearsal.start('E-diagnostic', {python:pythonDescriptor(root)});
      s.home = path.join(s.fake.temp, 'server');
      s.downloads = path.join(s.home, 'downloads');
      s.artifactRoot = s.downloads;
      await mkdir(s.downloads, {recursive:true});
      const env = {
        PATH:process.env.PATH, HOME:s.home, TMPDIR:s.home,
        ...pythonDescriptor(root).env,
        SSL_CERT_FILE:s.fake.cert, REHEARSAL_ALLOWED_PORT:String(new URL(s.fake.origin).port),
        REHEARSAL_EGRESS_TRAP:path.join(s.home,'egress-trap.txt'),
        REHEARSAL_LIBGEN_ORIGIN:s.fake.origin, REHEARSAL_LOG_DIR:path.join(s.home,'logs'),
        ZLIBRARY_EAPI_DOMAIN:s.fake.domain, ZLIBRARY_EMAIL:'rehearsal-account@example.invalid',
        ZLIBRARY_PASSWORD:'rehearsal-password-canary', ZLIBRARY_SESSION_DIR:path.join(s.home,'session'),
        XDG_STATE_HOME:s.home, XDG_CACHE_HOME:s.home, XDG_CONFIG_HOME:s.home,
        MCP_READABLE_ROOTS:s.downloads, ZLIBRARY_OUTPUT_ROOT:s.downloads,
        BOOK_SOURCE_PREFLIGHT:'false', BOOK_SOURCE_FALLBACK_ENABLED:'false',
        ANNAS_BASE_URL:s.fake.origin+'/annas', RETRY_MAX_RETRIES:'0',
        PYTHON_BRIDGE_TIMEOUT:'15000', LOG_LEVEL:'error',
      };
      s.transport = new StdioClientTransport({command:process.execPath,
        args:['--import',path.resolve(here,'../rehearsal/node-isolation.mjs'),path.join(root,'dist/index.js')],
        cwd:s.home, env, stderr:'pipe'});
      s.stderr = '';
      s.transport.stderr.on('data', d => {s.stderr += d;});
      s.client = new Client({name:'E-scripted-diagnostic',version:'1'});
      await s.client.connect(s.transport);
      s.tools = (await s.client.listTools()).tools;
      if (s.tools.map(t => t.name).sort().join(',') !== 'download,get_book,get_history,get_limits,search')
        throw new Error('E diagnostics require the current five-tool interface');
      await s.beginTask(task);
      if (task.id === 'E16') await s.fake.mode({mode:'e_history'});
      if (task.id === 'E13') await s.fake.mode({mode:'e_outcome_unknown'});
      return s;
    } catch(error) { await s.close(); throw error; }
  }
}
