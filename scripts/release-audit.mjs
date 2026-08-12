#!/usr/bin/env node
/**
 * release-audit — checks that the project's record of itself is still true.
 *
 * Two failure classes, one shape. Both are silent: nothing goes red, CI stays
 * green, and the repo quietly stops describing reality.
 *
 * DRIFT — the record contradicts what shipped. Written after the v1.4 drift
 * (2026-08-11), when two artifacts both claimed to define "v1.4": milestone #1
 * ("Source layer as a first-class citizen", from the 2026-07-24 roadmap) and
 * map issue #75 ("Any source can be downloaded", charted 2026-08-10). The
 * second shipped. Nobody reconciled them, so a released version sat at 5 open
 * / 0 closed while every issue that actually fed the release carried no
 * milestone at all, and the Releases page still showed v1.3.2 as latest.
 *
 * NEGLECT — work rots in place. PR #48 was green and conflict-free the day it
 * was opened by an outside contributor, and `CONFLICTING` seventeen days later
 * purely from sitting. Staleness is not a neutral holding state; it destroys
 * work that was already finished.
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

/**
 * Neglect thresholds, in days.
 *
 * STALE_PR_WARN is a nudge; STALE_PR_FAIL is where the weekly job starts
 * filing an issue about it. 14 is chosen against the case that motivated the
 * check: #48 was still cleanly mergeable at 7 days and had rotted by 17.
 * BRANCH_AGE matches the two-week branch rule already in the global guidance.
 *
 * PR age is measured from creation, so the clock cannot be reset by commenting
 * — see the note above the check itself.
 */
const STALE_PR_WARN = 7;
const STALE_PR_FAIL = 14;
const BRANCH_AGE = 14;

const DAY_MS = 86_400_000;
const daysSince = (iso) => Math.floor((Date.now() - Date.parse(iso)) / DAY_MS);

/**
 * When the milestone + map-issue scheme took effect (the day v1.4.0 shipped).
 *
 * Releases before this date were cut without it, so demanding a milestone for
 * v1.2.0 or v1.3.x would report five failures nobody can act on — and an audit
 * that cries wolf about history is one people learn to ignore, which costs more
 * than the check is worth. Rules bind from the day they are adopted.
 */
const PROCESS_START = Date.parse('2026-08-10T00:00:00Z');

/**
 * When issues were retroactively assigned to their release milestones. Distinct
 * from PROCESS_START: the scheme began on the 10th, the backfill happened on
 * the 11th, and issues closed in between were swept up by hand.
 */
const MILESTONE_BACKFILL = Date.parse('2026-08-11T00:00:00Z');

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

/**
 * Tags shaped `vX.Y.Z`, in version order. Older tags like `v1.2` predate the
 * convention.
 *
 * `--sort=v:refname` is load-bearing, not tidiness. Git lists tags
 * lexicographically unless `tag.sort` is configured, which puts `v1.10.0`
 * *before* `v1.9.0` — so "the last element" would silently become the wrong tag
 * the moment a minor version reaches two digits, and the [Unreleased] check
 * below would count commits since the wrong release.
 */
const tags = (tryRun('git', ['tag', '--list', 'v*', '--sort=v:refname']) ?? '')
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

/** Set when an authenticated GitHub query fails, meaning coverage is incomplete. */
let ghBroken = false;

if (!ghReady) {
  // Locally, no `gh` is an ordinary state: run the CHANGELOG and tag checks and
  // say plainly that the rest did not run. In CI it is a failure, because the
  // only reasons to reach here are a revoked token, a missing secret, or a
  // GitHub auth outage — and skipping every GitHub check while exiting 0 is the
  // same false green that `ghJson` was made to fail closed on. Fixing that after
  // authentication while leaving this path open closed half the hole.
  if (process.env.CI) {
    ghBroken = true;
    fail(
      'gh is unavailable or unauthenticated, so NO GitHub check ran in CI.',
      'Check the job\'s token and permissions. This is a failed audit, not a clean one.',
    );
  } else {
    notices.push('gh is unavailable or unauthenticated — GitHub checks skipped (local checks still ran).');
  }
} else {
  /**
   * Once `gh` is authenticated, a failed query is a FAILED AUDIT — never a
   * skipped check.
   *
   * This previously returned null on any error and each caller skipped its
   * check, so a rate limit, a transient 5xx, or a token missing a scope made
   * the audit examine nothing and print "release record is consistent". A
   * checker that reports success when it could not look is the exact
   * silent-wrongness failure this tool exists to catch, reproduced inside the
   * tool. It must fail loudly and fail closed.
   */
  const ghJson = (args, what) => {
    let out;
    try {
      out = run('gh', args);
    } catch (e) {
      ghBroken = true;
      const detail = (e.stderr || e.message || '').toString().trim().split('\n')[0];
      fail(
        `GitHub query failed, so "${what}" was NOT checked: ${detail || 'unknown error'}`,
        `Re-run once the API is reachable. This is a failed audit, not a clean one — the check did not run.`,
      );
      return null;
    }
    try {
      return JSON.parse(out);
    } catch {
      ghBroken = true;
      fail(
        `GitHub returned unparseable JSON, so "${what}" was NOT checked.`,
        `Check the gh version and the query. Treat this run as having verified nothing.`,
      );
      return null;
    }
  };

  // Rule: a tag that shipped has a GitHub Release. npm and GHCR are not the
  // record; the Releases page is what a human reads.
  //
  // Drafts are excluded deliberately: `gh release list` includes them, but a
  // draft is invisible to everyone except maintainers, so counting one as
  // "released" would let the Releases page stay blank while the audit passed.
  const releases = ghJson(
    ['release', 'list', '--limit', '100', '--json', 'tagName,isDraft'],
    'every tag has a published GitHub Release',
  );
  if (releases) {
    const released = new Set(releases.filter((r) => !r.isDraft).map((r) => r.tagName));
    const drafts = new Set(releases.filter((r) => r.isDraft).map((r) => r.tagName));
    for (const tag of tags) {
      if (!released.has(tag)) {
        const draftNote = drafts.has(tag)
          ? ' Its Release exists but is still a DRAFT, so nobody else can see it.'
          : '';
        fail(
          `Tag ${tag} was pushed but has no published GitHub Release.${draftNote}`,
          drafts.has(tag)
            ? `gh release edit ${tag} --draft=false`
            : `gh release create ${tag} --verify-tag --notes-file <changelog section>  (publish.yml now does this automatically)`,
        );
      }
    }
  }

  const milestones = ghJson(
    ['api', 'repos/:owner/:repo/milestones?state=all&per_page=100'],
    'milestones are well-formed and match the tags',
  );
  if (milestones) {
    const taggedVersions = new Set(tags.map((t) => t.slice(1)));

    // Rule 1, checked in the direction that actually matters: a release is a
    // milestone. Iterating milestones and comparing against tags catches a
    // malformed milestone but never a missing one, so a version could be
    // tagged with no milestone at all and the audit would pass in silence.
    const slots = new Map(); // "1.5" -> [milestone titles]
    for (const ms of milestones) {
      const m = ms.title.match(/^v(\d+\.\d+)/);
      if (m) slots.set(m[1], [...(slots.get(m[1]) ?? []), ms.title]);
    }
    for (const version of taggedVersions) {
      const tagged = tryRun('git', ['log', '-1', '--format=%cI', `v${version}`]);
      if (!tagged || Date.parse(tagged) < PROCESS_START) continue;

      const line = version.split('.').slice(0, 2).join('.');
      if (!slots.has(line)) {
        fail(
          `v${version} is tagged but no milestone claims v${line}.`,
          `A release is a milestone. Create "v${line} — <destination>" with "Map: #N" in its description and put the shipped issues on it.`,
        );
      }
    }

    // Rule: one release slot, one milestone. Two well-formed milestones both
    // claiming v1.5 would each pass every per-milestone check below — and two
    // milestones claiming one version is the precise state that motivated this
    // whole audit, so missing it would be the sharpest possible gap.
    for (const [line, titles] of slots) {
      if (titles.length > 1) {
        fail(
          `${titles.length} milestones claim release slot v${line}: ${titles.map((t) => `"${t}"`).join(', ')}.`,
          `One of them is the release. Retitle the others "(unslotted)" and drop their version number, or merge them.`,
        );
      }
    }

    for (const ms of milestones) {
      const versionMatch = ms.title.match(/^v(\d+\.\d+)/);
      const isUnslotted = /\(unslotted\)/i.test(ms.title);

      // Rule 2, and it fails rather than warns. This is THE invariant the
      // audit was built for: milestone #1 carried "v1.4" for a scope that never
      // shipped as v1.4, with nothing binding it to a map issue. As a warning
      // it neither failed the run nor filed the tracking issue, so the drift it
      // exists to prevent could persist while the job stayed green — visible
      // only to someone reading the logs of a passing build.
      if (versionMatch && !/Map:\s*#\d+/i.test(ms.description ?? '')) {
        fail(
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
  // --limit caps how many issues are fetched, not how many are examined after
  // filtering. At 100 the repo would silently outgrow the check: an older
  // post-backfill issue could lose its milestone and never be looked at, while
  // the audit kept reporting success. Ask for far more than the repo has, and
  // say so if the cap is ever reached rather than assuming full coverage.
  const CLOSED_ISSUE_LIMIT = 1000;
  const closedIssues = ghJson(
    ['issue', 'list', '--state', 'closed', '--limit', String(CLOSED_ISSUE_LIMIT),
      '--json', 'number,title,milestone,closedAt'],
    'closed issues name the release that shipped them',
  );
  if (closedIssues?.length === CLOSED_ISSUE_LIMIT) {
    ghBroken = true;
    fail(
      `Closed-issue query hit its ${CLOSED_ISSUE_LIMIT}-issue cap, so older issues were NOT checked.`,
      'Raise CLOSED_ISSUE_LIMIT or paginate. Coverage is incomplete until then.',
    );
  }
  if (closedIssues) {
    // Issues closed before the process existed are not actionable history.
    const inScope = closedIssues.filter((i) => Date.parse(i.closedAt) >= MILESTONE_BACKFILL);
    for (const i of inScope) {
      const title = i.title.slice(0, 60);
      if (i.milestone === null) {
        fail(
          `Closed issue #${i.number} ("${title}") carries no milestone.`,
          `gh issue edit ${i.number} --milestone "<the release that shipped it>"`,
        );
        continue;
      }
      // A non-null milestone is not the same as an answer. An issue closed
      // against an (unslotted) theme leaves the audit trail exactly as
      // ambiguous as no milestone at all: the theme names no release, so
      // nothing records which version the work reached users in. Warn rather
      // than fail — closing an issue against a theme is legitimate, and the
      // release milestone may not exist yet.
      if (!/^v\d+\.\d+/.test(i.milestone.title)) {
        warn(
          `Closed issue #${i.number} ("${title}") is on "${i.milestone.title}", which is not a release.`,
          `A closed issue should name the release that shipped it. Move it once that release has a milestone.`,
        );
      }
    }
  }

  const openNoMilestone = ghJson(
    ['issue', 'list', '--state', 'open', '--limit', '100', '--json', 'number,title,milestone'],
    'open issues are routed to a milestone',
  );
  if (openNoMilestone) {
    for (const i of openNoMilestone.filter((x) => x.milestone === null)) {
      warn(
        `Open issue #${i.number} ("${i.title.slice(0, 60)}") is unrouted.`,
        `Put it on a release milestone or an (unslotted) theme.`,
      );
    }
  }

  // ------------------------------------------------------------------ neglect

  const prs = ghJson(
    ['pr', 'list', '--state', 'all', '--limit', '200',
      '--json', 'number,state,headRefName,createdAt,updatedAt,isDraft,title,author,mergeable,isCrossRepository'],
    'no pull request or branch has been left to rot',
  ) ?? [];

  // Rule: an aging PR is a defect with a clock on it, not a queue entry.
  //
  // The clock runs from createdAt, not updatedAt. An open PR has by definition
  // never merged, and what rots it is elapsed time with the base moving
  // underneath — not silence. Keying off activity meant any comment reset the
  // clock while the PR stayed exactly as unlanded as before: the failure mode
  // wearing the costume of a fix. Observed live on 2026-08-11, when rebasing
  // #48 cleared this check on the strength of the comment rather than the merge.
  //
  // The consequence is deliberate: a PR under active review still fails at 14
  // days. "It is being discussed" is not a defence against having been open a
  // fortnight. Last-activity is reported alongside so the reader can tell a
  // moving PR from an abandoned one, but it does not change the verdict.
  for (const pr of prs.filter((p) => p.state === 'OPEN' && !p.isDraft)) {
    const age = daysSince(pr.createdAt);
    if (age < STALE_PR_WARN) continue;

    const quiet = daysSince(pr.updatedAt);
    const activity = quiet >= 1 ? `last activity ${quiet}d ago` : 'active today';
    const conflicting = pr.mergeable === 'CONFLICTING' ? ', already CONFLICTING' : '';
    const who = pr.author?.login ?? 'unknown';
    const msg =
      `PR #${pr.number} ("${pr.title.slice(0, 50)}", @${who}) has been open ${age} days (${activity}${conflicting}).`;
    const fixIt =
      `Land it or close it. A comment does not clear this — only merging or closing does.`;
    if (age >= STALE_PR_FAIL) fail(msg, fixIt);
    else warn(msg, fixIt);
  }

  // Rule: a branch outlives its PR only by accident. `delete_branch_on_merge`
  // was switched on 2026-08-11, so this catches pre-existing stragglers and any
  // branch merged by a route that bypasses the setting.
  const remoteBranches = (tryRun('git', [
    'for-each-ref', '--format=%(refname:short)|%(committerdate:iso8601)', 'refs/remotes/origin',
  ]) ?? '')
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [ref, date] = line.split('|');
      return { name: ref.replace(/^origin\//, ''), date };
    })
    .filter((b) => b.name !== 'HEAD' && b.name !== 'master');

  if (!remoteBranches.length) {
    notices.push('no remote branch refs found — branch checks skipped (shallow clone?).');
  }

  // Every branch verdict below is derived from `prs`. If that query failed it
  // is an empty array, and each branch would be reported as having no PR at all
  // — a page of confident, fabricated findings. The query failure is already
  // recorded as an error; do not compound it with invented ones.
  for (const branch of ghBroken ? [] : remoteBranches) {
    // Match on this repository's PRs only. A fork PR carries the contributor's
    // own branch name, and names collide — `master`, `main`, a shared fix/
    // slug. Counting one as this branch's open PR would let a dead origin
    // branch skip the age check for as long as the fork PR stayed open.
    const branchPrs = prs.filter((p) => !p.isCrossRepository && p.headRefName === branch.name);
    const merged = branchPrs.some((p) => p.state === 'MERGED');
    const hasOpen = branchPrs.some((p) => p.state === 'OPEN');

    if (merged && !hasOpen) {
      warn(
        `Branch origin/${branch.name} is still on the remote although its PR merged.`,
        `git push origin --delete ${branch.name}`,
      );
      continue;
    }
    if (hasOpen) continue; // the stale-PR check above owns this one

    // No PR at all. Dependabot opens and closes these on its own schedule, so
    // only flag a branch that has also gone quiet.
    const age = daysSince(branch.date);
    if (age >= BRANCH_AGE) {
      fail(
        `Branch origin/${branch.name} has no pull request and its last commit was ${age} days ago.`,
        `Open a PR for it, or delete it: git push origin --delete ${branch.name}`,
      );
    }
  }
}

// Local branches are the author's own workspace, so this runs only outside CI,
// where "delete it" is advice the reader can act on immediately.
if (!process.env.CI) {
  const localBranches = (tryRun('git', [
    'for-each-ref', '--format=%(refname:short)|%(committerdate:iso8601)', 'refs/heads',
  ]) ?? '')
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [name, date] = line.split('|');
      return { name, date };
    })
    .filter((b) => b.name !== 'master');

  for (const b of localBranches) {
    const age = daysSince(b.date);
    if (age >= BRANCH_AGE) {
      warn(
        `Local branch ${b.name} is ${age} days old.`,
        `A branch older than ${BRANCH_AGE} days is a smell — merge it or delete it.`,
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

// "Consistent" is a claim about what was checked, so it may only be made when
// everything was in fact checked. A run that could not reach GitHub knows less
// than a clean run and must never be reported as one.
if (ghBroken) {
  console.log('\n  INCOMPLETE — one or more GitHub checks did not run. This is not a clean audit.');
} else if (!errors.length && !warnings.length) {
  console.log('  release record is consistent.');
}
console.log('\nRules: docs/RELEASE_PROCESS.md');

process.exit(errors.length ? 1 : 0);
