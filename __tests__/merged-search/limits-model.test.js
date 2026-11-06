import { expect, test } from '@jest/globals';
import { projectSearchLimits, RecentSearches } from '../../dist/lib/search-limits.js';
import { planSearch } from '../../dist/lib/search-assembly.js';
const sources=()=>['libgen','zlibrary','annas','held'].map(name=>({name,capabilities:{configured:true,search_available:true,download_available:true,operations:['search'],limit:{kind:'none'}},defaultSearch:{on:true}}));
const settings={waitMs:12000};
test('F45 local model derives the same implicit intent as search and reports local filters without network capabilities',()=>{
 const v=sources();v[1].capabilities={...v[1].capabilities,search_available:false,off_reason:'credentials_missing'};v[1].defaultSearch={on:false,reason:'credentials_missing'};v[2].defaultSearch={on:false,reason:'route_off'};
 const result=projectSearchLimits(v,settings);expect(result.search.default_sources).toEqual(planSearch({query:'x'},v,settings).intent);expect(result.search.default_sources).toEqual(['libgen','held']);expect(result.search).toEqual({default_sources:['libgen','held'],wait_ms:12000,ordering:'fit-v1'});expect(result.sources.zlibrary.default_search).toBe(false);expect(result.sources.annas.off_reason).toBe('route_off');expect(result.sources.libgen.filters.exact).toBe('local');expect(result.sources.held.recent).toEqual({searches:0,late:0,failed:0});
});
test('operator list keeps unavailable intent and projects configured route/format order without live activation',()=>{
 const v=sources();v[1].capabilities.search_available=false;const configured={waitMs:9000,operatorSources:['zlibrary','libgen'],formatPreference:['epub','pdf']};const result=projectSearchLimits(v,configured);expect(result.search).toEqual({default_sources:['libgen','zlibrary'],route_order:['zlibrary','libgen'],format_preference:['epub','pdf'],wait_ms:9000,ordering:'fit-v1'});expect(result.sources.zlibrary.default_search).toBe(true);expect(result.sources.held.default_search).toBe(false);expect(Object.isFrozen(result.sources.libgen.filters)).toBe(true);
});
test('A2 breaker health and independent admission backoff are both visible with the longest local retry interval',()=>{
 const result=projectSearchLimits(sources(),settings,{zlibrary:{breaker:'closed',admission:'backoff',admissionRetryAfterS:17},libgen:{breaker:'open',breakerRetryAfterS:9,admission:'backoff',admissionRetryAfterS:3},annas:{breaker:'half_open',admission:'open'}});expect(result.sources.zlibrary).toMatchObject({breaker:'closed',admission:'backoff',retry_after_s:17});expect(result.sources.libgen).toMatchObject({breaker:'open',admission:'backoff',retry_after_s:9});expect(result.sources.annas).toMatchObject({breaker:'half_open',admission:'open'});expect(result.sources.annas).not.toHaveProperty('retry_after_s');
});
test('recent counts the last 20 dispatched source jobs per source and excludes admission refusals',()=>{
 const ring=new RecentSearches(['libgen','zlibrary']);for(let i=0;i<30;i++){ring.record('libgen',{state:i<10?'failed':'rows'});ring.record('libgen',{state:'not_asked',reason:'queue_wait'});}ring.record('libgen',{state:'late'});ring.record('zlibrary',{state:'failed'});expect(ring.snapshot()).toEqual({libgen:{searches:20,late:1,failed:0},zlibrary:{searches:1,late:0,failed:1}});expect(new RecentSearches(['libgen']).snapshot().libgen.searches).toBe(0);
});
test('limits snapshots remain frozen when later results, settings or owner snapshots change',()=>{
 const v=sources(),s={...settings,formatPreference:['pdf']},state={zlibrary:{breaker:'closed',admission:'open'}},ring=new RecentSearches(v.map(v=>v.name));ring.record('libgen',{state:'late'});const result=projectSearchLimits(v,s,state,ring.snapshot());ring.record('libgen',{state:'failed'});s.formatPreference[0]='epub';state.zlibrary.admission='backoff';v[0].capabilities.limit.kind='changed';expect(result.search.format_preference).toEqual(['pdf']);expect(result.sources.libgen.recent).toEqual({searches:1,late:1,failed:0});expect(result.sources.zlibrary.admission).toBe('open');expect(result.sources.libgen.limit.kind).toBe('none');
});
test('malformed local health and recent inputs are refused rather than reported as healthy',()=>{
 for(const state of [{held:{breaker:'maybe'}},{held:{admission:'backoff'}},{held:{breaker:'open',breakerRetryAfterS:-1}}])expect(()=>projectSearchLimits(sources(),settings,state)).toThrow();expect(()=>projectSearchLimits(sources(),settings,{}, {held:{searches:21,late:0,failed:0}})).toThrow();const ring=new RecentSearches(['libgen']);expect(()=>ring.record('held',{state:'late'})).toThrow();expect(()=>ring.record('libgen',{state:'invented'})).toThrow();
});
