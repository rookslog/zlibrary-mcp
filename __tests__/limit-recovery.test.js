import { expect, jest, test } from '@jest/globals';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
import { ErrorEnvelopeSchema } from '../dist/lib/tool-errors.js';

const env = { ZLIBRARY_EMAIL: 'offline@example.invalid', ZLIBRARY_PASSWORD: 'offline' };

test.each(['search', 'get_history'])(
  '%s: mixed limit and legacy count errors keep both repair hints in order',
  async (operation) => {
    const invoke = jest.fn();
    const result = await new AcquisitionTools(invoke, env).call(operation, { limit: 0, count: 10 });
    expect(invoke).not.toHaveBeenCalled();
    expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
    expect(result.error.code).toBe('invalid_argument');
    expect(result.error.detail).toEqual({ reason: 'schema', fields: ['limit', ''] });
    expect(result.error.recovery).toBe(
      'Set limit to an integer from 1 to 50 (default 10); use paging with next_cursor passed as cursor for more. ' +
        'Use snake_case arguments; count becomes limit and sources is a set.',
    );
  },
);

for (const operation of ['search', 'get_history']) {
  test.each([0, -1, 1.5, 51])(
    `${operation}: invalid limit %s explains how to repair it without a provider call`,
    async (limit) => {
      const invoke = jest.fn();
      const args =
        operation === 'search' ? { query: 'book', sources: ['zlibrary'], limit } : { limit };
      const result = await new AcquisitionTools(invoke, env).call(operation, args);
      expect(invoke).not.toHaveBeenCalled();
      expect(ErrorEnvelopeSchema.safeParse(result).success).toBe(true);
      expect(result.error).toMatchObject({
        code: 'invalid_argument',
        operation,
        retry: 'no',
        attempts: [],
        detail: { reason: 'schema', fields: ['limit'] },
      });
      expect(result.error.recovery).toMatch(/limit.*integer from 1 to 50/i);
      expect(result.error.recovery).toMatch(/default 10/i);
      expect(result.error.recovery).toMatch(/pag(?:e|ing).*cursor/i);
      expect(result.error.recovery).toBe(
        'Set limit to an integer from 1 to 50 (default 10); use paging with next_cursor passed as cursor for more.',
      );
    },
  );
}

test.each([
  [
    'search',
    { query: 5 },
    'Use snake_case arguments; count becomes limit and sources is a set.',
  ],
  [
    'get_history',
    { source: 'unknown' },
    'Use snake_case arguments; count becomes limit and sources is a set.',
  ],
  [
    'download',
    { ref: 5 },
    'Pass ref, optional output_dir and filename only; text processing was removed in 2.0.',
  ],
])('%s: other schema failures retain their recovery', async (operation, args, recovery) => {
  const invoke = jest.fn();
  const result = await new AcquisitionTools(invoke, env).call(operation, args);
  expect(invoke).not.toHaveBeenCalled();
  expect(result.error.code).toBe('invalid_argument');
  expect(result.error.recovery).toBe(recovery);
});
