import { checkMergedRowFilters } from '../../dist/lib/row-filters.js';
/** Synthetic source adapter: evaluate the actual request before observation.
 * Explicit evidence remains available for malformed-protocol fixtures. */
export function windowFixture(rows = [], extra = {}, request = {}) {
  const records = rows.map((row, index) => {
    const metadata = row.metadata ?? row;
    return { ...row, rank: index + 1, filterResult: row.filterResult ?? checkMergedRowFilters({ ...metadata, extension: metadata.format ?? metadata.extension }, request) };
  });
  return { ...extra, rows: records, accepted: records.filter(row => row.filterResult.status === 'pass').length };
}
