import { expect, test } from '@jest/globals';
import path from 'node:path';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { runPythonBridge } from '../../dist/lib/python-runner.js';

// Keep fan-out, sourceWindow translation, serialization, bridge dispatch,
// router and the LibGen adapter real. The fixture substitutes provider I/O.
function fixture() {
  const env = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith('ZLIBRARY_') && !key.startsWith('ANNAS_'),
    ),
  );
  env.PYTHONDONTWRITEBYTECODE = '1';
  return new AcquisitionTools(async (operation, args, options) => {
    const lines = await runPythonBridge(
      'comparison-bridge-worker.py',
      {
        pythonPath: path.resolve(
          process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
        ),
        scriptPath: path.resolve('__tests__/merged-search'),
        args: [operation, JSON.stringify(args)],
        mode: 'text',
        env,
      },
      { signal: options?.signal, timeoutMs: 20000 },
    );
    return JSON.parse(JSON.parse(lines.at(-1)).content[0].text);
  }, {});
}

test('comparison switch crosses the real TypeScript/Python join and reaches mirror two', async () => {
  const reply = await fixture().call('search', {
    query: 'Solaris',
    author: 'Lem',
    sources: ['libgen', 'annas'],
  });
  expect(reply.error).toBeUndefined();
  expect(reply.rows).toHaveLength(1);
  expect(reply.rows[0]).toMatchObject({ source: 'libgen', title: 'Solaris Lem (vg)' });
  expect(reply.sources.libgen).toMatchObject({ state: 'rows', rows: 1 });
  expect(reply.sources.annas).toMatchObject({ state: 'empty', rows: 0 });
  expect(reply.all_answered).toBe(true);
}, 30000);

test('sole-source join retains ordinary LibGen patience for mirror one', async () => {
  const reply = await fixture().call('search', {
    query: 'Solaris',
    author: 'Lem',
    sources: ['libgen'],
  });
  expect(reply.error).toBeUndefined();
  expect(reply.rows).toHaveLength(1);
  expect(reply.rows[0]).toMatchObject({ source: 'libgen', title: 'Solaris Lem (li)' });
  expect(reply.sources.annas).toMatchObject({ state: 'not_asked', reason: 'not_requested' });
  expect(reply.all_answered).toBe(true);
}, 30000);
