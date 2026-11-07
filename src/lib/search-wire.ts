/** The five terminal source states are closed; names and reason strings are open. */
const elapsed = { type: 'integer', minimum: 0 };
const sourceLine = {
  oneOf: [
    {
      type: 'object',
      required: ['state', 'rows', 'ms'],
      properties: { state: { const: 'rows' }, rows: { type: 'integer', minimum: 1 }, ms: elapsed },
    },
    {
      type: 'object',
      required: ['state', 'rows', 'ms'],
      properties: { state: { const: 'empty' }, rows: { const: 0 }, ms: elapsed },
    },
    {
      type: 'object',
      required: ['state', 'ms'],
      properties: { state: { const: 'late' }, ms: elapsed },
    },
    {
      type: 'object',
      required: ['state', 'code', 'reason', 'ms'],
      properties: {
        state: { const: 'failed' },
        code: { type: 'string' },
        reason: { type: 'string' },
        ms: elapsed,
      },
    },
    {
      type: 'object',
      required: ['state', 'reason'],
      properties: {
        state: { const: 'not_asked' },
        reason: { type: 'string' },
        retry_after_s: { type: 'number', minimum: 0 },
      },
    },
  ],
};
export const searchSuccessShape = {
  required: ['rows', 'sources', 'all_answered', 'ordering', 'note'],
  properties: {
    rows: {
      type: 'array',
      items: {
        type: 'object',
        required: ['ref', 'source', 'match'],
        properties: {
          ref: { type: 'string' },
          source: { type: 'string' },
          match: { enum: ['exact_title', 'all_terms', 'source_only'] },
          other_copies: {
            type: 'array',
            minItems: 1,
            items: {
              type: 'object',
              required: ['ref', 'source'],
              properties: {
                ref: { type: 'string' },
                source: { type: 'string' },
                filter: { enum: ['pass', 'fail', 'unknown'] },
                unknown: { type: 'array', items: { type: 'string' } },
              },
            },
          },
          route_basis: { enum: ['only_usable', 'operator_order', 'stable_tie'] },
          differs: { type: 'array', items: { type: 'string' } },
          likely_group: { type: 'string' },
          likely_same_file_as: { type: 'string' },
          download_available: { const: false },
        },
        if: { required: ['other_copies'] },
        then: { required: ['route_basis'] },
      },
    },
    sources: { type: 'object', minProperties: 1, additionalProperties: sourceLine },
    all_answered: { type: 'boolean' },
    ordering: { const: 'fit-v1' },
    note: { type: 'string' },
    next_cursor: { type: 'string' },
  },
};
