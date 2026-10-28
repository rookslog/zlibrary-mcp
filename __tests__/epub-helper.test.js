import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const realSetTimeout = global.setTimeout;
const delay = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));
let interpreter;
jest.unstable_mockModule('../dist/lib/venv-manager.js', () => ({
  getManagedPythonPath: async () => interpreter,
}));
const { ChatFiles } = await import('../dist/lib/chat-files.js');
let directory, root, old, warning;
let controls, requests;
beforeEach(async () => {
  controls = [];
  requests = [];
  old = { ...process.env };
  directory = await fs.mkdtemp(path.join(os.tmpdir(), 'epub-helper-'));
  root = path.join(directory, 'root');
  await fs.mkdir(root);
  await fs.copyFile('test_files/epub-nav/epub3.epub', path.join(root, 'book.epub'));
  process.env.ZLIBRARY_OUTPUT_ROOT = root;
  delete process.env.ZLIBRARY_PDF_PYTHON;
  process.env.LOG_LEVEL = 'warn';
  warning = jest.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(async () => {
  controls.forEach((c) => c.abort());
  await Promise.allSettled(requests);
  jest.useRealTimers();
  jest.restoreAllMocks();
  process.env = old;
  await fs.rm(directory, { recursive: true, force: true });
});
async function fakeHelper(body) {
  interpreter = path.join(directory, 'helper');
  await fs.writeFile(
    interpreter,
    `#!/bin/sh\ncat >/dev/null\necho $$ > '${directory}/pid'\n${body}\n`,
    { mode: 0o755 },
  );
}
const map = (signal) => {
  const control = new AbortController();
  if (signal?.aborted) control.abort();
  else signal?.addEventListener('abort', () => control.abort(), { once: true });
  controls.push(control);
  const request = new ChatFiles().call('get_book_map', { file: 'book.epub' }, control.signal);
  requests.push(request);
  return request;
};
const sections = (reply) => JSON.parse(reply.content[0].text).sections;
const diagnostic = () => warning.mock.calls.flat().join(' ');
async function started() {
  for (let n = 0; n < 500; n++) {
    try {
      return Number(await fs.readFile(path.join(directory, 'pid'), 'utf8'));
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
    await delay(10);
  }
  throw new Error('test helper did not start within 5 s');
}
async function dead(pid) {
  // Abort rejects on the child's error event, before the close/reap event.
  // Allow that lifecycle to finish, then require an actual ESRCH witness.
  for (let n = 0; n < 200; n++) {
    try {
      process.kill(pid, 0);
    } catch (error) {
      expect(error.code).toBe('ESRCH');
      return;
    }
    await delay(10);
  }
  throw new Error('EPUB test helper still exists 2 s after cancellation');
}
test('EPUB deadline stops the running helper at 10 seconds and logs its cause', async () => {
  await fakeHelper('exec sleep 30');
  jest.useFakeTimers({
    doNotFake: ['nextTick', 'setImmediate', 'queueMicrotask', 'performance', 'hrtime'],
  });
  let settled = false;
  const result = map().then((r) => {
    settled = true;
    return r;
  });
  const pid = await started();
  jest.advanceTimersByTime(9999);
  await delay(20);
  expect(settled).toBe(false);
  jest.advanceTimersByTime(1);
  expect(sections(await result)).toEqual([]);
  await dead(pid);
  expect(diagnostic()).toMatch(/EPUB.*10 s.*stopped/);
  expect(diagnostic()).not.toMatch(/PDF|ZLIBRARY_PDF_PYTHON/);
});
test.each([5242880, 5242881])('EPUB helper enforces the 5 MiB cap (%s bytes)', async (bytes) => {
  // Valid JSON at the boundary distinguishes output-limit rejection from parse errors.
  await fakeHelper(
    `exec python3 -c 'import sys,time; sys.stdout.write("{\\"sections\\":[]}" + " " * (${bytes}-15)); sys.stdout.flush(); time.sleep(${bytes > 5242880 ? 30 : 0})'`,
  );
  const reply = await map();
  const pid = await started();
  expect(sections(reply)).toEqual([]);
  await dead(pid);
  if (bytes === 5242881) expect(diagnostic()).toMatch(/EPUB.*output exceeded.*5 MiB/);
  else expect(diagnostic()).toBe('');
});
test('EPUB cancellation kills the active helper and emits one diagnostic', async () => {
  await fakeHelper('exec sleep 30');
  const controller = new AbortController();
  const result = map(controller.signal);
  const pid = await started();
  controller.abort();
  expect(sections(await result)).toEqual([]);
  await dead(pid);
  expect(warning).toHaveBeenCalledTimes(1);
  expect(diagnostic()).toMatch(/EPUB.*cancelled/);
});
test('EPUB queue saturation and queued cancellation are visible without reply changes', async () => {
  await fakeHelper('exec sleep 30');
  const active = new AbortController();
  const running = map(active.signal);
  const pid = await started();
  const controllers = Array.from({ length: 8 }, () => new AbortController());
  const waiting = controllers.map((c) => map(c.signal));
  // Source opens/resolver promises must enqueue all eight before the tenth request.
  await delay(100);
  expect(sections(await map())).toEqual([]);
  expect(diagnostic()).toMatch(/EPUB.*queue is full/);
  controllers[0].abort();
  expect(sections(await waiting[0])).toEqual([]);
  expect(diagnostic()).toMatch(/EPUB.*cancelled while queued/);
  controllers.slice(1).forEach((c) => c.abort());
  active.abort();
  await Promise.all([running, ...waiting]);
  await dead(pid);
});
test.each([undefined, 'relative-python'])(
  'EPUB interpreter failures name the managed environment (%s)',
  async (value) => {
    interpreter = value;
    expect(sections(await map())).toEqual([]);
    expect(diagnostic()).toMatch(/EPUB.*managed.*Python/i);
    expect(diagnostic()).not.toMatch(/PDF|ZLIBRARY_PDF_PYTHON/);
  },
);
test('EPUB start failure names the managed environment', async () => {
  interpreter = path.join(directory, 'missing');
  expect(sections(await map())).toEqual([]);
  expect(diagnostic()).toMatch(/EPUB.*could not start.*ENOENT/);
  expect(diagnostic()).not.toMatch(/PDF|ZLIBRARY_PDF_PYTHON/);
});
