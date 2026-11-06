/** Unfiltered synthetic source window. Production adapters must supply the
 * actual per-record predicate evidence before their window is observed. */
export function windowFixture(rows = [], extra = {}) {
  const records = rows.map((row, index) => ({ ...row, rank: index + 1, filterResult: row.filterResult ?? { status: 'pass', checks: {} } }));
  return { ...extra, rows: records, accepted: records.filter(row => row.filterResult.status === 'pass').length };
}
