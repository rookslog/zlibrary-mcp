import { afterEach, expect, jest, test } from '@jest/globals';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createServer } from '../dist/index.js';
import { AcquisitionTools } from '../dist/lib/acquisition-tools.js';
const opened=[];
afterEach(async()=>{for(const item of opened.splice(0))await item.close();});
async function fixture(invoke,env={}){
 const server=createServer();server.tools=new AcquisitionTools(invoke,env);
 const client=new Client({name:'filters-fixture',version:'1'});const [a,b]=InMemoryTransport.createLinkedPair();await Promise.all([client.connect(a),server.connect(b)]);opened.push(client,server);
 return {server,client,call:async args=>{const reply=await client.callTool({name:'search',arguments:args});expect(JSON.parse(reply.content[0].text)).toEqual(reply.structuredContent);const result=reply.structuredContent;
  for(const line of result.error?.attempts ?? Object.values(result.sources))if(line.filtering){const f=line.filtering;expect(f.examined).toBe(f.matched+f.rejected+f.unknown);expect(f.unknown_metadata).toBe('excluded');expect(f).not.toHaveProperty('native_only');}return result;}};
}
const row=(title,extension,extra={})=>({title,extension,md5:title.padEnd(32,'0'),...extra});
const zenv={ZLIBRARY_EMAIL:'fixture@example.invalid',ZLIBRARY_PASSWORD:'fixture-only'};
const zrow=(title,extra={})=>({id:title,hash:'fixture',title,source:'zlibrary',...extra});

test('formats check the whole window before limit; local pages retain counts and issue no requests',async()=>{
 const invoke=jest.fn(async()=>({books:[row('EPUB','epub'),row('Missing'),row('Dot PDF','.PDF'),row('Late PDF','pdf')]}));const {call}=await fixture(invoke);const first=await call({query:'book',sources:['libgen'],formats:[' PDF '],limit:1});expect(first.rows.map(r=>r.title)).toEqual(['Dot PDF']);expect(first.sources.libgen.filtering).toEqual({applied:['formats'],examined:4,matched:2,rejected:1,unknown:1,unknown_metadata:'excluded',scope:'first_upstream_page'});expect((await call({cursor:first.next_cursor})).rows.map(r=>r.title)).toEqual(['Late PDF']);expect(invoke).toHaveBeenCalledTimes(1);expect((await call({query:'book',sources:['libgen'],formats:['mobi']})).rows).toEqual([]);
});
test.each(['libgen','annas'])('%s checks language membership and readable inclusive years',async source=>{
 const {call}=await fixture(async()=>({books:[row('French dated PDF','pdf',{language:'English;French',year:'2014 Juin'}),row('Wrong language and missing year','pdf',{language:'German'}),row('Double year','pdf',{language:'French',year:'1988;2003'}),row('Range','pdf',{language:'French',year:'1974 Enero―1974 Marzo'}),row('Zero','pdf',{language:'French',year:0}),row('Short','pdf',{language:'French',year:101}),row('Above','pdf',{language:'French',year:2015})]}));const reply=await call({query:'book',sources:[source],languages:['fr'],year_from:2014,year_to:2014,formats:['.PDF']});expect(reply.rows.map(r=>[r.title,r.year])).toEqual([['French dated PDF',2014]]);expect(reply.sources[source].filtering).toMatchObject({applied:['year_from','year_to','languages','formats'],examined:7,matched:1,rejected:2,unknown:4});
});
test('literal unknown language remains visible in its source filtering receipt',async()=>{
 const {call}=await fixture(async()=>({books:[row('Literal','pdf',{language:'français'}),row('Alias mismatch','pdf',{language:'French'}),row('Unknown','pdf')]}));const reply=await call({query:'book',sources:['libgen'],languages:[' français ']});expect(reply.rows.map(r=>r.title)).toEqual(['Literal']);expect(reply.sources.libgen.filtering).toMatchObject({unrecognized:{languages:[' français ']},examined:3,matched:1,rejected:1,unknown:1});
});
test('native filters are normalized while local checks override provider claims without backfill',async()=>{
 const calls=[];const {call}=await fixture(async(_op,args)=>{calls.push(args);return {books:[zrow('French',{year:2000,language:'French',extension:'epub'}),zrow('Wrong',{year:2000,language:'English',extension:'epub'}),zrow('Missing',{language:'French',extension:'epub'})]};},zenv);const reply=await call({query:'book',sources:['zlibrary'],languages:['fr','FRA','french'],formats:['.EPUB'],year_from:2000,limit:3});expect(calls[0]).toMatchObject({languages:['french'],extensions:['epub'],count:50,from_year:2000});expect(reply.rows.map(r=>r.title)).toEqual(['French']);expect(reply.sources.zlibrary.filtering).toMatchObject({native_sent:{year_from:2000,languages:['french'],formats:['epub']},examined:3,matched:1,rejected:1,unknown:1});expect(reply.next_cursor).toBeUndefined();expect(calls).toHaveLength(1);
});
test('multi-source empty or short windows never borrow an upstream continuation',async()=>{
 const calls=[];const {call}=await fixture(async(op,args)=>{calls.push(op);return {books:args.source==='libgen'?[row('Winner','pdf')]:Array.from({length:50},(_,i)=>zrow(`Wrong${i}`,{extension:'epub'}))};},zenv);const reply=await call({query:'book',sources:['zlibrary','libgen'],formats:['pdf'],limit:2});expect(reply.rows.map(r=>r.title)).toEqual(['Winner']);expect(reply.sources.zlibrary).toMatchObject({state:'empty',rows:0,more:true});expect(reply.next_cursor).toBeUndefined();expect(calls).toHaveLength(2);
});
test('all empty multi-source windows are complete emptiness and stop without refill',async()=>{
 const calls=[];const {call}=await fixture(async(op,args)=>{calls.push([op,args.page]);return {books:args.source?[]:Array.from({length:50},(_,i)=>zrow(`Wrong${i}`,{extension:'epub'}))};},zenv);const reply=await call({query:'book',sources:['zlibrary','libgen','annas'],formats:['pdf']});expect(reply.rows).toEqual([]);expect(reply.all_answered).toBe(true);expect(reply.next_cursor).toBeUndefined();expect(calls).toHaveLength(3);
});
test('all accepted local slices retain their selections and exclude standalone failures',async()=>{
 const {call,client}=await fixture(async(_op,args)=>({books:args.source==='libgen'?[row('Rejected','epub'),row('One','pdf'),row('Two','pdf')]:[zrow('Mismatch',{extension:'epub'})]}),zenv);const first=await call({query:'book',sources:['zlibrary','libgen'],formats:['pdf'],limit:1});const next=await call({cursor:first.next_cursor});expect(first.rows[0].title).toBe('One');expect(next.rows.map(r=>r.title)).toEqual(['Two']);expect(next.next_cursor).toBeUndefined();expect(next.sources).toEqual(first.sources);const detail=(await client.callTool({name:'get_book',arguments:{ref:next.rows[0].ref}})).structuredContent;expect(detail.title).toBe('Two');expect(detail.source).toBe('libgen');
});
test('exact asks every selected source and reports local evidence plus native transmission',async()=>{
 const calls=[];const {call}=await fixture(async(op,args)=>{calls.push([op,args]);return {books:op==='search'?[zrow('Book: French PDF',{language:'French',extension:'pdf'})]:[row('Book: French PDF','pdf',{language:'French'})]};},zenv);const reply=await call({query:'book',sources:['libgen','zlibrary'],exact:true,languages:['fr'],formats:['pdf']});expect(calls).toHaveLength(2);expect(reply.rows).toHaveLength(2);expect(reply.sources.libgen.filtering.applied).toEqual(['languages','formats','exact']);expect(reply.sources.zlibrary.filtering).toMatchObject({applied:['languages','formats','exact'],native_sent:{exact:true,languages:['french'],formats:['pdf']}});expect(reply.sources.zlibrary.filtering).not.toHaveProperty('native_only');
});
test('exact-only checks title phrases locally and counts missing metadata as unknown',async()=>{
 const {call}=await fixture(async()=>({books:[zrow('Book'),zrow('Other'),{id:'missing',hash:'fixture'}]}),zenv);const reply=await call({query:'book',sources:['zlibrary'],exact:true});expect(reply.rows.map(r=>r.title)).toEqual(['Book']);expect(reply.sources.zlibrary.filtering).toMatchObject({native_sent:{exact:true},applied:['exact'],examined:3,matched:1,rejected:1,unknown:1});
});
test('empty plus missing credentials is partial failure, not proof of no matches',async()=>{
 const invoke=jest.fn(async()=>({books:[]}));const {call}=await fixture(invoke);const reply=await call({query:'book',sources:['libgen','zlibrary'],exact:true});expect(reply.error.code).toBe('partial_failure');expect(reply.error.attempts.map(a=>[a.source,a.state])).toEqual([['libgen','empty'],['zlibrary','not_asked'],['annas','not_asked']]);expect(reply.error.attempts[1].reason).toBe('credentials_missing');expect(invoke).toHaveBeenCalledTimes(1);
});
test('all empty is success; timeout plus empty remains partial failure with per-source evidence',async()=>{
 const {call}=await fixture(async()=>({books:[]}),zenv);const empty=await call({query:'book',sources:['libgen','zlibrary'],exact:true});expect(empty.rows).toEqual([]);expect(empty.all_answered).toBe(true);const failed=await fixture(async(_op,args)=>{if(args.source==='libgen')throw {context:{reason:'timeout'}};return {books:[]};},zenv);const reply=await failed.call({query:'book',sources:['libgen','zlibrary'],formats:['pdf']});expect(reply.error.code).toBe('partial_failure');expect(reply.error.attempts[0]).toMatchObject({state:'failed',reason:'timeout'});expect(reply.error.attempts[0].filtering).toBeUndefined();expect(reply.error.attempts[1].filtering.examined).toBe(0);
});
test('false exact and empty or blank lists add no filtering contract',async()=>{
 const {call}=await fixture(async()=>({books:[row('Unfiltered')]}));const reply=await call({query:'book',sources:['libgen'],exact:false,languages:[' ',''],formats:[]});expect(reply.rows).toHaveLength(1);expect(reply.sources.libgen.filtering).toBeUndefined();
});
test('local limits reports local exact for all sources without a bridge call',async()=>{
 const invoke=jest.fn();const {client}=await fixture(invoke);const limits=(await client.callTool({name:'get_limits',arguments:{}})).structuredContent;for(const source of ['libgen','annas','zlibrary'])expect(limits.sources[source].filters).toEqual({year_from:'local',year_to:'local',languages:'local',formats:'local',exact:'local'});expect(invoke).not.toHaveBeenCalled();
});
test('format grammar strips exactly one leading dot on request and catalogue row',async()=>{
 const {call}=await fixture(async()=>({books:[row('Two dots','..PDF'),row('One dot','.pdf'),row('No dot','pdf')]}));const reply=await call({query:'book',sources:['libgen'],formats:['..PDF']});expect(reply.rows.map(r=>r.title)).toEqual(['Two dots']);expect(reply.sources.libgen.filtering).toMatchObject({examined:3,matched:1,rejected:2,unknown:0});
});
test.each(['zlibrary','libgen'])('unfiltered %s windows retain non-year displays and local onward slices',async source=>{
 const {call}=await fixture(async()=>({books:[{id:'old',title:'Old',year:850},{id:'zero',title:'Zero',year:0},{id:'tail',title:'Tail',year:2001}]}),zenv);const reply=await call({query:'book',sources:[source],limit:2});expect(reply.rows.map(r=>[r.title,r.year])).toEqual([['Old',850],['Zero',0]]);expect(reply.sources[source].rows).toBe(3);expect(reply.ordering).toBe('fit-v1');expect((await call({cursor:reply.next_cursor})).rows[0].title).toBe('Tail');
});
test.each([850,101,0,-5,2.5,'1e3'])('non-year paths retain numeric public year %s',async year=>{
 const {call,client}=await fixture(async op=>op==='get_download_history'?[zrow('Book',{year})]:{books:[row('Book',undefined,{year,language:'French'})]},zenv);const search=await call({query:'book',sources:['libgen'],languages:['fr']});expect(search.rows[0].year).toBe(Number(year));expect((await client.callTool({name:'get_book',arguments:{ref:search.rows[0].ref}})).structuredContent.year).toBe(Number(year));expect((await client.callTool({name:'get_history',arguments:{limit:2}})).structuredContent.rows[0].year).toBe(Number(year));
});
test('history oversupply retains its upstream onward page',async()=>{
 const {client}=await fixture(async()=>[zrow('One'),zrow('Two'),zrow('Extra')],zenv);const reply=(await client.callTool({name:'get_history',arguments:{limit:2}})).structuredContent;expect(reply.rows).toHaveLength(2);expect(reply.next_cursor).toEqual(expect.any(String));
});
test('one-source refill rechecks mixed and empty full windows once per position',async()=>{
 const calls=[];const {call}=await fixture(async(_op,args)=>{calls.push(args.page);return {books:Array.from({length:50},(_,i)=>zrow(`p${args.page}-${i}`,{language:args.page===1||args.page===2&&i===0?'French':i===1?undefined:'English'}))};},zenv);const first=await call({query:'book',sources:['zlibrary'],languages:['fr'],limit:50});const mixed=await call({cursor:first.next_cursor});expect(mixed.rows).toHaveLength(1);expect(mixed.sources.zlibrary.filtering).toMatchObject({examined:50,matched:1,rejected:48,unknown:1});const empty=await call({cursor:mixed.next_cursor});expect(empty.rows).toEqual([]);expect(empty.sources.zlibrary.filtering).toMatchObject({examined:50,matched:0,rejected:49,unknown:1});expect(empty.next_cursor).toBeDefined();expect(await call({cursor:mixed.next_cursor})).toEqual(empty);expect(calls).toEqual([1,2,3]);
});
test('filtered oversupply retains a local onward slice and honest source counts',async()=>{
 const {call}=await fixture(async()=>({books:[zrow('One',{language:'French'}),zrow('Two',{language:'French'}),zrow('Three',{language:'French'})]}),zenv);const reply=await call({query:'book',sources:['zlibrary'],languages:['fr'],limit:2});expect(reply.rows).toHaveLength(2);expect(reply.sources.zlibrary.filtering.matched).toBe(3);expect(reply.next_cursor).toBeDefined();
});
test('format fallback and bounded accepted-record coverage remain visible',async()=>{
 const {call}=await fixture(async()=>({books:[row('Format fallback',undefined,{format:'PDF',language:'French'})]}));const reply=await call({query:'book',sources:['libgen'],formats:['pdf'],languages:[' FrEnCh ']});expect(reply.rows.map(r=>r.title)).toEqual(['Format fallback']);expect(reply.sources.libgen.filtering.matched).toBe(1);expect(reply.note).toMatch(/^1 file rows shown from 1 accepted records/);expect(reply.note).toMatch(/bounded source windows/);const empty=await call({query:'book',sources:['libgen'],formats:['epub']});expect(empty.rows).toEqual([]);expect(empty.note).toMatch(/^0 file rows shown from 0 accepted records/);
});
test('dot-only formats add no constraint or native extension, including beside a real format',async()=>{
 const sent=[];const {call}=await fixture(async(_op,args)=>{sent.push(args.extensions);return {books:[zrow('PDF',{extension:'pdf'}),zrow('EPUB',{extension:'epub'})]};},zenv);const all=await call({query:'book',sources:['zlibrary'],formats:['.',' . ']});expect(all.rows).toHaveLength(2);expect(all.sources.zlibrary.filtering).toBeUndefined();expect(sent[0]).toEqual([]);const pdf=await call({query:'book',sources:['zlibrary'],formats:['.','.pdf']});expect(pdf.rows.map(r=>r.title)).toEqual(['PDF']);expect(sent[1]).toEqual(['pdf']);
});
test.each([['afrikaans','af'],['swahili','sw']])('Anna %s request admits extracted %s and counts mismatches',async(language,code)=>{
 const {call}=await fixture(async()=>({books:[row('Match','pdf',{language:code}),row('Wrong','pdf',{language:'en'})]}));const reply=await call({query:'book',sources:['annas'],languages:[language]});expect(reply.rows.map(r=>r.title)).toEqual(['Match']);expect(reply.sources.annas.filtering).toMatchObject({examined:2,matched:1,rejected:1,unknown:0});
});
test.each([['Central khmer','central_khmer'],['Luba katanga','luba-katanga']])('spaced language %s transmits native %s without literal-warning evidence',async(spaced,native)=>{
 const calls=[];const {call}=await fixture(async(_op,args)=>{calls.push(args.languages);return {books:[zrow('Match',{language:spaced})]};},zenv);const a=await call({query:'book',sources:['zlibrary'],languages:[spaced]});const b=await call({query:'book',sources:['zlibrary'],languages:[native]});expect(a.rows.map(r=>r.title)).toEqual(['Match']);expect(b.rows.map(r=>r.title)).toEqual(['Match']);expect(calls).toEqual([[native],[native]]);expect(a.sources.zlibrary.filtering.unrecognized).toBeUndefined();
});
