import { EDiagnosticServer } from './e-server.js';
import { eTasks } from './tasks.js';
import { dataOf, rowsOf } from './oracles.js';

export async function exerciseEFixture(id, consume) {
  const task = eTasks.find(t => t.id === id.replace('-empty',''));
  if (!task) throw new Error('unknown E fixture: '+id);
  if (!task.coldSession) throw new Error('E fixture must start cold');
  const s = await EDiagnosticServer.start(task);
  const context = {variant:id.endsWith('-empty')?'empty':'rows'};
  const call = async (name,args={}) => dataOf(await s.call(name,args));
  const search = async (query, extra={}) => call('search',{query,sources:['zlibrary'],...extra});
  const download = row => call('download',{ref:row.ref,output_dir:s.downloads});
  let answer;
  try {
    switch(task.id) {
      case 'E4': {
        const found = await search('Seneca Letters');
        const rows = rowsOf(found);
        for (const row of rows) {
          const detail = await call('get_book',{ref:row.ref});
          if (detail.translator === 'Margaret Graver and A. A. Long') {
            await download(row);
            break;
          }
        }
        answer = {downloaded:'graver'};
        break;
      }
      case 'E6':
        await search('Persuasion',{sources:['annas']});
        answer = {annas:'empty'};
        break;
      case 'E8':
        await search(context.variant === 'empty'?'definitely absent zqxv':'Persuasion',{sources:['libgen','annas']});
        answer = {annas:context.variant === 'empty'?'empty':'untried'};
        break;
      case 'E11': {
        const first = await search('Niggli Crystal Forms',{limit:task.limit});
        const second = await call('search',{cursor:first.next_cursor});
        await download(rowsOf(second)[0]);
        answer = {downloaded:'niggli-1981'};
        break;
      }
      case 'E13': {
        await download(rowsOf(await search('Uncertain Transfer'))[0]);
        answer = {outcome:'unknown',recovery:'check destination and receipt; retry only with fresh intent'};
        break;
      }
      case 'E16': {
        const first = await call('get_history',{source:'zlibrary',limit:task.limit});
        await call('get_book',{ref:rowsOf(first)[0].ref});
        const second = await call('get_history',{cursor:first.next_cursor});
        await call('get_book',{ref:rowsOf(second)[0].ref});
        answer = {downloaded:true,translator:'John Macquarrie and Edward Robinson'};
        break;
      }
    }
    const sample = {task,arm:s.arm,...await s.finishTask(),final:JSON.stringify(answer),downloads:s.downloads,context};
    await s.assertFence();
    return await consume(sample);
  } finally { await s.close(); }
}

// Mutate actual replies/arguments rather than inventing a second protocol fake.
export function wrongVariants(sample) {
  const copy = () => structuredClone(sample.trace);
  const bad = [];
  const answer = (label, value) => bad.push({label,final:JSON.stringify(value)});
  switch(sample.task.id) {
    case 'E4': {
      const trace = copy(), c = trace.find(c => c.name === 'download');
      const campbell = rowsOf(dataOf(trace.find(c => c.name === 'search').reply)).find(r => r.year === 1969);
      c.args.ref = campbell.ref; c.selected = campbell;
      bad.push({label:'wrong Campbell edition',trace});
      bad.push({label:'translator evidence omitted',trace:copy().filter(c => c.name !== 'get_book')});
      break;
    }
    case 'E6':
      answer('empty mislabeled blocked',{annas:'blocked'});
      bad.push({label:'none without completed attempt',trace:[],providerEvents:[]});
      break;
    case 'E8':
      answer('blanket unknown',{annas:'unknown'});
      answer('wrong attempted status',{annas:sample.context.variant === 'empty'?'untried':'empty'});
      break;
    case 'E11': {
      const trace = copy(); trace.filter(c => c.name === 'search')[1].args.query = 'Niggli Crystal Forms';
      bad.push({label:'cursor combined with new query',trace});
      const firstPage = copy();
      firstPage.find(c => c.name === 'download').args.ref = rowsOf(dataOf(firstPage[0].reply))[0].ref;
      bad.push({label:'look-alike first page selection',trace:firstPage});
      bad.push({label:'limit boundary bypassed',trace:copy().map((c,i) => i?c:{...c,args:{...c.args,limit:50}})});
      break;
    }
    case 'E13': {
      const trace = copy(); trace.push(structuredClone(trace.find(c => c.name === 'download')));
      bad.push({label:'automatic second transfer',trace});
      const wrongError = copy(); wrongError.find(c => c.name === 'download').reply.structuredContent.error.code = 'provider_error';
      bad.push({label:'wrong expected error code',trace:wrongError});
      answer('false completed acquisition',{outcome:'downloaded',recovery:'none'});
      answer('retry without fresh intent',{outcome:'unknown',recovery:'retry now'});
      break;
    }
    case 'E16': {
      answer('competing translation substituted',{downloaded:true,translator:'Joan Stambaugh'});
      const trace = copy(); trace.filter(c => c.name === 'get_history')[1].args.limit = 1;
      bad.push({label:'history cursor combined with limit',trace});
      bad.push({label:'page two skipped',trace:copy().slice(0,2)});
      break;
    }
  }
  return bad;
}
