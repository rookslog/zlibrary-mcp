import { jest, test, expect } from '@jest/globals';
import { EvaluationServer } from './server.js';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { TOOL_DEFINITIONS } from '../../dist/index.js';

// These checks use the real evaluator policy, AcquisitionTools and tool definitions;
// the MCP client and bridge are test doubles.
function proxy({ history, textCopy = true, representation = 'both', env = {} }) {
  const s = new EvaluationServer();
  s.arm = { build: 'B', history, textCopy };
  s.rawTools = TOOL_DEFINITIONS;
  s.tools = TOOL_DEFINITIONS.filter((t) => history || t.name !== 'get_history');
  s.trace = [];
  s.observed = new Map();
  s.rows = new Map();
  s.refRows = new Map();
  s.invoke = jest.fn(async () => {
    throw new Error('offline proxy control attempted provider access');
  });
  const product = new AcquisitionTools(s.invoke, env);
  s.client = {
    async callTool({ name, arguments: args }) {
      const data = await product.call(name, args);
      const reply = {
        content: representation === 'structured' ? [] : [{ type: 'text', text: JSON.stringify(data) }],
        ...(representation === 'text' ? {} : { structuredContent: data }),
        ...(data.error ? { isError: true } : {}),
      };
      s.upstream = reply;
      return reply;
    },
  };
  return s;
}

function expectOnlyHistoryRemoved(s, reply) {
  const upstream = s.upstream.structuredContent ?? JSON.parse(s.upstream.content[0].text);
  const expected = structuredClone(upstream);
  expected.sources.zlibrary.operations = ['search', 'details', 'download'];
  const payloads = [
    ...(reply.structuredContent ? [reply.structuredContent] : []),
    ...reply.content.filter((c) => c.type === 'text').map((c) => JSON.parse(c.text)),
  ];
  expect(payloads.length).toBeGreaterThan(0);
  for (const data of payloads) {
    expect(data.sources.zlibrary).toEqual(expected.sources.zlibrary);
    expect(data).toEqual(expected);
  }
  for (const block of reply.content.filter((c) => c.type === 'text')) {
    expect(block.text).toBe(JSON.stringify(expected));
  }
  // The evaluator must not rewrite the underlying product reply in place.
  expect(upstream.sources.zlibrary.operations).toContain('history');
  expect(s.trace[0].reply).toEqual(reply);
}

for (const representation of ['both', 'structured', 'text']) {
  for (const textCopy of [true, false]) {
    test(`disabled limits omit history in ${representation} replies, textCopy=${textCopy}`, async () => {
      const s = proxy({ history: false, textCopy, representation });
      const reply = await s.call('get_limits', { sources: ['zlibrary', 'libgen'], mode: 'local' });
      expectOnlyHistoryRemoved(s, reply);
      expect(s.invoke).not.toHaveBeenCalled();
    });

    test(`disabled configured live limits omit only history in ${representation} replies, textCopy=${textCopy}`, async () => {
      // Synthetic constructor input only; never install account variables in process.env.
      const env = {
        ZLIBRARY_EMAIL: 'rehearsal-account@example.invalid',
        ZLIBRARY_PASSWORD: 'rehearsal-password-canary',
      };
      const s = proxy({ history: false, textCopy, representation, env });
      const reply = await s.call('get_limits', { sources: ['zlibrary', 'libgen'], mode: 'live' });
      expect(s.invoke).toHaveBeenCalledTimes(1);
      expect(s.invoke).toHaveBeenCalledWith('get_download_limits', {}, {});
      const upstream = s.upstream.structuredContent ?? JSON.parse(s.upstream.content[0].text);
      expect(upstream.sources.zlibrary.configured).toBe(true);
      expect(upstream.sources.zlibrary).not.toHaveProperty('note');
      expect(upstream.sources.zlibrary.error).toBeDefined();
      expectOnlyHistoryRemoved(s, reply);
    });

    test(`normal limits preserve history in ${representation} replies, textCopy=${textCopy}`, async () => {
      const s = proxy({ history: true, textCopy, representation });
      const reply = await s.call('get_limits', { sources: ['zlibrary'], mode: 'local' });
      const expected = structuredClone(s.upstream);
      if (!textCopy && expected.structuredContent) expected.content = [];
      expect(reply).toEqual(expected);
      const data = reply.structuredContent ?? JSON.parse(reply.content[0].text);
      expect(data.sources.zlibrary.operations).toEqual(['search', 'details', 'download', 'history']);
    });
  }
}

for (const history of [false, true]) {
  test(`legacy history recovery is rejected before forwarding, history=${history}`, async () => {
    const s = proxy({ history });
    await expect(s.call('get_download_history')).rejects.toThrow(
      'tool disabled or unknown: get_download_history',
    );
    expect(s.trace[0]).toMatchObject({ forwarded: false, rejected: true });
    expect(s.trace[0].error).not.toMatch(/\bget_history\b|\bUse\b/);
    expect(s.upstream).toBeUndefined();
  });
}

test('disabled advertisement has four unchanged tools and blocks direct history calls', async () => {
  const s = proxy({ history: false });
  const tools = s.listTools();
  expect(tools.map((t) => t.name)).toEqual(['search', 'get_book', 'download', 'get_limits']);
  for (const tool of tools) expect(tool).toEqual(TOOL_DEFINITIONS.find((t) => t.name === tool.name));
  expect(JSON.stringify(tools)).not.toMatch(/history/i);
  s.tools = tools;
  await expect(s.call('get_history')).rejects.toThrow('tool disabled or unknown: get_history');
  expect(s.trace[0]).toMatchObject({ forwarded: false, rejected: true });
  expect(s.upstream).toBeUndefined();
});

test('normal advertisement retains all five product tools', () => {
  const s = proxy({ history: true });
  expect(s.listTools()).toEqual(TOOL_DEFINITIONS);
  expect(s.listTools().map((t) => t.name)).toEqual([
    'search', 'get_book', 'download', 'get_limits', 'get_history',
  ]);
});
