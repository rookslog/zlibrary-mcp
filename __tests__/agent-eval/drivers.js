import { driverEnvironmentRecord } from './driver-environment.js';
import { spawn, execFileSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { here } from './server.js';

export const driverPins = {
  claude: { binary: 'claude', version: '2.1.303', model: 'claude-opus-5-5', effort: 'high' },
  codex: { binary: 'codex', version: '0.165.0', model: 'gpt-6.1-sol', effort: 'high' },
};
export function parseEvents(text) {
  return text
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return { unparsed: line };
      }
    });
}
export function usageFrom(driver, events) {
  if (driver === 'codex') {
    const usages = events.filter((e) => e.type === 'turn.completed').map((e) => e.usage);
    if (!usages.length)
      throw new Error('missing Codex turn.completed usage; do not substitute estimates');
    let previous;
    for (const u of usages) {
      validateCounters(u);
      if (previous) counterDelta(u, previous);
      previous = u;
    }
    const rawCounters = structuredClone(usages.at(-1));
    return {
      ...metrics(rawCounters),
      scope: 'session-cumulative',
      rawCounters,
      rawSnapshots: structuredClone(usages),
      cacheFirstRequest: null,
      source: 'turn.completed.usage',
    };
  }
  const result = events.findLast((e) => e.type === 'result');
  if (!result?.usage) throw new Error('missing Claude result.usage; do not substitute estimates');
  for (const k of [
    'input_tokens',
    'output_tokens',
    'cache_read_input_tokens',
    'cache_creation_input_tokens',
  ]) {
    if (!Number.isFinite(result.usage[k]) || result.usage[k] < 0)
      throw new Error('incomplete Claude usage: ' + k);
  }
  const u = result.usage,
    read = u.cache_read_input_tokens ?? 0,
    write = u.cache_creation_input_tokens ?? 0;
  const first = events.find((e) => e.type === 'assistant' && e.message?.usage)?.message.usage;
  return {
    inputTotal: u.input_tokens + read + write,
    inputUncached: u.input_tokens,
    inputCacheRead: read,
    inputCacheWrite: write,
    output: u.output_tokens,
    outputCached: null,
    outputUncached: u.output_tokens,
    cacheFirstRequest: first?.cache_read_input_tokens ?? null,
    source: 'result.usage',
    scope: 'invocation',
  };
}
export function finalFrom(driver, events) {
  if (driver === 'codex')
    return events
      .filter((e) => e.type === 'item.completed' && e.item?.type === 'agent_message')
      .map((e) => e.item.text)
      .join('\n');
  return events.findLast((e) => e.type === 'result')?.result ?? '';
}
export function sessionFrom(driver, events) {
  const ids = events
    .map((e) =>
      driver === 'codex' ? (e.type === 'thread.started' ? e.thread_id : undefined) : e.session_id,
    )
    .filter(Boolean);
  if (new Set(ids).size > 1)
    throw new Error('unexpected session identity changes in driver output');
  return ids[0];
}
const requiredCounters = ['input_tokens', 'cached_input_tokens', 'output_tokens'];
function validateCounters(u) {
  for (const key of requiredCounters)
    if (!Number.isSafeInteger(u?.[key]) || u[key] < 0)
      throw new Error('incomplete/negative Codex usage: ' + key);
  if (u.cached_input_tokens > u.input_tokens) throw new Error('cached input exceeds total');
  if (
    'cache_write_input_tokens' in u &&
    (!Number.isSafeInteger(u.cache_write_input_tokens) || u.cache_write_input_tokens < 0)
  )
    throw new Error('invalid cache-write counter');
}
function metrics(u) {
  return {
    inputTotal: u.input_tokens,
    inputCacheRead: u.cached_input_tokens,
    inputUncached: u.input_tokens - u.cached_input_tokens,
    inputCacheWrite: u.cache_write_input_tokens ?? null,
    output: u.output_tokens,
    outputUncached: u.output_tokens,
    outputCached: null,
  };
}
function counterDelta(current, previous) {
  validateCounters(current);
  validateCounters(previous);
  if ('cache_write_input_tokens' in current !== 'cache_write_input_tokens' in previous)
    throw new Error('incomplete cache-write counter continuity');
  const result = {};
  for (const key of [
    ...requiredCounters,
    ...('cache_write_input_tokens' in current ? ['cache_write_input_tokens'] : []),
  ]) {
    result[key] = current[key] - previous[key];
    if (result[key] < 0) throw new Error('negative cumulative counter delta: ' + key);
  }
  validateCounters(result);
  return result;
}
export function createUsageTracker(driver, { version } = {}) {
  if (driver === 'codex' && version !== '0.162.1')
    throw new Error('Codex cumulative usage scope uncorroborated for CLI ' + version);
  let sessionId, previous;
  return {
    consume(usage, session, expectedSession) {
      if (
        typeof session !== 'string' ||
        !session ||
        (expectedSession && session !== expectedSession) ||
        (sessionId && session !== sessionId)
      )
        throw new Error('unexpected/missing session identity');
      if (driver !== 'codex') {
        if (usage.scope !== 'invocation') throw new Error('unknown invocation usage scope');
        sessionId = session;
        return { ...usage, session };
      }
      if (usage.scope !== 'session-cumulative') throw new Error('unknown Codex usage scope');
      validateCounters(usage.rawCounters);
      const before =
        previous ??
        Object.fromEntries(
          Object.keys(usage.rawCounters)
            .filter((k) => [...requiredCounters, 'cache_write_input_tokens'].includes(k))
            .map((k) => [k, 0]),
        );
      if (usage.rawSnapshots !== undefined) {
        if (!Array.isArray(usage.rawSnapshots) || !usage.rawSnapshots.length)
          throw new Error('incomplete raw snapshots');
        let floor = before;
        for (const snapshot of usage.rawSnapshots) {
          counterDelta(snapshot, floor);
          floor = snapshot;
        }
        for (const key of [...requiredCounters, 'cache_write_input_tokens'])
          if (floor[key] !== usage.rawCounters[key])
            throw new Error('raw snapshot/final counter mismatch');
      }
      const delta = counterDelta(usage.rawCounters, before);
      const result = {
        ...usage,
        ...metrics(delta),
        scope: 'session-delta',
        session,
        previousCounters: structuredClone(before),
        provenance: 'Root same-session smoke + exact-OK probe, 2026-10-19; codex-cli 0.162.1 only',
      };
      previous = structuredClone(usage.rawCounters);
      sessionId = session;
      return result;
    },
  };
}

// Only explicitly identified MCP attempts are counted. Other opaque CLI errors
// remain driver errors, not invented tool decisions. Matching is a multiset.
export function reconcileAttempts(events, brokerTrace) {
  const attempts = new Map(),
    unavailable = [];
  const add = (id, name, args, error, status) => {
    const previous = attempts.get(id);
    name ??= previous?.name;
    if (!id || !name) {
      unavailable.push('MCP event missing id/name');
      return;
    }
    if (previous && previous.name !== name) unavailable.push('MCP attempt identity changed');
    const rawArguments = args;
    if (args === undefined) args = previous?.args;
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args);
      } catch {
        args = undefined;
      }
    }
    const argumentsUnavailable =
      args === undefined || args === null || typeof args !== 'object' || Array.isArray(args);
    attempts.set(id, {
      id,
      name,
      args: argumentsUnavailable ? undefined : args,
      rawArguments,
      argumentsUnavailable,
      error: error ?? previous?.error,
      status: status ?? previous?.status,
    });
  };
  const results = new Map();
  for (const e of events ?? []) {
    if (
      ['item.started', 'item.completed'].includes(e.type) &&
      e.item?.type === 'mcp_tool_call' &&
      e.item.server === 'books'
    ) {
      const i = e.item;
      add(i.id, i.tool, i.arguments, i.error, i.status);
    }
    for (const block of e.message?.content ?? []) {
      if (block.type === 'tool_use' && block.name?.startsWith('mcp__books__'))
        add(block.id, block.name.slice('mcp__books__'.length), block.input, undefined, 'started');
      if (block.type === 'tool_result') results.set(block.tool_use_id, block);
    }
  }
  const canonical = (value) =>
    JSON.stringify(value, (_k, v) =>
      v && typeof v === 'object' && !Array.isArray(v)
        ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b)))
        : v,
    );
  const trace = brokerTrace.map((c) => ({ ...c })),
    matched = new Set();
  for (const attempt of attempts.values()) {
    if (attempt.argumentsUnavailable)
      unavailable.push('MCP arguments unavailable for ' + attempt.id);
    const index = trace.findIndex(
      (c, i) =>
        !matched.has(i) && c.name === attempt.name && canonical(c.args) === canonical(attempt.args),
    );
    if (index >= 0) {
      matched.add(index);
      trace[index].driverCallId = attempt.id;
      continue;
    }
    const error =
      attempt.error ?? (results.get(attempt.id)?.is_error ? 'CLI rejected tool call' : undefined);
    const clientRejected = !!error || attempt.status === 'failed';
    trace.push({
      name: attempt.name,
      args: attempt.args,
      ...(attempt.argumentsUnavailable
        ? { argumentAvailability: 'unavailable', rawArguments: attempt.rawArguments }
        : {}),
      origin: 'driver',
      driverCallId: attempt.id,
      forwarded: false,
      clientRejected,
      error:
        typeof error === 'string'
          ? error
          : (JSON.stringify(error) ??
            (clientRejected ? 'CLI rejected tool call' : 'driver attempt not observed at broker')),
      unaccounted: !clientRejected,
    });
    matched.add(trace.length - 1);
  }
  return {
    trace,
    coverage: {
      recognizedAttempts: attempts.size,
      matchedBrokerCalls:
        brokerTrace.length -
        trace.slice(0, brokerTrace.length).filter((c) => !c.driverCallId).length,
      unavailable,
      scope:
        'recognized Codex mcp_tool_call / Claude MCP tool_use events only; CLI-suppressed attempts have no countable signal',
    },
  };
}
export function commandFor({
  driver,
  directory,
  socketPath,
  toolSearch = false,
  session,
  nonce,
  model,
}) {
  const pin = driverPins[driver];
  if (driver === 'claude') {
    const args = [
      '-p',
      '--model',
      model ?? pin.model,
      '--effort',
      pin.effort,
      '--output-format',
      'stream-json',
      '--verbose',
      '--setting-sources',
      '',
      '--strict-mcp-config',
      '--mcp-config',
      path.join(directory, 'mcp.json'),
      '--tools',
      toolSearch ? 'ToolSearch' : '',
      '--allowedTools',
      'mcp__books__*',
      ...(toolSearch ? ['ToolSearch'] : []),
      '--permission-mode',
      'dontAsk',
      '--disable-slash-commands',
      '--system-prompt',
      `Evaluation ${nonce}. Use the provided book tools only. Do not use shell, files, web, or other agents.`,
      ...(session ? ['--resume', session] : ['--session-id', randomUUID()]),
    ];
    return {
      binary: pin.binary,
      args,
      env: { ENABLE_TOOL_SEARCH: String(toolSearch), MCP_CONNECTION_NONBLOCKING: '0' },
      unsetEnv: [
        'DISABLE_PROMPT_CACHING',
        'DISABLE_PROMPT_CACHING_OPUS',
        'DISABLE_PROMPT_CACHING_SONNET',
        'DISABLE_PROMPT_CACHING_FABLE',
        'DISABLE_PROMPT_CACHING_HAIKU',
      ],
      caching:
        'enabled (unset every DISABLE_PROMPT_CACHING* override); provider cache is not forcibly flushed',
    };
  }
  const config = [
    ['model_reasoning_effort', pin.effort],
    ['approval_policy', 'never'],
    ['sandbox_mode', 'read-only'],
    ['web_search', 'disabled'],
    ['mcp_servers.books.command', process.execPath],
    ['mcp_servers.books.args', [path.join(here, 'proxy.mjs'), socketPath]],
    ['mcp_servers.books.required', true],
    ['mcp_servers.books.default_tools_approval_mode', 'approve'],
    [
      'developer_instructions',
      `Evaluation ${nonce}. Use only the books MCP tools. Do not run commands, read files, use web, or delegate.`,
    ],
  ];
  const args = [
    'exec',
    ...(session ? ['resume'] : []),
    '--ignore-user-config',
    '--ignore-rules',
    '--skip-git-repo-check',
    '--json',
    '--model',
    model ?? pin.model,
    '--disable',
    'code_mode_tool_search',
    '--disable',
    'code_mode',
    '--disable',
    'multi_agent',
    '--disable',
    'memories',
    '--disable',
    'plugins',
    '--disable',
    'shell_tool',
    ...config.flatMap(([key, value]) => ['-c', `${key}=${JSON.stringify(value)}`]),
    ...(session ? [session] : []),
  ];
  return {
    binary: pin.binary,
    args,
    env: {},
    unsetEnv: [],
    caching: 'provider default; no supported CLI cache-off/flush flag found',
    toolSearch:
      'code_mode_tool_search disabled; retired tool_search switch deliberately not passed',
  };
}
// POSIX detached child.pid is the sole trusted PGID. Never derive signal targets
// from driver stdout/stderr. Deliberate setsid escape and numeric PGID reuse are
// outside ordinary same-group containment; no broad process scans are performed.
export function assertDriverPlatform(platform = process.platform) {
  if (!['darwin', 'linux', 'freebsd', 'openbsd', 'netbsd', 'sunos', 'aix'].includes(platform))
    throw new Error('model-driver capture requires supported POSIX process groups');
}
export async function captureChild(binary, args, options, limits = {}) {
  assertDriverPlatform(limits.platform);
  const {
    timeoutMs = 300000,
    killGraceMs = 2000,
    drainTimeoutMs = 2000,
    signalGroup = (pgid, signal) => process.kill(-pgid, signal),
  } = limits;
  for (const value of [timeoutMs, killGraceMs, drainTimeoutMs])
    if (!Number.isSafeInteger(value) || value < 1)
      throw new Error('positive capture deadlines required');
  let stdout = '',
    stderr = '';
  const cleanup = {
    scope: 'owned-posix-process-group',
    pgid: null,
    state: 'not-started',
    success: false,
    termSent: false,
    killSent: false,
    observations: [],
  };
  const capture = {
    complete: false,
    closed: false,
    code: null,
    signal: null,
    timedOut: false,
    drainTimedOut: false,
    residualGroup: false,
    leaderExited: false,
    cleanup,
  };
  await new Promise((resolve) => {
    let child,
      timer,
      drainTimer,
      killTimer,
      observeTimer,
      cleanupDeadline,
      settled = false,
      cleaning = false,
      cleanupDone = false,
      drainExpired = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      for (const t of [timer, drainTimer, killTimer, observeTimer, cleanupDeadline])
        clearTimeout(t);
      capture.complete =
        capture.closed &&
        cleanup.success &&
        !capture.spawnError &&
        !capture.timedOut &&
        !capture.drainTimedOut &&
        !capture.residualGroup;
      // Readers live through the entire bounded cleanup/drain attempt.
      if (!capture.closed) {
        child?.stdout?.destroy();
        child?.stderr?.destroy();
      }
      resolve();
    };
    const maybeFinish = () => {
      if (cleanupDone && (capture.closed || drainExpired)) finish();
    };
    const probe = () => {
      try {
        signalGroup(cleanup.pgid, 0);
        cleanup.observations.push('alive');
        return 'alive';
      } catch (error) {
        if (error.code === 'ESRCH') {
          cleanup.observations.push('gone');
          return 'gone';
        }
        cleanup.error = error.code ?? error.message;
        cleanup.observations.push('unknown');
        return 'unknown';
      }
    };
    const done = (state) => {
      if (cleanupDone) return;
      cleanupDone = true;
      cleanup.state = state;
      cleanup.success = state === 'gone';
      for (const t of [killTimer, observeTimer, cleanupDeadline]) clearTimeout(t);
      maybeFinish();
    };
    const observe = () => {
      if (cleanupDone) return;
      const state = probe();
      if (state !== 'alive') {
        done(state);
        return;
      }
      observeTimer = setTimeout(observe, 25);
    };
    const send = (signal) => {
      if (cleanupDone) return false;
      // Re-probe before every signal and stop forever once gone.
      const state = probe();
      if (state !== 'alive') {
        done(state);
        return false;
      }
      try {
        signalGroup(cleanup.pgid, signal);
        cleanup[signal === 'SIGTERM' ? 'termSent' : 'killSent'] = true;
        return true;
      } catch (error) {
        if (error.code === 'ESRCH') done('gone');
        else {
          cleanup.error = error.code ?? error.message;
          done('unknown');
        }
        return false;
      }
    };
    const boundDrain = () => {
      drainTimer ??= setTimeout(() => {
        capture.drainTimedOut = true;
        drainExpired = true;
        startCleanup('drain-timeout');
        maybeFinish();
      }, drainTimeoutMs);
    };
    const startCleanup = (reason) => {
      if (cleaning || cleanupDone) return;
      cleaning = true;
      cleanup.reason = reason;
      if (!Number.isSafeInteger(cleanup.pgid) || cleanup.pgid < 1) {
        done('no-owned-pgid');
        return;
      }
      const state = probe();
      if (state !== 'alive') {
        done(state);
        return;
      }
      const terminate = () => {
        // Irreversible even if the pre-signal probe now finds the group gone.
        if (reason === 'closed') capture.residualGroup = true;
        cleanup.state = 'terminating';
        if (!send('SIGTERM')) return;
        killTimer = setTimeout(() => {
          if (send('SIGKILL')) cleanup.state = 'observing-after-kill';
        }, killGraceMs);
        cleanupDeadline = setTimeout(() => {
          const state = probe();
          done(
            state === 'gone' ? 'gone' : state === 'unknown' ? 'unknown' : 'alive-after-deadline',
          );
          drainExpired = true;
          maybeFinish();
        }, killGraceMs + drainTimeoutMs);
        observeTimer = setTimeout(observe, 25);
      };
      if (
        reason === 'closed' &&
        capture.leaderExited &&
        capture.code === 0 &&
        !capture.signal &&
        !capture.spawnError &&
        !capture.timedOut &&
        !capture.drainTimedOut &&
        !capture.residualGroup
      ) {
        // EOF/close is not immediate PGID absence. Only ESRCH observed within
        // this monotonic window admits natural exit; a late timer cannot extend it.
        const deadline = performance.now() + 100;
        const observeNatural = () => {
          if (cleanupDone) return;
          if (
            performance.now() >= deadline ||
            capture.spawnError ||
            capture.timedOut ||
            capture.drainTimedOut
          ) {
            terminate();
            return;
          }
          const state = probe();
          if (state !== 'alive') {
            // A delayed probe cannot extend admission, but ESRCH still stops
            // all future probes/signals, even when observed too late to pass.
            if (performance.now() >= deadline) capture.residualGroup = true;
            done(state);
            return;
          }
          observeTimer = setTimeout(
            observeNatural,
            Math.min(25, Math.max(1, deadline - performance.now())),
          );
        };
        cleanup.state = 'observing-natural-exit';
        observeTimer = setTimeout(observeNatural, 25);
      } else terminate();
    };
    try {
      child = spawn(binary, args, { ...options, detached: true });
      cleanup.pgid = Number.isSafeInteger(child.pid) && child.pid > 0 ? child.pid : null;
    } catch (error) {
      capture.spawnError = error.message;
      cleanupDone = true;
      cleanup.state = 'no-owned-pgid';
      drainExpired = true;
      finish();
      return;
    }
    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');
    child.stdout?.on('data', (data) => {
      stdout += data;
    });
    child.stderr?.on('data', (data) => {
      stderr += data;
    });
    child.once('error', (error) => {
      capture.spawnError = error.message;
      boundDrain();
      startCleanup('spawn-error');
    });
    child.once('exit', (code, signal) => {
      capture.code = code;
      capture.signal = signal;
      capture.leaderExited = true;
      clearTimeout(timer);
      boundDrain();
    });
    child.once('close', (code, signal) => {
      capture.code = code;
      capture.signal = signal;
      capture.closed = true;
      clearTimeout(timer);
      clearTimeout(drainTimer);
      startCleanup('closed');
      maybeFinish();
    });
    timer = setTimeout(() => {
      capture.timedOut = true;
      boundDrain();
      startCleanup('timeout');
    }, timeoutMs);
  });
  return { stdout, stderr, capture };
}
export async function runDriver(command, prompt, directory, limits = {}) {
  assertDriverPlatform(limits.platform);
  const current = execFileSync(command.binary, ['--version'], { encoding: 'utf8' }).trim();
  const driver = command.binary,
    pin = driverPins[driver];
  if (!current.split(/\s+/).includes(pin.version))
    throw new Error(`CLI version mismatch: ${current}; expected ${pin.version}`);
  const env = { ...process.env, ...command.env };
  for (const name of command.unsetEnv) delete env[name];
  const driverEnvironment = driverEnvironmentRecord(driver, env);
  await limits.beforeSpawn?.();
  const { stdout, stderr, capture } = await captureChild(
    command.binary,
    [...command.args, prompt],
    {
      cwd: directory,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    },
    limits,
  );
  const attemptId = randomUUID();
  await writeFile(path.join(directory, `${attemptId}.jsonl`), stdout);
  await writeFile(path.join(directory, `${attemptId}.stderr`), stderr);
  await writeFile(
    path.join(directory, `${attemptId}.capture.json`),
    JSON.stringify(capture, null, 2) + '\n',
  );
  await writeFile(path.join(directory, 'last-driver.jsonl'), stdout);
  await writeFile(path.join(directory, 'last-driver.stderr'), stderr);
  const events = parseEvents(stdout);
  const result = {
    version: current,
    driverEnvironment,
    capture,
    events,
    rawFiles: {
      stdout: `${attemptId}.jsonl`,
      stderr: `${attemptId}.stderr`,
      capture: `${attemptId}.capture.json`,
    },
  };
  try {
    if (!capture.complete)
      throw new Error(
        `${driver} capture failed/incomplete: ${JSON.stringify(capture)}; inspect preserved raw output`,
      );
    if (capture.code !== 0 || capture.signal)
      throw new Error(
        `${driver} exited ${capture.code}/${capture.signal}; inspect preserved raw output`,
      );
    if (
      events.some(
        (e) =>
          e.type === 'turn.failed' || e.type === 'error' || (e.type === 'result' && e.is_error),
      )
    )
      throw new Error('driver reported an error');
    result.usage = usageFrom(driver, events);
    result.final = finalFrom(driver, events);
    result.session = sessionFrom(driver, events);
    return result;
  } catch (error) {
    error.driverResult = result;
    throw error;
  }
}
