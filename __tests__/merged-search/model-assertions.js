import assert from 'node:assert/strict';
export function assertAccounting(reply, registered, wanted) {
  const lines = reply.error ? Object.fromEntries(reply.error.attempts.map(({ source, ...line }) => [source, line])) : reply.sources;
  assert.deepEqual(Object.keys(lines), registered);
  for (const line of Object.values(lines)) {
    assert.ok(['rows','empty','late','failed','not_asked'].includes(line.state));
    if (line.state !== 'not_asked') assert.ok(Number.isInteger(line.ms) && line.ms >= 0);
    if (line.state === 'rows') assert.ok(Number.isInteger(line.rows) && line.rows > 0);
    if (line.state === 'empty') assert.equal(line.rows, 0);
    if (['not_asked','failed'].includes(line.state)) assert.ok(typeof line.reason === 'string' && line.reason);
    if (line.state === 'failed') assert.ok(typeof line.code === 'string' && line.code);
  }
  if (reply.error) { assert.ok(!('rows' in reply)); return; }
  assert.equal(reply.all_answered, wanted.every(source => ['rows','empty'].includes(lines[source].state)));
  assert.equal(reply.ordering, 'fit-v1'); assert.ok(typeof reply.note === 'string' && reply.note.length);
  for (const row of reply.rows) {
    assert.equal(lines[row.source]?.state, 'rows');
    assert.ok(row.ref && ['exact_title','all_terms','source_only'].includes(row.match));
    if (row.other_copies?.length) assert.ok(row.route_basis);
    for (const copy of row.other_copies ?? []) {
      assert.ok(['rows','empty'].includes(lines[copy.source]?.state));
      if (lines[copy.source].state === 'empty') assert.ok(['fail','unknown'].includes(copy.filter));
    }
  }
}
