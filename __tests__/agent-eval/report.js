import { tasks, arms } from './tasks.js';
import { admitReceipt, cellIdentity, comparisonSettings } from './admission.js';
const common = (row) => !['T5', 'T6', 'T10'].includes(row.task ?? row.id);
export function distribution(values) {
  const v = [...values].sort((a, b) => a - b),
    n = v.length;
  return n
    ? {
        median: n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2,
        min: v[0],
        max: v[n - 1],
        n,
      }
    : null;
}
const inability = (row) => row.task === 'T7' && row.inability === true;
const successes = (rows) => ({
  successes: rows.filter((x) => x.success === true && !inability(x)).length,
  total: rows.length,
  fraction: rows.filter((x) => x.success === true && !inability(x)).length / rows.length,
});
const atLeast = (b, a) => b.successes * a.total >= a.successes * b.total;
export function summarize(receipts) {
  const groups = {},
    diagnostics = [],
    candidates = new Map();
  for (const [index, raw] of receipts.entries()) {
    const r = raw && typeof raw === 'object' ? raw : { status: 'malformed' };
    const key = `${r.driver}/${r.settings?.toolSearch ? 'search-on' : 'search-off'}/${r.arm?.id}`;
    const admission = admitReceipt(r);
    diagnostics.push({ index, key, ...admission, status: r.status });
    const list = candidates.get(key) ?? [];
    list.push({ r, index, admission });
    candidates.set(key, list);
  }
  for (const [key, list] of candidates) {
    // One receipt is one complete cell. Never silently concatenate duplicate or
    // incompatible cells, even if both individually look successful.
    const identities = new Set(list.map((x) => cellIdentity(x.r)));
    const conflict = list.length !== 1;
    if (conflict)
      for (const x of list) {
        x.admission.eligible = false;
        x.admission.reasons.push(
          identities.size > 1
            ? 'incompatible same-key cell receipts'
            : 'duplicate complete cell receipt',
        );
        Object.assign(diagnostics[x.index], x.admission);
      }
    const { r, admission } = list[0];
    const g = (groups[key] = {
      driver: r.driver,
      toolSearch: r.settings?.toolSearch,
      arm: r.arm,
      tools: r.build?.tools,
      eligible: admission.eligible,
      admissionReasons: admission.reasons,
      identity: cellIdentity(r),
      compatibility: comparisonSettings(r),
      warmRuns: r.settings?.warmRuns,
      phases: {},
      absoluteViolations: [],
      historyCalls: 0,
      coldEvidence: [],
    });
    if (!g.eligible) continue;
    for (const x of r.results) {
      if (x.phase === 'cold' && x.task === 'T1')
        g.coldEvidence.push(
          x.usage.cacheFirstRequest === 0
            ? 'no first-request read observed'
            : x.usage.cacheFirstRequest > 0
              ? 'cache hit'
              : 'unknown',
        );
      g.historyCalls += x.trace.filter((c) => c.name === 'get_history').length;
      if (r.arm.build === 'B') {
        if (['T2', 'T4', 'T5', 'T7', 'T11'].includes(x.task) && x.quotaSpent !== 0)
          g.absoluteViolations.push(`${x.task}: quota spent ${x.quotaSpent}`);
        if (x.refUnavailable)
          g.absoluteViolations.push(
            `${x.task}: ref_unavailable (inspect arguments for hallucination)`,
          );
        if (
          x.task === 'T5' &&
          (x.success !== true ||
            x.answer.status !== 'correct' ||
            x.wrongDecisions !== 0 ||
            x.trace.some((c) => /^(search|full_text|download)/.test(c.name)) ||
            x.providerEvents.some((e) =>
              /book\/search|\/libgen\/index.php|\/annas\/search|\/file$|\/get.php/.test(e.route),
            ))
        )
          g.absoluteViolations.push('T5: B-only negative guard failed');
      }
      const phase = (g.phases[x.phase] ??= { runs: {}, perTask: {}, rows: [] });
      const scored = { ...x, inability: r.arm.history === false && x.task === 'T7' };
      phase.rows.push(scored);
      const run = (phase.runs[x.run] ??= {
        wrongDecisions: 0,
        inputTotal: 0,
        output: 0,
        calls: 0,
        successes: 0,
        tasks: 0,
        quotaSpent: 0,
      });
      (phase.perTask[x.task] ??= []).push(scored);
      if (common(x)) {
        for (const k of ['wrongDecisions', 'calls']) run[k] += x[k];
        run.inputTotal += x.usage.inputTotal;
        run.output += x.usage.output;
        run.successes += Number(x.success && !inability(scored));
        run.tasks++;
      }
      run.quotaSpent += x.quotaSpent;
    }
    if (r.arm.build === 'B' && !(r.build.tools.serializedCharacters < 6600))
      g.absoluteViolations.push('tools/list is not under 6600 characters');
    for (const phase of Object.values(g.phases)) {
      phase.totals = Object.fromEntries(
        ['wrongDecisions', 'calls', 'inputTotal', 'output', 'successes', 'quotaSpent'].map((k) => [
          k,
          distribution(Object.values(phase.runs).map((r) => r[k])),
        ]),
      );
      phase.success = successes(phase.rows.filter(common));
      phase.allTaskSuccess = successes(phase.rows);
      for (const [task, rows] of Object.entries(phase.perTask))
        phase.perTask[task] = {
          success: successes(rows),
          wrongDecisions: distribution(rows.map((x) => x.wrongDecisions)),
          allWrongZero: rows.every((x) => x.wrongDecisions === 0),
          quotaSpent: distribution(rows.map((x) => x.quotaSpent)),
          ...(rows.some(inability)
            ? {
                outcome: 'inability',
                inability: {
                  correct: rows.filter((x) => x.success === true).length,
                  total: rows.length,
                },
              }
            : {}),
        };
      delete phase.rows;
    }
  }
  const comparisons = [];
  for (const [key, b] of Object.entries(groups).filter(([, g]) => g.arm?.id === 'B')) {
    const a = groups[`${b.driver}/${b.toolSearch ? 'search-on' : 'search-off'}/A`];
    if (!a) {
      comparisons.push({ key, status: 'inconclusive: missing matching A' });
      continue;
    }
    if (!a.eligible || !b.eligible) {
      comparisons.push({
        key,
        status: 'ineligible: invalid evidence',
        reasons: { A: a.admissionReasons, B: b.admissionReasons },
      });
      continue;
    }
    if (a.compatibility !== b.compatibility) {
      comparisons.push({
        key,
        status: 'ineligible: incompatible model/CLI/settings/baseline',
        reasons: ['A/B comparison settings differ'],
      });
      continue;
    }
    const checks = [];
    for (const phase of ['cold', 'warm']) {
      const ap = a.phases[phase],
        bp = b.phases[phase];
      checks.push({
        phase,
        valid: true,
        atLeastFiveRuns: true,
        wrongDecisionsAtMostHalf:
          bp.totals.wrongDecisions.median <= 0.5 * ap.totals.wrongDecisions.median,
        inputAtMost60Percent: bp.totals.inputTotal.median <= 0.6 * ap.totals.inputTotal.median,
        success: { A: ap.success, B: bp.success },
        successAtLeastA: atLeast(bp.success, ap.success),
        perTask: Object.fromEntries(
          tasks.filter(common).map((t) => [
            t.id,
            {
              success: { A: ap.perTask[t.id].success, B: bp.perTask[t.id].success },
              successAtLeastA: atLeast(bp.perTask[t.id].success, ap.perTask[t.id].success),
              zeroWhereAZero:
                ap.perTask[t.id].wrongDecisions.median !== 0 || bp.perTask[t.id].allWrongZero,
            },
          ]),
        ),
      });
    }
    const coldEligible =
      a.coldEvidence.length === 5 &&
      b.coldEvidence.length === 5 &&
      [...a.coldEvidence, ...b.coldEvidence].every((x) => x === 'no first-request read observed');
    const allChecks = checks.every(
      (c) =>
        c.wrongDecisionsAtMostHalf &&
        c.inputAtMost60Percent &&
        c.successAtLeastA &&
        Object.values(c.perTask).every((t) => t.successAtLeastA && t.zeroWhereAZero),
    );
    comparisons.push({
      key,
      status:
        !allChecks || b.absoluteViolations.length
          ? 'bar not met'
          : !coldEligible
            ? 'inconclusive: strict cold-cache eligibility not established'
            : 'bar met',
      checks,
      coldEligible,
      absoluteViolations: b.absoluteViolations,
    });
  }
  const variantComparisons = [];
  for (const [key, b] of Object.entries(groups).filter(([, g]) => g.arm?.id === 'B')) {
    for (const variant of arms.filter((a) => a.build === 'B' && a.id !== 'B')) {
      const v = groups[`${b.driver}/${b.toolSearch ? 'search-on' : 'search-off'}/${variant.id}`];
      const entry = { key, variant: variant.id };
      if (!v) entry.status = 'inconclusive: missing matching variant';
      else if (!b.eligible || !v.eligible) entry.status = 'ineligible: invalid evidence';
      else if (b.compatibility !== v.compatibility)
        entry.status = 'ineligible: incompatible model/CLI/settings/baseline';
      else {
        entry.status = 'descriptive comparison; not an A-bar verdict';
        entry.phases = Object.fromEntries(
          ['cold', 'warm'].map((p) => [
            p,
            {
              totals: { B: b.phases[p].totals, variant: v.phases[p].totals },
              success: { B: b.phases[p].allTaskSuccess, variant: v.phases[p].allTaskSuccess },
              perTask: Object.fromEntries(
                tasks.map((t) => [
                  t.id,
                  {
                    B: b.phases[p].perTask[t.id],
                    variant: v.phases[p].perTask[t.id],
                    quotaSpent: {
                      B: b.phases[p].perTask[t.id].quotaSpent,
                      variant: v.phases[p].perTask[t.id].quotaSpent,
                    },
                  },
                ]),
              ),
            },
          ]),
        );
      }
      variantComparisons.push(entry);
    }
  }
  // Removal requires the complete specified driver/search matrix, not merely
  // the absence of calls in whichever subset happened to be supplied.
  const enabled = arms.filter((a) => a.build === 'B' && a.history);
  const completeEnabled = [
    ['codex', false],
    ['claude', false],
    ['claude', true],
  ].every(([driver, search]) => {
    const cohort = enabled.map(
      (a) => groups[`${driver}/${search ? 'search-on' : 'search-off'}/${a.id}`],
    );
    return (
      cohort.every((g) => g?.eligible) && new Set(cohort.map((g) => g.compatibility)).size === 1
    );
  });
  return {
    groups,
    diagnostics,
    comparisons,
    variantComparisons,
    historyDecision: Object.values(groups).some(
      (g) => g.eligible && g.arm.build === 'B' && g.arm.history && g.historyCalls > 0,
    )
      ? 'retain: get_history used in history-enabled arm'
      : completeEnabled
        ? 'remove: no driver used get_history in any task/run of the complete history-enabled matrix'
        : 'not established: incomplete/invalid enabled evidence cannot remove history',
    exclusions: ['T5', 'T6', 'T10'],
    bOnlyGuards: ['T5'],
    note: 'Only admitted exact cells are aggregated. Empirical success counts/fractions gate overall and per task; median/min/max for calls/tokens/wrong decisions. Only B receives an A-bar verdict; variants are descriptive, and disabled T7 correct inability is not task completion. Every B observation must be zero where A median wrong=0. Strict Codex first-request cache remains unknown.',
  };
}
