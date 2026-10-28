import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { lstat, readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { root } from './server.js';
import { pinnedInputs, verifyBuildArtifacts } from './build-proof.js';
import { eTasks } from './tasks.js';
import { scoreTask } from './oracles.js';
import { exerciseEFixture, wrongVariants } from './e-scripted.js';

// Dirty evaluator bytes are recorded as diagnostics, never as a clean/paid proof.
async function evaluatorSnapshot() {
  const files = [];
  async function visit(name) {
    if (/(?:^|\/)__pycache__(?:\/|$)|\.pyc$/.test(name)) return;
    const file = path.join(root,name), stat = await lstat(file);
    if (stat.isSymbolicLink()) throw new Error('diagnostic evaluator symlink: '+name);
    if (stat.isDirectory()) {
      for (const child of (await readdir(file)).sort()) await visit(name+'/'+child);
    } else files.push({path:name,sha256:createHash('sha256').update(await readFile(file)).digest('hex')});
  }
  await visit('__tests__/agent-eval'); await visit('__tests__/rehearsal');
  return {kind:'working evaluator diagnostic snapshot; not clean committed identity',files,
    sha256:createHash('sha256').update(JSON.stringify(files)).digest('hex')};
}
export async function runESelfTest(out) {
  const receipt = {date:new Date().toISOString(),kind:'scripted-current-interface-diagnostic',
    status:'running',settledABaseline:false,modelCalls:0,results:[]};
  await mkdir(path.dirname(path.resolve(out)),{recursive:true});
  const persist = () => writeFile(out,JSON.stringify(receipt,null,2)+'\n');
  try {
    const production = await pinnedInputs(root);
    receipt.production = {head:production.sha,inputs:production.inputs};
    receipt.buildProof = await verifyBuildArtifacts({buildRoot:root,pin:production.sha});
    receipt.evaluator = await evaluatorSnapshot();
    for (const id of [...eTasks.map(t => t.id),'E8-empty']) {
      await exerciseEFixture(id,async sample => {
        const good = await scoreTask(sample), bad = [];
        assert.equal(good.success,true,`${id}: ${JSON.stringify(good.wrong)}`);
        const variants = [
          {label:'abstention',trace:[],providerEvents:[],quotaSpent:0,final:'I do not know.'},
          {label:'memorized answer without tools',trace:[],providerEvents:[],quotaSpent:0},
          ...wrongVariants(sample),
        ];
        for (const variant of variants) {
          const input = {...sample,...variant}, score = await scoreTask(input);
          assert.equal(score.success,false,`${id}/${variant.label} tied the good path`);
          assert(score.wrongDecisions > good.wrongDecisions,`${id}/${variant.label} wrong-decision tie`);
          bad.push({label:variant.label,score,trace:input.trace,final:input.final});
        }
        receipt.results.push({id,coldSession:true,...sample,good,bad});
        await persist();
      });
    }
    const end = await pinnedInputs(root);
    receipt.productionStable = end.sha === production.sha && end.inputs.sha256 === production.inputs.sha256;
    receipt.evaluatorEnd = await evaluatorSnapshot();
    receipt.evaluatorStable = receipt.evaluator.sha256 === receipt.evaluatorEnd.sha256;
    assert(receipt.productionStable && receipt.evaluatorStable,'source drift during E self-test');
    receipt.status = 'E self-test pass (scripted current interface; NOT settled A or model performance)';
  } catch(error) {receipt.status='failed';receipt.error=String(error.stack);throw error;}
  finally {await persist();}
  return receipt;
}
