import { test, expect } from '@jest/globals';
import { EvaluationServer } from './server.js';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { TOOL_DEFINITIONS } from '../../dist/index.js';

// Exercise the real evaluator call policy and product capability builder. Only
// the provider-facing MCP client is replaced; any bridge access is an error.
function proxy({ history, textCopy = true, representation = 'both' }) {
  const s = new EvaluationServer();
  s.arm = { build: 'B', history, textCopy };
  s.rawTools = TOOL_DEFINITIONS;
  s.tools = TOOL_DEFINITIONS.filter((t) => history || t.name !== 'get_history');
  s.trace = [];
  s.observed = new Map();
  s.rows = new Map();
  s.refRows = new Map();
  const product = new AcquisitionTools(async () => {
    throw new Error('offline proxy control attempted provider access');
  }, {});
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

for (const representation of ['both', 'structured', 'text']) {
  for (const textCopy of [true, false]) {
    test(`disabled limits omit history in ${representation} replies, textCopy=${textCopy}`, async () => {
      const s = proxy({ history: false, textCopy, representation });
      const reply = await s.call('get_limits', { sources: ['zlibrary', 'libgen'], mode: 'local' });
      const payloads = [
        ...(reply.structuredContent ? [reply.structuredContent] : []),
        ...reply.content.filter((c) => c.type === 'text').map((c) => JSON.parse(c.text)),
      ];
      expect(payloads.length).toBeGreaterThan(0);
      for (const data of payloads) {
        expect(data.sources.zlibrary.operations).toEqual(['search', 'details', 'download']);
        expect(data.sources.libgen).toEqual(
          (s.upstream.structuredContent ?? JSON.parse(s.upstream.content[0].text)).sources.libgen,
        );
      }
      // The evaluator must not rewrite the underlying product reply in place.
      const upstream = s.upstream.structuredContent ?? JSON.parse(s.upstream.content[0].text);
      expect(upstream.sources.zlibrary.operations).toContain('history');
      expect(s.trace[0].reply).toEqual(reply);
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
