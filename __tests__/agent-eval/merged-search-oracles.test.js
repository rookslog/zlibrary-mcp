import {test,expect} from '@jest/globals';
import {scoreTask} from './oracles.js';
import {tasks,arms} from './tasks.js';
import {EvaluationServer} from './server.js';
const call=(name,args,data)=>({name,args,reply:{structuredContent:data}});
const row={ref:'libgen-observed',title:'Persuasion',source:'libgen'};
const lines=(z={state:'not_asked',reason:'not_in_default_order'})=>({libgen:{state:'rows',rows:1,ms:1},zlibrary:z,annas:{state:'not_asked',reason:'route_off'}});
const score=(data,searchArgs={},defaultSources=['libgen'])=>scoreTask({task:tasks.find(t=>t.id==='T11'),arm:{...arms.find(a=>a.id==='B-Q2-default'),defaultOrder:defaultSources},trace:[call('get_limits',{mode:'live'},{search:{default_sources:defaultSources}}),call('search',{query:'Persuasion',...searchArgs},data)],remainingBefore:0});
test('omitted sources use resolved intent and source accounting, never first-answer routing',async()=>{
 expect((await score({rows:[row],sources:lines(),all_answered:true})).success).toBe(true);
 expect((await score({rows:[row],routing:{served_by:'libgen'}})).success).toBe(false);
 expect((await score({rows:[row],sources:lines(),all_answered:false},{},['libgen','zlibrary'])).success).toBe(false);
 // List order gives no protection from dispatching an exhausted source.
 expect((await score({rows:[row],sources:lines({state:'empty',rows:0,ms:2}),all_answered:true},{},['libgen','zlibrary'])).success).toBe(false);
});
test.each(['late','failed','not_asked'])('a %s LibGen line cannot vouch for a returned LibGen ref',async state=>{
 const sources=lines();sources.libgen={state,reason:'synthetic',code:'provider_error',ms:1};
 expect((await score({rows:[row],sources,all_answered:false})).success).toBe(false);
});
test('explicit non-exhausted source is checked against actual lines and returned provenance',async()=>{
 const data={rows:[row],sources:lines(),all_answered:true};
 expect((await score(data,{sources:['libgen']})).success).toBe(true);
 expect((await score({...data,sources:lines({state:'empty',rows:0,ms:2})},{sources:['libgen']})).success).toBe(false);
 expect((await score({...data,rows:[{...row,source:'zlibrary'}]},{sources:['libgen']})).success).toBe(false);
});
test('evaluation broker observes actual alternative-copy refs but not availability hints',async()=>{
 const server=new EvaluationServer();Object.assign(server,{arm:{history:true,textCopy:true},trace:[],observed:new Map(),rows:new Map(),refRows:new Map(),tools:[{name:'search'},{name:'get_book'}],client:{callTool:async()=>({structuredContent:{rows:[{ref:'primary',source:'zlibrary',title:'Persuasion',other_copies:[{ref:'alternative',source:'libgen'}],also_available_on:['annas']}]}})}});
 await server.call('search',{query:'Persuasion'});
 expect(server.refRows.get('alternative')).toMatchObject({ref:'alternative',source:'libgen',title:'Persuasion'});
 expect([...server.refRows.keys()]).toEqual(['primary','alternative']);
 server.client.callTool=async()=>({structuredContent:{ref:'alternative',source:'libgen',title:'Persuasion',identifiers:{md5:'a'.repeat(32)},evidence:{}}});
 await server.call('get_book',{ref:'alternative'});
 expect(server.trace.at(-1)).toMatchObject({observedSelection:true,selected:{source:'libgen',ref:'alternative'}});
});

test('same-task search provenance includes alternative refs and refuses a forged source',async()=>{
 const {mkdtemp,writeFile,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');const path=await import('node:path');const dir=await mkdtemp(path.join(tmpdir(),'merged-oracle-'));
 try{
  const file=path.join(dir,'book.epub');await writeFile(file,'Synthetic fixture 301\n');
  const selected={ref:'alternative',title:'Persuasion',format:'epub',source:'libgen',id:'301'};
  const search=call('search',{query:'Persuasion',sources:['libgen','annas']},{rows:[{...selected,ref:'recommended',source:'annas',other_copies:[{ref:'alternative',source:'libgen'}]}]});
  const download={...call('download',{ref:'alternative'},{path:file,source:'libgen'}),selected,observedSelection:true};
  const run=trace=>scoreTask({task:tasks.find(t=>t.id==='T1'),arm:arms.find(a=>a.id==='B'),trace,downloads:dir});
  expect((await run([search,download])).success).toBe(true);
  expect((await run([search,{...download,selected:{...selected,source:'zlibrary'}}])).success).toBe(false);
 }finally{await rm(dir,{recursive:true,force:true});}
});
