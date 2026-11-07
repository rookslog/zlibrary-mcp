#!/usr/bin/env node
/** Enforce filtering before artifact uploads in jobs that consume secrets. */
import { readFileSync, readdirSync, realpathSync } from 'node:fs';
import { basename, join } from 'node:path';
import { parseDocument } from 'yaml';
import { fileURLToPath } from 'node:url';

// Exceptions use "workflow/job" for reusable callers or "workflow/job/step-index"
// for uploads. Each named entry must state a nonempty reason.
// Keep empty unless a reviewed workflow needs a specific, documented exception.
const exceptions = new Map();

function secretContext(expression) {
  // Exact named token access is the sole exemption. Dynamic indexing and
  // aggregate context access still require filtering.
  const withoutToken = expression
    .replace(/\bsecrets\s*\[\s*'GITHUB_TOKEN'\s*\]/gi, '')
    .replace(/'(?:''|[^'])*'/g, "''")
    .replace(/\bsecrets\s*\.\s*GITHUB_TOKEN\b/gi, '');
  return /\bsecrets\b/i.test(withoutToken);
}

function consumesSecret(node) {
  if (Array.isArray(node)) return node.some(consumesSecret);
  if (node && typeof node === 'object') {
    return Object.entries(node).some(
      ([key, value]) =>
        (key === 'secrets' && value === 'inherit') ||
        (key === 'if' && typeof value === 'string' && secretContext(value)) ||
        consumesSecret(value),
    );
  }
  if (typeof node !== 'string') return false;
  return [...node.matchAll(/\$\{\{((?:'(?:''|[^'])*'|[^'}]|}(?!}))*?)\}\}/g)].some(
    ([, expression]) => secretContext(expression),
  );
}

function requiresFilterSuccess(condition) {
  if (typeof condition !== 'string') return false;
  const expression = condition.trim().replace(/^\$\{\{\s*([\s\S]*?)\s*\}\}$/, '$1');
  // Only conjunctions are admitted. A substring in a disjunction, negation,
  // function, or quoted literal does not establish a necessary success gate.
  return (
    expression
      .split('&&')
      .some((part) => /^\s*steps\.filter_output\.outcome\s*==\s*'success'\s*$/.test(part)) &&
    expression
      .split('&&')
      .every((part) =>
        /^(?:\s*(?:always\(\)|success\(\)|steps\.filter_output\.outcome\s*==\s*'success')\s*)$/.test(
          part,
        ),
      )
  );
}

function reasonedException(exceptions, key) {
  const reason = exceptions.get(key);
  return typeof reason === 'string' && reason.trim().length > 0;
}

function artifactUpload(step) {
  return /^actions\/upload-artifact@/i.test(step.uses || '');
}

export function checkWorkflow(workflow, file, namedExceptions = exceptions) {
  if (!workflow || !workflow.jobs || typeof workflow.jobs !== 'object') {
    throw new Error('Invalid workflow jobs');
  }
  const failures = [];
  const shared = Object.fromEntries(Object.entries(workflow).filter(([key]) => key !== 'jobs'));
  for (const [jobName, job] of Object.entries(workflow.jobs)) {
    if (!consumesSecret(shared) && !consumesSecret(job)) continue;
    const jobKey = `${basename(file)}/${jobName}`;
    if (job.uses) {
      if (!reasonedException(namedExceptions, jobKey)) {
        failures.push(`${jobKey} requires a named exception for a secret-using reusable caller`);
      }
      continue;
    }
    const steps = job.steps || [];
    const filterIndex = steps.findIndex(
      (candidate) =>
        candidate.id === 'filter_output' &&
        candidate.run?.trim() === 'python3 scripts/filter_live_test_output.py',
    );
    const uploadIndices = steps.flatMap((step, index) => (artifactUpload(step) ? [index] : []));
    const lastUpload = Math.max(-1, ...uploadIndices);
    const redirectedFilter =
      filterIndex >= 0 &&
      (Object.hasOwn(steps[filterIndex], 'working-directory') ||
        Object.hasOwn(job.defaults?.run || {}, 'working-directory') ||
        Object.hasOwn(workflow.defaults?.run || {}, 'working-directory'));
    const interrupted = steps
      .slice(filterIndex + 1, lastUpload)
      .some((step) => !artifactUpload(step));
    for (const index of uploadIndices) {
      const step = steps[index];
      const key = `${jobKey}/${index}`;
      if (reasonedException(namedExceptions, key)) continue;
      const paths =
        typeof step.with?.path === 'string'
          ? step.with.path
              .split(/\r?\n/)
              .map((line) => line.trim())
              .filter(Boolean)
          : [];
      const preparedPaths =
        paths.length > 0 && paths.every((path) => path === 'live.log' || path === 'live.xml');
      if (
        filterIndex < 0 ||
        filterIndex >= index ||
        !requiresFilterSuccess(step.if) ||
        !preparedPaths ||
        redirectedFilter ||
        interrupted
      ) {
        failures.push(
          `${key} requires adjacent uploads of prepared output after successful filtering`,
        );
      }
    }
  }
  return failures;
}

function main() {
  let failures = 0;
  try {
    const directory = process.argv[2] || '.github/workflows';
    for (const file of readdirSync(directory).sort()) {
      const document = parseDocument(readFileSync(join(directory, file), 'utf8'), {
        uniqueKeys: true,
      });
      if (document.errors.length) throw new Error('Invalid workflow YAML');
      for (const failure of checkWorkflow(document.toJS(), file)) {
        console.error(`Workflow secret policy: ${failure}`);
        failures++;
      }
    }
  } catch {
    console.error('Workflow secret policy: could not inspect workflows');
    failures++;
  }
  if (failures === 0) console.log('Workflow secret policy passed');
  return failures ? 1 : 0;
}

if (
  process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1])
) {
  process.exitCode = main();
}
