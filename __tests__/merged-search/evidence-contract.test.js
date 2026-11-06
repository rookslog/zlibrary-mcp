import { expect, test } from '@jest/globals';
import { mergeRows } from '../../dist/lib/merge.js';
import { checkMergedRowFilters } from '../../dist/lib/row-filters.js';
const request={query:'Solaris',languages:['fr'],formats:['pdf']};
const copy=(id,metadata={},filters=request)=>{const values={title:'Solaris',author:'Lem',language:'French',format:'pdf',...metadata};return {id,source:id,ref:`r-${id}`,rank:1,md5:'a'.repeat(32),metadata:values,filterResult:checkMergedRowFilters({...values,extension:values.format},filters)};};
const invalid=[
 ['missing',v=>delete v.filterResult],
 ['legacy status only',v=>{delete v.filterResult;v.filterStatus='unknown';}],
 ['missing checks',v=>v.filterResult={status:'pass'}],
 ['unknown without evidence',v=>v.filterResult={status:'unknown',checks:{}}],
 ['contradictory status',v=>v.filterResult={status:'pass',checks:{languages:{status:'fail',reason:'outside'}}}],
 ['invalid status',v=>v.filterResult={status:'mystery',checks:{}}],
 ['array checks',v=>v.filterResult={status:'pass',checks:[]}],
 ['missing reason',v=>v.filterResult={status:'fail',checks:{languages:{status:'fail'}}}],
 ['unregistered predicate',v=>v.filterResult={status:'pass',checks:{other:{status:'pass',reason:'unknown'}}}],
];
test.each(invalid)('F5 refuses %s filter evidence before producing rows',(_name,damage)=>{const value=copy('bad');damage(value);expect(()=>mergeRows([value],request)).toThrow();});
test('F5 derives active filtering when the flag is omitted; rejected match and rank cannot recommend',()=>{
 const rejected=copy('rejected',{language:'English'}),passing={...copy('passing',{title:'On Solaris'}),rank:9},rival={...copy('rival',{author:'Other'}),rank:2,md5:'b'.repeat(32)};const rows=mergeRows([rejected,passing,rival],request);expect(rows.map(row=>row.ref)).toEqual([rival.ref,passing.ref]);expect(rows[1]).toMatchObject({language:'French',match:'all_terms',other_copies:[{ref:rejected.ref,source:rejected.source,filter:'fail',language:'English'}]});expect(mergeRows([rejected],request)).toEqual([]);
});
test('F5 derived activation names unknown fields instead of an empty legacy list',()=>{
 const passing=copy('passing'),unknown=copy('unknown',{language:undefined});const [row]=mergeRows([passing,unknown],request);expect(row.other_copies).toEqual([{ref:unknown.ref,source:unknown.source,filter:'unknown',unknown:['language']}]);
});
test.each([['filtered',false,request],['unfiltered',true,{}]])('F5 rejects %s evidence with contradictory filtersActive=%s',(_label,flag,filters)=>{
 expect(()=>mergeRows([copy('a',{},filters)],request,{filtersActive:flag})).toThrow();
});
test('F5 an empty window has no contradictory evidence and still supports an active request',()=>{expect(mergeRows([],request,{filtersActive:true})).toEqual([]);});
test.each([true,false])('F5 a matching explicit activation flag %s preserves derived copy labels',active=>{
 const filters=active?request:{},a=copy('a',{},filters),b=copy('b',{},filters);const [row]=mergeRows([a,b],request,{filtersActive:active,routeOrder:['a','b']});expect(row.ref).toBe(a.ref);expect(row.other_copies[0].filter).toBe(active?'pass':undefined);
});

test.each(['empty', 'different'])(
  'G4 rejects %s evidence beside copies checked against other keys in either order', kind => {
    const rejected = copy('rejected', { language: 'English' });
    const incomplete = copy('unchecked', {}, kind === 'empty' ? {} : { year_from: 1900 });
    for (const copies of [[incomplete, rejected], [rejected, incomplete]])
      expect(() => mergeRows(copies, request)).toThrow(TypeError);
  },
);
test('G4 equal predicate keys are valid regardless of insertion order', () => {
  const a = copy('a'), b = copy('b');
  b.filterResult.checks = Object.fromEntries(Object.entries(b.filterResult.checks).reverse());
  expect(mergeRows([a, b], request)).toHaveLength(1);
});
