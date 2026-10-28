import { expect, test } from '@jest/globals';
import { PdfQueue } from '../dist/lib/pdf-queue.js';

// Missing serialization/cap, stale cancelled waiters, or deadlines starting on
// execution rather than enqueue each break these observable task outcomes.
test('FIFO serializes jobs and refuses the ninth waiting job', async () => {
  const queue = new PdfQueue();
  let release;
  const first = queue.run(
    () =>
      new Promise((r) => {
        release = r;
      }),
    1000,
  );
  const order = [];
  const waiting = Array.from({ length: 8 }, (_, i) =>
    queue.run(async () => {
      order.push(i);
      return i;
    }, 1000),
  );
  await expect(queue.run(async () => 99, 1000)).rejects.toThrow(
    /request was valid; the server is busy.*full/i,
  );
  release('first');
  expect(await first).toBe('first');
  expect(await Promise.all(waiting)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  expect(order).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
});
test('cancelled and expired waiting requests never execute and queue remains usable', async () => {
  const queue = new PdfQueue();
  let release;
  let executed = false;
  const first = queue.run(
    () =>
      new Promise((r) => {
        release = r;
      }),
    1000,
  );
  const controller = new AbortController();
  const cancelled = queue.run(
    async () => {
      executed = true;
    },
    1000,
    controller.signal,
  );
  controller.abort();
  await expect(cancelled).rejects.toThrow(/cancelled/);
  await expect(
    queue.run(async () => {
      executed = true;
    }, 5),
  ).rejects.toThrow(/request was valid; the server is busy.*timed out/i);
  release();
  await first;
  expect(executed).toBe(false);
  expect(await queue.run(async () => 42, 1000)).toBe(42);
});
