import {readFile,realpath} from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {dataOf,rowsOf,expectedError} from './oracles.js';
import {evaluateEAnswer} from './answers.js';
import {scoreSlice2} from './e-slice2-oracles.js';
const data=c=>dataOf(c?.reply ?? {});
const ok=c=>!!c && !c.error && !c.reply?.isError && !data(c).error;
const same=(a,b)=>JSON.stringify(a) === JSON.stringify(b);
export async function scoreETask(input) {
  const {task,trace,final='',quotaSpent=0,providerEvents=[],downloads,context={}}=input;
  if(!['E4','E6','E8','E11','E13','E16'].includes(task.id)) return scoreSlice2(input);
  const wrong=[],files=[];
  const mark=(reason,c)=>wrong.push({reason,...(c?{tool:c.name,callIndex:trace.indexOf(c)}:{})});
  const allowed={E4:['search','get_book','get_limits','download'],E6:['search','get_book','get_limits'],E8:['search','get_book','get_limits'],
    E11:['search','get_book','get_limits','download'],E13:['search','get_book','get_limits','download'],E16:['get_history','get_book','get_limits']}[task.id];
  for(const c of trace) {
    if(!allowed.includes(c.name)) mark('off-oracle tool',c);
    if(!ok(c) && !expectedError(task,c)) mark('unexpected error',c);
    if(c.args?.cursor) {
      const previous=trace.slice(0,trace.indexOf(c)).find(p=>p.name === c.name && data(p).next_cursor === c.args.cursor);
      if(!previous || Object.keys(c.args).length !== 1) mark('continuation must use an observed cursor alone',c);
    }
  }
  const searches=trace.filter(c=>c.name === 'search' && ok(c));
  const details=trace.filter(c=>c.name === 'get_book' && ok(c));
  const histories=trace.filter(c=>c.name === 'get_history' && ok(c));
  const transfers=trace.filter(c=>c.name === 'download');
  const before=(a,b)=>!!a && !!b && trace.indexOf(a)<trace.indexOf(b);
  const issued=providerEvents.filter(e=>/^\/eapi\/book\/\d+\/hash\d+\/file$/.test(e.route));
  const acquired=providerEvents.filter(e=>/^\/files\//.test(e.route));
  let answer;
  if(['E4','E11'].includes(task.id)) {
    const target=task.id === 'E4'?202:402,download=transfers[0];
    const selected=searches.flatMap(s=>rowsOf(data(s)).map(row=>({s,row}))).find(({s,row})=>
      before(s,download) && row.ref === download?.args.ref && same(row,download?.selected));
    if(!selected || !download?.observedSelection || !ok(download)) mark('missing observed same-task selected acquisition');
    if(task.id === 'E4') {
      if(!details.some(c=>before(c,download) && c.args.ref === download?.args.ref && data(c).identifiers?.id === '202' &&
        data(c).translator === 'Margaret Graver and A. A. Long')) mark('missing Graver translator evidence before acquisition');
      answer={downloaded:'graver'};
    } else {
      if(selected?.row.title !== 'Crystal Forms' || Number(selected?.row.year) !== 1981) mark('wrong Niggli edition');
      answer={downloaded:'niggli-1981'};
    }
    if(transfers.length !== 1 || quotaSpent !== 1 || issued.length !== 1 || acquired.length !== 1 ||
      issued[0]?.route !== `/eapi/book/${target}/hash${target}/file` || acquired[0]?.route !== `/files/${target}.pdf`)
      mark('requires exactly one correct issuance and transfer');
    try {
      const file=await realpath(data(download).path ?? data(download).file_path),root=await realpath(downloads);
      if(!file.startsWith(root+path.sep)) throw new Error('outside root');
      const bytes=await readFile(file),expected=Buffer.from('%PDF-1.4\n'+`Synthetic fixture ${target}\n`.repeat(60));
      if(!bytes.equals(expected)) throw new Error('wrong exact bytes');
      const receipt=JSON.parse(await readFile(file+'.acq.json','utf8'));
      if(receipt.state !== 'complete' || receipt.source !== 'zlibrary' || receipt.identifiers?.id !== String(target) || receipt.identifiers?.hash !== `hash${target}` || receipt.path !== file || receipt.bytes !== bytes.length || receipt.sha256 !== createHash('sha256').update(bytes).digest('hex')) throw new Error('wrong receipt');
      files.push({path:file,bytes:bytes.length,fixtureId:target,receipt});
    } catch {mark('missing correct exact bytes and receipt');}
  }
  if(task.id === 'E6' || task.id === 'E8') {
    const attempts=trace.filter(c=>c.name === 'search').flatMap(c=>data(c).routing?.attempts ?? data(c).error?.attempts ?? []);
    const anna=attempts.filter(a=>a.source === 'annas').at(-1);
    const routes=providerEvents.filter(e=>e.route === '/annas/search');
    const permittedSources=task.id === 'E6'?['annas']:['libgen','annas'];
    if(!searches.length || searches.some(c=>(data(c).routing?.requested ?? c.args.sources ?? []).some(s=>!permittedSources.includes(s)))) mark('missing requested-source search evidence');
    if(task.id === 'E6' && (!anna || !routes.length || !['empty','rows'].includes(anna.outcome))) mark('Anna search must actually complete');
    if(task.id === 'E8' && !attempts.some(a=>a.source === 'libgen' && ['rows','empty'].includes(a.outcome))) mark('missing initial LibGen observation');
    if(anna && !routes.length) mark('Anna attempt lacks provider evidence');
    const status=anna?.outcome === 'failed' && anna.reason === 'browser_check'?'blocked':anna?.outcome ?? 'untried';
    answer={annas:status};
  }
  if(task.id === 'E13') {
    const c=transfers[0];
    if(transfers.length !== 1 || !expectedError(task,c ?? {}) || !c?.observedSelection ||
      !searches.some(s=>before(s,c) && rowsOf(data(s)).some(r=>r.ref === c.args.ref && r.title === 'Uncertain Transfer')) ||
      quotaSpent !== 1 || issued.length !== 1 || acquired.length !== 1 ||
      issued[0]?.route !== '/eapi/book/403/hash403/file' || acquired[0]?.route !== '/files/403.pdf') mark('requires one issued uncertain acquisition, no automatic repeat');
    if(context.variant === 'real-timeout' && (!context.timeoutReceipt?.timeout_branch || context.timeoutReceipt?.published !== false ||
      context.timeoutReceipt?.issued !== true || context.timeoutReceipt?.cancelled !== true)) mark('real production timeout branch not observed');
    answer={outcome:'unknown',check_destination:true,check_receipt:true,fresh_intent_required:true,automatic_retry:false};
  }
  if(task.id === 'E16') {
    const target=details.find(d=>data(d).identifiers?.id === '901' && data(d).translator === 'John Macquarrie and Edward Robinson');
    if(!target || !histories.some(h=>before(h,target) && rowsOf(data(h)).some(r=>r.ref === target.args.ref && r.title === 'Being and Time')) ||
      !providerEvents.some(e=>e.route === '/eapi/user/book/downloaded')) mark('missing observed target-translation history evidence');
    answer={downloaded:true,translator:'John Macquarrie and Edward Robinson'};
  }
  if(!['E4','E11','E13'].includes(task.id) && (quotaSpent || issued.length || acquired.length || transfers.length)) mark('read-only fixture acquired a book');
  const answerScore=evaluateEAnswer({final,expected:answer});
  if(answerScore.status !== 'correct') mark('missing or contradictory required answer fields');
  return {task:task.id,success:wrong.length === 0,pathCorrect:wrong.length === 0,outsideBar:true,answer:answerScore,
    wrongDecisions:wrong.length,wrong,calls:trace.length,quotaSpent,files,contractGaps:[],
    invalidCalls:trace.filter(c=>c.error || c.reply?.isError).length,expectedErrors:trace.filter(c=>expectedError(task,c)).length,
    refUnavailable:trace.filter(c=>data(c).error?.code === 'ref_unavailable').length};
}
