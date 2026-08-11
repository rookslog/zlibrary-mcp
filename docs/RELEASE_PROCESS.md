# Release process

Enforced by `npm run audit:release` ([scripts/release-audit.mjs](../scripts/release-audit.mjs)),
which runs inside `npm run doctor` and weekly in CI. This document is the reasoning; the
script is the enforcement. If you change a rule here, change the check there in the same
pass.

## The five rules

1. **A release is a milestone.** Not a document, not an issue, not a heading in a roadmap.
   If it does not have a milestone, it is not a release.
2. **A release milestone names its map issue** — literally `Map: #N` in its description.
   The milestone is the index of what shipped; the map issue is the narrative of why.
3. **A theme without a release slot carries no version number in its title.** It is marked
   `(unslotted)` instead. Version numbers are promises about ordering; do not make one you
   have not decided.
4. **Every closed issue carries the milestone of the release that shipped it.** Assignment
   is decided by the CHANGELOG: an issue belongs to the release whose CHANGELOG section
   describes its outcome. Work closed after a tag but not yet released goes on the *next*
   release's milestone.
5. **A version does not ship while its milestone has open issues.** Move them or close
   them; do not tag around them.

Two corollaries the script also checks:

- **A tag that shipped has a GitHub Release.** npm and GHCR are distribution. The Releases
  page is the record a human reads, and it is the one that silently falls behind.
- **`## [Unreleased]` is written as work lands**, not reconstructed from `git log` at
  release time.

## The neglect rules

Drift is the record contradicting what shipped. Neglect is finished work rotting in
place. Same silence, so the same script checks both.

6. **An open pull request is a defect with a clock on it, not a queue entry.** Warned at
   7 days without activity, failed at 14. Merge it, close it, or say on the thread what
   it is waiting for — those are the three options, and leaving it is not among them.
7. **A branch does not outlive its pull request.** `delete_branch_on_merge` has been on
   since 2026-08-11; the check catches stragglers and anything merged by a route that
   bypasses it. A remote branch with no PR at all and no commits for 14 days fails.
8. **A local branch older than 14 days is a smell** — merge it or delete it. Checked
   outside CI only, since it is the author's own workspace.

**Why a clock at all.** PR #48 was opened by an outside contributor on 2026-07-25 with
all checks green and no conflicts. It was still cleanly mergeable a week later. By the
time the tracker was audited on 2026-08-11 it was `CONFLICTING` — the base had moved
under it, and careful work by someone with no commit access had been destroyed by nothing
more than seventeen days of silence. Staleness is not a neutral holding state.

`npm run doctor` runs the upstream contract check first and this audit second, so a stale
PR can never mask upstream drift — those are unrelated failures and the more urgent one
goes first.

## Why these rules exist

On 2026-08-11 the tracker was audited and found in this state:

- Two artifacts both defined "v1.4". Milestone #1, `v1.4 — Source layer as a first-class
  citizen`, came from the 2026-07-24 roadmap and scoped adapter work (#39, #40, #51, #52,
  #53). Map issue #75, `v1.4 — Any source can be downloaded`, was charted 2026-08-10 and
  scoped LibGen downloads. **The second one shipped, as v1.4.0.**
- Nobody reconciled them. The milestone was never renamed, rescoped or closed, so a
  released version showed **5 open / 0 closed** — while every issue that actually fed the
  release (#73, #74, #76, #77, #79, #80, #81) carried no milestone at all.
- The binding that would have prevented this already existed, applied exactly once:
  milestone #3's description read `Map: #95`. It was written down nowhere and enforced by
  nothing, so v1.5 was reproducing the same duplicate pair at the moment of the audit.
- Standing decisions lived only inside closed-issue bodies. #75 recorded "Z-Library-as-adapter
  (#40) is out of v1.4; migration is v1.5 work." #40 was still sitting on the v1.4 milestone.
- `v1.4.0` was tagged and live on npm and GHCR while the Releases page still showed v1.3.2
  as latest (#108). `v1.2.0` had the same gap and nobody had noticed in four months.
- `## [Unreleased]` was empty with 17 commits behind it.

Every rule above is one of those failures, inverted.

## Cutting a release

1. Confirm the milestone is at **0 open issues**, and that its description names its map issue.
2. Write the `## [x.y.z]` CHANGELOG section, promoting what is under `## [Unreleased]`.
   Release notes are generated from this section — an absent section fails the release job
   rather than producing an empty one.
3. Bump `version` in `package.json`.
4. `npm run audit:release` — it must exit 0.
5. Commit, merge to `master`, then tag `vX.Y.Z` and push the tag.
6. `publish.yml` then publishes to npm (OIDC trusted publishing), pushes the GHCR image,
   **and creates the GitHub Release** from the CHANGELOG section.
7. Close the milestone. Close the map issue with its outcome recorded in the body.

## Opening the next release

1. Create the milestone `vX.Y — <destination in one clause>`.
2. Create the map issue with the same destination, and put `Map: #N` in the milestone
   description.
3. Put the map issue on its own milestone.
4. Anything already closed-but-unreleased goes on this milestone immediately.

## Milestones as of 2026-08-11

| Milestone | Kind | Map |
|---|---|---|
| `v1.4 — Any source can be downloaded` (closed) | release, shipped 2026-08-10 | #75 |
| `v1.5 — Anna's Archive is a real source` | release, open | #95 |
| `Source layer as a first-class citizen (unslotted)` | theme, no slot | — |
| `Lightweight core (unslotted)` | theme, no slot | — |
