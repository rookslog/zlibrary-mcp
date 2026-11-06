import { expect, test } from '@jest/globals';
import { mergeRows } from '../../dist/lib/merge.js';
const md5='a'.repeat(32);
const copy=(source,metadata)=>({source,id:source,ref:`r-${source}`,rank:1,md5,filterResult:{status:'pass',checks:{}},metadata:{title:'Solaris',author:'Lem',language:'French',format:'pdf',publisher:'Press',...metadata}});
const merge=(field,a,b)=>{const input=[copy('libgen',{[field]:a}),copy('zlibrary',{[field]:b})],before=JSON.stringify(input);const [row]=mergeRows(input,{}, {routeOrder:['libgen','zlibrary']});expect(JSON.stringify(input)).toBe(before);return row;};
test.each([
 ['title','SOLARIS.','solaris'],['author','STANISŁAW LEM.','stanisław lem'],['language','French','french'],['format','PDF','pdf'],['publisher','Gallimard.','gallimard'],
])('F4 normalized %s equality suppresses false disagreements while displays stay raw',(field,a,b)=>{
 const row=merge(field,a,b);expect(row.differs).toBeUndefined();expect(row[field]).toBe(a);
});
test('F4 language aliases use the same comparison key as the local filter',()=>{
 for(const [a,b] of [['fr','French'],['fre','French'],['mo','Romanian']]){const row=merge('language',a,b);expect(row.differs).toBeUndefined();expect(row.language).toBe(a);}
});
test('F4 Norwegian row-code precision survives alias normalization',()=>{
 for(const [a,b] of [['nb','nob'],['nn','nno']])expect(merge('language',a,b).differs).toBeUndefined();expect(merge('language','nb','nn').differs).toEqual(['language']);
});
test.each([['title','Été','Ete'],['author','José','Jose'],['publisher','Université','Universite']])('F4 accents remain real %s disagreements',(field,a,b)=>{
 const row=merge(field,a,b);expect(row.differs).toEqual([field]);expect(row[field]).toBe(a);
});
test('F4 blank language strings compare without calling a nonempty language-key precondition',()=>{
 const row=merge('language','  ','\t');expect(row.differs).toBeUndefined();expect(row.language).toBe('  ');
});
