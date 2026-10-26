import { test, expect } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { ERROR_CODES, ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';
import { TOOL_DEFINITIONS } from '../dist/index.js';

test('receipt host observations do not leak into public download failures', async () => {
  const tools = new AcquisitionTools(async (operation) => {
    if (operation === 'search_multi_source') {
      return { books: [{ md5: 'a'.repeat(32), source: 'libgen', title: 'Offline' }] };
    }
    throw {
      message: 'bytes.private.invalid secret URL token',
      context: {
        details: {
          reason: 'all_sources_failed',
          host: 'bytes.private.invalid',
          failures: [{ provider: 'libgen', host: 'mirror.private.invalid', reason: 'read_timeout', detail: 'secret' }],
        },
      },
    };
  }, {});
  const found = await tools.call('search', { query: 'Offline', sources: ['libgen'] });
  const result = await tools.call('download', { ref: found.rows[0].ref });
  expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
  expect(result.error.attempts).toHaveLength(1);
  expect(JSON.stringify(result)).not.toMatch(/private\.invalid|secret|host|mirror|provenance/);
  expect(ERROR_CODES).toHaveLength(19);
  expect(TOOL_DEFINITIONS.map((tool) => tool.name)).toEqual(['search', 'get_book', 'download', 'get_limits', 'get_history']);
  expect(JSON.stringify({ tools: TOOL_DEFINITIONS })).toHaveLength(6551);
});
