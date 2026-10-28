// E task runner plumbing. Dry-run is the default; model execution retains clean
// evaluator/build/runtime admission and remains outside the A/B comparison bar.
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { randomUUID, createHash } from 'node:crypto';
import path from 'node:path';
import { eTasks, eTaskForCase } from './tasks.js';
import { answerInstruction } from './answers.js';
import { scoreTask, dataOf, rowsOf } from './oracles.js';
import { EDiagnosticServer } from './e-server.js';
import { root, here, evalSnapshot } from './server.js';
import { pdfRuntime } from './e-pdf-runtime.js';
import { pinnedInputs, verifyBuildArtifacts, artifactProofErrors } from './build-proof.js';
import { verifyEvaluatorIdentity } from './evaluator-identity.js';
import { verifyNodeRuntime, nodeProofErrors } from './node-runtime.js';
import { verifyPythonRuntime, pythonProofErrors } from './python-runtime.js';
import { commandFor, runDriver, driverPins, reconcileAttempts } from './drivers.js';
export async function runEModelTasks(opts) {
  if (opts['self-test']) throw new Error('Use --e-self-test for scripted E diagnostics');
  if (!['cold', 'shared'].includes(opts['session-mode']))
    throw new Error('session-mode must be cold or shared');
  const cases = (
    opts['e-cases'] ??
    [
      ...eTasks.map((t) => t.id),
      'E4-second',
      'E11-first',
      'E6-rows',
      'E16-first',
      'E13-real-timeout',
    ].join(',')
  ).split(',');
  const selected = cases.map(eTaskForCase);
  const coldSession = opts['cold-session'] ?? opts['session-mode'] === 'cold';
  const receipt = {
    date: new Date().toISOString(),
    kind: 'E task trials; outside comparison bar',
    coldSession,
    modelCalls: 0,
    compileTimeoutMs: 180000,
    tasks: selected.map((t) => ({ ...t, answerInstruction: answerInstruction(t) })),
    results: [],
    commands: [],
  };
  await mkdir(path.dirname(path.resolve(opts.out)), { recursive: true });
  const persist = () => {
    receipt.totals = eRunTotals(receipt.results);
    return writeFile(opts.out, JSON.stringify(receipt, null, 2) + '\n');
  };
  if (!opts.paid) {
    // Planning uses explicit placeholders for state created only by a live
    // server/session. commandFor is shared with execution; no server is started.
    receipt.commands = selected.map((task) => {
      const directory = path.resolve(opts.out + '.artifacts', `0-${task.caseId}-DRY-RUN`),
        socketPath = path.join(directory, 'broker.sock'),
        downloads = path.join(directory, 'downloads'),
        command = commandFor({
          driver: opts.driver ?? 'codex',
          directory,
          socketPath,
          toolSearch: opts['tool-search'],
          nonce: 'DRY-RUN-NONCE',
          model: opts.model,
        }),
        prompt = `${task.prompt}\nUse ${downloads} for downloads.${answerInstruction(task)}`;
      return {
        run: 0,
        task: task.caseId,
        ...command,
        prompt,
        argv: [...command.args, prompt],
        placeholders:
          'DRY-RUN directory, broker socket, downloads and nonce are replaced at execution; no server or driver started',
      };
    });
    receipt.status = 'dry-run; no server or model started';
    await persist();
    process.stdout.write(`C/F totals: ${JSON.stringify(receipt.totals)}\n`);
    return;
  }
  // This branch is implemented for the lead's later authorized run; diagnostics
  // never enter it. Dirty evaluator work cannot reach a driver.
  if (!driverPins[opts.driver]) throw new Error('unknown driver');
  if (path.resolve(opts['server-root']) !== root)
    throw new Error('E trials require this pinned evaluator worktree');
  const evaluator = await verifyEvaluatorIdentity(),
    production = await pinnedInputs(root);
  const proof = await verifyBuildArtifacts({
    buildRoot: root,
    pin: production.sha,
    compileTimeoutMs: receipt.compileTimeoutMs,
  });
  const node = await verifyNodeRuntime({ root }),
    python = await verifyPythonRuntime({ buildRoot: root });
  const errors = [
    ...artifactProofErrors(proof, { sha: production.sha, source: await evalSnapshot(root) }),
    ...nodeProofErrors(node, evaluator.files),
    ...pythonProofErrors(python, { artifactProof: proof }),
  ];
  if (errors.length) throw new Error(errors.join('; '));
  receipt.pdfRuntime = selected.some((t) => t.reader) ? pdfRuntime() : null;
  receipt.admission = {
    evaluator,
    production: production.inputs,
    head: production.sha,
    proof,
    node,
    python,
  };
  const checkIdentity = async () => {
    const fresh = await pinnedInputs(root),
      identity = await verifyEvaluatorIdentity();
    if (
      fresh.sha !== production.sha ||
      fresh.inputs.sha256 !== production.inputs.sha256 ||
      identity.sha256 !== evaluator.sha256
    )
      throw new Error('E trial source identity drift');
    const n = await verifyNodeRuntime({ root }),
      p = await verifyPythonRuntime({ buildRoot: root });
    if (receipt.pdfRuntime && JSON.stringify(pdfRuntime()) !== JSON.stringify(receipt.pdfRuntime))
      throw new Error('PDF runtime drift');
    if (n.sha256 !== node.sha256 || p.sha256 !== python.sha256)
      throw new Error('E trial runtime drift');
  };
  const runs = Number(opts.runs);
  if (!Number.isInteger(runs) || runs < 1) throw new Error('positive integer runs required');
  let server, session, signature;
  try {
    for (let run = 0; run < runs; run++)
      for (const task of selected) {
        const nextSignature = JSON.stringify({
          reader: !!task.reader,
          noAccount: task.id === 'E18' && task.variant !== 'malformed',
          realTimeout: task.variant === 'real-timeout',
        });
        const fresh = coldSession || !server || signature !== nextSignature;
        if (fresh) {
          await server?.close();
          server = await EDiagnosticServer.start(task);
          await server.listen();
          session = undefined;
        } else {
          await server.fake.mode({ mode: 'healthy', reset: true, remaining: 2 });
          await server.configure(task);
        }
        signature = nextSignature;
        const context = {
          variant: task.variant,
          noAccount: task.id === 'E18' && task.variant !== 'malformed',
        };
        let setup = '';
        if (task.reader) {
          const bytes = execFileSync(
            path.join(root, '.venv/bin/python'),
            [
              '-B',
              '-c',
              `import sys;sys.path.insert(0,${JSON.stringify(path.join(root, '__tests__/rehearsal'))});from e_pdf_fixture import pdf_fixture;sys.stdout.buffer.write(pdf_fixture(${task.id === 'E15' ? 'True' : 'False'}))`,
            ],
            {
              env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1' },
              maxBuffer: 2 * 1024 * 1024,
            },
          );
          context.file = path.join(server.downloads, 'reader.pdf');
          context.fixtureId = task.id === 'E14' ? 507 : 508;
          context.pdfSha256 = createHash('sha256').update(bytes).digest('hex');
          await writeFile(context.file, bytes);
          setup = `Supplied file: ${context.file}. `;
        }
        if (task.id === 'E12') {
          const rows = rowsOf(
            dataOf(await server.call('search', { query: 'Leavey Glas', sources: ['zlibrary'] })),
          );
          const row = rows.find((r) => r.title === 'Glas');
          const detail = dataOf(await server.call('get_book', { ref: row.ref }));
          await server.restart();
          setup = `Previous turn selected: ${JSON.stringify(detail)}. `;
        }
        const directory = path.resolve(
          opts.out + '.artifacts',
          `${run}-${task.caseId}-${randomUUID()}`,
        );
        await mkdir(directory, { recursive: true });
        await writeFile(
          path.join(directory, 'mcp.json'),
          JSON.stringify({
            mcpServers: {
              books: {
                command: process.execPath,
                args: [path.join(here, 'proxy.mjs'), server.socketPath],
                alwaysLoad: !opts['tool-search'],
              },
            },
          }),
        );
        const command = commandFor({
          driver: opts.driver,
          directory,
          socketPath: server.socketPath,
          toolSearch: opts['tool-search'],
          session,
          nonce: randomUUID(),
          model: opts.model,
        });
        receipt.commands.push({ run, task: task.caseId, ...command });
        const raw = await runDriver(
          command,
          `${setup}${task.prompt}\nUse ${server.downloads} for downloads.${answerInstruction(task, server.arm)}`,
          directory,
          { beforeSpawn: checkIdentity },
        );
        receipt.modelCalls++;
        const otherTools = raw.events.filter(
          (e) =>
            e.type === 'item.completed' &&
            ['command_execution', 'web_search', 'file_change'].includes(e.item?.type),
        );
        if (otherTools.length) throw new Error('driver used non-evaluation tools');
        const observed = await server.finishTask(),
          attempts = reconcileAttempts(raw.events, observed.trace);
        if (attempts.coverage.unavailable.length || attempts.trace.some((c) => c.unaccounted))
          throw new Error('incomplete driver attempt telemetry');
        if (task.id === 'E13' && task.variant === 'real-timeout')
          context.timeoutReceipt = JSON.parse(
            await readFile(path.join(server.home, 'timeout-branch.json'), 'utf8'),
          );
        const sample = {
          task,
          arm: server.arm,
          ...observed,
          trace: attempts.trace,
          context,
          downloads: server.downloads,
          final: raw.final,
        };
        const score = await scoreTask(sample);
        receipt.results.push({
          run,
          caseId: task.caseId,
          coldSession: fresh,
          ...sample,
          score,
          session: raw.session,
          rawFiles: raw.rawFiles,
          rawUsage: raw.usage,
          attemptCoverage: attempts.coverage,
        });
        session = coldSession ? undefined : raw.session;
        await server.assertFence();
        await checkIdentity();
        await persist();
      }
    receipt.status = 'E trials complete; not comparison admission or general model readiness';
  } catch (error) {
    receipt.status = 'failed';
    receipt.error = String(error.stack);
    throw error;
  } finally {
    await server?.close();
    await persist();
    process.stdout.write(`C/F totals: ${JSON.stringify(receipt.totals)}\n`);
  }
}

export function eRunTotals(results) {
  const current = { cases: 0, passed: 0, failed: 0 },
    future = { cases: 0, gapOnly: 0, wrong: 0 };
  for (const { task, expectsFutureContract, context, score } of results) {
    if (task?.futureOnly || expectsFutureContract || context?.expectsFutureContract) {
      future.cases++;
      future[
        !score.success &&
        score.contractGaps?.length &&
        score.wrongDecisions === score.contractGaps.length
          ? 'gapOnly'
          : 'wrong'
      ]++;
    } else {
      current.cases++;
      current[score.success ? 'passed' : 'failed']++;
    }
  }
  return { current, future };
}
