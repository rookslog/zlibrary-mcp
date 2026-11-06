import {afterEach,beforeEach,expect,jest,test} from '@jest/globals';
import {AcquisitionTools} from '../../dist/lib/acquisition-tools.js';
import {SourceRegistry} from '../../dist/lib/source-registry.js';
import {windowFixture} from './window-fixture.js';
beforeEach(()=>jest.useFakeTimers());afterEach(()=>{jest.clearAllTimers();jest.useRealTimers();});
function fixture(){
 let calls=0;const jobs=[];
 const window=(page,label='fresh')=>windowFixture(Array.from({length:page===1?50:1},(_,i)=>({id:`${label}-${page}-${i}`,metadata:{title:`${label} ${i}`},book:{id:`${label}-${page}-${i}`,title:`${label} ${i}`}})),page===1?{more:true}:{});
 const registry=new SourceRegistry([{name:'libgen',capabilities:()=>({configured:true,search_available:true,download_available:true}),defaultSearch:()=>({on:true}),md5:()=>undefined,searchWindow:(req,signal)=>{calls++;if(req.page===1)return Promise.resolve(window(1));return new Promise(resolve=>jobs.push({signal,resolve:label=>resolve(window(req.page,label))}));}}]);
 const tools=new AcquisitionTools(async()=>{throw Error('no bridge');},{},{registry});
 return {tools,jobs,calls:()=>calls};
}
test('R7-01 public abandoned cursor position stays fenced until its adapter promise settles',async()=>{
 const f=fixture();const first=await f.tools.call('search',{query:'fresh',limit:50});const caller=new AbortController();
 const pending=f.tools.call('search',{cursor:first.next_cursor},{signal:caller.signal});await jest.advanceTimersByTimeAsync(0);caller.abort();expect((await pending).error.code).toBe('cancelled');await jest.advanceTimersByTimeAsync(0);
 const retry=f.tools.call('search',{cursor:first.next_cursor});await jest.advanceTimersByTimeAsync(0);
 try{expect(f.calls()).toBe(2);expect((await retry).error.code).toBe('cancelled');}finally{for(const job of f.jobs)job.resolve('late');await jest.advanceTimersByTimeAsync(0);}
 const fresh=f.tools.call('search',{cursor:first.next_cursor});await jest.advanceTimersByTimeAsync(0);expect(f.calls()).toBe(3);f.jobs.at(-1).resolve('replacement');const page=await fresh;expect(page.rows[0].title).toBe('replacement 0');expect(await f.tools.call('search',{cursor:first.next_cursor})).toEqual(page);
});
test('R7-02 a sole cursor subscriber enforces its timeout and retains the abandoned-position fence',async()=>{
 const f=fixture();const first=await f.tools.call('search',{query:'fresh',limit:50});const pending=f.tools.call('search',{cursor:first.next_cursor},{timeoutMs:40});let reply;pending.then(r=>reply=r);await jest.advanceTimersByTimeAsync(40);
 try{expect(reply).toMatchObject({error:{code:'provider_timeout',retry:'safe',detail:{reason:'caller_timeout'}}});expect(reply.error.attempts).toEqual([{source:'libgen',state:'failed',code:'provider_timeout',reason:'caller_timeout',ms:40}]);expect(f.jobs[0].signal.aborted).toBe(true);const retry=f.tools.call('search',{cursor:first.next_cursor});await jest.advanceTimersByTimeAsync(0);expect(f.calls()).toBe(2);await retry;}finally{for(const job of f.jobs)job.resolve('late');await jest.advanceTimersByTimeAsync(0);await pending;}
});
test('R7-02 short-budget refill caller cannot abort a longer-budget peer or alter local replay',async()=>{
 const f=fixture();const first=await f.tools.call('search',{query:'fresh',limit:50});let shortReply,longReply;
 const a=f.tools.call('search',{cursor:first.next_cursor},{timeoutMs:10}).then(r=>shortReply=r);const b=f.tools.call('search',{cursor:first.next_cursor},{timeoutMs:100}).then(r=>longReply=r);
 await jest.advanceTimersByTimeAsync(10);
 try{expect(shortReply?.error.code).toBe('provider_timeout');expect(longReply).toBeUndefined();expect(f.jobs[0].signal.aborted).toBe(false);}finally{f.jobs[0].resolve('peer');await Promise.all([a,b]);}
 expect(longReply.rows[0].title).toBe('peer 0');expect(await f.tools.call('search',{cursor:first.next_cursor},{timeoutMs:1})).toEqual(longReply);expect(f.calls()).toBe(2);
});
