import { expect, test } from '@jest/globals';
import { planSearch, prepareSearch, renderSearchPage } from '../../dist/lib/search-assembly.js';
import { ToolFailure } from '../../dist/lib/tool-errors.js';
import { assertAccounting } from './model-assertions.js';
import { fanOut } from '../../dist/lib/fan-out.js';
import { SourceRegistry } from '../../dist/lib/source-registry.js';
import { windowFixture } from './window-fixture.js';
const names = ['libgen','zlibrary','annas'];
const views = () => names.map(name => ({ name, capabilities: { configured: true, search_available: true, download_available: true }, defaultSearch: { on: true } }));
const copy = (source, id = source, metadata = {}, md5) => ({ source, id, ref: `r-${id}`, md5, metadata: { title: 'Solaris', author: 'Lem', language: 'French', format: 'pdf', ...metadata } });
const answer = (source, copies = [copy(source)]) => ({ source, state: 'answered', ms: 10, copies });
const failure = source => ({ source, state: 'failed', ms: 2, failure: new ToolFailure('provider_error', 'Search failed.', { retry: 'safe', detail: { reason: 'http_error' } }) });
const build = (req = {}, results = names.map(n => answer(n)), sourceViews = views(), settings = { waitMs: 12000 }) => {
  const plan = planSearch({ query: 'Solaris', ...req }, sourceViews, settings);
  const prepared = prepareSearch(plan, results); return { plan, prepared, reply: renderSearchPage(prepared) };
};

test('F01 F02 F03 F04 source-set replies use canonical order and stable row projections', () => {
  const results = names.map(n => answer(n));
  const expected = build({ sources: ['libgen','zlibrary'] }, results.slice(0,2)).reply;
  for (const sources of [['zlibrary','libgen'],['libgen','zlibrary']]) for (const data of [results.slice(0,2),results.slice(0,2).reverse()]) {
    const reply = build({ sources }, data).reply; expect(reply).toEqual(expected); assertAccounting(reply, names, ['libgen','zlibrary']);
  }
  const single = build({ sources: ['libgen'] }, [answer('libgen')]).reply;
  expect(single.sources.annas).toEqual({ state: 'not_asked', reason: 'not_requested' }); assertAccounting(single,names,['libgen']);
});
test('F05 F06 F07 unset defaults exclude install-disabled sources but explicit intent survives refusal', () => {
  const v = views(); v[1].capabilities.search_available = false; v[1].capabilities.off_reason = 'credentials_missing'; v[1].defaultSearch = { on: false, reason: 'credentials_missing' }; v[2].defaultSearch = { on: false, reason: 'route_off' };
  const plain = build({},[answer('libgen')],v).reply; expect(plain.all_answered).toBe(true); expect(plain.sources.zlibrary.reason).toBe('credentials_missing'); expect(plain.sources.annas.reason).toBe('route_off'); assertAccounting(plain,names,['libgen']);
  const named = build({ sources: ['zlibrary'] },[],v).reply; expect(named.error.code).toBe('credentials_missing'); expect(named.error.source).toBe('zlibrary'); assertAccounting(named,names,['zlibrary']);
});
test('F08 F09 operator intent is explicit, distinct, and independent from dynamic admission', () => {
  const { reply } = build({},[answer('libgen')],views(),{waitMs:12000,operatorSources:['libgen']});
  expect(reply.sources.annas.reason).toBe('not_in_default_order'); assertAccounting(reply,names,['libgen']);
  for (const operatorSources of [['libgen','libgen'],['unknown']]) expect(() => build({},[],views(),{waitMs:12000,operatorSources})).toThrow();
});
test('F10 F16 failed and refused sources retain typed causes and retry-after beside successful rows', () => {
  const data = [answer('libgen'), failure('zlibrary'), { source:'annas',state:'not_asked',reason:'breaker_open',retry_after_s:7 }];
  const { reply } = build({},data); expect(reply.sources.zlibrary).toEqual({state:'failed',code:'provider_error',reason:'http_error',ms:2}); expect(reply.sources.annas.retry_after_s).toBe(7); expect(reply.all_answered).toBe(false); assertAccounting(reply,names,names);
});
test.each([
  ['empty','late','partial_failure'],['failed','late','all_sources_failed'],['empty','empty',null],['not_asked','failed','all_sources_failed'],
])('F17 F18 A6 no-row truth table %s plus %s', (a,b,code) => {
  const state = (name,s) => s === 'empty' ? answer(name,[]) : s === 'failed' ? failure(name) : s === 'late' ? { source:name,state:s,ms:12000 } : { source:name,state:s,reason:'auth_backoff',retry_after_s:4 };
  const {reply} = build({sources:['libgen','zlibrary']},[state('libgen',a),state('zlibrary',b)]);
  expect(reply.error?.code ?? null).toBe(code); if(code) expect(reply.error.retry).toBe('safe'); assertAccounting(reply,names,['libgen','zlibrary']);
});
test('A6 single-source late keeps its own timeout and caller cancellation remains cancelled', () => {
  expect(build({sources:['libgen']},[{source:'libgen',state:'late',ms:12000}]).reply.error.code).toBe('provider_timeout');
  const plan=planSearch({query:'Solaris'},views(),{waitMs:12000}); expect(prepareSearch(plan,[],{cancelled:true}).error.code).toBe('cancelled');
});
test.each([{query:''},{query:'Solaris',limit:0},{query:'Solaris',sources:['held']},{query:'Solaris',sources:['libgen','libgen']},{query:'Solaris',year_from:2001,year_to:2000}])('F19 invalid model intent %p is refused before collection', request => {
  expect(() => planSearch(request,views(),{waitMs:12000})).toThrow();
});
test('M05 M15 filtering is per copy, counts before grouping, accepted evidence alone ranks, and OR disagreements remain', () => {
  const md5='a'.repeat(32); const data=[answer('libgen',[copy('libgen','passing',{title:'On Solaris'},md5)]),answer('zlibrary',[copy('zlibrary','rejected',{language:'English'},md5)]),answer('annas',[copy('annas','unknown',{language:undefined},md5)])];
  const {reply}=build({languages:['fr']},data); expect(reply.rows).toHaveLength(1); expect(reply.rows[0].match).toBe('all_terms'); expect(reply.rows[0].ref).toBe('r-passing'); expect(reply.sources.libgen.rows).toBe(1); expect(reply.sources.zlibrary.rows).toBe(0); expect(reply.sources.zlibrary.filtering.rejected).toBe(1); expect(reply.sources.annas.filtering.unknown).toBe(1);
  expect(reply.rows[0].other_copies).toEqual(expect.arrayContaining([{ref:'r-rejected',source:'zlibrary',filter:'fail',language:'English'},{ref:'r-unknown',source:'annas',filter:'unknown',unknown:['language']}])); assertAccounting(reply,names,names);
  const both=build({languages:['fr','en']},data).reply; expect(both.rows[0].differs).toContain('language'); expect(both.rows[0].other_copies.some(c=>c.filter==='pass')).toBe(true);
});
test('M11 note distinguishes page rows from accepted bounded records and names incomplete sources', () => {
  const {reply}=build({limit:1},[answer('libgen',[copy('libgen','a'),copy('libgen','b')]),answer('zlibrary',[copy('zlibrary','c')]),{source:'annas',state:'late',ms:12000}]);
  expect(reply.note).toMatch(/^1 file rows shown from 3 accepted records/); expect(reply.note).toContain('annas: late'); expect(reply.note).toMatch(/not everything\.$/); expect(reply.rows).toHaveLength(1);
});
test('F34 local exact applies across sources, records native exact evidence, and excludes unknown author', () => {
  const data=names.map(n=>answer(n,[copy(n,n,{title:'Solaris: A novel',author:'Stanisław Lem'})])); data[1].nativeSent={exact:true};
  const {reply}=build({query:'Solaris',author:'Lem',exact:true},data);
  expect(reply.rows).toHaveLength(3); expect(reply.sources.zlibrary.filtering.native_sent.exact).toBe(true); expect(reply.sources.libgen.filtering.applied).toContain('exact'); expect(reply.sources.zlibrary.filtering).not.toHaveProperty('native_only');
});
for(const mode of ['named','operator','default']) for(const reason of ['credentials_missing','route_off','breaker_open','queue_wait']) for(const status of ['rows','empty','no_answer']) {
  test(`M10 ${mode} intent with ${reason} and ${status}`,()=>{
    const v=views(); const install=['credentials_missing','route_off'].includes(reason); if(install){v[2].capabilities.search_available=false;v[2].capabilities.off_reason=reason;v[2].defaultSearch={on:false,reason};}
    const req=mode==='named'?{sources:names}:{};const settings={waitMs:12000,...(mode==='operator'?{operatorSources:names}:{})};
    const data=['libgen','zlibrary'].map(n=>status==='rows'?answer(n):status==='empty'?answer(n,[]):failure(n)); if(!install)data.push({source:'annas',state:'not_asked',reason,retry_after_s:1});
    const {reply}=build(req,data,v,settings); const wanted=mode==='default'&&install?['libgen','zlibrary']:names;
    const expected=status==='rows'?null:status==='empty'&&wanted.length===2?null:status==='empty'?'partial_failure':'all_sources_failed';
    expect(reply.error?.code??null).toBe(expected); if(!reply.error)expect(reply.all_answered).toBe(wanted.length===2); assertAccounting(reply,names,wanted);
  });
}
test('M09 semantic accounting rejects missing lines, malformed states, provenance and route-basis contradictions', () => {
  const base=build().reply; assertAccounting(base,names,names);
  for(const mutate of [r=>delete r.sources.annas,r=>r.sources.annas={state:'late'},r=>r.sources.libgen.rows=0,r=>r.all_answered=false,r=>r.sources.libgen={state:'failed',code:'provider_error',reason:'x',ms:1}]){const bad=structuredClone(base);mutate(bad);expect(()=>assertAccounting(bad,names,names)).toThrow();}
  const md5='a'.repeat(32);const grouped=structuredClone(build({},names.map(n=>answer(n,[copy(n,n,{},md5)]))).reply);delete grouped.rows[0].route_basis;expect(()=>assertAccounting(grouped,names,names)).toThrow();
});

test('A6 aggregate errors retain allowlisted diagnostics and shared configuration recovery', () => {
  const diagnostics={failure_kind:'transport',http_status:503,attempt:1,refresh_count:0,request_id:'12345678-1234-4234-8234-123456789abc'};
  const f=new ToolFailure('provider_error','Search failed.',{retry:'safe',detail:{reason:'http_error',diagnostics}});
  const r=build({sources:['libgen','zlibrary']},[{source:'libgen',state:'failed',ms:1,failure:f},answer('zlibrary',[])]).reply;
  expect(r.error.detail.diagnostics).toEqual(diagnostics);
  const bad=new ToolFailure('provider_error','Search failed.',{detail:{reason:'http_error',diagnostics:{...diagnostics,body:'private'}}});
  expect(build({sources:['libgen','zlibrary']},[{source:'libgen',state:'failed',ms:1,failure:bad},answer('zlibrary',[])]).reply.error.detail).not.toHaveProperty('diagnostics');
  const config=()=>new ToolFailure('invalid_argument','Source configuration failed.',{retry:'no',recovery:'Configure deadline.',detail:{reason:'configuration_error',request_issued:false}});
  expect(build({sources:['libgen','zlibrary']},['libgen','zlibrary'].map(source=>({source,state:'failed',ms:1,failure:config()}))).reply.error.recovery).toBe('Configure deadline.');
});
test('M09 malformed collected states, source identities and duplicate refs cannot become a reply', () => {
  const plan=planSearch({query:'Solaris',sources:['libgen']},views(),{waitMs:12000});
  for(const data of [[{source:'libgen',state:'unrecognized',ms:1}],[{source:'libgen',state:'late',ms:1,copies:[copy('libgen')]}],[{source:'libgen',state:'late'}],[answer('libgen',[copy('annas')])],[answer('libgen',[copy('libgen'),copy('libgen')])],[{source:'libgen',state:'not_asked',reason:''}]]) expect(()=>prepareSearch(plan,data)).toThrow();
});

test('N1 S4b adapter evidence survives the request guard and preserves all copy outcomes', async () => {
  const request={query:'Solaris',exact:true,languages:['fr'],formats:['pdf'],year_from:2000,year_to:2002};
  const md5='a'.repeat(32);
  const records=[
    {id:'pass',md5,metadata:{title:'Solaris',author:'Lem',language:'French',format:'pdf',year:2001}},
    {id:'fail',md5,metadata:{title:'Other',language:'English',format:'epub',year:1990}},
    {id:'unknown',md5,metadata:{}},
  ];
  const source={name:'libgen',capabilities:()=>views()[0].capabilities,defaultSearch:()=>({on:true}),md5:row=>row.md5,
    searchWindow:async actual=>windowFixture(records,{},actual)};
  const snapshot=await fanOut(new SourceRegistry([source]),request);
  expect(snapshot.sources.libgen).toMatchObject({state:'rows',rows:1});
  const kept=snapshot.windows.get('libgen').rows;
  expect(kept.map(r=>r.filterResult.status)).toEqual(['pass','fail','unknown']);
  for(const row of kept)expect(Object.keys(row.filterResult.checks).sort()).toEqual(['exact','formats','languages','year_from','year_to']);
  const plan=planSearch(request,[views()[0]],{waitMs:12000});
  const prepared=prepareSearch(plan,[{source:'libgen',state:'answered',ms:1,copies:kept.map(row=>({...row,source:'libgen',ref:`r-${row.id}`}))}]);
  expect(prepared.acceptedRecords).toBe(1);expect(prepared.copies).toHaveLength(3);
  const reply=renderSearchPage(prepared);expect(reply.rows).toHaveLength(1);expect(reply.rows[0].ref).toBe('r-pass');
  expect(reply.rows[0].other_copies.map(c=>[c.ref,c.filter])).toEqual([['r-fail','fail'],['r-unknown','unknown']]);
});

test('N1 note omits healthy unrequested sources and keeps intended refusals', () => {
  const single = build({ sources: ['libgen'] }, [answer('libgen')]).reply;
  expect(single.note).not.toContain('zlibrary:');
  expect(single.note).not.toContain('annas:');
  const partial = build({ sources: ['libgen', 'annas'] }, [answer('libgen'), { source: 'annas', state: 'not_asked', reason: 'breaker_open' }]).reply;
  expect(partial.note).toContain('annas: not_asked (breaker_open)');
  expect(partial.note).not.toContain('zlibrary:');
});
test.each(['default', 'named'])('N1 note still lists off sources under %s intent', mode => {
  const v = views();
  v[1].capabilities.search_available = false;
  v[1].capabilities.off_reason = 'credentials_missing';
  v[1].defaultSearch = { on: false, reason: 'credentials_missing' };
  v[2].defaultSearch = { on: false, reason: 'route_off' };
  const { reply } = build(mode === 'named' ? { sources: ['libgen'] } : {}, [answer('libgen')], v);
  expect(reply.note).toContain('zlibrary: not_asked');
  expect(reply.note).toContain('annas: not_asked');
});

test('H2 T8 note includes unavailable sources whose default-search switch is still on', () => {
  const v = views();
  v[1].capabilities.search_available = false;
  v[1].capabilities.off_reason = 'credentials_missing';
  expect(v[1].defaultSearch.on).toBe(true);
  const { reply } = build({}, [answer('libgen'), answer('annas')], v);
  expect(reply.all_answered).toBe(true);
  expect(reply.sources.zlibrary).toEqual({ state: 'not_asked', reason: 'credentials_missing' });
  expect(reply.note).toContain('zlibrary: not_asked (credentials_missing)');
});
