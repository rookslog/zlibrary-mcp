#!/usr/bin/env node
/** Enforce filtering before artifact uploads in jobs that consume secrets. */
import { readFileSync, readdirSync } from 'node:fs';
import { basename, join } from 'node:path';
import { parseDocument } from 'yaml';

// Exceptions are keyed by "workflow/job/step-index" and must state a reason.
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

let failures = 0;
try {
  const directory = process.argv[2] || '.github/workflows';
  for (const file of readdirSync(directory).sort()) {
    const document = parseDocument(readFileSync(join(directory, file), 'utf8'), {
      uniqueKeys: true,
    });
    if (document.errors.length) throw new Error('Invalid workflow YAML');
    const workflow = document.toJS();
    if (!workflow || typeof workflow.jobs !== 'object') throw new Error('Invalid workflow jobs');
    for (const [jobName, job] of Object.entries(workflow.jobs)) {
      const shared = Object.fromEntries(Object.entries(workflow).filter(([key]) => key !== 'jobs'));
      if (!consumesSecret(shared) && !consumesSecret(job)) continue;
      const steps = job.steps || [];
      for (const [index, step] of steps.entries()) {
        if (!/^actions\/upload-artifact@/i.test(step.uses || '')) continue;
        const key = `${basename(file)}/${jobName}/${index}`;
        if (exceptions.has(key) && exceptions.get(key).trim()) continue;
        const filterIndex = steps.findIndex(
          (candidate) =>
            candidate.id === 'filter_output' &&
            candidate.run?.trim() === 'python3 scripts/filter_live_test_output.py',
        );
        if (filterIndex < 0 || filterIndex >= index || !requiresFilterSuccess(step.if)) {
          console.error(`Workflow secret policy: ${key} requires successful output filtering`);
          failures++;
        }
      }
    }
  }
} catch {
  console.error('Workflow secret policy: could not inspect workflows');
  failures++;
}
if (failures === 0) console.log('Workflow secret policy passed');
process.exitCode = failures ? 1 : 0;
