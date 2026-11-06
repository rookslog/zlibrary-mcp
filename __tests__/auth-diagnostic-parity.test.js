import { expect, test } from '@jest/globals';
import { readFileSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import ts from 'typescript';
import { authDiagnosticsFor, safeEapiFailureEnvelope } from '../lib/auth-diagnostics.js';
import { isRetryableError, isTransientDependencyError, withRetry } from '../lib/retry-manager.js';
import { BridgeTimeoutError } from '../lib/errors.js';
import { PythonBridgeError } from '../lib/errors.js';
import { EapiSessionOwner } from '../lib/eapi-session.js';
import { errorEnvelope } from '../lib/tool-errors.js';

const diagnostics = {
  failure_kind: 'ambiguous_status',
  attempt: 1,
  refresh_count: 0,
  request_id: '00000000-0000-4000-8000-000000000001',
};

test.each(['user:password', 'user:password@host.invalid', 'host.invalid/path?key=secret', 'host.invalid\nsecret'])(
  'redirect projection drops an invalid hostname and provider prose: %s', host => {
    const result = safeEapiFailureEnvelope({
      type: 'RedirectRefusedError', redirect_host: host, error: 'secret provider body',
      diagnostics: { ...diagnostics, failure_kind: 'transport', http_status: 307 },
    });
    expect(result.redirect_host).toBeUndefined();
    expect(result.error).toBe('EAPI request failed (transport)');
    expect(JSON.stringify(result)).not.toContain('secret');
    expect(JSON.stringify(result)).not.toContain('password');
  },
);

test.each(['receiver.invalid', '2001:db8::7', '<missing>', '<invalid>'])(
  'redirect projection retains only the bounded hostname: %s', host => {
    const result = safeEapiFailureEnvelope({
      type: 'RedirectRefusedError', redirect_host: host, error: 'untrusted provider body',
      diagnostics: { ...diagnostics, failure_kind: 'transport', http_status: 307 },
    });
    expect(result.error).toBe(`Redirect refused (HTTP 307) to host ${host}`);
    expect(result.redirect_host).toBe(host);
    expect(result.type).toBe('RedirectRefusedError');
    expect(JSON.stringify(result)).not.toContain('untrusted provider body');
  },
);

// Discover the actual policy lists, so additions to either boundary/retry policy
// cannot silently escape these cross-language contract checks.
function sourcePolicy(file, argument) {
  const source = ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
  );
  const found = [];
  const arrays = new Map();
  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isArrayLiteralExpression(node.initializer)
    )
      arrays.set(node.name.getText(source), node.initializer);
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === 'includes' &&
      node.arguments[0]?.getText(source) === argument
    ) {
      const receiver = node.expression.expression;
      const array = ts.isArrayLiteralExpression(receiver)
        ? receiver
        : arrays.get(receiver.getText(source));
      if (array) found.push(...array.elements.map((v) => v.text));
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  expect(found.length).toBeGreaterThan(0);
  return found;
}
function acquisitionReasons() {
  const file = 'src/lib/auth-diagnostics.ts';
  const source = ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
  );
  let reasons;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'messages')
      reasons = node.initializer.properties.map((p) => p.name.getText(source));
    ts.forEachChild(node, visit);
  }
  visit(source);
  expect(reasons.length).toBeGreaterThan(0);
  return reasons;
}
async function python(code, args = []) {
  return JSON.parse(
    (
      await promisify(execFile)(join(process.cwd(), '.venv/bin/python'), ['-c', code, ...args], {
        encoding: 'utf8',
        env: { ...process.env, PYTHONPATH: join(process.cwd(), 'lib') },
      })
    ).stdout,
  );
}

test('transient type parity: Python, TypeScript and retry policy preserve the same names', async () => {
  const retryTypes = sourcePolicy('src/lib/retry-manager.ts', 'error.context?.pythonErrorType');
  const tsTypes = sourcePolicy('src/lib/auth-diagnostics.ts', 'value.type');
  const py = await python(
    `import json, sys, zlibrary
from auth_diagnostics import FAILURE_TYPES, failure_type
projected = {name: failure_type(type(name, (Exception,), {})()) for name in json.loads(sys.argv[1])}
print(json.dumps({'origin': zlibrary.__file__, 'types': sorted(FAILURE_TYPES),
 'projected': projected}))`,
    [JSON.stringify(retryTypes)],
  );
  expect(py.origin).toBe(join(process.cwd(), 'zlibrary/src/zlibrary/__init__.py'));
  expect([...tsTypes].sort()).toEqual(py.types);
  expect(py.projected).toEqual(Object.fromEntries(retryTypes.map((type) => [type, type])));
  for (const type of retryTypes) {
    expect(py.projected[type]).toBe(type);
    const projected = safeEapiFailureEnvelope({ type, diagnostics });
    expect(projected.type).toBe(type);
    const error = { code: 'PYTHON_ERROR', context: { pythonErrorType: projected.type } };
    expect(isTransientDependencyError(error)).toBe(true);
    expect(isRetryableError(error)).toBe(true);
    let attempts = 0;
    await withRetry(
      async () => {
        if (++attempts === 1) throw error;
      },
      { maxRetries: 1, initialDelay: 1, shouldRetry: isRetryableError },
    );
    expect(attempts).toBe(2);
  }
  expect(safeEapiFailureEnvelope({ type: 'untrusted-type', diagnostics }).type).toBe(
    'RuntimeError',
  );
});

test('acquisition reason parity preserves Python/TypeScript envelopes, codes and recovery', async () => {
  const reasons = acquisitionReasons();
  const py = await python(
    `import asyncio, contextlib, io, json, sys
import python_bridge as bridge
from lib.acquisition import AcquisitionError
async def initialize(): pass
bridge.initialize_eapi_client = initialize
bridge._eapi_client = None
bridge._session_input = None
results = {}
async def run():
 for reason in json.loads(sys.argv[1]):
  async def fail(*args): raise AcquisitionError(reason, 'untrusted-provider-prose', issued=True)
  bridge._dispatch_bridge_function = fail
  sys.argv = ['python_bridge.py', 'search', '{}']
  stderr = io.StringIO()
  with contextlib.redirect_stderr(stderr):
   try: await bridge.main()
   except SystemExit as error: assert error.code == 1
  results[reason] = json.loads(stderr.getvalue().strip().splitlines()[-1])
asyncio.run(run())
print(json.dumps(results))`,
    [JSON.stringify(reasons)],
  );
  for (const reason of reasons) {
    expect(py[reason].details.reason).toBe(reason);
    if (reason === 'walk_budget_exhausted') expect(py[reason]).not.toHaveProperty('diagnostics');
    else expect(py[reason].diagnostics.failure_kind).toBe('ambiguous_status');
    const projected = safeEapiFailureEnvelope(py[reason]);
    expect(projected.details).toEqual({ reason, request_issued: true });
    const local = safeEapiFailureEnvelope({
      type: 'AcquisitionError',
      details: { reason, request_issued: true },
    });
    const envelope = (value) =>
      errorEnvelope(
        {
          context: {
            pythonErrorType: value.type,
            details: value.details,
            diagnostics: value.diagnostics,
          },
        },
        'download',
        'zlibrary',
      );
    const result = envelope(projected);
    const baseline = envelope(local);
    expect(result.error.code).toBe(baseline.error.code);
    expect(result.error.code).not.toBe('internal');
    expect(result.error.recovery).toBe(baseline.error.recovery);
    expect(result.error.retry).toBe(baseline.error.retry);
    expect(JSON.stringify(result)).not.toContain('untrusted-provider-prose');
  }
});

test.each(acquisitionReasons())(
  'TypeScript preserves acquisition reason %s with diagnostics',
  (reason) => {
    const projected = safeEapiFailureEnvelope({
      type: 'AcquisitionError',
      diagnostics,
      details: { reason, request_issued: true },
    });
    expect(projected.details).toEqual({ reason, request_issued: true });
  },
);

test('caller abort is cancellation, not a transport outage; real timeout stays transport', () => {
  const abort = new BridgeTimeoutError('Bridge call aborted', { reason: 'aborted' });
  abort.context.diagnostics = authDiagnosticsFor(abort, diagnostics.request_id, 1, 0);
  expect(abort.context.diagnostics.failure_kind).toBe('ambiguous_status');
  expect(errorEnvelope(abort, 'download', 'zlibrary').error.code).toBe('cancelled');
  expect(isTransientDependencyError(abort)).toBe(false);
  expect(isRetryableError(abort)).toBe(false);
  const timeout = new BridgeTimeoutError('Bridge timed out', { reason: 'timeout' });
  expect(authDiagnosticsFor(timeout, diagnostics.request_id, 1, 0).failure_kind).toBe('transport');
});

// Valid typed transport evidence may fill an unknown reason, not overwrite a
// deliberate control outcome or admit a malformed diagnostic record.
test('ordinary transport fallback uses an existing code and preserves timeout/abort controls', () => {
  const transport = { ...diagnostics, failure_kind: 'transport' };
  const unknown = {
    context: {
      pythonErrorType: 'RuntimeError',
      diagnostics: transport,
      details: { request_issued: false },
    },
  };
  expect(errorEnvelope(unknown, 'search', 'zlibrary').error).toMatchObject({
    code: 'provider_unreachable',
    retry: 'safe',
    detail: { reason: 'connect_error' },
  });
  expect(
    errorEnvelope(
      { context: { diagnostics: { ...transport, body: 'untrusted' } } },
      'search',
      'zlibrary',
    ).error.code,
  ).toBe('internal');
  const timeout = new BridgeTimeoutError('bounded timeout', {
    reason: 'timeout',
    pythonErrorType: 'WriteTimeout',
    details: { request_issued: true },
    diagnostics: transport,
  });
  expect(errorEnvelope(timeout, 'download', 'zlibrary').error).toMatchObject({
    code: 'outcome_unknown',
    retry: 'may_spend_quota',
    detail: { reason: 'timeout', request_issued: true },
  });
  expect(isRetryableError(timeout)).toBe(false);
  const abort = new BridgeTimeoutError('caller aborted', {
    reason: 'aborted',
    pythonErrorType: 'WriteError',
    details: { request_issued: true },
    diagnostics: transport,
  });
  expect(errorEnvelope(abort, 'download', 'zlibrary').error.code).toBe('cancelled');
  expect(isRetryableError(abort)).toBe(false);
});

// These consumers have their own closed lists beyond the bridge projection and
// retry policy. Every listed type must survive both language boundaries.
test.each([
  ['src/lib/auth-diagnostics.ts', 'type'],
  ['src/lib/auth-diagnostics.ts', 'value?.type'],
  ['src/lib/zlibrary-api.ts', 'error?.context?.pythonErrorType'],
  ['src/lib/tool-errors.ts', 'context.pythonErrorType'],
])('closed consumer type parity covers %s (%s)', async (file, argument) => {
  const types = [...new Set(sourcePolicy(file, argument))];
  const projected = await python(
    `import json, sys
from auth_diagnostics import failure_type
print(json.dumps({name: failure_type(type(name, (Exception,), {})()) for name in json.loads(sys.argv[1])}))`,
    [JSON.stringify(types)],
  );
  for (const type of types) {
    expect(projected[type]).toBe(type);
    expect(safeEapiFailureEnvelope({ type, diagnostics }).type).toBe(type);
  }
});

test.each([false, true])(
  'EAPI source status and failures survive both boundaries (aggregate=%s)',
  async (aggregate) => {
    const py = await python(
      `import asyncio, contextlib, io, json, sys
import python_bridge as bridge
from lib.sources.errors import ProviderResponseError, AllSourcesFailedError
aggregate = sys.argv[1]
async def initialize(): pass
async def dispatch(*args):
 failure = ProviderResponseError('libgen', 'untrusted-host', 'HTTP 503', reason='http_error')
 raise AllSourcesFailedError('download', [failure]) if aggregate == 'true' else failure
bridge.initialize_eapi_client = initialize
bridge._dispatch_bridge_function = dispatch
bridge._eapi_client = None
bridge._session_input = None
async def run():
 sys.argv = ['python_bridge.py', 'search', '{}']
 stderr = io.StringIO()
 with contextlib.redirect_stderr(stderr):
  try: await bridge.main()
  except SystemExit as error: assert error.code == 1
 return json.loads(stderr.getvalue().strip().splitlines()[-1])
print(json.dumps(asyncio.run(run())))`,
      [JSON.stringify(aggregate)],
    );
    const projected = safeEapiFailureEnvelope(py);
    const details = aggregate ? projected.details.failures[0] : projected.details;
    expect(details).toMatchObject({ reason: 'http_error', detail: 'HTTP 503' });
    expect(JSON.stringify(projected)).not.toContain('untrusted-host');
    const error = { context: { pythonErrorType: projected.type, details: projected.details } };
    expect(isTransientDependencyError(error)).toBe(true);
    const permanent = structuredClone(projected.details);
    (aggregate ? permanent.failures[0] : permanent).detail = 'HTTP 400';
    expect(isTransientDependencyError({ context: { ...error.context, details: permanent } })).toBe(
      false,
    );
  },
);

test.each(['operation', 'login'])(
  'retained %s EAPI context keeps source configuration refusals local without cooldown',
  async (action) => {
    const reply = await python(
      `import asyncio, contextlib, io, json, os, socket, sys
from unittest.mock import patch
import python_bridge as bridge
from lib.sources.config import get_source_config
os.environ['PYTHON_BRIDGE_TIMEOUT'] = '9999'
bridge._eapi_client = None
bridge._source_router = None
bridge._session_input = {'action': sys.argv[1]}
async def initialize(): get_source_config()
bridge.initialize_eapi_client = initialize
def forbidden(*args, **kwargs):
 raise AssertionError('local configuration refusal must not contact a provider')
async def run():
 sys.argv = ['python_bridge.py', 'search_multi_source', '{"query":"offline","source":"libgen"}']
 stdout, stderr = io.StringIO(), io.StringIO()
 with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
  try: await bridge.main()
  except SystemExit as error: assert error.code == 1
 if stdout.getvalue():
  return json.loads(json.loads(stdout.getvalue())['content'][0]['text'])
 return json.loads(stderr.getvalue().strip().splitlines()[-1])
with patch.object(socket, 'getaddrinfo', forbidden), patch.object(socket.socket, 'connect', forbidden):
 print(json.dumps(asyncio.run(run())))`,
      [action],
    );
    expect(reply).not.toHaveProperty('diagnostics');
    expect(reply).not.toHaveProperty('login_failed');
    const projected = safeEapiFailureEnvelope(reply);
    expect(projected).toMatchObject({
      type: 'SourceConfigurationError',
      details: {
        reason: 'configuration_error',
        request_issued: false,
        variable: 'PYTHON_BRIDGE_TIMEOUT',
      },
    });
    const failure = new PythonBridgeError(
      projected.error,
      { pythonErrorType: projected.type, details: projected.details },
      false,
    );
    const owner = new EapiSessionOwner();
    const identity = 'retained-context-reader';
    let logins = 0;
    const invoke = async (input) => {
      if (input.action === 'login') {
        if (++logins === 1) throw failure;
        owner.consume(
          {
            identity,
            generation: input.generation,
            domain: 'offline.invalid',
            remix_userid: '7',
            remix_userkey: 'synthetic-session',
          },
          identity,
          input.generation,
        );
      }
      return 'ok';
    };
    await expect(owner.run(identity, invoke)).rejects.toBe(failure);
    await expect(owner.run(identity, invoke)).resolves.toBe('ok');
    expect(logins).toBe(2);
  },
);

test('source detail projection drops provider prose and malformed HTTP status tokens', () => {
  const projected = safeEapiFailureEnvelope({
    type: 'RuntimeError',
    diagnostics,
    details: {
      reason: 'http_error',
      detail: 'HTTP 503 secret',
      host: 'secret.invalid',
      failures: [
        null,
        {
          provider: 'libgen',
          reason: 'http_error',
          detail: 'HTTP 503\n',
          message: 'secret',
        },
        { provider: 'secret', reason: 'secret', detail: 'HTTP 503' },
      ],
    },
  });
  expect(projected.details).toEqual({
    reason: 'http_error',
    failures: [{ provider: 'libgen', reason: 'http_error' }, {}],
  });
  expect(JSON.stringify(projected)).not.toContain('secret');
});

test('Python and TypeScript source projection reasons stay in parity with Node controls', async () => {
  const typescriptReasons = sourcePolicy('src/lib/auth-diagnostics.ts', 'detail.reason');
  expect(typescriptReasons).toHaveLength(new Set(typescriptReasons).size);
  const py = await python(
    `import ast, inspect, json
import python_bridge as bridge
from lib.sources.errors import REASON_TEXT
tree = ast.parse(inspect.getsource(bridge._safe_eapi_details))
assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
 and any(isinstance(target, ast.Name) and target.id == 'reasons' for target in node.targets))
expression = assignment.value
assert isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr)
assert ast.unparse(expression.left) == 'set(REASON_TEXT)'
reasons = sorted(set(REASON_TEXT) | ast.literal_eval(expression.right))
projected = {reason: bridge._safe_eapi_details({'reason': reason,
 'failures': [{'provider': 'libgen', 'reason': reason}]}) for reason in reasons}
print(json.dumps({'reasons': reasons, 'projected': projected,
 'unknown': bridge._safe_eapi_details({'reason': 'r1_unknown_reason'})}))`,
  );
  // These two Node runner controls predate this PR; Python source exceptions
  // do not emit them. All source/acquisition reasons must survive both sides.
  expect([...new Set(typescriptReasons)].sort()).toEqual(
    [...py.reasons, 'timeout', 'aborted'].sort(),
  );
  expect(py.unknown).toEqual({});
  for (const reason of py.reasons) {
    expect(py.projected[reason]).toEqual({
      reason,
      failures: [{ provider: 'libgen', reason }],
    });
    expect(
      safeEapiFailureEnvelope({
        type: 'RuntimeError',
        diagnostics,
        details: py.projected[reason],
      }).details,
    ).toEqual(py.projected[reason]);
  }
  for (const reason of ['timeout', 'aborted'])
    expect(
      safeEapiFailureEnvelope({
        type: 'RuntimeError',
        diagnostics,
        details: { reason },
      }).details,
    ).toEqual({ reason });
  expect(
    safeEapiFailureEnvelope({
      type: 'RuntimeError',
      diagnostics,
      details: { reason: 'r1_unknown_reason' },
    }).details,
  ).toEqual({});
});
