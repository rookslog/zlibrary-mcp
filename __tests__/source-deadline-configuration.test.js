import { expect, test } from '@jest/globals';
import { errorEnvelope, ERROR_CODES } from '../dist/lib/tool-errors.js';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { parseBridgeErrorEnvelope } from '../dist/lib/python-bridge.js';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// The code remains one of the existing 19; the recovery must address operator envs.
test.each(['search', 'download'])(
  'short %s source owner reports configuration recovery',
  (operation) => {
    const variable =
      operation === 'download' ? 'PYTHON_BRIDGE_LONG_TIMEOUT' : 'PYTHON_BRIDGE_TIMEOUT';
    const result = errorEnvelope(
      {
        context: {
          pythonErrorType: 'SourceConfigurationError',
          details: { reason: 'configuration_error', request_issued: false, variable },
        },
      },
      operation,
      'libgen',
    );
    expect(ERROR_CODES).toHaveLength(19);
    expect(result.error.code).toBe('invalid_argument');
    expect(result.error.message).toMatch(/configuration error/i);
    expect(result.error.recovery).toContain('docs/RETRY_CONFIGURATION.md');
    expect(result.error.recovery).toContain('10000');
    expect(result.error.recovery).toContain(`Configure ${variable} to at least 10000 ms`);
    expect(result.error.recovery).not.toContain(' or ');
    expect(result.error.recovery).not.toContain('Correct the arguments');
    expect(result.error.retry).toBe('no');
  },
);

test('private failure projection retains a source configuration verdict without provider prose', async () => {
  const { safeEapiFailureEnvelope } = await import('../dist/lib/auth-diagnostics.js');
  const safe = safeEapiFailureEnvelope({
    type: 'SourceConfigurationError',
    error: 'untrusted configuration value and local path',
    details: {
      reason: 'configuration_error',
      request_issued: false,
      variable: 'PYTHON_BRIDGE_LONG_TIMEOUT',
    },
  });
  expect(safe).toMatchObject({
    type: 'SourceConfigurationError',
    details: {
      reason: 'configuration_error',
      request_issued: false,
      variable: 'PYTHON_BRIDGE_LONG_TIMEOUT',
    },
  });
  expect(JSON.stringify(safe)).not.toContain('untrusted');
  expect(
    errorEnvelope({ context: { pythonErrorType: safe.type, details: safe.details } }, 'download')
      .error.recovery,
  ).toContain('docs/RETRY_CONFIGURATION.md');
});

// Exercise main's real dispatch/configuration/serialization, with external I/O forbidden.
async function shortOwnerEnvelope(source = 'libgen') {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const child = await new Promise((resolve) =>
    execFile(
      `${root}.venv/bin/python`,
      [
        '-B',
        '-c',
        `
import asyncio, sys
from unittest.mock import patch
sys.path.insert(0, 'lib')
from lib import python_bridge as bridge
sys.argv = ['python_bridge.py', 'search_multi_source', sys.argv[1]]
def forbidden(*args, **kwargs):
    raise AssertionError('configuration refusal must precede network/authentication')
with patch('socket.socket.connect', forbidden), patch('socket.create_connection', forbidden), patch('socket.getaddrinfo', forbidden), patch.object(bridge, 'initialize_eapi_client', forbidden), patch.object(bridge.SourceRouter, 'search', forbidden):
    asyncio.run(bridge.main())
`,
        JSON.stringify({ query: 'deadline fixture', source, count: 10 }),
      ],
      {
        cwd: root,
        env: { PYTHON_BRIDGE_TIMEOUT: '9999', PYTHONDONTWRITEBYTECODE: '1' },
        encoding: 'utf8',
        timeout: 15000,
      },
      (error, stdout, stderr) => resolve({ error, stdout, stderr }),
    ),
  );
  expect(child.error?.code).toBe(1);
  expect(child.error?.killed).toBe(false);
  expect(child.stdout).toBe('');
  const envelope = parseBridgeErrorEnvelope(child.stderr);
  expect(envelope).toMatchObject({
    type: 'SourceConfigurationError',
    details: {
      reason: 'configuration_error',
      request_issued: false,
      operation: 'search',
      variable: 'PYTHON_BRIDGE_TIMEOUT',
    },
  });
  return envelope;
}

function bridgeFailure(envelope) {
  return { context: { pythonErrorType: envelope.type, details: envelope.details } };
}

test('real short-owner bridge envelope retains Node configuration recovery', async () => {
  const result = errorEnvelope(bridgeFailure(await shortOwnerEnvelope()), 'search', 'libgen');
  expect(result.error.code).toBe('invalid_argument');
  expect(result.error.retry).toBe('no');
  expect(result.error.recovery).toContain('Configure PYTHON_BRIDGE_TIMEOUT to at least 10000 ms');
  expect(result.error.recovery).toContain('docs/RETRY_CONFIGURATION.md');
});

test('public two-source search keeps shared local configuration recovery and ordered attempts', async () => {
  const envelopes = Object.fromEntries(
    await Promise.all(
      ['libgen', 'annas'].map(async (source) => [source, await shortOwnerEnvelope(source)]),
    ),
  );
  const tools = new AcquisitionTools(async (_operation, args) => {
    throw bridgeFailure(envelopes[args.source]);
  }, {});
  const result = await tools.call('search', {
    query: 'deadline fixture',
    sources: ['libgen', 'annas'],
  });
  expect(result.error.code).toBe('all_sources_failed');
  expect(result.error.retry).toBe('no');
  expect(result.error.recovery).toContain('Configure PYTHON_BRIDGE_TIMEOUT to at least 10000 ms');
  expect(result.error.recovery).toContain('docs/RETRY_CONFIGURATION.md');
  expect(result.error.attempts).toEqual([
    {
      source: 'libgen',
      outcome: 'failed',
      code: 'invalid_argument',
      reason: 'configuration_error',
    },
    { source: 'annas', outcome: 'failed', code: 'invalid_argument', reason: 'configuration_error' },
  ]);
});

test.each(['provider', 'credentials', 'empty'])(
  'mixed %s search does not get shared configuration recovery',
  async (other) => {
    const tools = new AcquisitionTools(async (_operation, args) => {
      if (args.source === 'libgen')
        throw bridgeFailure({
          type: 'SourceConfigurationError',
          details: {
            reason: 'configuration_error',
            request_issued: false,
            variable: 'PYTHON_BRIDGE_TIMEOUT',
          },
        });
      if (other === 'empty') return { books: [] };
      throw bridgeFailure({
        type: other === 'credentials' ? 'ProviderConfigurationError' : 'ProviderUnreachableError',
        details: {
          reason: other === 'credentials' ? 'configuration_error' : 'dns_failure',
          request_issued: false,
        },
      });
    }, {});
    const result = await tools.call('search', {
      query: 'deadline fixture',
      sources: ['libgen', 'annas'],
    });
    expect(result.error.code).toBe(other === 'empty' ? 'partial_failure' : 'all_sources_failed');
    expect(result.error.recovery).not.toContain('10000');
  },
);

test.each([undefined, 'UNTRUSTED_VALUE/path'])(
  'unknown variable %s is not reflected in recovery or projection',
  async (variable) => {
    const { safeEapiFailureEnvelope } = await import('../dist/lib/auth-diagnostics.js');
    const envelope = {
      type: 'SourceConfigurationError',
      error: 'untrusted prose',
      details: { reason: 'configuration_error', request_issued: false, variable },
    };
    const safe = safeEapiFailureEnvelope(envelope);
    expect(safe.details).not.toHaveProperty('variable');
    const result = errorEnvelope(bridgeFailure(envelope), 'search');
    expect(result.error.recovery).toContain('Configure PYTHON_BRIDGE_TIMEOUT to at least 10000 ms');
    expect(JSON.stringify(result)).not.toContain('UNTRUSTED_VALUE');
  },
);

test.each([
  ['search', 'PYTHON_BRIDGE_LONG_TIMEOUT'],
  ['download', 'PYTHON_BRIDGE_TIMEOUT'],
])('serialized variable takes precedence over the public %s operation', (operation, variable) => {
  const result = errorEnvelope(
    bridgeFailure({
      type: 'SourceConfigurationError',
      details: { reason: 'configuration_error', request_issued: false, variable },
    }),
    operation,
  );
  expect(result.error.recovery).toContain(`Configure ${variable} to at least 10000 ms`);
});
