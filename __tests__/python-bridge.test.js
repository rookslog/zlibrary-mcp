import './helpers/environment-login.js';
import { jest, describe, test, expect } from '@jest/globals';
import { spawnSync } from 'node:child_process';

describe('Python Bridge', () => {
  async function setup({
    bridgeResult = [JSON.stringify({ success: true, data: 'test data' })],
    bridgeError,
    venvError,
  } = {}) {
    jest.resetModules();
    jest.clearAllMocks();

    jest.unstable_mockModule('../lib/venv-manager.js', () => ({
      getManagedPythonPath: venvError
        ? jest.fn().mockRejectedValue(venvError)
        : jest.fn().mockResolvedValue('/mock/venv/python'),
    }));
    const runPythonBridge = bridgeError
      ? jest.fn().mockRejectedValue(bridgeError)
      : jest.fn().mockResolvedValue(bridgeResult);
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge,
      LONG_BRIDGE_TIMEOUT_MS: 2400000,
    }));
    jest.unstable_mockModule('child_process', () => ({
      spawn: jest.fn(() => {
        throw new Error('raw spawn used');
      }),
    }));

    return { pythonBridge: await import('../lib/python-bridge.js'), runPythonBridge };
  }

  test('uses the owned runner instead of creating an unmanaged subprocess', async () => {
    // Mutation caught: replacing runPythonBridge with a direct child_process.spawn
    // bypasses timeout, abort, registry, and process-tree cleanup.
    const { pythonBridge } = await setup({
      bridgeResult: [JSON.stringify({ success: true, data: 'owned' })],
    });

    await expect(
      pythonBridge.callPythonFunction('test_function', { arg: 'value' }),
    ).resolves.toEqual({ success: true, data: 'owned' });
  });

  test('parses a successful result from the owned runner', async () => {
    const { pythonBridge } = await setup();

    await expect(
      pythonBridge.callPythonFunction('test_function', ['arg1', 'arg2']),
    ).resolves.toEqual({ success: true, data: 'test data' });
  });

  test('non-zero-exit errors omit raw child buffers', async () => {
    const bridgeError = Object.assign(new Error('process exited with code 1'), {
      exitCode: 1,
      stderr: 'Python error message',
      stdout: '',
    });
    const { pythonBridge } = await setup({ bridgeError });

    await expect(pythonBridge.callPythonFunction('test_function', ['arg1'])).rejects.toThrow(
      'Python process exited with code 1',
    );
  });

  test('preserves a structured provider envelope on non-zero exit', async () => {
    const details = {
      operation: 'download',
      failures: [{ provider: 'libgen', host: 'libgen.li', reason: 'connect_timeout' }],
    };
    const bridgeError = Object.assign(new Error('process exited with code 1'), {
      exitCode: 1,
      stderr: `diagnostic\n${JSON.stringify({
        error: 'download failed on every source',
        type: 'AllSourcesFailedError',
        details,
      })}`,
      stdout: '',
    });
    const { pythonBridge } = await setup({ bridgeError });

    const error = await pythonBridge.callPythonFunction('download_book', {}).catch((err) => err);

    expect(error.context.details).toEqual(details);
    expect(error.retryable).toBe(false);
  });

  test('EAPI failures in the lower bridge facade discard raw buffers and arguments', async () => {
    const diagnostics = {
      failure_kind: 'ambiguous_status',
      http_status: 400,
      attempt: 1,
      refresh_count: 0,
      request_id: '00000000-0000-4000-8000-000000000001',
    };
    const bridgeError = Object.assign(new Error('i251-token'), {
      exitCode: 1,
      stdout: 'i251-token',
      stderr: JSON.stringify({
        error: 'i251-token',
        type: 'HTTPStatusError',
        diagnostics,
        body: 'i251-token',
      }),
    });
    const { pythonBridge } = await setup({ bridgeError });
    const error = await pythonBridge
      .callPythonFunction('search', { query: 'i251-token' })
      .catch((e) => e);
    expect(String(error.stack) + JSON.stringify(error)).not.toContain('i251-token');
    expect(error.context.diagnostics).toEqual(diagnostics);
  });

  test('keeps protocol_error from a real recent-books contract error envelope', async () => {
    const child = spawnSync('.venv/bin/python', ['-c', `
import asyncio, os, socket, sys
from unittest.mock import AsyncMock
sys.path.insert(0, os.path.join(os.getcwd(), 'lib'))
def forbidden(*args, **kwargs):
    raise AssertionError('real network forbidden')
socket.socket.connect = forbidden
socket.getaddrinfo = forbidden
from lib import python_bridge as bridge
eapi = bridge.EAPIClient('zlibrary.example', remix_userkey='offline-cookie-marker')
eapi.get_recently = AsyncMock(return_value={'success': 1, 'error': 'untrusted-provider-text'})
bridge._eapi_client = eapi
bridge.initialize_eapi_client = AsyncMock(return_value=eapi)
sys.argv = ['python_bridge.py', 'get_recent_books', '{}']
asyncio.run(bridge.main())
`], { encoding: 'utf8', timeout: 10000 });
    expect(child.error).toBeUndefined();
    expect(child.status).toBe(1);
    expect(child.stdout).toBe('');
    const raw = JSON.parse(child.stderr.trim().split('\n').at(-1));
    expect(raw.details.reason).toBe('protocol_error');
    expect(raw.diagnostics.failure_kind).toBe('ambiguous_status');
    expect(child.stderr).not.toContain('untrusted-provider-text');
    const { pythonBridge } = await setup();
    const parsed = pythonBridge.parseBridgeErrorEnvelope(child.stderr);
    expect(parsed.details.reason).toBe('protocol_error');
    expect(parsed.error).toBe('EAPI request failed (ambiguous_status)');
  });

  test('treats all-permanent quota/configuration aggregates as non-retryable', async () => {
    const { pythonBridge } = await setup();
    const details = {
      failures: [
        { provider: 'annas', reason: 'configuration_error' },
        { provider: 'annas', reason: 'quota_exhausted' },
      ],
    };

    expect(pythonBridge.isBridgeDetailRetryable(details)).toBe(false);
  });

  test('mixed permanent/transient details still veto Node retry', async () => {
    const { pythonBridge } = await setup();
    const details = {
      failures: [
        { provider: 'annas', reason: 'quota_exhausted' },
        { provider: 'libgen', reason: 'http_error' },
      ],
    };

    expect(pythonBridge.isBridgeDetailRetryable(details)).toBe(false);
  });

  test('JSON parse errors omit raw output and parser excerpts', async () => {
    const { pythonBridge } = await setup({ bridgeResult: ['Invalid JSON{'] });

    await expect(pythonBridge.callPythonFunction('test_function', [])).rejects.toThrow(
      'Failed to parse Python result JSON.',
    );
  });

  test('propagates managed-Python setup failures without spawning', async () => {
    const { pythonBridge } = await setup({ venvError: new Error('Failed to get venv path') });

    await expect(pythonBridge.callPythonFunction('test_function', [])).rejects.toThrow(
      'Failed to get venv path',
    );
  });

  test.each(['download_book'])('uses the long bridge default for %s', async (functionName) => {
    const { pythonBridge, runPythonBridge } = await setup();

    await pythonBridge.callPythonFunction(functionName, {});

    expect(runPythonBridge.mock.calls[0][2].timeoutMs).toBe(2400000);
  });

  test('leaves ordinary bridge calls on the runner default', async () => {
    const { pythonBridge, runPythonBridge } = await setup();

    await pythonBridge.callPythonFunction('search', {});

    expect(runPythonBridge.mock.calls[0][2].timeoutMs).toBeUndefined();
  });

  test('preserves an explicit timeout override for a long-running call', async () => {
    const { pythonBridge, runPythonBridge } = await setup();

    await pythonBridge.callPythonFunction('download_book', {}, { timeoutMs: 1234 });

    expect(runPythonBridge.mock.calls[0][2].timeoutMs).toBe(1234);
  });
});
