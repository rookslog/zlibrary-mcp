#!/usr/bin/env node
/**
 * release-audit — checks that the release record agrees with itself.
 *
 * Written after the v1.4 drift (2026-08-11). Two artifacts both claimed to
 * define "v1.4": milestone #1 ("Source layer as a first-class citizen", from
 * the 2026-07-24 roadmap) and map issue #75 ("Any source can be downloaded",
 * charted 2026-08-10). The second shipped. Nobody reconciled them, so a
 * released version sat at 5 open / 0 closed while every issue that actually
 * fed the release carried no milestone at all, and the Releases page still
 * showed v1.3.2 as latest. Each of those is a check below.
 *
 * The rules being enforced are stated in docs/RELEASE_PROCESS.md. This script
 * is the enforcement; that document is the reasoning.
 *
 * GitHub checks need an authenticated `gh`. Without one they are skipped with
 * a notice rather than failing, so a contributor without repo access can still
 * run the local CHANGELOG and tag checks.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const errors = [];
const warnings = [];
const notices = [];

const fail = (msg, fix) => errors.push({ msg, fix });
const warn = (msg, fix) => warnings.push({ msg, fix });

function run(cmd, args) {
  return execFileSync(cmd, args, { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

function tryRun(cmd, args) {
  try {
    return run(cmd, args);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- local state

const changelogPath = join(ROOT, 'CHANGELOG.md');
if (!existsSync(changelogPath)) {
  console.error('release-audit: CHANGELOG.md not found; nothing to audit against.');
  process.exit(1);
}
const changelog = readFileSync(changelogPath, 'utf8');

/** Versions with a `## [x.y.z]` section. */
const changelogVersions = new Set(
  [...changelog.matchAll(/^## \[(\d+\.\d+\.\d+)\]/gm)].map((m) => m[1]),
);

/** Tags shaped `vX.Y.Z`. Older tags like `v1.2` predate the convention. */
const tags = (tryRun('git', ['tag', '--list', 'v*']) ?? '')
  .split('\n')
  .filter((t) => /^v\d+\.\d+\.\d+$/.test(t));

const pkgVersion = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).version;

// Rule: every tagged version has release notes to be built from.
for (const tag of tags) {
  const version = tag.slice(1);
  if (!changelogVersions.has(version)) {
    fail(
      `Tag ${tag} has no "## [${version}]" section in CHANGELOG.md.`,
      `Add the section. publish.yml builds release notes from it and fails the release without it.`,
    );
  }
}

// Rule: the version you are about to ship is described before you ship it.
if (!changelogVersions.has(pkgVersion)) {
  warn(
    `package.json is at ${pkgVersion}, which has no CHANGELOG.md section yet.`,
    `Write the "## [${pkgVersion}]" section before tagging.`,
  );
}

// Rule: work that has landed is recorded as it lands, not at release time.
const lastTag = tags.length ? tags[tags.length - 1] : null;
if (lastTag) {
  const commitsSince = Number(tryRun('git', ['rev-list', '--count', `${lastTag}..HEAD`]) ?? '0');
  const unreleased = changelog.match(/^## \[Unreleased\]\s*\n([\s\S]*?)(?=^## \[)/m);
  const unreleasedBody = (unreleased?.[1] ?? '').trim();
  if (commitsSince > 0 && unreleasedBody === '') {
    warn(
      `${commitsSince} commit(s) since ${lastTag} but "## [Unreleased]" in CHANGELOG.md is empty.`,
      `Record landed work under [Unreleased] as it merges, so the next release notes are not reconstructed from git log.`,
    );
  }
}

// --------------------------------------------------------------- github state

const ghReady = tryRun('gh', ['auth', 'status']) !== null;

if (!ghReady) {
  notices.push('gh is unavailable or unauthenticated — GitHub checks skipped (local checks still ran).');
} else {
  const ghJson = (args) => {
    const out = tryRun('gh', args);
    if (out === null) return null;
    try {
      return JSON.parse(out);
    } catch {
      return null;
    }
  };

  // Rule: a tag that shipped has a GitHub Release. npm and GHCR are not the
  // record; the Releases page is what a human reads.
  const releases = ghJson(['release', 'list', '--limit', '100', '--json', 'tagName']);
  if (releases) {
    const released = new Set(releases.map((r) => r.tagName));
    for (const tag of tags) {
      if (!released.has(tag)) {
        fail(
          `Tag ${tag} was pushed but has no GitHub Release.`,
          `gh release create ${tag} --verify-tag --notes-file <changelog section>  (publish.yml now does this automatically)`,
        );
      }
    }
  }

  const milestones = ghJson(['api', 'repos/:owner/:repo/milestones?state=all&per_page=100']);
  if (milestones) {
    const taggedVersions = new Set(tags.map((t) => t.slice(1)));

    for (const ms of milestones) {
      const versionMatch = ms.title.match(/^v(\d+\.\d+)/);
      const isUnslotted = /\(unslotted\)/i.test(ms.title);

      // Rule: a release milestone names its map issue; a theme carries no
      // version number. Milestone #1's title said "v1.4" for a scope that
      // never shipped as v1.4 — that is exactly this check.
      if (versionMatch && !/Map:\s*#\d+/i.test(ms.description ?? '')) {
        warn(
          `Milestone "${ms.title}" is version-numbered but names no map issue.`,
          `Add "Map: #N" to its description, or drop the version number and mark it (unslotted).`,
        );
      }
      if (!versionMatch && !isUnslotted && ms.state === 'open') {
        warn(
          `Milestone "${ms.title}" has neither a version number nor an "(unslotted)" marker.`,
          `A milestone is either a release or an explicitly unslotted theme. Pick one.`,
        );
      }

      // Rule: a version does not ship while its milestone has open issues.
      if (versionMatch) {
        const shipped = [...taggedVersions].some((v) => v.startsWith(`${versionMatch[1]}.`));
        if (shipped && ms.open_issues > 0) {
          fail(
            `Milestone "${ms.title}" has ${ms.open_issues} open issue(s), but ${versionMatch[0]} is already tagged.`,
            `Either the issues shipped and should be closed, or they did not and belong on a later milestone — retitle this one (unslotted) if it is a theme rather than a release.`,
          );
        }
        if (shipped && ms.state === 'open' && ms.open_issues === 0) {
          warn(
            `Milestone "${ms.title}" is complete and its version is tagged, but the milestone is still open.`,
            `gh api -X PATCH repos/:owner/:repo/milestones/${ms.number} -f state=closed`,
          );
        }
      }
    }
  }

  // Rule: a closed issue carries the milestone of the release that shipped it.
  // Without this the audit trail is blank, which is how v1.4 ended up with
  // 0 closed issues against a shipped release.
  const closedNoMilestone = ghJson([
    'issue', 'list', '--state', 'closed', '--limit', '100',
    '--json', 'number,title,milestone,closedAt',
  ]);
  if (closedNoMilestone) {
    const orphans = closedNoMilestone
      .filter((i) => i.milestone === null)
      // Issues closed before the process existed are not actionable history.
      .filter((i) => Date.parse(i.closedAt) >= Date.parse('2026-08-11T00:00:00Z'));
    for (const i of orphans) {
      fail(
        `Closed issue #${i.number} ("${i.title.slice(0, 60)}") carries no milestone.`,
        `gh issue edit ${i.number} --milestone "<the release that shipped it>"`,
      );
    }
  }

  const openNoMilestone = ghJson([
    'issue', 'list', '--state', 'open', '--limit', '100', '--json', 'number,title,milestone',
  ]);
  if (openNoMilestone) {
    for (const i of openNoMilestone.filter((x) => x.milestone === null)) {
      warn(
        `Open issue #${i.number} ("${i.title.slice(0, 60)}") is unrouted.`,
        `Put it on a release milestone or an (unslotted) theme.`,
      );
    }
  }
}

// ------------------------------------------------------------------- report

const bullet = (items, label) => {
  if (!items.length) return;
  console.log(`\n${label}`);
  for (const { msg, fix } of items) {
    console.log(`  • ${msg}`);
    console.log(`    ↳ ${fix}`);
  }
};

console.log('release-audit');
for (const n of notices) console.log(`  note: ${n}`);

bullet(errors, `FAIL (${errors.length})`);
bullet(warnings, `WARN (${warnings.length})`);

if (!errors.length && !warnings.length) {
  console.log('  release record is consistent.');
}
console.log('\nRules: docs/RELEASE_PROCESS.md');

process.exit(errors.length ? 1 : 0);
