import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import fs from 'node:fs';
import { historyScenario } from './history-fixture.js';
beforeEach(() => jest.useFakeTimers({ now: Date.UTC(2001, 0, 1) }));
afterEach(() => jest.useRealTimers());
const golden = JSON.parse(fs.readFileSync(new URL('./history-golden.json', import.meta.url), 'utf8'));
function check(result) {
  expect(JSON.stringify(result)).toBe(JSON.stringify(golden));
  expect(result.first.rows[0].ref).toBeDefined(); expect(result.first.rows[1]).not.toHaveProperty('ref');
  expect(result.first.rows.map(row => row.downloaded_at)).toEqual(['2001-01-02', '2001-01-03']);
  expect(result.first).not.toHaveProperty('routing'); expect(result.first).not.toHaveProperty('match');
  expect(result.next.rows[0].title).toBe('Refill 1'); expect(result.replay.rows[0].title).toBe('Refill 2');
  expect(result.unsupported.error.code).toBe('unsupported_operation');
  expect(result.wrongOperation.error.code).toBe('invalid_argument');
  const acquisition = result.calls.find(call => call.operation === 'acquire_book');
  expect(acquisition.args.book_details.source).toBe('zlibrary'); expect(acquisition.args.book_details.hash).toBe('hash-101');
}
test('F31 M16 history preserves golden bytes, provider order, ref consumers and upstream replay', async () => check(await historyScenario()));
test('S4a history no longer executes the search page path', async () => check(await historyScenario(true)));
