import { checkMergedRowFilters } from '../../dist/lib/row-filters.js';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import { fanOut } from '../../dist/lib/fan-out.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { RefStore } from '../../dist/lib/refs.js';
import { windowFixture } from './window-fixture.js';
let now;
beforeEach(()=>{jest.useFakeTimers();now=0;});
afterEach(()=>{jest.clearAllTimers();jest.useRealTimers();});
const spec=(name,work)=>({name,capabilities:()=>({configured:true,search_available:true,download_available:true}),defaultSearch:()=>({on:true}),md5:row=>row.md5,searchWindow:work});
const record=(id,status)=>({id,md5:'a'.repeat(32),metadata:{language:status==='fail'?'English':status==='pass'?'French':undefined},filterResult:{status,checks:status==='pass'?{languages:{status:'pass',reason:'language_matches'}}:status==='fail'?{languages:{status:'fail',reason:'language_outside_requested'}}:{languages:{status:'unknown',reason:'language_missing'}}}});

test('F2 all fifty rejected records retain rank and evidence inside an empty accepted window',async()=>{
 const data=windowFixture(Array.from({length:50},(_,i)=>record(String(i),'fail')),{more:true});
 const result=await fanOut(new SourceRegistry([spec('only',async()=>data)]),{languages:['fr']},{now:()=>now});
 expect(result.sources.only).toMatchObject({state:'empty',rows:0});expect(result.note).toMatch(/^0 accepted records/);const kept=result.windows.get('only');expect(kept.accepted).toBe(0);expect(kept.rows).toHaveLength(50);expect(kept.rows.map(r=>r.rank)).toEqual(Array.from({length:50},(_,i)=>i+1));expect(kept.rows.every(r=>r.filterResult.status==='fail')).toBe(true);expect(Object.isFrozen(kept.rows[49].filterResult.checks.languages)).toBe(true);data.rows[49].metadata.language='changed';expect(kept.rows[49].metadata.language).toBe('English');
});
test('F2 mixed records all survive in the snapshot, but a late rejected copy cannot enter it or mint a ref',async()=>{
 let resolve;const late=new Promise(r=>resolve=r);const data=windowFixture(['pass','fail','unknown'].map((s,i)=>record(String(i),s)));
 const outcome=fanOut(new SourceRegistry([spec('first',async()=>data),spec('late',()=>late)]),{languages:['fr']},{now:()=>now,waitMs:100});await jest.advanceTimersByTimeAsync(0);now=100;await jest.advanceTimersByTimeAsync(100);const result=await outcome;
 expect(result.sources.first.rows).toBe(1);expect(result.note).toMatch(/^1 accepted records/);expect(result.windows.get('first').rows.map(r=>r.filterResult.status)).toEqual(['pass','fail','unknown']);expect(result.windows.has('late')).toBe(false);const refs=new RefStore({nonce:1n});for(const row of result.windows.get('first').rows)refs.mint('r',row);const before=JSON.stringify([...result.windows]);let reads=0;resolve({get rows(){reads++;return windowFixture([record('late','fail')]).rows;},accepted:0});await jest.advanceTimersByTimeAsync(0);expect(reads).toBe(0);expect(refs.counter).toBe(3n);expect(JSON.stringify([...result.windows])).toBe(before);
});
test.each([
 {rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:{}}}],accepted:0},
 {rows:[{id:'x',rank:2,filterResult:{status:'pass',checks:{}}}],accepted:1},
 {rows:[{id:'x',rank:1}],accepted:1},
])('F2 a malformed count/rank/evidence window is a protocol failure %p',async data=>{
 const result=await fanOut(new SourceRegistry([spec('only',async()=>data)]),{},{now:()=>now});expect(result.sources.only).toMatchObject({state:'failed',code:'provider_error',reason:'protocol_error'});expect(result.windows.size).toBe(0);
});

const active={query:'Solaris',languages:['fr'],exact:true};
test.each([
 ['empty checks',{status:'pass',checks:{}}],
 ['wrong predicate',{status:'pass',checks:{formats:{status:'pass',reason:'format_matches'}}}],
 ['same count wrong predicates',{status:'pass',checks:{formats:{status:'pass',reason:'format_matches'},year_from:{status:'pass',reason:'year_matches'}}}],
])('N1 rejects %s that do not answer the actual request',async(_name,evidence)=>{
 const data={rows:[{id:'unchecked',rank:1,metadata:{title:'Other',language:'English'},filterResult:evidence}],accepted:1};const result=await fanOut(new SourceRegistry([spec('only',async()=>data)]),active,{now:()=>now});expect(result.sources.only).toMatchObject({state:'failed',code:'provider_error',reason:'protocol_error'});expect(result.windows.size).toBe(0);
});
const n2cases=[
 ['V1 fifty-one records',()=>windowFixture(Array.from({length:51},(_,i)=>({id:String(i)}))),{},'failed'],
 ['V4 array checks',()=>({rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:[]}}],accepted:1}),{},'failed'],
 ['V5 unknown key',()=>({rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:{other:{status:'pass',reason:'unsupported'}}}}],accepted:1}),{},'failed'],
 ['V6 bad check status',()=>({rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:{languages:{status:'other',reason:'bad'}}}}],accepted:1}),{languages:['fr']},'failed'],
 ['V7 non-string reason',()=>({rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:{languages:{status:'pass',reason:4}}}}],accepted:1}),{languages:['fr']},'failed'],
 ['V8 inconsistent aggregate',()=>({rows:[{id:'x',rank:1,filterResult:{status:'pass',checks:{languages:{status:'fail',reason:'outside'}}}}],accepted:1}),{languages:['fr']},'failed'],
 ['V9 failure outranks unknown',()=>({rows:[{id:'x',rank:1,filterResult:{status:'fail',checks:{languages:{status:'fail',reason:'outside'},formats:{status:'unknown',reason:'missing'}}}}],accepted:0}),{languages:['fr'],formats:['pdf']},'empty'],
];
test.each(n2cases)('N2 %s enforces the source-window protocol',async(_name,build,request,state)=>{
 const result=await fanOut(new SourceRegistry([spec('only',async()=>build())]),request,{now:()=>now});expect(result.sources.only.state).toBe(state);if(state==='failed'){expect(result.sources.only).toMatchObject({code:'provider_error',reason:'protocol_error'});expect(result.windows.size).toBe(0);}else{expect(result.sources.only.rows).toBe(0);expect(result.windows.get('only').accepted).toBe(0);}
});

test('N1 complete real-checker evidence accepts all five predicates in any key order',async()=>{
 const request={query:'Solaris',exact:true,languages:['fr'],formats:['pdf'],year_from:2000,year_to:2002},metadata={title:'Solaris',author:'Lem',language:'French',format:'pdf',year:2001};const evidence=checkMergedRowFilters({...metadata,extension:metadata.format},request);expect(Object.keys(evidence.checks)).toHaveLength(5);const data=windowFixture([{id:'checked',metadata,filterResult:{...evidence,checks:Object.fromEntries(Object.entries(evidence.checks).reverse())}}]);const result=await fanOut(new SourceRegistry([spec('only',async()=>data)]),request,{now:()=>now});expect(result.sources.only).toMatchObject({state:'rows',rows:1});expect(result.windows.get('only').rows[0].filterResult.checks).toEqual(evidence.checks);
});
