import { expect, jest, test } from '@jest/globals';
import { ToolFailure } from '../../dist/lib/tool-errors.js';
import { RefStore } from '../../dist/lib/refs.js';
import { SearchRecords } from '../../dist/lib/search-records.js';
import { planSearch } from '../../dist/lib/search-assembly.js';
const views = ['libgen','zlibrary','annas'].map(name=>({name,capabilities:{configured:true,search_available:true,download_available:true},defaultSearch:{on:true}}));
const plan=(sources=['zlibrary'],extra={})=>planSearch({query:'Solaris',sources,limit:1,...extra},views,{waitMs:12000});
const copy=(source,id,extra={})=>({source,id,ref:`input-${source}-${id}`,metadata:{title:'Solaris',language:'French'},book:{id},...extra});
const window=(source,ids,more=false)=>({source,state:'answered',ms:1,copies:ids.map(id=>copy(source,id)),...(more?{more:true}:{})});
const store=capacity=>new SearchRecords({capacity,nonce:1n});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

test('F36 local cursors replay the frozen ranked window with zero fetches and retained selections',async()=>{
 const s=store(2),fetch=jest.fn(),input=[window('zlibrary',['a','b','c'])];const first=s.retain(plan(),input,{refill:fetch});input[0].copies[1].metadata.title='changed';
 const second=await s.page(first.next_cursor),again=await s.page(first.next_cursor);expect(second).toEqual(again);expect(second.rows[0].title).toBe('Solaris');expect(fetch).not.toHaveBeenCalled();expect(s.select(second.rows[0].ref).copy.id).toBe('b');expect(s.select(second.rows[0].ref).request.query).toBe('Solaris');expect(Object.isFrozen(second.rows[0])).toBe(true);
 expect((await s.page(second.next_cursor)).next_cursor).toBeUndefined();
});
test('F37 multi-source retained windows stop locally and preserve more without refill',async()=>{
 const s=store(2),refill=jest.fn();const p=s.retain(plan(['libgen','zlibrary'],{limit:50}),[window('libgen',['a'],true),window('zlibrary',['b'],true)],{refill});expect(p.next_cursor).toBeUndefined();expect(p.sources.libgen.more).toBe(true);expect(refill).not.toHaveBeenCalled();
});
test('F38 M07 refill is single-flight, memoized after a lost reply, and never re-queries a changing provider',async()=>{
 const s=store(2),pending=deferred();const refill=jest.fn(()=>pending.promise);const first=s.retain(plan(),[window('zlibrary',['a'],true)],{refill});
 const a=s.page(first.next_cursor),b=s.page(first.next_cursor);await Promise.resolve();expect(refill).toHaveBeenCalledTimes(1);expect(refill.mock.calls[0][0]).toBe(2);expect(refill.mock.calls[0][1]).toBeInstanceOf(AbortSignal);
 pending.resolve(window('zlibrary',['b','c'],true));await a;const received=await b;refill.mockImplementation(()=>Promise.resolve(window('zlibrary',['changed'])));
 expect(await s.page(first.next_cursor)).toEqual(received);expect(refill).toHaveBeenCalledTimes(1);expect(received.rows[0].ref).not.toBe(first.rows[0].ref);expect(s.select(first.rows[0].ref).copy.id).toBe('a');
 const third=await s.page(received.next_cursor);expect(s.select(third.rows[0].ref).copy.id).toBe('c');expect(refill).toHaveBeenCalledTimes(1);
});
test('F39 filters do not backfill a short or empty page; counts remain bound to its own window',()=>{
 const s=store(2),refill=jest.fn();const result=window('zlibrary',['a','b'],true);result.copies[1].metadata.language='English';const first=s.retain(plan(['zlibrary'],{limit:10,languages:['fr']}),[result],{refill});expect(first.rows).toHaveLength(1);expect(first.sources.zlibrary.filtering).toMatchObject({examined:2,matched:1,rejected:1});expect(refill).not.toHaveBeenCalled();expect(first.next_cursor).toBeDefined();
 const empty=s.retain(plan(['zlibrary'],{languages:['de']}),[result],{refill});expect(empty.rows).toEqual([]);expect(empty.next_cursor).toBeDefined();expect(refill).not.toHaveBeenCalled();
});
test('M08 LRU pressure evicts all row/copy/cursor aliases together and expired access never fetches',async()=>{
 const s=store(1),refill=jest.fn();const md5='a'.repeat(32);const a=s.retain(plan(['libgen','zlibrary']),[{...window('libgen',['a']),copies:[copy('libgen','a',{md5})]},{...window('zlibrary',['b','c'],true),copies:[copy('zlibrary','b',{md5}),copy('zlibrary','c')]}],{refill});
 const refs=[a.rows[0].ref,...a.rows[0].other_copies.map(c=>c.ref)];s.retain(plan(),[window('zlibrary',['new'])]);for(const ref of refs)expect(()=>s.select(ref)).toThrow(expect.objectContaining({code:'ref_unavailable'}));await expect(s.page(a.next_cursor)).rejects.toMatchObject({code:'ref_unavailable'});expect(refill).not.toHaveBeenCalled();
});
test('M08 touching any alias retains the whole parent rather than evicting siblings independently',()=>{
 const s=store(2);const first=s.retain(plan(),[window('zlibrary',['a','b'])]);const second=s.retain(plan(),[window('zlibrary',['c'])]);s.select(first.rows[0].ref);s.retain(plan(),[window('zlibrary',['d'])]);expect(()=>s.select(first.rows[0].ref)).not.toThrow();expect(()=>s.select(second.rows[0].ref)).toThrow(expect.objectContaining({code:'ref_unavailable'}));
});
test('M08 eviction during refill aborts its waiter and fences late completion before minting refs',async()=>{
 const s=store(1),pending=deferred();let signal;const first=s.retain(plan(),[window('zlibrary',['a'],true)],{refill:(_page,sig)=>{signal=sig;return pending.promise;}});const later=s.page(first.next_cursor);await Promise.resolve();const replacement=s.retain(plan(),[window('zlibrary',['new'])]);expect(signal.aborted).toBe(true);pending.resolve(window('zlibrary',['late'],true));await expect(later).rejects.toMatchObject({code:'ref_unavailable'});expect(s.select(replacement.rows[0].ref).copy.id).toBe('new');await expect(s.page(first.next_cursor)).rejects.toMatchObject({code:'ref_unavailable'});
});
test('canonical allocation ignores arrival order and hides standalone rejected records but keeps alternate selections',()=>{
 const make=reverse=>{const s=store(2),md5='a'.repeat(32),data=[{...window('libgen',[]),copies:[copy('libgen','a',{md5})]},{...window('zlibrary',[]),copies:[copy('zlibrary','b',{md5,metadata:{title:'Solaris',language:'English'}}),copy('zlibrary','hidden',{metadata:{language:'English'}})]}];const result=s.retain(plan(['libgen','zlibrary'],{languages:['fr']}),reverse?data.reverse():data);return {s,result};};
 const a=make(false),b=make(true);expect(a.result).toEqual(b.result);const alternate=a.result.rows[0].other_copies[0];expect(a.s.select(alternate.ref).copy.id).toBe('b');expect(a.s.select(alternate.ref).copy.ref).toBe(alternate.ref);expect(a.s.select(alternate.ref).request.languages).toEqual(['fr']);expect(a.result.rows[0].ref).toMatch(/^r[0-9a-z]+$/);expect(()=>a.s.select('input-zlibrary-hidden')).toThrow();
});
test('refill keeps prior pages immutable and suppresses already retained MD5 files without merging uncertain files',async()=>{
 const s=store(2),md5='a'.repeat(32);const first=s.retain(plan(),[{...window('zlibrary',[],true),copies:[copy('zlibrary','a',{md5})]}],{refill:async()=>({...window('zlibrary',[]),copies:[copy('zlibrary','dup',{md5}),copy('zlibrary','new'),copy('zlibrary','other')]})});const next=await s.page(first.next_cursor);expect(s.select(next.rows[0].ref).copy.id).toBe('new');expect(next.sources.zlibrary.rows).toBe(3);expect(first.rows).toHaveLength(1);expect(s.select(first.rows[0].ref).copy.id).toBe('a');expect(s.select((await s.page(next.next_cursor)).rows[0].ref).copy.id).toBe('other');
});
test('failed refill can retry explicitly; wrong kind and invalid capacity are refused',async()=>{
 for(const capacity of [0,-1,1.5])expect(()=>store(capacity)).toThrow();const s=store(1);const refill=jest.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(window('zlibrary',['b']));const first=s.retain(plan(),[window('zlibrary',['a'],true)],{refill});await expect(s.page(first.rows[0].ref)).rejects.toMatchObject({code:'invalid_argument'});await expect(s.page(first.next_cursor)).rejects.toThrow('offline');const next=await s.page(first.next_cursor);expect(s.select(next.rows[0].ref).copy.id).toBe('b');expect(refill).toHaveBeenCalledTimes(2);
});

test('retiring a stable alias releases it permanently without rewinding allocation',()=>{
 const refs=new RefStore({nonce:1n});const a=refs.mintStable('r',{id:'a'});refs.discard(a);refs.discard(a);expect(()=>refs.get(a,'r')).toThrow(expect.objectContaining({code:'ref_unavailable',info:expect.objectContaining({detail:expect.objectContaining({reason:'expired'})})}));const b=refs.mintStable('r',{id:'a'});expect(b).not.toBe(a);expect(refs.get(b,'r')).toEqual({id:'a'});
});

for(const mode of ['named','operator']) for(const source of ['libgen','zlibrary']) {
 test(`R4-01 ${mode} intent retains refusals while the sole dispatched ${source} refills and replays`,async()=>{
  const blocked=source==='libgen'?'zlibrary':'libgen';const input={query:'Solaris',limit:1,...(mode==='named'?{sources:[blocked,source]}:{})};const settings={waitMs:12000,...(mode==='operator'?{operatorSources:[blocked,source]}:{})};const p=planSearch(input,views,settings);const s=store(2),pending=deferred(),refill=jest.fn(()=>pending.promise);const refusal={source:blocked,state:'not_asked',reason:'breaker_open',retry_after_s:10};
  const first=s.retain(p,[window(source,['a','b'],true),refusal],{refill});const local=await s.page(first.next_cursor);expect(refill).not.toHaveBeenCalled();expect(local.next_cursor).toBeDefined();refusal.reason='changed after retention';
  const a=s.page(local.next_cursor),b=s.page(local.next_cursor),paired=Promise.all([a,b]);await Promise.resolve();expect(refill).toHaveBeenCalledTimes(1);expect(refill.mock.calls[0][0]).toBe(2);pending.resolve(window(source,['c']));const [,next]=await paired;expect(await s.page(local.next_cursor)).toEqual(next);expect(refill).toHaveBeenCalledTimes(1);
  for(const page of [first,local,next]){expect(page.sources[blocked]).toEqual({state:'not_asked',reason:'breaker_open',retry_after_s:10});expect(page.all_answered).toBe(false);expect(page.note).toContain(`${blocked}: not_asked (breaker_open)`);expect(page.rows[0].source).toBe(source);}expect(s.select(next.rows[0].ref).copy.id).toBe('c');expect(p.intent).toEqual(['libgen','zlibrary']);
 });
}
test.each(['late','failed'])('R4-01 a dispatched %s peer still prevents single-source refill',state=>{
 const s=store(2),refill=jest.fn();const peer=state==='late'?{source:'libgen',state,ms:12000}:{source:'libgen',state,ms:1,failure:new ToolFailure('provider_error','Failed.',{retry:'safe'})};const first=s.retain(plan(['libgen','zlibrary']),[peer,window('zlibrary',['a'],true)],{refill});expect(first.next_cursor).toBeUndefined();expect(refill).not.toHaveBeenCalled();
});
test('R4-01 empty refill with an unfulfilled intended source remains partial_failure',async()=>{
 const s=store(2),refill=jest.fn(async()=>window('zlibrary',[]));const first=s.retain(plan(['libgen','zlibrary']),[{source:'libgen',state:'not_asked',reason:'auth_backoff',retry_after_s:4},window('zlibrary',['a'],true)],{refill});expect(first.next_cursor).toBeDefined();await expect(s.page(first.next_cursor)).rejects.toMatchObject({code:'partial_failure',info:{attempts:expect.arrayContaining([expect.objectContaining({source:'libgen',state:'not_asked',reason:'auth_backoff'})])}});expect(refill).toHaveBeenCalledTimes(1);
});
