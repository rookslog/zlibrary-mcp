// Stop at the compiler boundary: no server, runtime admission or model is started.
import { test, expect, jest } from '@jest/globals';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const build = await import('./build-proof.js');
const identity = await import('./evaluator-identity.js');
let observed;
jest.unstable_mockModule('./build-proof.js', () => ({
  ...build,
  pinnedInputs: async () => ({ sha: 'a'.repeat(40), inputs: {} }),
  verifyBuildArtifacts: async (options) => {
    observed = options;
    throw new Error('E3 stopped before compiler/model');
  },
}));
jest.unstable_mockModule('./evaluator-identity.js', () => ({
  ...identity,
  verifyEvaluatorIdentity: async () => ({}),
}));
const { runEModelTasks } = await import('./e-runner.js');
const { root } = await import('./server.js');
test('paid E admission actually forwards the 180-second compiler allowance', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'e3-admission-'));
  try {
    await expect(
      runEModelTasks({
        paid: true,
        driver: 'codex',
        'server-root': root,
        'session-mode': 'cold',
        'e-cases': 'E4',
        out: path.join(directory, 'receipt.json'),
      }),
    ).rejects.toThrow('E3 stopped before compiler/model');
    expect(observed).toMatchObject({ buildRoot: root, compileTimeoutMs: 180000 });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
