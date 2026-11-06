import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import os from 'node:os';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
import { createServer, TOOL_DEFINITIONS } from '../../dist/index.js';
import { RefStore } from '../../dist/lib/refs.js';
import { SearchRecords } from '../../dist/lib/search-records.js';
import { planSearch } from '../../dist/lib/search-assembly.js';
import { ToolFailure } from '../../dist/lib/tool-errors.js';
const original={...process.env};
beforeEach(()=>jest.useFakeTimers({now:1000000}));
afterEach(()=>{jest.clearAllTimers();jest.useRealTimers();process.env={...original};});
const book=(id,extra={})=>({id,title:'Solaris',author:'Lem',language:'French',extension:'pdf',...extra});
const env={ZLIBRARY_EMAIL:'synthetic.invalid',ZLIBRARY_PASSWORD:'fixture-only'};
const make=(invoke,extra={},opts={})=>new AcquisitionTools(invoke,{...env,...extra},opts);
const views=[{name:'zlibrary',capabilities:{configured:true,search_available:true,download_available:true},defaultSearch:{on:true}}];
const plan=()=>planSearch({query:'Solaris',sources:['zlibrary'],limit:1},views,{waitMs:100});
const collected=(id,more=true)=>[{source:'zlibrary',state:'answered',ms:1,copies:[{source:'zlibrary',id,ref:`input-${id}`,metadata:{title:'Solaris'},book:book(id)}],...(more?{more:true}:{})}];

test('F01 F05 F07 F35 public default search agrees with local limits and exact applies to LibGen',async()=>{
 const calls=[];const tools=new AcquisitionTools(async(op,args)=>{calls.push([op,args]);return {books:[book('one')]};},{});
 const limits=await tools.call('get_limits');expect(limits.search).toMatchObject({default_sources:['libgen'],wait_ms:12000,ordering:'fit-v1'});expect(limits.sources.annas).toMatchObject({default_search:false,off_reason:'route_off'});expect(calls).toEqual([]);
 const reply=await tools.call('search',{query:'Solaris',exact:true});expect(reply.rows).toHaveLength(1);expect(reply.rows[0].match).toBe('exact_title');expect(reply.all_answered).toBe(true);expect(Object.keys(reply.sources)).toEqual(['libgen','zlibrary','annas']);expect(reply.sources.zlibrary.reason).toBe('credentials_missing');expect(reply.sources.libgen.filtering.applied).toEqual(['exact']);expect(reply).not.toHaveProperty('routing');expect(calls).toEqual([['search_multi_source',{query:'Solaris',source:'libgen',count:50,multi_source:false}]]);
});
test('F02 F12 public source sets dispatch together and retain one frozen page in canonical order',async()=>{
 const outputs=[];for(const sources of [['libgen','zlibrary'],['zlibrary','libgen']]){
  const calls=[];let release;const slow=new Promise(r=>release=r);const tools=make(async(op,args)=>{calls.push([op,args]);return op==='search'?slow:{books:[book('libgen',{md5:'a'.repeat(32)})]};},{BOOK_SOURCE_DEFAULT_ORDER:'libgen,zlibrary'},{now:()=>0,nonce:1n});
  const pending=tools.call('search',{query:'Solaris',sources});await jest.advanceTimersByTimeAsync(0);expect(calls).toHaveLength(2);release({books:[book('zlib',{md5:'A'.repeat(32),hash:'opaque'})]});const reply=await pending;expect(reply.rows).toHaveLength(1);expect(reply.rows[0].other_copies).toHaveLength(1);expect(reply.sources.zlibrary.rows).toBe(1);expect(calls.find(c=>c[0]==='search')[1].count).toBe(50);expect(calls.find(c=>c[0]==='search_multi_source')[1].multi_source).toBe(true);outputs.push(reply);
 }expect(outputs[0]).toEqual(outputs[1]);
});
test('F11 F14 public cut is terminal and fences late output before refs or cursor publication',async()=>{
 let resolve,child;let now=0;const tools=make(async(op,args,options)=>{if(op==='search'){child=options.signal;return new Promise(r=>resolve=r);}return {books:[book('fast')]};},{BOOK_SOURCE_SEARCH_WAIT_MS:'100'},{now:()=>now});
 const pending=tools.call('search',{query:'Solaris'});await jest.advanceTimersByTimeAsync(0);now=100;await jest.advanceTimersByTimeAsync(100);const reply=await pending;expect(reply.sources.zlibrary).toMatchObject({state:'late',ms:100});expect(child.reason).toBe('search_cut');expect(reply.all_answered).toBe(false);const before=JSON.stringify(reply);resolve({books:[book('late')]});await jest.advanceTimersByTimeAsync(0);expect(JSON.stringify(reply)).toBe(before);expect(reply.rows).toHaveLength(1);expect(reply.note).toMatch(/zlibrary: late/);expect((await tools.call('get_limits')).sources.zlibrary.recent).toEqual({searches:1,late:1,failed:0});
});
test('F13 a single dispatched public source retains its full wait',async()=>{
 let resolve,signal;const tools=make(async(_op,_args,options)=>{signal=options.signal;return new Promise(r=>resolve=r);},{BOOK_SOURCE_SEARCH_WAIT_MS:'100'});const pending=tools.call('search',{query:'Solaris',sources:['zlibrary']});await jest.advanceTimersByTimeAsync(30000);expect(signal.aborted).toBe(false);resolve({books:[book('slow')]});expect((await pending).sources.zlibrary.state).toBe('rows');
});
test('F15 public caller cancellation returns cancelled without comparison origin',async()=>{
 const signals=[];const tools=make(async(_op,_args,options)=>{signals.push(options.signal);return new Promise(()=>{});});const caller=new AbortController();const pending=tools.call('search',{query:'Solaris'},{signal:caller.signal});await jest.advanceTimersByTimeAsync(0);caller.abort();const reply=await pending;expect(reply.error.code).toBe('cancelled');expect(reply.error.attempts).toHaveLength(3);expect(reply.error.attempts.slice(0,2).every(a=>a.state==='failed'&&a.code==='cancelled'&&a.reason==='aborted')).toBe(true);expect(signals).toHaveLength(2);expect(signals.every(s=>s.aborted&&s.reason!=='search_cut')).toBe(true);
});
test.each([11999,12000,12001])('M06 public strict observation at %i preserves cut membership',async observed=>{
 let resolve,now=0;const tools=make(async(op)=>op==='search'?new Promise(r=>resolve=r):{books:[book('fast')]},{},{now:()=>now});const pending=tools.call('search',{query:'Solaris'});await jest.advanceTimersByTimeAsync(0);now=observed;resolve({books:[book('boundary')]});await jest.advanceTimersByTimeAsync(observed);const reply=await pending;expect(reply.sources.zlibrary.state).toBe(observed<12000?'rows':'late');expect(reply.rows).toHaveLength(observed<12000?2:1);
});
test('F09 startup now rejects malformed wait/format settings before creating a server',async()=>{
 process.env.ZLIBRARY_OUTPUT_ROOT=os.tmpdir();process.env.BOOK_SOURCE_SEARCH_WAIT_MS='bad';expect(()=>createServer()).toThrow(/BOOK_SOURCE_SEARCH_WAIT_MS/);delete process.env.BOOK_SOURCE_SEARCH_WAIT_MS;process.env.BOOK_FORMAT_PREFERENCE='pdf,,pdf';expect(()=>createServer()).toThrow(/BOOK_FORMAT_PREFERENCE/);
});
test('F10 admission snapshot refuses before dispatch and limits exposes the same gate',async()=>{
 const calls=[];const tools=make(async(op)=>{calls.push(op);return {books:[book('fast')]};},{},{localState:()=>({zlibrary:{breaker:'closed',admission:'backoff',admissionRetryAfterS:7}})});const reply=await tools.call('search',{query:'Solaris'});expect(calls).toEqual(['search_multi_source']);expect(reply.sources.zlibrary).toEqual({state:'not_asked',reason:'auth_backoff',retry_after_s:7});const limits=await tools.call('get_limits');expect(limits.sources.zlibrary).toMatchObject({breaker:'closed',admission:'backoff',retry_after_s:7,recent:{searches:0,failed:0,late:0}});
});
test('F16 typed failures keep configuration recovery through the public frozen snapshot',async()=>{
 const tools=make(async()=>{throw new ToolFailure('invalid_argument','Source configuration failed.',{retry:'no',recovery:'Configure deadline.',detail:{reason:'configuration_error',request_issued:false}});});const reply=await tools.call('search',{query:'Solaris'});expect(reply.error).toMatchObject({code:'all_sources_failed',recovery:'Configure deadline.'});expect(reply.error.attempts).toHaveLength(3);
});
test('F36 F38 public cursors memoize refill and selections keep source-bound raw books',async()=>{
 let calls=0;const tools=make(async(op,args)=>{if(op==='search'){calls++;return {books:Array.from({length:50},(_,i)=>book(`${args.page}-${i}`,{hash:'opaque'}))};}return {title:'Detail'};});const first=await tools.call('search',{query:'Solaris',sources:['zlibrary'],limit:50});const second=await tools.call('search',{cursor:first.next_cursor});expect(calls).toBe(2);expect(await tools.call('search',{cursor:first.next_cursor})).toEqual(second);expect(calls).toBe(2);const detail=await tools.call('get_book',{ref:second.rows[0].ref});expect(detail.source).toBe('zlibrary');expect(detail.identifiers.hash).toBe('opaque');
});
test('retained window cap stops further requests while existing pages and refs remain usable',async()=>{
 const store=new SearchRecords({capacity:2,maxWindows:2,nonce:1n});let calls=0;const first=store.retain(plan(),collected('a'),{refill:async()=>{calls++;return collected('b')[0];}});const second=await store.page(first.next_cursor);expect(second.next_cursor).toBeUndefined();expect(second.sources.zlibrary.more).toBe(true);expect(store.select(first.rows[0].ref).copy.id).toBe('a');expect(await store.page(first.next_cursor)).toEqual(second);expect(calls).toBe(1);
});
test('one refill subscriber cancels independently and its peer materializes a memoized page',async()=>{
 const store=new SearchRecords({capacity:2});let release,signal,calls=0;const first=store.retain(plan(),collected('a'),{refill:async(_page,s)=>{calls++;signal=s;return new Promise(r=>release=r);}});const caller=new AbortController();const a=store.page(first.next_cursor,{signal:caller.signal}).catch(e=>e);const b=store.page(first.next_cursor);await jest.advanceTimersByTimeAsync(0);caller.abort();let settled=false;a.then(()=>settled=true);await jest.advanceTimersByTimeAsync(0);expect(settled).toBe(true);expect((await a).code).toBe('cancelled');expect(signal.aborted).toBe(false);release(collected('b',false)[0]);expect((await b).rows).toHaveLength(1);expect(await store.page(first.next_cursor)).toEqual(await b);expect(calls).toBe(1);
});
test('last refill subscriber withdraws ownership and late completion cannot materialize',async()=>{
 const store=new SearchRecords({capacity:2});let release,signal;const first=store.retain(plan(),collected('a'),{refill:async(_page,s)=>{signal=s;return new Promise(r=>release=r);}});const caller=new AbortController();const a=store.page(first.next_cursor,{signal:caller.signal}).catch(e=>e);await jest.advanceTimersByTimeAsync(0);caller.abort();await jest.advanceTimersByTimeAsync(0);expect(signal.aborted).toBe(true);expect((await a).code).toBe('cancelled');release(collected('late')[0]);await jest.advanceTimersByTimeAsync(0);expect(store.select(first.rows[0].ref).copy.id).toBe('a');
});

test('A5 public wire schema accepts merged replies and refuses malformed state evidence',async()=>{
 const {default:Ajv}=await import('ajv');const validate=new Ajv({strict:false}).compile(TOOL_DEFINITIONS.find(t=>t.name==='search').outputSchema);
 const reply=await new AcquisitionTools(async()=>({books:[book('one')]}),{}).call('search',{query:'Solaris'});expect(validate(reply)).toBe(true);
 for(const line of [{state:'late'},{state:'rows',rows:0,ms:1},{state:'failed',ms:1}]){const bad=structuredClone(reply);bad.sources.libgen=line;expect(validate(bad)).toBe(false);}
 const bad=structuredClone(reply);bad.rows[0].other_copies=[{ref:'r-other',source:'libgen'}];delete bad.rows[0].route_basis;expect(validate(bad)).toBe(false);
});

test('abandoned refill remains fenced until its owner settles and cannot overlap a retry',async()=>{
 const store=new SearchRecords({capacity:2});let release,calls=0;const first=store.retain(plan(),collected('a'),{refill:async()=>{calls++;return new Promise(r=>release=r);}});const caller=new AbortController();const a=store.page(first.next_cursor,{signal:caller.signal}).catch(e=>e);await jest.advanceTimersByTimeAsync(0);caller.abort();await a;await jest.advanceTimersByTimeAsync(0);const retry=store.page(first.next_cursor).catch(e=>e);await jest.advanceTimersByTimeAsync(0);expect(calls).toBe(1);release(collected('late')[0]);await retry;
});
test('M04 real owner backoff and breaker admission are visible locally without side effects',async()=>{
 const {EapiSessionOwner}=await import('../../dist/lib/eapi-session.js');const {CircuitBreaker}=await import('../../dist/lib/circuit-breaker.js');const owner=new EapiSessionOwner();const breaker=new CircuitBreaker({threshold:1,timeout:100});
 const pending=owner.run('synthetic-state',(_input,signal)=>new Promise((_r,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error('synthetic'),{context:{reason:'aborted'}})),{once:true})),{timeoutMs:50}).catch(e=>e);await jest.advanceTimersByTimeAsync(50);await pending;
 const tools=make(async()=>{throw new Error('must not issue');},{},{localState:()=>({zlibrary:{...owner.admission('synthetic-state'),...breaker.snapshot()}})});const limits=await tools.call('get_limits');expect(limits.sources.zlibrary).toMatchObject({admission:'backoff',retry_after_s:30,breaker:'closed'});expect(owner.admission('unseen')).toEqual({admission:'open'});
 await breaker.execute(async()=>{throw new Error('synthetic');}).catch(()=>{});expect(breaker.snapshot()).toMatchObject({breaker:'open',breakerBlocked:true,breakerRetryAfterS:0.1});await jest.advanceTimersByTimeAsync(101);expect(breaker.snapshot().breakerBlocked).toBe(false);expect(breaker.getFailureCount()).toBe(1);
});

test('public dispatch retains an explicit bridge budget while owning the child signal',async()=>{
 let received;const tools=make(async(_op,_args,options)=>{received=options;return {books:[book('one')]};});const caller=new AbortController();const reply=await tools.call('search',{query:'Solaris',sources:['zlibrary']},{timeoutMs:456,signal:caller.signal});expect(reply.rows).toHaveLength(1);expect(received.timeoutMs).toBe(456);expect(received.signal).not.toBe(caller.signal);
});

test('A6 public admission uses each frozen capability snapshot once',async()=>{
 const {SourceRegistry}=await import('../../dist/lib/source-registry.js');const {windowFixture}=await import('./window-fixture.js');const reads={first:0,second:0};let issued=0;
 const registry=new SourceRegistry(['first','second'].map(name=>({name,capabilities:()=>({configured:true,search_available:++reads[name]===1,download_available:true}),defaultSearch:()=>({on:true}),md5:()=>undefined,searchWindow:async()=>{issued++;return windowFixture([{id:name,metadata:{title:name}}]);}})));
 const tools=new AcquisitionTools(async()=>{throw new Error('No provider call');},{},{registry});const reply=await tools.call('search',{query:'Synthetic'});expect(reply.rows).toHaveLength(2);expect(reads).toEqual({first:1,second:1});expect(issued).toBe(2);
});
test('M06 public planning stall consumes the comparison deadline before any issue',async()=>{
 const {SourceRegistry}=await import('../../dist/lib/source-registry.js');let now=0,issued=0;
 const registry=new SourceRegistry(['first','second'].map(name=>({name,capabilities:()=>{now=100;return {configured:true,search_available:true,download_available:true};},defaultSearch:()=>({on:true}),md5:()=>undefined,searchWindow:async()=>{issued++;return {rows:[],accepted:0};}})));
 const reply=await new AcquisitionTools(async()=>{throw new Error('No provider call');},{BOOK_SOURCE_SEARCH_WAIT_MS:'100'},{registry,now:()=>now}).call('search',{query:'Synthetic'});expect(issued).toBe(0);expect(reply.error.code).toBe('all_sources_failed');expect(reply.error.attempts.every(a=>a.state==='late'&&a.ms===100)).toBe(true);
});
