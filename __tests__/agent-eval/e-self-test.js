import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { lstat, readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { root } from './server.js';
import { pinnedInputs, verifyBuildArtifacts } from './build-proof.js';
import {pdfRuntime} from './e-pdf-runtime.js';
import { scoreTask } from './oracles.js';
import { exerciseEFixture, wrongVariants } from './e-scripted.js';
import {eCases, futureContractWitness} from './e-slice2.js';

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
export async function runESelfTest(out,{cases=eCases,executedBad=true}={}) {
  const receipt = {date:new Date().toISOString(),kind:'scripted-current-interface-diagnostic',
    status:'running',settledABaseline:false,modelCalls:0,results:[]};
  await mkdir(path.dirname(path.resolve(out)),{recursive:true});
  let pendingWrite=Promise.resolve();
  const persist=()=>pendingWrite=pendingWrite.then(()=>writeFile(out,JSON.stringify(receipt,null,2)+'\n'));
  const batches=async(items,work)=>{for(let i=0;i<items.length;i+=2){const results=await Promise.allSettled(items.slice(i,i+2).map(work));const failed=results.find(r=>r.status === 'rejected');if(failed)throw failed.reason;}};
  try {
    const production = await pinnedInputs(root);
    receipt.production = {head:production.sha,inputs:production.inputs};
    receipt.buildProof = await verifyBuildArtifacts({buildRoot:root,pin:production.sha,compileTimeoutMs:180000});
    receipt.evaluator = await evaluatorSnapshot();
    receipt.pdfRuntime=pdfRuntime();
    await batches(cases,async id=>{
      await exerciseEFixture(id,async sample => {
        const current = await scoreTask(sample), bad = [];
        receipt.active ??= {};receipt.active[id]={id,...sample,current};
        const witness = futureContractWitness(sample);
        if(witness) {
          assert.equal(current.success,false,`${id}: future contract unexpectedly passed`);
          assert(current.contractGaps.length,`${id}: missing explicit contract gap`);
        }
        const positive = witness ?? sample;
        const good = witness ? await scoreTask(witness) : current;
        assert.equal(good.success,true,`${id}: ${JSON.stringify(good.wrong)}`);
        const variants = [
          {label:'abstention',trace:[],providerEvents:[],quotaSpent:0,final:'I do not know.'},
          {label:'memorized answer without tools',trace:[],providerEvents:[],quotaSpent:0},
          ...wrongVariants(positive),
        ];
        for (const variant of variants) {
          const input = {...positive,...variant}, score = await scoreTask(input);
          assert.equal(score.success,false,`${id}/${variant.label} tied the good path`);
          assert(score.wrongDecisions > good.wrongDecisions,`${id}/${variant.label} wrong-decision tie`);
          bad.push({label:variant.label,score,trace:input.trace,final:input.final});
        }
        const alternateAnswer={...positive,final:JSON.stringify({note:'Tool observations retained',...Object.fromEntries(Object.entries(JSON.parse(positive.final)).reverse())},null,2)};
        assert.equal((await scoreTask(alternateAnswer)).success,true,`${id}: alternative answer serialization rejected`);
        delete receipt.active[id];
        receipt.results.push({id,coldSession:true,...sample,current,alternativeAnswerPassed:true,
          expectsFutureContract:!!witness,
          ...(witness?{futureOracleWitness:{...witness,score:good}}:{good}),bad});
        await persist();
        process.stdout.write(`${id}: ${current.success?'current PASS':'expects future contract'}; ${bad.length} bad traces rejected\n`);
      });
    });
    receipt.executedBad=[];
    if(executedBad) await batches([...new Set(cases.map(id=>id.split('-')[0]))],async base=>{
      await exerciseEFixture(base,async sample=>{
        const score=await scoreTask(sample);
        assert.equal(score.success,false,`${base}: executed wrong move passed`);
        assert(score.wrongDecisions > (score.contractGaps?.length ?? 0),`${base}: wrong call hidden by future-contract gap`);
        receipt.executedBad.push({id:base,...sample,score});
        await persist();
        process.stdout.write(`${base}: executed wrong move rejected\n`);
      },{wrongMove:true});
    });
    receipt.results.sort((a,b)=>cases.indexOf(a.id)-cases.indexOf(b.id));
    receipt.executedBad.sort((a,b)=>Number(a.id.slice(1))-Number(b.id.slice(1)));
    const end = await pinnedInputs(root);
    receipt.productionStable = end.sha === production.sha && end.inputs.sha256 === production.inputs.sha256;
    receipt.evaluatorEnd = await evaluatorSnapshot();
    receipt.evaluatorStable = receipt.evaluator.sha256 === receipt.evaluatorEnd.sha256;
    assert(receipt.productionStable && receipt.evaluatorStable,'source drift during E self-test');
    assert.deepEqual(pdfRuntime(),receipt.pdfRuntime,'PDF runtime identity drift');
    receipt.currentGoodCases=receipt.results.filter(r=>r.current.success).length;
    receipt.futureContractCases=receipt.results.filter(r=>r.expectsFutureContract).length;
    receipt.rejectedBadTraces=receipt.results.reduce((n,r)=>n+r.bad.length,0);
    receipt.status = 'E self-test pass (current diagnostics and explicit future-contract gaps; NOT settled A or model performance)';
  } catch(error) {receipt.status='failed';receipt.error=String(error.stack);throw error;}
  finally {await persist();}
  return receipt;
}
