import {test,expect} from '@jest/globals';
import {selectableRows} from './oracles.js';
import {EvaluationServer} from './server.js';
const primary={ref:'primary',source:'annas',title:'Primary edition',author:'A',format:'epub',id:'wrong-primary-id',year:2000,other_copies:[{ref:'alternative',source:'libgen'}],differs:['title','format']};
function broker(){const s=new EvaluationServer();Object.assign(s,{arm:{history:true,textCopy:true},trace:[],observed:new Map(),rows:new Map(),refRows:new Map(),tools:[{name:'search'},{name:'get_book'},{name:'download'}],client:{callTool:async()=>({structuredContent:{rows:[primary]}})}});return s;}
test('alternative selection authority does not invent unknown title format author year or identity',()=>{
 expect(selectableRows({rows:[primary]})[1]).toEqual({ref:'alternative',source:'libgen'});
 const own={ref:'alternative',source:'libgen',title:'Other edition',format:'pdf'};expect(selectableRows({rows:[{...primary,other_copies:[own]}]})[1]).toEqual(own);
});
test('matching successful detail refreshes copy-specific selection evidence for the next call',async()=>{
 const s=broker();await s.call('search',{query:'edition'});s.client.callTool=async()=>({structuredContent:{ref:'alternative',source:'libgen',title:'Other edition',format:'pdf',identifiers:{id:'301'},evidence:{}}});await s.call('get_book',{ref:'alternative'});
 expect(s.refRows.get('alternative')).toMatchObject({ref:'alternative',source:'libgen',title:'Other edition',format:'pdf',identifiers:{id:'301'}});expect(s.trace.at(-1).selected.title).toBe('Other edition');s.client.callTool=async()=>({structuredContent:{path:'/fixture/book.pdf'}});await s.call('download',{ref:'alternative'});expect(s.trace.at(-1)).toMatchObject({observedSelection:true,fixtureId:'301',selected:{title:'Other edition',format:'pdf',source:'libgen'}});
});
test.each(['wrong_ref','wrong_source','error'])('%s detail cannot replace previously observed copy evidence',async kind=>{
 const s=broker();await s.call('search',{query:'edition'});const prior=structuredClone(s.refRows.get('alternative'));s.client.callTool=async()=>({...(kind==='error'?{isError:true}:{}),structuredContent:{ref:kind==='wrong_ref'?'unobserved':'alternative',source:kind==='wrong_source'?'zlibrary':'libgen',title:'Poison'}});await s.call('get_book',{ref:'alternative'});expect(s.refRows.get('alternative')).toEqual(prior);expect(s.refRows.has('unobserved')).toBe(false);
});

test('successful matching detail exposes actual related refs without inheriting selected metadata',async()=>{
 const s=broker();await s.call('search',{query:'edition'});s.client.callTool=async()=>({structuredContent:{ref:'alternative',source:'libgen',title:'Own',other_copies:[{ref:'related',source:'annas',title:'Related own',format:'pdf'}]}});await s.call('get_book',{ref:'alternative'});expect(s.refRows.get('related')).toEqual({ref:'related',source:'annas',title:'Related own',format:'pdf'});
});
test.each([true,false])('copy inspection is allowed for acquisition and judges own title/format: correct=%s',async correct=>{
 const {mkdtemp,mkdir,writeFile,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');const path=await import('node:path');const {scoreTask}=await import('./oracles.js');const {tasks,arms}=await import('./tasks.js');const dir=await mkdtemp(path.join(tmpdir(),'copy-own-score-'));
 try{const file=path.join(dir,'book.epub');await writeFile(file,'Synthetic fixture 301\n');const s=broker();s.client.callTool=async()=>({structuredContent:{rows:[{...primary,title:'Persuasion',format:'epub',id:'301'}]}});await s.call('search',{query:'Persuasion',sources:['annas','libgen']});s.client.callTool=async()=>({structuredContent:{ref:'alternative',source:'libgen',title:correct?'Persuasion':'Wrong edition',format:correct?'epub':'pdf',identifiers:{id:'301'}}});await s.call('get_book',{ref:'alternative'});s.client.callTool=async()=>({structuredContent:{path:file,source:'libgen'}});await s.call('download',{ref:'alternative'});const result=await scoreTask({task:tasks.find(t=>t.id==='T1'),arm:arms.find(a=>a.id==='B'),trace:s.trace,downloads:dir});expect(result.success).toBe(correct);}finally{await rm(dir,{recursive:true,force:true});}
});
test('observed own detail facts can establish both editions after one grouped search row',async()=>{
 const {scoreTask}=await import('./oracles.js');const {tasks,arms}=await import('./tasks.js');const s=broker();s.client.callTool=async()=>({structuredContent:{rows:[{ref:'primary',source:'annas',title:'Letters on Ethics: To Lucilius',year:2015,other_copies:[{ref:'alternative',source:'libgen'}]}]}});await s.call('search',{query:'Seneca'});
 for(const [ref,source,title,year] of [['primary','annas','Letters on Ethics: To Lucilius',2015],['alternative','libgen','Letters from a Stoic',1969]]){s.client.callTool=async()=>({structuredContent:{ref,source,title,year}});await s.call('get_book',{ref});}
 const result=await scoreTask({task:tasks.find(t=>t.id==='T2'),arm:arms.find(a=>a.id==='B'),trace:s.trace,final:JSON.stringify({translations:[{translator:'Margaret Graver and A. A. Long',year:2015},{translator:'Robin Campbell',year:1969}]})});expect(result.success).toBe(true);
});
test('frozen search replay and sibling projections cannot overwrite fresh own detail',async()=>{
 const s=broker();await s.call('search',{query:'edition'});s.client.callTool=async()=>({structuredContent:{ref:'alternative',source:'libgen',title:'Fresh own',format:'pdf',translator:'Own translator'}});await s.call('get_book',{ref:'alternative'});
 s.client.callTool=async()=>({structuredContent:{ref:'primary',source:'annas',title:'Primary edition',other_copies:[{ref:'alternative',source:'libgen',title:'Old held',format:'epub'}]}});await s.call('get_book',{ref:'primary'});expect(s.refRows.get('alternative')).toMatchObject({title:'Fresh own',format:'pdf',translator:'Own translator'});
 s.client.callTool=async()=>({structuredContent:{rows:[primary]}});await s.call('search',{cursor:'observed'});expect(s.refRows.get('alternative')).toMatchObject({title:'Fresh own',format:'pdf',translator:'Own translator'});
});
