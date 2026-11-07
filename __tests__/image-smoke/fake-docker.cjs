#!/usr/bin/env node
// Docker-command double only. The server, bridge, HTTPS fake and fences remain real.
// This cannot corroborate image layers, namespaces, mounts or Docker cleanup.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.IMAGE_SMOKE_TEST_ROOT;
const state = process.env.IMAGE_SMOKE_TEST_STATE;
const args = process.argv.slice(2);
const verb = args.shift();
const record = (name) => path.join(state, name + '.json');
let mapping = [['/app', root]];
const translate = (s) => mapping.reduce((v, [from, to]) => v.replaceAll(from, to), s);
function launch(command, childArgs, env, name, image, cwd) {
  const child = spawn(command, childArgs, { env, cwd, stdio: ['pipe', 'pipe', 'inherit'] });
  if (name) fs.writeFileSync(record(name), JSON.stringify({ pid: child.pid, mapping, env, image }));
  child.on('error', (e) => { process.stderr.write(e.message); process.exitCode = 1; });
  process.on('SIGTERM', () => child.kill('SIGTERM'));
  process.on('SIGINT', () => child.kill('SIGINT'));
  child.on('exit', (code) => { process.exitCode = code ?? 1; });
  if (command.endsWith('/python')) {
    process.stdin.pipe(child.stdin);
    child.stdout.pipe(process.stdout);
    return;
  }
  let input = '';
  process.stdin.on('data', (d) => {
    input += d;
    let newline;
    while ((newline = input.indexOf('\n')) >= 0) {
      const line = input.slice(0, newline);
      input = input.slice(newline + 1);
      const request = JSON.parse(line);
      if (image === 'no-contact' && request.method === 'tools/call') {
        const data = { rows: [{
          title: 'Rehearsal sequential', ref: 'fixture-ref', source: 'zlibrary', match: 'source_only',
        }], ordering: 'fit-v1', note: 'One fixture result.', all_answered: true, sources: {
          libgen: { state: 'not_asked', reason: 'not_requested' },
          zlibrary: { state: 'rows', rows: 1, ms: 0 },
          annas: { state: 'not_asked', reason: 'not_requested' },
        } };
        process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {
          content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data,
        } }) + '\n');
      } else child.stdin.write(line + '\n');
    }
  });
  process.stdin.on('end', () => child.stdin.end());
  let output = '';
  child.stdout.on('data', (d) => {
    output += d;
    let newline;
    while ((newline = output.indexOf('\n')) >= 0) {
      const line = output.slice(0, newline);
      output = output.slice(newline + 1);
      const response = JSON.parse(line);
      if (image === 'stdout-noise' && response.result?.serverInfo)
        process.stdout.write('not a JSON-RPC message\n');
      if (image === 'wrong-version' && response.result?.serverInfo)
        response.result.serverInfo.version = '0.0.0-wrong';
      if (image === 'wrong-tools' && response.result?.tools)
        response.result.tools.pop();
      if (image === 'wrong-schema' && response.result?.tools)
        response.result.tools.find(t => t.name === 'download').inputSchema.properties.ref.type = 'number';
      if (image === 'wrong-envelope' && response.result?.content)
        response.result.content = [{ type: 'text', text: '{}' }];
      if (image === 'egress' && response.result?.content)
        fs.appendFileSync(env.REHEARSAL_EGRESS_TRAP, 'blocked outbound attempt\n');
      process.stdout.write(JSON.stringify(response) + '\n');
    }
  });
}
if (verb === 'run') {
  let name, command, network, cwd;
  const env = { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1' };
  const variables = [];
  while (args[0]?.startsWith('-')) {
    const flag = args.shift();
    if (['--rm', '-i'].includes(flag)) continue;
    const value = args.shift();
    if (flag === '--name') name = value;
    else if (flag === '--entrypoint') command = value;
    else if (flag === '--user') {}
    else if (flag === '--workdir') cwd = value;
    else if (flag === '--network') network = value;
    else if (flag === '--pull' && value === 'never') {}
    else if (flag === '--env') variables.push(value);
    else if (flag === '--mount') {
      const parts = Object.fromEntries(value.split(',').map((p) => p.split('=')));
      mapping.unshift([parts.dst, parts.src]);
    } else throw new Error('unsupported fake Docker flag: ' + flag);
  }
  const image = args.shift();
  if (command === '/app/.venv/bin/python' && network !== 'none')
    throw new Error('fake provider must have network none');
  if (command === 'node' && !network?.startsWith('container:zlib-image-smoke-'))
    throw new Error('MCP must share the isolated fake namespace');
  for (const v of variables) {
    const equals = v.indexOf('=');
    env[v.slice(0, equals)] = translate(v.slice(equals + 1));
  }
  launch(translate(command), args.map(translate), env, name, image, cwd ? translate(cwd) : undefined);
} else if (verb === 'exec') {
  const name = args.shift();
  const container = JSON.parse(fs.readFileSync(record(name)));
  mapping = container.mapping;
  const command = args.shift();
  // Failure control: simulate a fence that didn't leave evidence. The real
  // canary's unblocked-connect control is separately exercised without sockets.
  if (container.image === 'missing-canary' && args.includes('/smoke/controls/egress-canary.py'))
    process.exit(0);
  launch(translate(command), args.map(translate), container.env, null);
} else if (verb === 'rm') {
  if (args.shift() !== '--force') throw new Error('expected force cleanup');
  for (const name of args) {
    if (fs.existsSync(record(name))) {
      const { pid } = JSON.parse(fs.readFileSync(record(name)));
      try { process.kill(pid, 'SIGTERM'); } catch (e) { if (e.code !== 'ESRCH') throw e; }
      fs.unlinkSync(record(name));
    }
  }
} else throw new Error('unsupported fake Docker verb: ' + verb);
