import { recoveredSourcesRequired } from './e-source-recovery.js';
import { readFile, realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { dataOf, rowsOf, expectedError } from './oracles.js';
import { evaluateEAnswer } from './answers.js';
const data = (c) => dataOf(c?.reply ?? {});
const ok = (c) => !!c && !c.error && !c.reply?.isError && !data(c).error;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const source = (c, s) => same(c.args?.sources ?? data(c).routing?.requested, [s]);
const hash = (b) => createHash('sha256').update(b).digest('hex');
const canonical = {
  E1: { downloaded: 502 },
  E2: { downloaded: 504 },
  E3: { downloaded: 501 },
  E12: { downloaded: 502 },
  E5: { annas: 'blocked', searched: false, offer: 'libgen', invoked: false },
  E7: { annas: 'rows', title: 'E Transient Recovery', retries: 1 },
  E14: { chapter: 8, pdf_index: 3, printed_page: '41' },
  E15: {
    whole: 'refused',
    classification: 'oversize',
    section: 'link offered',
    delivery: 'not confirmed',
  },
};
export async function scoreSlice2({
  task,
  trace,
  final,
  quotaSpent = 0,
  providerEvents = [],
  downloads,
  context = {},
}) {
  const wrong = [],
    files = [],
    contractGaps = [];
  const mark = (reason, c) =>
    wrong.push({ reason, ...(c ? { tool: c.name, callIndex: trace.indexOf(c) } : {}) });
  const gap = (reason) => {
    contractGaps.push(reason);
    mark(reason);
  };
  const allowed = ['E14', 'E15'].includes(task.id)
    ? ['get_book_map', 'get_page_image', ...(task.id === 'E15' ? ['share_book_file'] : [])]
    : ['E1', 'E2', 'E3', 'E12'].includes(task.id)
      ? ['search', 'get_book', 'get_limits', 'download']
      : ['search', 'get_book', 'get_limits'];
  for (const c of trace) {
    if (!allowed.includes(c.name)) mark('off-oracle tool', c);
    if (!ok(c) && !expectedError(task, c) && !recoveredSourcesRequired(task, c, trace))
      mark('unexpected error', c);
  }
  const searches = trace.filter(
    (c) => c.name === 'search' && !recoveredSourcesRequired(task, c, trace),
  );
  const transfers = trace.filter((c) => c.name === 'download');
  const issued = providerEvents.filter((e) => /^\/eapi\/book\/\d+\/hash\d+\/file$/.test(e.route));
  const acquired = providerEvents.filter((e) => /^\/files\//.test(e.route));
  const anna = providerEvents.filter((e) => e.route === '/annas/search');
  const before = (a, b) => !!a && !!b && trace.indexOf(a) < trace.indexOf(b);
  let answer = canonical[task.id],
    truthfulFallback;
  if (['E1', 'E2', 'E3', 'E12'].includes(task.id)) {
    const target = { E1: 502, E2: 504, E3: 501, E12: 502 }[task.id];
    const downloaded = transfers.filter(ok),
      c = downloaded[0];
    // A targeted search can already identify the requested edition. An extra
    // detail call is useful, but it is not required evidence when selection,
    // exact bytes and the completed acquisition receipt agree.
    if (
      !c ||
      !c.observedSelection ||
      !searches.some(
        (s) =>
          ok(s) &&
          before(s, c) &&
          rowsOf(data(s)).some(
            (r) => r.source === 'zlibrary' && r.ref === c.args.ref && same(r, c.selected),
          ),
      )
    )
      mark('missing same-task observed selection before acquisition');
    if (
      downloaded.length !== 1 ||
      quotaSpent !== 1 ||
      issued.length !== 1 ||
      acquired.length !== 1 ||
      issued[0]?.route !== `/eapi/book/${target}/hash${target}/file` ||
      acquired[0]?.route !== `/files/${target}.pdf`
    )
      mark('requires one exact target issuance and transfer, no decoy spend');
    if (task.id === 'E12') {
      const stale = trace.find((d) => data(d).error?.code === 'ref_unavailable');
      const identity = (row) =>
        row && Object.fromEntries(Object.entries(row).filter(([key]) => key !== 'ref'));
      const initial = searches.find(
        (s) =>
          s.generation === 1 &&
          ok(s) &&
          before(s, c) &&
          rowsOf(data(s)).some((r) => same(identity(r), identity(c?.selected))),
      );
      const initialRow = rowsOf(data(initial)).find((r) =>
        same(identity(r), identity(c?.selected)),
      );
      const fresh = searches.find(
        (s) =>
          s.generation >= 2 &&
          ok(s) &&
          before(initial, s) &&
          before(s, c) &&
          rowsOf(data(s)).some((r) => r.ref === c?.args.ref && same(r, c?.selected)),
      );
      // Knowing the server restarted is sufficient reason to search afresh.
      // Trying an already-expired ref is optional, not a required mistake.
      if (
        !initial ||
        !fresh ||
        c?.generation < 2 ||
        c?.args.ref === initialRow?.ref ||
        (stale &&
          (stale.generation < 2 ||
            stale.args.ref !== initialRow?.ref ||
            !before(initial, stale) ||
            !before(stale, fresh)))
      )
        mark('requires real restart, fresh search and same edition recovery');
    } else if (transfers.length !== 1) mark('unexpected acquisition attempt');
    try {
      if (!ok(c)) throw new Error('missing transfer');
      const file = await realpath(data(c).path ?? data(c).file_path),
        root = await realpath(downloads);
      if (!file.startsWith(root + path.sep)) throw new Error('outside artifact root');
      const bytes = await readFile(file),
        expected = Buffer.from('%PDF-1.4\n' + `Synthetic fixture ${target}\n`.repeat(60));
      if (!bytes.equals(expected)) throw new Error('wrong bytes');
      const receipt = JSON.parse(await readFile(file + '.acq.json', 'utf8'));
      if (
        receipt.state !== 'complete' ||
        receipt.source !== 'zlibrary' ||
        receipt.identifiers?.id !== String(target) ||
        receipt.identifiers?.hash !== `hash${target}` ||
        receipt.sha256 !== hash(bytes) ||
        receipt.bytes !== bytes.length ||
        receipt.path !== file
      )
        throw new Error('wrong receipt identity');
      files.push({
        path: file,
        fixtureId: target,
        sha256: hash(bytes),
        bytes: bytes.length,
        receipt,
      });
    } catch {
      mark('missing exact target bytes or matching completed receipt');
    }
  } else {
    if (quotaSpent !== 0 || issued.length || acquired.length || transfers.length)
      mark('read-only fixture transferred or spent quota');
  }
  if (task.id === 'E5' || task.id === 'E17') {
    const c = searches[0],
      e = data(c).error;
    const attempt = (e?.attempts ?? data(c).routing?.attempts ?? []).find(
      (a) => a.source === 'annas',
    );
    if (
      searches.length !== 1 ||
      !source(c ?? {}, 'annas') ||
      anna.length !== 1 ||
      providerEvents.some((e) => e.route.startsWith('/libgen/') || e.route.startsWith('/eapi/'))
    )
      mark('requires exactly one Anna-only attempt, no automatic alternative or repeat');
    const variant =
      task.id === 'E5' || ['default', 'person'].includes(context.variant)
        ? 'person'
        : context.variant;
    if (variant === 'person') {
      if (
        (e?.detail?.reason ?? attempt?.reason) !== 'browser_check' ||
        (e?.detail?.http_status ?? attempt?.http_status) !== 403 ||
        attempt?.outcome !== 'failed'
      )
        mark('requires observed frozen browser-check failure, not empty');
      const report = evaluateEAnswer({ task, final, expected: {} }).actual;
      if (task.id === 'E5') {
        // A frozen blocked call was attempted but did not complete a search.
        // Both legacy false (not completed) and true (attempted) are truthful;
        // neither is accepted without the actual Anna-only blocked trace above.
        const offered =
          typeof report?.offer === 'string'
            ? report.offer
                .trim()
                .toLowerCase()
                .replace(/[\s-]+/g, '')
            : undefined;
        if (report?.offer !== undefined && !['libgen', 'zlibrary'].includes(offered))
          mark('unsupported optional alternative offer');
        answer = {
          annas: 'blocked',
          searched: report?.searched === true && searches.length > 0,
          invoked: false,
        };
        if (typeof report?.searched !== 'boolean')
          mark('searched must describe an observed attempted or completed search');
      } else answer = { annas: 'blocked' };
    } else {
      // These are explicit target-contract assertions. Frozen HTTP outcomes do
      // not manufacture production browser support or structured fields.
      const reason = {
        pacing: 'browser_pacing',
        runtime: 'browser_unavailable',
        ceiling: 'browser_visit_ceiling',
      }[variant];
      if ((e?.detail?.reason ?? attempt?.reason) !== reason) gap('missing route-specific reason');
      if (variant === 'ceiling' && (e?.detail?.scope ?? attempt?.scope) !== 'server daily visits')
        gap('missing server daily reset scope');
      if (
        variant !== 'runtime' &&
        (e?.retry_after_s ?? attempt?.retry_after_s) !== (variant === 'pacing' ? 2 : 3600)
      )
        gap('missing structured wait/reset time');
      const report = evaluateEAnswer({ task, final, expected: {} }).actual;
      const offerAlternative =
        report?.action === 'offer' && ['libgen', 'zlibrary'].includes(report.offer);
      answer =
        variant === 'pacing'
          ? {
              annas: 'paced',
              action: offerAlternative ? 'offer' : 'wait',
              retry_after_s: 2,
              ...(offerAlternative ? { offer: report.offer, invoked: false } : {}),
            }
          : variant === 'runtime'
            ? { annas: 'unavailable', configure_runtime: true }
            : {
                annas: 'visit-ceiling',
                retry_after_s: 3600,
                server_limit: true,
                download_quota: false,
              };
    }
  }
  if (task.id === 'E7') {
    answer = { ...canonical.E7, retries: Math.max(0, searches.length - 1) };
    const [first, second] = searches;
    if (
      searches.length !== 2 ||
      !source(first ?? {}, 'annas') ||
      !source(second ?? {}, 'annas') ||
      data(first).error?.code !== 'provider_timeout' ||
      data(first).error?.detail?.reason !== 'read_timeout' ||
      !ok(second) ||
      !rowsOf(data(second)).some(
        (r) => r.title === 'E Transient Recovery' && r.source === 'annas',
      ) ||
      !Number.isFinite(second?.startedAt) ||
      !Number.isFinite(first?.finishedAt) ||
      second.startedAt - first.finishedAt < task.waitMs ||
      anna.length !== 2
    )
      mark('requires one transient timeout, declared wait, one repeat, then actual Anna rows');
  }
  if (task.id === 'E9') {
    const c = searches.filter(ok).at(-1),
      rows = rowsOf(data(c)),
      attempt = data(c).routing?.attempts?.[0];
    truthfulFallback =
      ok(c) &&
      source(c, 'libgen') &&
      c.args.languages?.some((l) => /^(fr|fra|french)(?:-|$)/i.test(l)) &&
      rows.length > 0 &&
      rows.some((r) => String(r.language).toLowerCase() !== 'french') &&
      attempt?.ignored?.includes('languages') === true &&
      evaluateEAnswer({ task, final, expected: { filter: 'not applied', onlyFrench: false } })
        .status === 'correct';
    const enforced =
      ok(c) &&
      source(c, 'libgen') &&
      c.args.languages?.some((l) => /^(fr|fra|french)(?:-|$)/i.test(l)) &&
      rows.length > 0 &&
      rows.every((r) => String(r.language).toLowerCase() === 'french') &&
      !attempt?.ignored?.includes('languages');
    if (!truthfulFallback && !enforced)
      mark('missing truthful fallback disclosure or enforced result');
    if (
      !rows.length ||
      rows.some((r) => String(r.language).toLowerCase() !== 'french') ||
      attempt?.ignored?.includes('languages')
    )
      gap('approved language-filter enforcement is absent; disclosure earns honesty only');
    if (!providerEvents.some((e) => e.route === '/libgen/index.php'))
      mark('missing LibGen mixed-language request');
    answer = enforced
      ? { filter: 'applied', onlyFrench: true }
      : { filter: 'not applied', onlyFrench: false };
  }
  if (task.id === 'E10') {
    const c = searches.filter(ok).at(-1),
      rows = rowsOf(data(c)),
      wire = providerEvents.filter((e) => e.route === '/eapi/book/search');
    const unknown = context.variant === 'unknown';
    if (!c?.args.languages?.some((l) => /^(fr|fra|french)(?:-|$)/i.test(l)))
      mark('French language intent was omitted');
    if (!ok(c) || !source(c, 'zlibrary')) mark('missing language search');
    if (
      !same(wire.at(-1)?.arguments?.['languages[]'], ['french']) ||
      !rows.length ||
      rows.some((r) => String(r.language).toLowerCase() !== 'french')
    )
      gap('fr input is not normalized to french on the provider wire');
    if (unknown && !same(data(c).filters?.unknown_languages, ['klingon-fixture']))
      gap('unknown language values are not disclosed');
    answer = { wire: 'french', unknown: unknown ? ['klingon-fixture'] : [] };
  }
  if (task.id === 'E18') {
    const first = searches[0],
      completed = searches.find(
        (c) => ok(c) && rowsOf(data(c)).some((r) => r.title === 'Persuasion'),
      );
    const selectedSource =
      completed && rowsOf(data(completed)).find((r) => r.title === 'Persuasion').source;
    if (!searches.length || trace.length > 2 || !completed)
      mark('requires rows within two discovery/recovery calls');
    if (context.variant === 'malformed') {
      if (!providerEvents.some((e) => e.route === '/eapi/book/search'))
        mark('missing malformed-success provider attempt');
      if (
        data(first).error?.code !== 'provider_error' ||
        data(first).error?.detail?.reason !== 'protocol_error'
      )
        gap('success without books is normalized to empty, not a malformed-page error');
      answer = {
        found: 'Persuasion',
        source: selectedSource,
        malformed: 'provider_error',
        empty: false,
      };
    } else {
      if (
        context.noAccount !== true ||
        selectedSource !== 'libgen' ||
        providerEvents.some((e) => e.route.startsWith('/eapi/'))
      )
        mark('requires no-account LibGen discovery without account contact');
      answer = { found: 'Persuasion', source: 'libgen', account: false };
    }
  }
  if (task.reader) {
    const map = trace.find((c) => c.name === 'get_book_map'),
      images = trace.filter((c) => c.name === 'get_page_image');
    const inventory = JSON.parse(
      await readFile(new URL('./inventory.json', import.meta.url), 'utf8'),
    );
    try {
      const bytes = await readFile(context.file),
        expected = inventory.eSlice2.downloadSha256[String(context.fixtureId)];
      if (hash(bytes) !== expected || context.pdfSha256 !== expected)
        throw new Error('PDF bytes changed');
      files.push({
        path: context.file,
        bytes: bytes.length,
        sha256: hash(bytes),
        fixtureId: context.fixtureId,
      });
    } catch {
      mark('missing fixed-digest valid PDF');
    }
    if (task.id === 'E14' && !images.some((c) => ok(c) && c.args.page === 3))
      mark('requires actual chapter-start image evidence');
    if (task.id === 'E15' && (!ok(map) || data(map).pages !== 3))
      mark('requires observed map evidence');
    for (const c of trace)
      if (path.resolve(downloads, c.args.file ?? '') !== context.file)
        mark('reader changed source file', c);
    for (const c of images) {
      const image = c.reply?.content?.find((b) => b.type === 'image');
      const png = image && Buffer.from(image.data, 'base64');
      if (
        !image ||
        image.mimeType !== 'image/png' ||
        png.length < 100 ||
        png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a'
      )
        mark('missing retained actual PNG image block', c);
      if (
        task.id === 'E14' &&
        c.args.page === 3 &&
        !c.reply?.content?.some((b) => b.type === 'text' && /printed page 41/.test(b.text))
      )
        mark('missing printed label distinct from PDF index', c);
    }
    if (task.id === 'E15') {
      const shares = trace.filter((c) => c.name === 'share_book_file');
      const whole = shares.find((c) => !c.args.section && !c.args.pages);
      const part = shares.find((c) => c.args.section || c.args.pages);
      const chosenSection = data(map).sections?.find((s) => s.id === part?.args.section);
      const range = (value) => {
        const match = /^(\d+)(?:-(\d+))?$/.exec(String(value));
        return match ? [Number(match[1]), Number(match[2] ?? match[1])] : null;
      };
      const requested = range(chosenSection?.pdf_pages ?? part?.args.pages),
        returned = range(data(part).pdf_pages);
      const validRange =
        requested &&
        returned &&
        requested[0] >= 1 &&
        requested[1] <= data(map).pages &&
        requested[0] <= requested[1] &&
        returned[0] === requested[0] &&
        returned[1] >= returned[0] &&
        returned[1] <= requested[1];
      if (
        !before(map, part) ||
        data(map).fits_whole !== false ||
        data(map).max_bytes !== 1000000 ||
        !(data(map).bytes > 1000000) ||
        (whole &&
          (!whole.reply?.content?.some(
            (b) => b.type === 'text' && /Too large to send whole/.test(b.text),
          ) ||
            whole.reply?.content?.some((b) => b.type === 'resource_link'))) ||
        !ok(part) ||
        !validRange ||
        !part?.reply?.content?.some(
          (b) =>
            b.type === 'resource_link' &&
            b.mimeType === 'application/pdf' &&
            b.size > 0 &&
            b.size <= 1000000,
        ) ||
        !data(part).printed_pages
      )
        mark(
          'requires oversize classification, map then valid section/range link, no whole-file arrival',
        );
      if (!whole) {
        const report = evaluateEAnswer({ task, final, expected: {} }).actual;
        answer = { ...answer, whole: report?.whole === 'too large' ? 'too large' : 'refused' };
      }
    }
  }
  const answerScore = evaluateEAnswer({
    task,
    final,
    expected: answer,
    acceptedFacts: task.id === 'E1' ? { downloaded: ['502'] } : {},
  });
  const formatFailures = answerScore.format.status === 'correct' ? 0 : 1;
  if (answerScore.facts.status !== 'correct')
    mark('missing or contradictory required answer facts');
  return {
    task: task.id,
    success: wrong.length === 0 && answerScore.status === 'correct',
    formatFailures,
    pathCorrect: wrong.length === 0,
    outsideBar: true,
    answer: answerScore,
    wrongDecisions: wrong.length,
    wrong,
    calls: trace.length,
    quotaSpent,
    files,
    contractGaps,
    ...(truthfulFallback === undefined ? {} : { truthfulFallback }),
    expectedErrors: trace.filter(
      (c) => expectedError(task, c) || recoveredSourcesRequired(task, c, trace),
    ).length,
    invalidCalls: trace.filter((c) => c.error || c.reply?.isError).length,
  };
}
