#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { writeFile, mkdir, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { tasks, arms } from './tasks.js';
import { resolveBaselinePin } from './build-proof.js';
import { EvaluationServer, evalSnapshot, root, here } from './server.js';
import { dataOf, rowsOf, scoreTask } from './oracles.js';
import {
  commandFor,
  runDriver,
  driverPins,
  createUsageTracker,
  reconcileAttempts,
} from './drivers.js';
import { answerInstruction } from './answers.js';
import { receiptSchemaVersion, measuredStatus } from './admission.js';
import { scriptedTask } from './scripted.js';

const { values: opts } = parseArgs({
  options: {
    'self-test': { type: 'boolean' },
    'e-self-test': { type: 'boolean' },
    'e-cases': {type:'string'},
    suite:{type:'string',default:'T'},
    'cold-session':{type:'boolean'},
    'session-mode':{type:'string',default:'cold'},
    'dry-run': { type: 'boolean' },
    paid: { type: 'boolean' },
    'server-root': { type: 'string', default: root },
    'a-pin': { type: 'string' },
    'a-root': { type: 'string', default: root },
    arm: { type: 'string', default: 'A' },
    driver: { type: 'string', default: 'codex' },
    model: { type: 'string' },
    'tool-search': { type: 'boolean' },
    runs: { type: 'string', default: '5' },
    'warm-runs': { type: 'string', default: '1' },
    'default-order-env': { type: 'string', default: 'BOOK_SOURCE_DEFAULT_ORDER' },
    out: { type: 'string', default: path.resolve(root, 'test_output/agent-eval.json') },
  },
});
if (opts['e-self-test']) {
  if (opts.paid || opts['self-test'] || opts['dry-run'])
    throw new Error('E diagnostics cannot be combined with paid or other runner modes');
  const { runESelfTest } = await import('./e-self-test.js');
  const receipt = await runESelfTest(opts.out,{...(opts['e-cases']?{cases:opts['e-cases'].split(',')}:{})});
  process.stdout.write(`${receipt.status}; ${receipt.results.length} cold cases; ${opts.out}\n`);
  process.exit(0);
}
if(opts.suite.toUpperCase() === 'E') {
  const {runEModelTasks}=await import('./e-runner.js');
  await runEModelTasks(opts);
  process.exit(0);
}
if(opts.suite.toUpperCase() !== 'T') throw new Error('suite must be T or E');
const arm = arms.find((a) => a.id === opts.arm);
if (!arm) throw new Error('unknown arm');
if (!driverPins[opts.driver]) throw new Error('unknown driver');
if (opts['tool-search'] && opts.driver !== 'claude')
  throw new Error('tool-search-on arm is Claude-only');
const repetitions = Number(opts.runs),
  warmRuns = Number(opts['warm-runs']);
if (
  !Number.isInteger(repetitions) ||
  repetitions < 1 ||
  !Number.isInteger(warmRuns) ||
  warmRuns < 1
)
  throw new Error('positive integer run counts required');
if (opts.paid && (opts['self-test'] || opts['dry-run']))
  throw new Error('choose paid OR offline mode');
const selfTest = opts['self-test'];
if (selfTest && arm.build !== 'A') throw new Error('self-test targets A; B is not required');
const baseline = await resolveBaselinePin(
  arm.build === 'A' ? opts['server-root'] : opts['a-root'],
  opts['a-pin'],
);
const plan = {
  schemaVersion: receiptSchemaVersion,
  fencePassed: false,
  date: new Date().toISOString(),
  baselinePin: baseline.pin,
  baselineSelection: baseline,
  arm,
  driver: opts.driver,
  settings: {
    ...driverPins[opts.driver],
    model: opts.model ?? driverPins[opts.driver].model,
    toolSearch: !!opts['tool-search'],
    temperature: 'driver default',
    repetitions,
    warmRuns,
    defaultOrderEnv: opts['default-order-env'],
    cell: 'one T1–T12 episode; cold=fresh driver session; warm=complete episode continued in that same session',
    coldCaveat:
      'Fresh session is not a provider cache flush. First-request cache evidence is recorded; unknown/hit does not establish strict cold eligibility.',
    outputCache:
      'drivers expose no cached-output split; outputCached=null, outputUncached=output (generated output)',
  },
  tasks,
  results: [],
  commands: [],
};
await mkdir(path.dirname(opts.out), { recursive: true });
if (!selfTest && !opts.paid) {
  const example = commandFor({
    driver: opts.driver,
    directory: '/RUN/cell',
    socketPath: '/RUN/broker.sock',
    toolSearch: opts['tool-search'],
    nonce: 'UNIQUE-CELL-NONCE',
    model: opts.model,
  });
  plan.commands.push(example);
  plan.status = 'dry-run; no server or model started';
  await writeFile(opts.out, JSON.stringify(plan, null, 2) + '\n');
  process.stdout.write(`Dry-run plan written to ${opts.out}; no model invoked.\n`);
} else {
  let s;
  try {
    s = await EvaluationServer.start({
      buildRoot: opts['server-root'],
      pin: arm.build === 'A' ? baseline.pin : undefined,
      arm,
      defaultOrderEnv: opts['default-order-env'],
    });
  } catch (error) {
    plan.status = 'failed';
    plan.error = String(error.stack);
    plan.sourceStable = false;
    await writeFile(opts.out, JSON.stringify(plan, null, 2) + '\n');
    throw error;
  }
  let failure;
  try {
    plan.evaluator = s.evaluator;
    plan.python = s.python;
    plan.node = s.node;
    plan.build = {
      root: s.buildRoot,
      sha: s.sha,
      source: s.sourceStart,
      tools: s.measurement,
      artifactProof: s.artifactProof,
      node: s.buildNode,
    };
    if (selfTest) {
      // Falsifiers use the real running A server, not a second fake protocol server.
      for (const args of [
        { query: 'definitely absent zqxv' },
        { query: 'Persuasion', extensions: ['epub'] },
        { query: 'Being and Time' },
      ]) {
        assert.equal(rowsOf(dataOf(await s.call('search_books', args))).length, 0);
      }
      assert.equal(
        rowsOf(
          dataOf(
            await s.call('search_multi_source', {
              query: 'definitely absent zqxv',
              source: 'libgen',
            }),
          ),
        ).length,
        0,
      );
      await s.fake.mode({ mode: 'empty' });
      assert.equal(rowsOf(dataOf(await s.call('search_books', { query: 'Persuasion' }))).length, 0);
      await s.fake.mode({ mode: 'healthy' });
      const page2 = await s.fake.request('/__stats');
      assert(page2.events.length > 0);
      // Driver-facing proxy must preserve measured schemas and transformed results.
      const { Client } = await import('@modelcontextprotocol/sdk/client/index.js');
      const { StdioClientTransport } = await import('@modelcontextprotocol/sdk/client/stdio.js');
      const proxy = new Client({ name: 'proxy-control', version: '1' });
      const transport = new StdioClientTransport({
        command: process.execPath,
        args: [path.join(here, 'proxy.mjs'), s.socketPath],
      });
      try {
        await proxy.connect(transport);
        assert.deepEqual((await proxy.listTools()).tools, s.tools);
        const before = s.trace.length;
        await assert.rejects(
          () => proxy.callTool({ name: 'get_history', arguments: { limit: 10 } }),
          /disabled or unknown/,
        );
        const rejected = s.trace.slice(before);
        const scored = await scoreTask({
          task: tasks.find((t) => t.id === 'T7'),
          arm: arms.find((a) => !a.history),
          trace: rejected,
          final: 'Account history is not available in this configuration.',
          downloads: s.downloads,
        });
        assert.equal(scored.calls, 1);
        assert.equal(scored.invalidCalls, 1);
        assert.equal(scored.success, false);
        assert.equal(rejected[0].forwarded, false);
        plan.invalidAttemptControl = {
          kind: 'real A proxy unknown-call control, not B runtime',
          trace: rejected,
          score: scored,
        };
      } finally {
        await proxy.close();
        await transport.close();
      }
    }
    for (let run = 0; run < (selfTest ? 1 : repetitions); run++) {
      // Each replicate uses fresh session identity. Tool handles survive all turns.
      let session;
      const usageTracker = createUsageTracker(opts.driver, {
        version: driverPins[opts.driver].version,
      });
      const nonce = randomUUID(),
        context = {};
      const directory = path.join(path.resolve(opts.out + '.artifacts'), `driver-${run}-${nonce}`);
      await mkdir(directory, { recursive: true });
      await writeFile(
        path.join(directory, 'mcp.json'),
        JSON.stringify({
          mcpServers: {
            books: {
              command: process.execPath,
              args: [path.join(here, 'proxy.mjs'), s.socketPath],
              alwaysLoad: !opts['tool-search'],
            },
          },
        }),
      );
      for (let phaseIndex = 0; phaseIndex < (selfTest ? 1 : 1 + warmRuns); phaseIndex++) {
        const phase = phaseIndex === 0 ? 'cold' : 'warm';
        s.downloads = path.join(s.artifactRoot, `run-${run}-${phaseIndex}`);
        await mkdir(path.join(s.downloads, 'inbox'), { recursive: true });
        for (const task of tasks) {
          // T6 gets an actual observed page/cursor, not a made-up continuation.
          let setup = '';
          if (task.id === 'T6') {
            const args =
              arm.build === 'A'
                ? { query: 'Phenomenology', count: 10 }
                : { query: 'Phenomenology', sources: ['zlibrary'], limit: 10 };
            const first = dataOf(await s.call(arm.build === 'A' ? 'search_books' : 'search', args));
            context.cursor = first.next_cursor;
            setup = `The first ten were already shown: ${JSON.stringify(first)}. `;
          }
          await s.beginTask(task);
          const prompt = `${setup}${task.prompt}\nUse ${s.downloads} for downloads; the inbox is ${path.join(s.downloads, 'inbox')}. Answer this task only.${answerInstruction(task, arm)}`;
          let final,
            usage = null,
            raw,
            taskFailure;
          try {
            if (selfTest) final = await scriptedTask(s, task, context);
            else {
              const command = commandFor({
                driver: opts.driver,
                directory,
                socketPath: s.socketPath,
                toolSearch: opts['tool-search'],
                session,
                nonce,
                model: opts.model,
              });
              plan.commands.push({ run, phase, task: task.id, ...command });
              raw = await runDriver(command, prompt, directory, {
                beforeSpawn: () => s.assertNodeIdentity(),
              });
              const otherTools = raw.events.filter(
                (e) =>
                  e.type === 'item.completed' &&
                  ['command_execution', 'web_search', 'file_change'].includes(e.item?.type),
              );
              if (otherTools.length)
                throw new Error(
                  'driver used non-evaluation tools; cell invalid (raw output retained)',
                );
              await writeFile(
                path.join(directory, `${phaseIndex}-${task.id}.jsonl`),
                raw.events.map((e) => JSON.stringify(e)).join('\n') + '\n',
              );
              if (!raw.session && !session)
                throw new Error(
                  'driver omitted session ID; warm continuation cannot be established',
                );
              if (plan.driverVersion && plan.driverVersion !== raw.version)
                throw new Error('CLI changed within campaign');
              plan.driverVersion = raw.version;
              usage = usageTracker.consume(raw.usage, raw.session, session);
              session = raw.session;
              final = raw.final;
            }
          } catch (error) {
            taskFailure = error;
            raw = error.driverResult ?? raw;
            final ??= '';
          }
          if (raw?.driverEnvironment) {
            if (
              plan.driverEnvironment &&
              plan.driverEnvironment.sha256 !== raw.driverEnvironment.sha256
            )
              taskFailure ??= new Error('driver environment changed within cell');
            plan.driverEnvironment ??= raw.driverEnvironment;
          }
          const observations = await s.finishTask();
          const attempts = reconcileAttempts(raw?.events ?? [], observations.trace);
          observations.trace = attempts.trace;
          observations.attemptCoverage = attempts.coverage;
          if (attempts.coverage.unavailable.length || attempts.trace.some((c) => c.unaccounted))
            taskFailure ??= new Error('incomplete driver attempt telemetry; cell invalid');
          if (selfTest && task.id === 'T7') {
            const visit = async (dir) => {
              for (const e of await readdir(dir, { withFileTypes: true })) {
                const file = path.join(dir, e.name);
                if (e.isDirectory()) await visit(file);
                else
                  assert(
                    !(await readFile(file)).includes(Buffer.from('Synthetic fixture 901\n')),
                    'history is not disk presence',
                  );
              }
            };
            await visit(s.artifactRoot);
          }
          const scored = await scoreTask({
            task,
            arm,
            ...observations,
            final,
            downloads: s.downloads,
            context,
          });
          if (taskFailure) scored.success = false;
          plan.results.push({
            run,
            phase,
            phaseIndex,
            session: session ?? raw?.session ?? 'scripted',
            requestedModel: plan.settings.model,
            driverVersion: raw?.version,
            rawUsage: raw?.usage,
            rawFiles: raw?.rawFiles,
            capture: raw?.capture,
            driverEnvironment: raw?.driverEnvironment,
            ...scored,
            usage,
            final,
            ...observations,
            context: { ...context },
            events: raw?.events,
            driverError: taskFailure ? String(taskFailure.message) : undefined,
          });
          // Incremental durable output survives driver errors, interruption, or later failed bars.
          await writeFile(opts.out, JSON.stringify(plan, null, 2) + '\n');
          if (taskFailure) throw taskFailure;
          if (selfTest)
            assert.equal(scored.success, true, `${task.id}: ${JSON.stringify(scored.wrong)}`);
        }
      }
    }
    await s.assertFence();
    plan.fencePassed = true;
    plan.status = selfTest ? 'self-test pass (scripted A; NOT model performance)' : measuredStatus;
    plan.falsifiers = selfTest
      ? [
          'unknown queries empty on both sources',
          'format mismatch empty',
          'history-only book absent from search',
          'mode=empty respected',
          'proxy tool schemas unchanged',
          'T1–T12 oracle paths + artifact bytes checked',
        ]
      : undefined;
  } catch (e) {
    failure = e;
    plan.status = 'failed';
    plan.error = String(e.stack);
  } finally {
    // Persist source/cleanup failures as failed evidence, never measured success.
    try {
      const identities = await s.finishIdentity();
      plan.evaluatorEnd = identities.evaluator;
      plan.pythonEnd = identities.python;
      plan.nodeEnd = identities.node;
      plan.build.nodeEnd = identities.buildNode;
      plan.nodeStable = true;
      plan.pythonStable = true;
      plan.evaluatorStable = plan.evaluatorEnd.sha256 === s.evaluator.sha256;
      if (!plan.evaluatorStable) throw new Error('evaluator source drift during evaluation');
    } catch (error) {
      plan.evaluatorStable = false;
      plan.pythonStable = false;
      plan.nodeStable = false;
      plan.sourceError = error.message;
      failure ??= error;
    }
    try {
      const end = await evalSnapshot(s.buildRoot);
      plan.sourceStable =
        JSON.stringify(s.sourceStart.trees) === JSON.stringify(end.trees) &&
        s.sourceStart.sha === end.sha;
      if (!plan.sourceStable) throw new Error('production source drift during evaluation');
    } catch (error) {
      plan.sourceStable = false;
      plan.sourceError = String(error.message);
      failure ??= error;
    }
    try {
      await s.close();
    } catch (error) {
      plan.cleanupError = String(error.message);
      failure ??= error;
    }
    if (failure) {
      plan.status = 'failed';
      plan.error ??= String(failure.stack);
    }
    await writeFile(opts.out, JSON.stringify(plan, null, 2) + '\n');
  }
  if (failure) throw failure;
  process.stdout.write(
    `${plan.status}; ${plan.results.length} tasks; ${s.measurement.count} tools, ${s.measurement.serializedCharacters} characters. Receipt: ${opts.out}\n`,
  );
}
