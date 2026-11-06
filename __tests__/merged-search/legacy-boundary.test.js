import { afterEach, expect, test } from '@jest/globals';
import { createServer, TOOL_DEFINITIONS } from '../../dist/index.js';
import { AcquisitionTools } from '../../dist/lib/acquisition-tools.js';
const original={...process.env};
afterEach(()=>{process.env={...original};});
test('C switches public search and local limits together without changing the five-tool surface',async()=>{
 delete process.env.ZLIBRARY_EMAIL;delete process.env.ZLIBRARY_PASSWORD;delete process.env.BOOK_SOURCE_DEFAULT_ORDER;delete process.env.BOOK_SOURCE_SEARCH_WAIT_MS;delete process.env.BOOK_FORMAT_PREFERENCE;process.env.ZLIBRARY_OUTPUT_ROOT=process.env.TMPDIR;
 const server=createServer();await server.close();const calls=[];const tools=new AcquisitionTools(async(op,args)=>{calls.push([op,args]);return {books:[{id:'synthetic',title:'Legacy',author:'Synthetic',extension:'pdf'}]};},{});
 expect(TOOL_DEFINITIONS.map(t=>t.name)).toEqual(['search','get_book','download','get_limits','get_history']);const limits=await tools.call('get_limits');expect(calls).toHaveLength(0);expect(limits.search.default_sources).toEqual(['libgen']);const reply=await tools.call('search',{query:'Legacy',exact:true});expect(reply.rows[0].match).toBe('exact_title');expect(reply.sources.libgen.state).toBe('rows');expect(reply.all_answered).toBe(true);expect(reply).not.toHaveProperty('routing');expect(limits.sources.libgen.filters.exact).toBe('local');expect(calls).toHaveLength(1);
});
test('C validates search settings at startup before any bridge call',()=>{
 process.env.ZLIBRARY_OUTPUT_ROOT=process.env.TMPDIR;process.env.BOOK_SOURCE_SEARCH_WAIT_MS='bad';expect(()=>createServer()).toThrow(/BOOK_SOURCE_SEARCH_WAIT_MS/);
});
