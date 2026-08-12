import { jest, describe, test, expect } from '@jest/globals';

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
    jest.unstable_mockModule('../lib/python-runner.js', () => ({
      runPythonBridge: bridgeError
        ? jest.fn().mockRejectedValue(bridgeError)
        : jest.fn().mockResolvedValue(bridgeResult),
    }));
    jest.unstable_mockModule('child_process', () => ({
      spawn: jest.fn(() => {
        throw new Error('raw spawn used');
      }),
    }));

    return import('../lib/python-bridge.js');
  }

  test('uses the owned runner instead of creating an unmanaged subprocess', async () => {
    // Mutation caught: replacing runPythonBridge with a direct child_process.spawn
    // bypasses timeout, abort, registry, and process-tree cleanup.
    const pythonBridge = await setup({
      bridgeResult: [JSON.stringify({ success: true, data: 'owned' })],
    });

    await expect(
      pythonBridge.callPythonFunction('test_function', { arg: 'value' }),
    ).resolves.toEqual({ success: true, data: 'owned' });
  });

  test('parses a successful result from the owned runner', async () => {
    const pythonBridge = await setup();

    await expect(
      pythonBridge.callPythonFunction('test_function', ['arg1', 'arg2']),
    ).resolves.toEqual({ success: true, data: 'test data' });
  });

  test('preserves the public non-zero-exit error shape', async () => {
    const bridgeError = Object.assign(new Error('process exited with code 1'), {
      exitCode: 1,
      stderr: 'Python error message',
      stdout: '',
    });
    const pythonBridge = await setup({ bridgeError });

    await expect(pythonBridge.callPythonFunction('test_function', ['arg1'])).rejects.toThrow(
      'Python process exited with code 1: Python error message. Raw stdout:',
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
    const pythonBridge = await setup({ bridgeError });

    const error = await pythonBridge.callPythonFunction('download_book', {}).catch((err) => err);

    expect(error.context.details).toEqual(details);
    expect(error.retryable).toBe(false);
  });

  test('preserves the public JSON parse error shape', async () => {
    const pythonBridge = await setup({ bridgeResult: ['Invalid JSON{'] });

    await expect(pythonBridge.callPythonFunction('test_function', [])).rejects.toThrow(
      /^Failed to parse Python result JSON: .*?\. Raw output: Invalid JSON\{\. Stderr: $/,
    );
  });

  test('propagates managed-Python setup failures without spawning', async () => {
    const pythonBridge = await setup({ venvError: new Error('Failed to get venv path') });

    await expect(pythonBridge.callPythonFunction('test_function', [])).rejects.toThrow(
      'Failed to get venv path',
    );
  });
});
