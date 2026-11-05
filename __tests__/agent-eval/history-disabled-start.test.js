import { jest, test, expect } from '@jest/globals';
import * as fs from 'node:fs/promises';
import { TOOL_DEFINITIONS } from '../../dist/index.js';

// Exercise the real start/listTools wiring, with no child process, socket or
// filesystem writes. Identity checks and all external startup services are doubles;
// this does not qualify the real runtime, artifact admission, broker or stdio.
const sha = 'a'.repeat(40);
const hash = 'b'.repeat(64);
const python = { sha256: hash, descriptor: { env: {} } };
const fake = {
  temp: '/tmp/agent-eval-start-double',
  cert: '/tmp/agent-eval-start-double/cert.pem',
  origin: 'https://127.0.0.1:1',
  domain: 'fixture.invalid',
  mode: jest.fn(async () => {}),
  close: jest.fn(async () => {}),
};
const connect = jest.fn(async () => {});
const broker = {
  once: jest.fn(),
  listen: jest.fn((_socket, ready) => ready()),
  close: jest.fn((closed) => closed()),
};
jest.unstable_mockModule('node:fs/promises', () => ({
  ...fs,
  mkdir: jest.fn(async () => {}),
  readdir: jest.fn(async () => []),
}));
jest.unstable_mockModule('node:net', () => ({ createServer: jest.fn(() => broker) }));
jest.unstable_mockModule('@modelcontextprotocol/sdk/client/index.js', () => ({
  Client: class {
    connect = connect;
    listTools = async () => ({ tools: structuredClone(TOOL_DEFINITIONS) });
    close = async () => {};
  },
}));
jest.unstable_mockModule('@modelcontextprotocol/sdk/client/stdio.js', () => ({
  StdioClientTransport: class {
    stderr = { on: jest.fn() };
    close = async () => {};
  },
}));
jest.unstable_mockModule('../rehearsal/harness.js', () => ({
  Rehearsal: { start: async () => fake },
}));
jest.unstable_mockModule('../rehearsal/source-identity.js', () => ({
  snapshotSource: async () => ({ sha, trees: {} }),
}));
jest.unstable_mockModule('./build-proof.js', () => ({
  gitRead: () => sha,
  pinnedInputs: async () => ({ sha, inputs: { sha256: hash, files: [] } }),
  verifyBuildArtifacts: async () => ({ inputs: { files: [] } }),
  artifactProofErrors: () => [],
}));
jest.unstable_mockModule('./node-runtime.js', () => ({
  verifyNodeRuntime: async () => ({ sha256: hash, local: {} }),
  nodeProofErrors: () => [],
}));
jest.unstable_mockModule('./python-runtime.js', () => ({
  verifyPythonRuntime: async () => python,
  pythonProofErrors: () => [],
}));
jest.unstable_mockModule('./evaluator-identity.js', () => ({
  verifyEvaluatorIdentity: async () => ({ sha256: hash, files: [] }),
}));
const { EvaluationServer } = await import('./server.js');

for (const history of [false, true]) {
  test(`start stores the filtered advertisement and measurement, history=${history}`, async () => {
    connect.mockClear();
    const s = await EvaluationServer.start({
      buildRoot: '/tmp/agent-eval-start-double/selected',
      pin: sha,
      arm: { build: 'B', history, textCopy: true },
    });
    try {
      expect(connect).toHaveBeenCalledTimes(1);
      expect(s.rawTools).toEqual(TOOL_DEFINITIONS);
      const expected = TOOL_DEFINITIONS.filter((t) => history || t.name !== 'get_history');
      expect(s.tools).toEqual(expected);
      expect(s.tools).toHaveLength(history ? 5 : 4);
      expect(s.measurement).toEqual({
        count: expected.length,
        serializedCharacters: JSON.stringify({ tools: expected }).length,
        utf8Bytes: Buffer.byteLength(JSON.stringify({ tools: expected })),
      });
      if (!history) expect(JSON.stringify(s.tools)).not.toMatch(/history/i);
    } finally {
      await s.close();
    }
  });
}
