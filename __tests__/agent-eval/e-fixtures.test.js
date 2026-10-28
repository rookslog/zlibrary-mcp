import { test, expect } from '@jest/globals';
import * as definitions from './tasks.js';
import { scoreTask } from './oracles.js';
import { readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { root } from './server.js';
import path from 'node:path';

const ids = ['E4', 'E6', 'E8', 'E11', 'E13', 'E16'];
test('E tasks are separately selectable and cold without changing the T matrix', () => {
  expect(definitions.tasks.map(t => t.id)).toEqual(Array.from({length:12}, (_, i) => `T${i+1}`));
  expect(definitions.eTasks?.map(t => t.id).sort()).toEqual(Array.from({length:18},(_,i)=>`E${i+1}`).sort());
  expect(definitions.eTasks?.every(t => t.coldSession === true)).toBe(true);
});
test('a task may accept its declared error without accepting a different error', async () => {
  const input = {
    task: {...definitions.tasks.find(t => t.id === 'T4'), expectedErrors: [{tool:'get_limits', code:'provider_timeout',requestIssued:false}]},
    arm: definitions.arms.find(a => a.id === 'B'),
    trace: [{name:'get_limits', args:{mode:'live', sources:['zlibrary']}, reply:{isError:true, structuredContent:{error:{code:'provider_timeout',requestIssued:false}}}}],
  };
  expect((await scoreTask(input)).wrongDecisions).toBe(0);
  input.trace[0].reply.structuredContent.error.code = 'internal';
  expect((await scoreTask(input)).wrongDecisions).toBe(1);
});
for (const id of [...ids, 'E8-empty']) {
  test(`${id}: real current-interface good trace beats abstention and targeted wrong moves`, async () => {
    const { exerciseEFixture, wrongVariants } = await import('./e-scripted.js');
    await exerciseEFixture(id, async sample => {
      const good = await scoreTask(sample);
      expect(good).toMatchObject({success:true, wrongDecisions:0});
      const none = await scoreTask({...sample, trace:[], providerEvents:[], quotaSpent:0, final:'I do not know.'});
      expect(none.success).toBe(false);
      for (const mutation of wrongVariants(sample)) {
        const bad = await scoreTask({...sample, ...mutation});
        expect({mutation:mutation.label, success:bad.success}).toEqual({mutation:mutation.label, success:false});
        expect(bad.wrongDecisions).toBeGreaterThan(good.wrongDecisions);
      }
      if (['E4','E11'].includes(id)) {
        const file = good.files[0].path, original = await readFile(file);
        try {
          await writeFile(file, Buffer.concat([original, Buffer.from('wrong trailing bytes')]));
          expect((await scoreTask(sample)).success).toBe(false);
        } finally { await writeFile(file, original); }
      }
      expect((await scoreTask({...sample,final:sample.final+' I claim the opposite.'})).success).toBe(false);
      // No tools plus the exact expected answer still cannot pass.
      expect((await scoreTask({...sample, trace:[], providerEvents:[], quotaSpent:0})).success).toBe(false);
    });
  }, 300000);
}

test('E inventory exactly matches fixed Python data and payload digests', async () => {
  const actual = JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-B','-c',`
import sys,json,hashlib
sys.path.insert(0, ${JSON.stringify(path.join(root,'__tests__/rehearsal'))})
from fake_catalogue import E_CATALOGUE,E_HISTORY,payload
print(json.dumps(dict(catalogue=E_CATALOGUE,history=E_HISTORY,downloadSha256={str(r['id']):hashlib.sha256(payload(r)).hexdigest() for r in E_CATALOGUE+E_HISTORY})))
`], {encoding:'utf8',env:{PATH:process.env.PATH,PYTHONDONTWRITEBYTECODE:'1'}}));
  const inventory = JSON.parse(await readFile(new URL('./inventory.json',import.meta.url),'utf8'));
  for (const [key,value] of Object.entries(actual)) expect(inventory.eFixtures[key]).toEqual(value);
  expect(actual.catalogue.map(r => r.id)).toEqual([401,402,403]);
  expect(actual.history.map(r => r.id)).toEqual([902,901]);
});
