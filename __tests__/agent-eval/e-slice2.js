// Slice-2 scripted diagnostics: all replies below come from the actual MCP process.
import {writeFile, readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {EDiagnosticServer} from './e-server.js';
import {eTasks} from './tasks.js';
import {dataOf, rowsOf} from './oracles.js';
import {root} from './server.js';
export const slice2Ids = ['E5','E17','E18','E1','E2','E3','E7','E9','E10','E12','E14','E15'];
export const eCases = ['E5','E5-offer-zlibrary','E17-person','E17-person-minimal','E17-pacing','E17-pacing-offer','E17-runtime','E17-ceiling','E18','E18-direct','E18-malformed',
  'E1','E1-query','E1-direct','E2','E2-query','E2-direct','E3','E3-query','E3-direct','E7','E9','E10','E10-unknown','E10-canonical',
  'E12','E12-query','E12-direct','E12-fresh','E14','E14-image','E15','E15-range','E15-front','E15-whole','E15-noimage','E15-map','E4','E4-query','E4-second','E6','E6-query','E6-rows',
  'E8','E8-empty','E8-query','E8-complete','E8-rows','E8-blocked','E11','E11-filter','E11-first','E11-limit',
  'E13','E13-real-timeout','E16','E16-first','E16-limit'];
export async function exerciseSlice2(id,consume,options={}) {
  const [base,variant='default'] = id.split('-');
  const task = eTasks.find(t=>t.id === base);
  const s = await EDiagnosticServer.start({...task,variant});
  const context = {variant,caseId:id,executedWrong:!!options.wrongMove};
  const call = async(name,args={})=>dataOf(await s.call(name,args));
  const search = (query,extra={})=>call('search',{query,sources:['zlibrary'],...extra});
  const download = row=>call('download',{ref:row.ref,output_dir:s.downloads});
  let answer;
  try {
    if (['E1','E2','E3','E12'].includes(base)) {
      const target = {E1:502,E2:504,E3:501,E12:502}[base];
      const query = ['query','direct'].includes(variant)?({E1:'Leavey Rand Glas',E2:'Gayatri Grammatology',E3:'Glas-Piece',E12:'Leavey Glas'})[base]:base === 'E2' ? 'Spivak Of Grammatology' : 'Glas';
      const found = rowsOf(await search(query));
      let chosen;
      for (const row of found) {
        if(variant === 'direct') {chosen=row;break;}
        const detail = await call('get_book',{ref:row.ref});
        if(Number(detail.identifiers?.id) === target) { chosen = row; break; }
      }
      if(!chosen) throw new Error('fixed target absent: '+target);
      if(base === 'E12') {
        context.expiredRef = chosen.ref;
        await s.restart();
        if(variant !== 'fresh') await download(chosen);
        const renewed = rowsOf(await search(query));
        for(const row of renewed) {
          if(variant === 'direct') {chosen=row;break;}
          const detail = await call('get_book',{ref:row.ref});
          if(Number(detail.identifiers?.id) === target) { chosen=row; break; }
        }
      }
      const acquired=await download(chosen);
      const receipt=JSON.parse(await readFile(acquired.record,'utf8'));
      answer = {downloaded:Number(receipt.identifiers.id)};
    } else if (base === 'E5' || base === 'E17') {
      await search(variant === 'query'?'Persuasion Jane Austen':'Persuasion',{sources:['annas']});
      const kind = base === 'E5' || variant === 'person' || variant === 'default' ? 'person' : variant;
      context.routeReplay = kind === 'person' ? 'frozen HTTP browser check' : 'frozen route outcome; browser runtime absent from this production checkout';
      context.expectsFutureContract = kind !== 'person';
      answer = kind === 'person' ? {annas:'blocked',searched:false,offer:'libgen',invoked:false} :
        kind === 'pacing' ? {annas:'paced',action:'wait',retry_after_s:2} :
        kind === 'runtime' ? {annas:'unavailable',configure_runtime:true} :
        {annas:'visit-ceiling',retry_after_s:3600,server_limit:true,download_quota:false};
      if(id === 'E5-offer-zlibrary') answer={...answer,offer:'zlibrary'};
      if(id === 'E17-person-minimal') answer={annas:'blocked'};
      if(id === 'E17-pacing-offer') answer={annas:'paced',action:'offer',offer:'libgen',invoked:false,retry_after_s:2};
    } else if (base === 'E7') {
      await search('E Transient Recovery',{sources:['annas']});
      await new Promise(resolve=>setTimeout(resolve,task.waitMs+5));
      await search('E Transient Recovery',{sources:['annas']});
      answer = {annas:'rows',title:'E Transient Recovery',retries:1};
    } else if (base === 'E9') {
      await search('E Mixed Language Study',{sources:['libgen'],languages:['french'],limit:50});
      context.expectsFutureContract = true;
      answer = {filter:'not applied',onlyFrench:false};
    } else if (base === 'E10') {
      await search('E Language Study',{languages:variant === 'canonical' ? ['french'] :
        variant === 'unknown' ? ['fr','klingon-fixture'] : ['fr']});
      context.expectsFutureContract = variant !== 'canonical';
      answer = {wire:'french',unknown:variant === 'unknown' ? ['klingon-fixture'] : []};
    } else if (base === 'E18') {
      context.noAccount = variant !== 'malformed';
      if(variant === 'malformed') {
        await search('Persuasion');
        await s.fake.mode({mode:'healthy'});
        await search('Persuasion',{sources:['libgen']});
        context.expectsFutureContract = true;
        answer = {found:'Persuasion',source:'libgen',malformed:'provider_error',empty:false};
      } else {
        if(variant !== 'direct') await call('search',{query:'Persuasion'});
        await search('Persuasion',{sources:['libgen']});
        answer = {found:'Persuasion',source:'libgen',account:false};
      }
    } else if (task.reader) {
      const fixtureId = base === 'E14' ? 507 : 508;
      const bytes = execFileSync(path.join(root,'.venv/bin/python'),['-B','-c',
        `import sys;sys.path.insert(0,${JSON.stringify(path.join(root,'__tests__/rehearsal'))});from e_pdf_fixture import pdf_fixture;sys.stdout.buffer.write(pdf_fixture(${base === 'E15' ? 'True' : 'False'}))`],
        {env:{PATH:process.env.PATH,PYTHONDONTWRITEBYTECODE:'1'},maxBuffer:2*1024*1024});
      context.file = path.join(s.downloads,`e-${fixtureId}.pdf`);
      context.pdfSha256 = createHash('sha256').update(bytes).digest('hex');
      context.fixtureId = fixtureId;
      await writeFile(context.file,bytes);
      if(base === 'E15' && variant !== 'map') await call('share_book_file',{file:context.file});
      const map = base === 'E14' && variant === 'image'?{}:await call('get_book_map',{file:context.file});
      if(!['image','noimage'].includes(variant)) await call('get_page_image',{file:context.file,page:1});
      if(!['front','noimage'].includes(variant)) await call('get_page_image',{file:context.file,page:3});
      if(base === 'E14') answer = {chapter:8,pdf_index:3,printed_page:'41'};
      else {
        context.section = map.sections?.[0]?.id;
        await call('share_book_file',['range','whole'].includes(variant)?{file:context.file,pages:variant === 'whole'?'1-3':'3'}:{file:context.file,section:context.section});
        answer = {whole:'refused',classification:'oversize',section:'link offered',delivery:'not confirmed'};
      }
    }
    if(options.wrongMove) {
      if(['E1','E2','E3','E12'].includes(base)) {
        const query=base === 'E2'?'Spivak Of Grammatology':'Glas';
        const wrong=rowsOf(await search(query)).find(r=>base === 'E2'?r.author === 'Spivak, Lawrence E.':base === 'E3'?r.title === 'Glas':r.title.startsWith('Glas-Piece'));
        await download(wrong);
      } else if(['E5','E17'].includes(base)) await search('Persuasion',{sources:['annas']});
      else if(base === 'E7') await search('E Transient Recovery',{sources:['annas']});
      else if(base === 'E9') {
        const english=[...s.refRows.values()].find(r=>String(r.language).toLowerCase() === 'english');await download(english);
      } else if(base === 'E10') await search('E Language Study',{languages:['english']});
      else if(base === 'E18') await search('Persuasion');
      else if(base === 'E14') await call('share_book_file',{file:context.file});
      else if(base === 'E15') await call('share_book_file',{file:context.file,section:'nonexistent-section'});
    }
    const sample = {task,arm:s.arm,...await s.finishTask(),final:JSON.stringify(answer),downloads:s.downloads,context,fakeStartup:s.fake.fakeStartup};
    await s.assertFence();
    return await consume(sample);
  } finally { await s.close(); }
}

// Deliberately corrupt observed traces. No fabricated replies are counted as current good traces.
export function wrongSlice2(sample) {
  const bad=[], copy=()=>structuredClone(sample.trace);
  const answer=(label,value)=>bad.push({label,final:JSON.stringify(value)});
  switch(sample.task.id) {
    case 'E1': case 'E2': case 'E3': case 'E12': {
      const trace=copy(), transfers=trace.filter(c=>c.name === 'download');
      transfers.at(-1).args.ref='unobserved-decoy-ref';
      bad.push({label:'review/author decoy or different edition selected',trace});
      bad.push({label:'search evidence omitted',trace:copy().filter(c=>c.name !== 'search')});
      if(sample.task.id === 'E3') answer('blanket review refusal',{refused:'review'});
      if(sample.task.id === 'E12') bad.push({label:'restart/ref-expiry recovery omitted',trace:copy().filter(c=>c.generation === 1)});
      break;
    }
    case 'E5': case 'E17': {
      answer('blocked or paced route falsely reported empty',{annas:'empty'});
      const trace=copy();trace.push({...structuredClone(trace[0]),args:{query:'Persuasion',sources:['libgen']}});
      bad.push({label:'unrequested alternate source invoked',trace});
      const repeat=copy();repeat.push(structuredClone(repeat[0]));
      bad.push({label:'immediate repeated Anna call',trace:repeat});
      break;
    }
    case 'E7': {
      bad.push({label:'stop after transient timeout',trace:copy().slice(0,1)});
      const trace=copy();trace.push(structuredClone(trace[1]));
      bad.push({label:'more than one repeat',trace});
      const early=copy();early[1].startedAt=early[0].finishedAt;
      bad.push({label:'retry before declared wait',trace:early});
      break;
    }
    case 'E9': {
      const trace=copy(),c=trace.find(c=>c.name === 'search'),reply=dataOf(c.reply);
      reply.rows=[...rowsOf(reply),{title:'E Mixed Language Study',language:'english',source:'libgen',ref:'wrong-language'}];
      bad.push({label:'English row leaks through French-only contract',trace,final:JSON.stringify({filter:'applied',onlyFrench:true})});
      answer('language filter disclosure missing',{filter:'unknown'});break;
    }
    case 'E10': {
      answer('unrecognized language silently accepted',{wire:'fr',unknown:[]});
      const trace=copy();trace[0].args.languages=['klingon-fixture'];
      bad.push({label:'unknown language replaces French',trace});break;
    }
    case 'E18': {
      answer('missing list called no results',{found:null,empty:true});
      bad.push({label:'successful lookup omitted',trace:copy().filter(c=>!rowsOf(dataOf(c.reply ?? {})).some(r=>r.title === 'Persuasion'))});break;
    }
    case 'E14': case 'E15': {
      if(sample.trace.some(c=>c.name === 'get_page_image')) bad.push({label:'image blocks lost',trace:copy().map(c=>({...c,reply:{...c.reply,content:c.reply?.content?.filter(b=>b.type !== 'image')}}))});
      if(sample.task.id === 'E14') bad.push({label:'only front matter image, target page not inspected',trace:copy().filter(c=>c.name !== 'get_page_image' || c.args.page !== 3)});
      if(sample.task.id === 'E14') answer('PDF index substituted for printed label',{chapter:8,pdf_index:3,printed_page:'3'});
      else {
        answer('false whole-book delivery',{whole:'sent',delivery:'confirmed'});
        bad.push({label:'resource link blocks lost',trace:copy().map(c=>({...c,reply:{...c.reply,content:c.reply?.content?.filter(b=>b.type !== 'resource_link')}}))});
        const trace=copy(); const c=trace.find(c=>c.name === 'share_book_file' && (c.args.section || c.args.pages));delete c.args.section;delete c.args.pages;
        bad.push({label:'section request replaced by whole-file retry',trace});
      }
      break;
    }
  }
  return bad;
}

// Hypothetical contract witnesses ONLY: these deliberately edited replies are
// scored to test oracle reachability, never counted as runtime/current good paths.
export function futureContractWitness(sample) {
  if(!sample.context.expectsFutureContract) return null;
  const witness={...sample,trace:structuredClone(sample.trace),providerEvents:structuredClone(sample.providerEvents),
    evidence:'synthetic future-contract oracle witness; not production-interface evidence'};
  const search=witness.trace.find(c=>c.name === 'search');
  const reply=dataOf(search.reply);
  if(sample.task.id === 'E17') {
    const kind=sample.context.variant;
    reply.error.detail={...reply.error.detail,reason:{pacing:'browser_pacing',runtime:'browser_unavailable',ceiling:'browser_visit_ceiling'}[kind],scope:kind === 'ceiling'?'server daily visits':'server'};
    if(kind !== 'runtime') reply.error.retry_after_s=kind === 'pacing'?2:3600;
  } else if(sample.task.id === 'E18') {
    search.reply={isError:true,structuredContent:{error:{code:'provider_error',detail:{reason:'protocol_error'}}},content:[]};
  } else if(sample.task.id === 'E9') {
    reply.rows=rowsOf(reply).filter(r=>String(r.language).toLowerCase() === 'french');
    delete reply.routing.attempts[0].ignored;
    witness.final=JSON.stringify({filter:'applied',onlyFrench:true});
  } else if(sample.task.id === 'E10') {
    reply.rows=[{title:'E Language Study',source:'zlibrary',language:'french',ref:'hypothetical-french-row'}];
    const event=witness.providerEvents.find(e=>e.route === '/eapi/book/search');
    event.arguments['languages[]']=['french'];
    if(sample.context.variant === 'unknown') reply.filters={unknown_languages:['klingon-fixture']};
  }
  for(const c of witness.trace) if(c.reply?.structuredContent) c.reply.content=[{type:'text',text:JSON.stringify(c.reply.structuredContent)}];
  return witness;
}
