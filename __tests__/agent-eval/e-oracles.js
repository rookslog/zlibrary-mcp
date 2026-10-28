import { readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { dataOf, rowsOf, expectedError } from './oracles.js';

const ok = c => !c.error && !c.reply?.isError && !dataOf(c.reply ?? {}).error;
const data = c => dataOf(c.reply ?? {});
const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
const onlyCursor = (call, cursor) => typeof cursor === 'string' &&
  Object.keys(call.args ?? {}).length === 1 && call.args.cursor === cursor;
const matchRow = (row, title, year) => row?.title === title && Number(row.year) === year;

export async function scoreETask({task, trace, final='', quotaSpent=0, providerEvents=[], downloads, context={}}) {
  const wrong = [], calls = new Set(), files = [];
  const mark = (reason,c) => { const index = trace.indexOf(c); if(c) calls.add(index);
    wrong.push({reason, ...(c ? {tool:c.name, arguments:c.args, callIndex:index} : {})}); };
  const permitted = {E4:['search','get_book','download'],E6:['search'],E8:['search'],
    E11:['search','get_book','download'],E13:['search','download'],E16:['get_history','get_book']}[task.id] ?? [];
  for (const c of trace) {
    if (!permitted.includes(c.name)) mark('off-oracle tool',c);
    if (!ok(c) && !expectedError(task,c)) mark('unexpected error',c);
  }
  const searches = trace.filter(c => c.name === 'search' && ok(c));
  const transfers = trace.filter(c => c.name === 'download');
  const details = trace.filter(c => c.name === 'get_book' && ok(c));
  const histories = trace.filter(c => c.name === 'get_history' && ok(c));
  const rows = c => rowsOf(data(c));
  const before = (a,b) => trace.indexOf(a) < trace.indexOf(b);
  const sourceOnly = (c, source) => same(c.args?.sources,[source]);
  const issued = providerEvents.filter(e => /^\/eapi\/book\/\d+\/hash\d+\/file$/.test(e.route));
  const acquired = providerEvents.filter(e => /^\/files\//.test(e.route));
  let answer;
  switch(task.id) {
    case 'E4': {
      const found = searches.find(c => sourceOnly(c,'zlibrary') &&
        rows(c).some(r => matchRow(r,'Letters on Ethics: To Lucilius',2015)) &&
        rows(c).some(r => matchRow(r,'Letters from a Stoic',1969)));
      const detail = details.find(c => data(c).translator === 'Margaret Graver and A. A. Long' &&
        /Translated by Margaret Graver and A\. A\. Long/.test(data(c).description ?? '') &&
        data(c).identifiers?.id === '202');
      const download = transfers[0];
      if (!found || !detail || !download || !before(found,detail) || !before(detail,download) ||
          !rows(found).some(r => r.ref === detail.args?.ref) || download.args?.ref !== detail.args?.ref)
        mark('missing observed translator evidence before selected acquisition');
      answer = {downloaded:'graver'};
      break;
    }
    case 'E6': {
      const attempt = searches.find(c => sourceOnly(c,'annas') && c.args.query === 'Persuasion' &&
        rows(c).length === 0 && data(c).routing?.attempts?.some(a => a.source === 'annas' && a.outcome === 'empty'));
      if (searches.length !== 1 || !attempt || !providerEvents.some(e => e.route === '/annas/search'))
        mark('missing completed Anna\'s-only empty attempt');
      answer = {annas:'empty'};
      break;
    }
    case 'E8': {
      const query = context.variant === 'empty' ? 'definitely absent zqxv' : 'Persuasion';
      const search = searches[0], attempts = search && data(search).routing?.attempts;
      const empty = context.variant === 'empty';
      if (searches.length !== 1 || !search || !same(search.args.sources,['libgen','annas']) ||
          search.args.query !== query || !Array.isArray(attempts) ||
          !same(attempts.map(a => ({source:a.source,outcome:a.outcome})), empty
            ? [{source:'libgen',outcome:'empty'},{source:'annas',outcome:'empty'}]
            : [{source:'libgen',outcome:'rows'}]) ||
          (empty ? rows(search).length !== 0 : !rows(search).some(r => r.source === 'libgen' && r.title === 'Persuasion')) ||
          providerEvents.filter(e => e.route === '/annas/search').length !== (empty ? 1 : 0))
        mark('missing observed ordered-source rows/empty evidence');
      answer = {annas:empty ? 'empty' : 'untried'};
      break;
    }
    case 'E11': {
      const [first,second] = searches;
      if (searches.length !== 2 || !first || !second || first.args.limit !== task.limit ||
          !sourceOnly(first,'zlibrary') || !/niggli/i.test(first.args.query ?? '') ||
          !onlyCursor(second,data(first).next_cursor) ||
          rows(first).length !== 1 || !matchRow(rows(first)[0],'Crystal Forms',1979) ||
          rows(second).length !== 1 || !matchRow(rows(second)[0],'Crystal Forms',1981) ||
          !transfers[0] || !before(second,transfers[0]) || transfers[0].args.ref !== rows(second)[0].ref)
        mark('missing page-one shortage, cursor-only continuation or page-two identity');
      const pages = providerEvents.filter(e => e.route === '/eapi/book/search').map(e => Number(e.arguments.page?.[0]));
      if (!same(pages,[1,2])) mark('skipped or repeated qualifying provider page');
      answer = {downloaded:'niggli-1981'};
      break;
    }
    case 'E13': {
      const download = transfers[0];
      if (transfers.length !== 1 || !download || !expectedError(task,download) ||
          !searches.some(c => before(c,download) && sourceOnly(c,'zlibrary') &&
            rows(c).some(r => r.title === 'Uncertain Transfer' && r.ref === download.args.ref)) ||
          !download.observedSelection || quotaSpent !== 1 || issued.length !== 1 || acquired.length !== 1 ||
          issued[0]?.route !== '/eapi/book/403/hash403/file' || acquired[0]?.route !== '/files/403.pdf')
        mark('requires one issuance and one uncertain transfer, without automatic retry');
      answer = {outcome:'unknown',recovery:'check destination and receipt; retry only with fresh intent'};
      break;
    }
    case 'E16': {
      const [first,second] = histories;
      if (histories.length !== 2 || !first || !second || first.args.source !== 'zlibrary' ||
          first.args.limit !== task.limit || !onlyCursor(second,data(first).next_cursor) ||
          rows(first).length !== 1 || !matchRow(rows(first)[0],'Being and Time',1996) ||
          rows(second).length !== 1 || !matchRow(rows(second)[0],'Being and Time',1962))
        mark('missing competing history row, boundary or cursor-only target page');
      for (const [page,translator] of [[first,'Joan Stambaugh'],[second,'John Macquarrie and Edward Robinson']]) {
        if (!page || !details.some(c => before(page,c) && c.args.ref === rows(page)[0]?.ref && data(c).translator === translator))
          mark('missing observed translation detail for history row');
      }
      const pages = providerEvents.filter(e => e.route === '/eapi/user/book/downloaded').map(e => Number(e.arguments.page?.[0]));
      if (!same(pages,[1,2])) mark('missing sequential account-history provider pages');
      answer = {downloaded:true,translator:'John Macquarrie and Edward Robinson'};
      break;
    }
    default: mark('unknown E task');
  }
  if (['E4','E11'].includes(task.id)) {
    const id = task.id === 'E4' ? 202 : 402;
    if (transfers.length !== 1 || quotaSpent !== 1 || issued.length !== 1 || acquired.length !== 1 ||
        issued[0]?.route !== `/eapi/book/${id}/hash${id}/file` || acquired[0]?.route !== `/files/${id}.pdf`)
      mark('requires exactly one correct issuance and transfer');
    for (const c of transfers) {
      const prior = searches.filter(s => before(s,c)).flatMap(rows);
      if (!c.observedSelection || !prior.some(r => r.ref === c.args?.ref && same(r,c.selected)))
        mark('unobserved or modified selected row',c);
      try {
        if (!ok(c)) throw new Error('failed acquisition');
        const file = await realpath(data(c).path ?? data(c).file_path), root = await realpath(downloads);
        if (!file.startsWith(root + path.sep)) throw new Error('outside root');
        const bytes = await readFile(file);
        const expected = Buffer.from('%PDF-1.4\n' + `Synthetic fixture ${id}\n`.repeat(60));
        if (!bytes.equals(expected)) throw new Error('wrong identity bytes');
        files.push({path:file,bytes:bytes.length,fixtureId:id});
      } catch {mark('missing or incorrect exact artifact bytes',c);}
    }
  } else if (task.id !== 'E13' && (quotaSpent !== 0 || issued.length || acquired.length)) mark('read-only task spent quota or transferred');
  // Canonical whole-answer grammar, not substring credit or a general semantic judge.
  const correctAnswer = typeof final === 'string' && final === JSON.stringify(answer);
  if (!correctAnswer) mark('missing or contradictory canonical answer');
  const success = wrong.length === 0;
  return {task:task.id, pathCorrect:success, success, outsideBar:true,
    answer:{status:correctAnswer?'correct':'incorrect'}, wrongDecisions:calls.size + wrong.filter(w => !w.tool).length,
    wrong, calls:trace.length, quotaSpent, files,
    invalidCalls:trace.filter(c => c.error || c.reply?.isError).length,
    expectedErrors:trace.filter(c => expectedError(task,c)).length, refUnavailable:0};
}
