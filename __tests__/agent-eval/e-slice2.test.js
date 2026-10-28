import {test, expect} from '@jest/globals';
import {eTasks} from './tasks.js';
import {exerciseEFixture, wrongVariants} from './e-scripted.js';
import {scoreTask} from './oracles.js';
import {futureContractWitness} from './e-slice2.js';

const cases = ['E5','E17-person','E17-pacing','E17-runtime','E17-ceiling','E18','E18-malformed',
  'E1','E1-direct','E2','E2-direct','E3','E3-direct','E7','E9','E10','E10-unknown','E10-canonical','E12','E12-direct','E12-fresh','E14','E15','E15-whole','E15-noimage','E15-map'];
test('all eighteen E tasks remain cold and separate from the legacy matrix', () => {
  expect(eTasks.map(t=>t.id).sort()).toEqual(Array.from({length:18},(_,i)=>`E${i+1}`).sort());
  expect(eTasks.every(t=>t.coldSession)).toBe(true);
});
for (const id of cases) test(`${id}: discriminate actual interface or retain an explicit future contract`, async()=>{
  await exerciseEFixture(id,async sample=>{
    const current = await scoreTask(sample);
    if (sample.context.expectsFutureContract) {
      expect(current.success).toBe(false);
      expect(current.contractGaps.length).toBeGreaterThan(0);
      expect(current.truthfulFallback ?? false).toBe(id === 'E9');
    } else expect(current).toMatchObject({success:true,wrongDecisions:0});
    const witness=futureContractWitness(sample), positive=witness ?? sample;
    const good=witness ? await scoreTask(witness) : current;
    expect(good).toMatchObject({success:true,wrongDecisions:0});
    const controls = [
      {label:'abstention',trace:[],providerEvents:[],quotaSpent:0,final:'I do not know.'},
      {label:'answer without tools',trace:[],providerEvents:[],quotaSpent:0},
      ...wrongVariants(positive),
    ];
    expect(controls.length).toBeGreaterThanOrEqual(3);
    for(const control of controls) {
      const score = await scoreTask({...positive,...control});
      expect({label:control.label,success:score.success}).toEqual({label:control.label,success:false});
      expect(score.wrongDecisions).toBeGreaterThan(good.wrongDecisions);
    }
  });
},300000);
