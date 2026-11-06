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
