// Current-interface scripted diagnostics. No driver, paid admission or settled-A override.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { mkdir } from 'node:fs/promises';
import {createServer} from 'node:net';
import {createInterface} from 'node:readline';
import path from 'node:path';
import { EvaluationServer, root, here } from './server.js';
import { Rehearsal } from '../rehearsal/harness.js';
import { pythonDescriptor } from './python-runtime.js';
import {pdfRuntime} from './e-pdf-runtime.js';

export class EDiagnosticServer extends EvaluationServer {
  static async start(task) {
    const s = new EDiagnosticServer();
    s.arm = {id:'E-current-diagnostic', build:'B', history:true, textCopy:true};
    s.trace = [];
    s.observed = new Map(); s.rows = new Map(); s.refRows = new Map();
    try {
      s.fake = await Rehearsal.start('E-diagnostic', {python:pythonDescriptor(root),startupTimeoutMs:30000});
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
        PYTHON_BRIDGE_TIMEOUT:'60000', LOG_LEVEL:'error',
      };
      if (task.id === 'E18' && task.variant !== 'malformed') {
        delete env.ZLIBRARY_EMAIL; delete env.ZLIBRARY_PASSWORD;
      }
      if (task.reader) Object.assign(env, {ZLIBRARY_CHAT_FILES:'1', ZLIBRARY_CHAT_FILES_MAX_BYTES:'1000000',
        ZLIBRARY_PDF_PYTHON:pdfRuntime().executable});
      if(task.id === 'E13' && task.variant === 'real-timeout') Object.assign(env,{EVAL_REAL_ACQUISITION_TIMEOUT:'1',EVAL_TIMEOUT_RECEIPT:path.join(s.home,'timeout-branch.json'),PYTHON_BRIDGE_LONG_TIMEOUT:'120000'});
      s.env = env; s.generation = 0; s.reader = task.reader === true;
      await s.connect();
      await s.configure(task);
      return s;
    } catch(error) { await s.close(); throw error; }
  }
  async configure(task) {
    await this.beginTask(task);
      const modes = {E5:'e_person', E7:'e_transient',
        E17:({person:'e_person',pacing:'e_pacing',runtime:'e_runtime',ceiling:'e_ceiling'})[(!task.variant || task.variant === 'default')?'person':task.variant],
        E18:task.variant === 'malformed' ? 'e_malformed' : undefined};
      await this.fake.mode({mode:modes[task.id] || 'e_catalogue'});
      if(task.id === 'E4' && task.variant === 'second') await this.fake.mode({mode:'e_graver_second'});
      if(task.id === 'E11' && task.variant === 'first') await this.fake.mode({mode:'e_target_first'});
      if(['E6','E8'].includes(task.id) && task.variant === 'rows') await this.fake.mode({mode:'e_annas_rows'});
      if(task.id === 'E8' && task.variant === 'blocked') await this.fake.mode({mode:'e_person'});
      if (task.id === 'E16') await this.fake.mode({mode:task.variant === 'first'?'e_history_first':'e_history'});
      if (task.id === 'E13') await this.fake.mode({mode:task.variant === 'real-timeout'?'e_real_timeout':'e_outcome_unknown'});
  }
  async connect() {
      this.generation++;
      this.transport = new StdioClientTransport({command:process.execPath,
        args:['--import',path.resolve(here,'../rehearsal/node-isolation.mjs'),path.join(root,'dist/index.js')],
        cwd:this.home, env:this.env, stderr:'pipe'});
      this.stderr = '';
      this.transport.stderr.on('data', d => {this.stderr += d;});
      this.client = new Client({name:'E-scripted-diagnostic',version:'1'});
      await this.client.connect(this.transport);
      // Diagnostic scheduling budget only; preserve the actual tool and provider behavior.
      const callTool=this.client.callTool.bind(this.client);
      this.client.callTool=(request,schema,options)=>callTool(request,schema,{...options,timeout:150000});
      this.tools = (await this.client.listTools()).tools;
      const names = ['download','get_book','get_history','get_limits','search',
        ...(this.reader ? ['get_book_map','share_book_file','get_page_image'] : [])].sort();
      if (this.tools.map(t => t.name).sort().join(',') !== names.join(','))
        throw new Error('E diagnostic tool exposure mismatch');
  }
  async listen() {
    this.socketPath=path.join(this.fake.temp,'broker.sock');
    this.broker=createServer(socket=>{
      createInterface({input:socket}).on('line',async line=>{
        try {const req=JSON.parse(line);socket.end(JSON.stringify({result:req.method === 'list'?{tools:this.tools}:await this.call(req.name,req.args)})+'\n');}
        catch(error){socket.end(JSON.stringify({error:String(error.message)})+'\n');}
      });
    });
    await new Promise((resolve,reject)=>{this.broker.once('error',reject);this.broker.listen(this.socketPath,resolve);});
  }
  async restart() {
    await this.client.close(); await this.transport.close();
    this.observed.clear(); this.rows.clear(); this.refRows.clear();
    await this.connect();
  }
  async call(name,args={}) {
    const startedAt = performance.now(), index = this.trace.length;
    try {return await super.call(name,args);}
    finally { Object.assign(this.trace[index], {generation:this.generation, startedAt,finishedAt:performance.now()}); }
  }
}
