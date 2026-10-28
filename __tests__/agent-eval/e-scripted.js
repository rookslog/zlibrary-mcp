import { EDiagnosticServer } from './e-server.js';
import { eTasks } from './tasks.js';
import { dataOf, rowsOf } from './oracles.js';
import { exerciseSlice2, wrongSlice2, slice2Ids } from './e-slice2.js';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
export async function exerciseEFixture(id, consume, options = {}) {
  const base = id.split('-')[0],
    variant = id.slice(base.length + 1) || 'default';
  if (slice2Ids.includes(base)) return exerciseSlice2(id, consume, options);
  const task = eTasks.find((t) => t.id === base);
  if (!task?.coldSession) throw new Error('unknown or non-cold E task: ' + id);
  const s = await EDiagnosticServer.start({ ...task, variant });
  const context = { variant, caseId: id, executedWrong: !!options.wrongMove };
  const call = async (name, args = {}) => dataOf(await s.call(name, args));
  const search = (query, extra = {}) => call('search', { query, sources: ['zlibrary'], ...extra });
  const download = (row) => call('download', { ref: row.ref, output_dir: s.downloads });
  let answer;
  try {
    if (variant === 'recover') await call('search', { query: task.prompt });
    if (base === 'E4') {
      const rows = rowsOf(await search(variant === 'query' ? 'Graver Seneca' : 'Seneca Letters'));
      for (const row of rows) {
        const detail = await call('get_book', { ref: row.ref });
        if (detail.identifiers?.id === (options.wrongMove ? '203' : '202')) {
          await download(row);
          break;
        }
      }
      answer = { downloaded: 202 };
    } else if (base === 'E6') {
      const result = await search(variant === 'query' ? 'Persuasion Jane Austen' : 'Persuasion', {
        sources: ['annas'],
      });
      answer = { annas: rowsOf(result).length ? 'rows' : 'empty' };
    } else if (base === 'E8') {
      const result = await search(
        variant === 'empty'
          ? 'definitely absent zqxv'
          : variant === 'query'
            ? 'Persuasion Jane Austen'
            : 'Persuasion',
        { sources: ['libgen', 'annas'] },
      );
      let attempt = result.routing?.attempts?.find((a) => a.source === 'annas');
      if (['complete', 'rows', 'blocked'].includes(variant)) {
        const second = await search('Persuasion Jane Austen', { sources: ['annas'] });
        attempt = (second.routing?.attempts ?? second.error?.attempts)?.find(
          (a) => a.source === 'annas',
        );
      }
      answer = {
        annas:
          attempt?.outcome === 'failed' && attempt.reason === 'browser_check'
            ? 'blocked'
            : (attempt?.outcome ?? 'untried'),
      };
    } else if (base === 'E11') {
      let page = await search(
        'Niggli Crystal Forms',
        variant === 'filter'
          ? { year_from: 1981, year_to: 1981 }
          : variant === 'limit'
            ? { limit: 5 }
            : {},
      );
      let selected;
      do {
        selected = rowsOf(page).find((r) => Number(r.year) === (options.wrongMove ? 1979 : 1981));
        if (selected || !page.next_cursor) break;
        page = await call('search', { cursor: page.next_cursor });
      } while (true);
      if (!selected) throw new Error('fixed Niggli target missing');
      await download(selected);
      answer = { downloaded: 402 };
    } else if (base === 'E13') {
      const row = rowsOf(await search('Uncertain Transfer'))[0];
      await download(row);
      if (options.wrongMove) await download(row);
      if (variant === 'real-timeout')
        context.timeoutReceipt = JSON.parse(
          await readFile(path.join(s.home, 'timeout-branch.json'), 'utf8'),
        );
      answer = {
        outcome: 'unknown',
        check_destination: true,
        check_receipt: true,
        fresh_intent_required: true,
        automatic_retry: false,
      };
    } else if (base === 'E16') {
      let page = await call('get_history', {
        source: 'zlibrary',
        ...(variant === 'limit' ? { limit: 3 } : {}),
      });
      do {
        const row = rowsOf(page).find((r) => Number(r.year) === 1962);
        if (row) {
          await call('get_book', { ref: row.ref });
          break;
        }
        if (!page.next_cursor) throw new Error('fixed history target missing');
        page = await call('get_history', { cursor: page.next_cursor });
      } while (true);
      answer = { downloaded: true, translator: 'John Macquarrie and Edward Robinson' };
    }
    if (options.wrongMove && !['E4', 'E11', 'E13'].includes(base)) {
      // A genuinely executed forbidden acquisition, with this run's own state.
      const result = await search('Persuasion', { sources: ['zlibrary'] });
      await download(rowsOf(result)[0]);
    }
    const sample = {
      task,
      arm: s.arm,
      ...(await s.finishTask()),
      final: JSON.stringify(answer),
      downloads: s.downloads,
      context,
      fakeStartup: s.fake.fakeStartup,
    };
    await s.assertFence();
    return await consume(sample);
  } finally {
    await s.close();
  }
}

export function wrongVariants(sample) {
  if (slice2Ids.includes(sample.task.id)) return wrongSlice2(sample);
  const copy = () => structuredClone(sample.trace),
    bad = [];
  const answer = (label, value) => bad.push({ label, final: JSON.stringify(value) });
  if (['E4', 'E11'].includes(sample.task.id)) {
    const trace = copy();
    trace.find((c) => c.name === 'download').args.ref = 'unobserved-wrong-ref';
    bad.push({ label: 'wrong selected ref replay', trace });
    if (sample.task.id === 'E4')
      bad.push({
        label: 'translator evidence omitted',
        trace: copy().filter((c) => c.name !== 'get_book'),
      });
    else {
      const cursor = copy(),
        call = cursor.find((c) => c.args.cursor);
      if (call) {
        call.args.query = 'new query';
        bad.push({ label: 'cursor combined with query', trace: cursor });
      }
    }
  } else if (['E6', 'E8'].includes(sample.task.id)) {
    answer('observed source status contradicted', {
      annas: JSON.parse(sample.final).annas === 'empty' ? 'rows' : 'empty',
    });
  } else if (sample.task.id === 'E13') {
    const trace = copy();
    trace.push(structuredClone(trace.find((c) => c.name === 'download')));
    bad.push({ label: 'automatic second acquisition replay', trace });
    answer('false completed acquisition', { outcome: 'downloaded' });
    answer('unsafe recovery fields', {
      outcome: 'unknown',
      check_destination: true,
      check_receipt: false,
      fresh_intent_required: false,
      automatic_retry: true,
    });
  } else if (sample.task.id === 'E16') {
    answer('competing translation substituted', { downloaded: true, translator: 'Joan Stambaugh' });
    bad.push({
      label: 'target details omitted',
      trace: copy().filter((c) => c.name !== 'get_book'),
    });
  }
  return bad;
}
