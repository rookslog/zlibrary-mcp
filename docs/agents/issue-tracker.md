# Issue tracker: GitHub

GitHub issues and PRs are the shared discussion surface. This document describes
optional maintainer automation; contributors can use the GitHub UI and follow
[CONTRIBUTING.md](../../CONTRIBUTING.md) without installed skills or the `gh` CLI.
Labeling, assignment, dependencies and milestones are maintainer responsibilities.
The commands below are examples for authorized tracker operations, not permission
to publish, close or assign work. Small fixes can go directly to a PR.

## Conventions

- **Create an issue**: prepare a UTF-8 body file, then use `gh issue create --title "..." --body-file <path>`. Preserve actual newlines; never interpolate untrusted text into shell code.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body-file <path>`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: yes.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

Evaluate external PRs as submitted work. Do not require their authors to create
a separate issue, adopt Wayfinder or reproduce a planning map. Use issue labels
only where helpful; GitHub review status governs review readiness. Commands:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Optional for substantial planning: a **map** is an issue linking related questions
and tasks; **children** are its individual issues. The same organization can be
maintained manually. Keep unresolved decisions distinct from implementation
briefs, and do not create a map for every contribution.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` when authorized to take the work; an assignee is not an exclusive lock.
- **Resolve**: `gh issue comment <n> --body-file <path>`, close only when its completion criteria are met (and any required human decision is recorded), then append a context pointer (gist + link) to the map's Decisions-so-far.

## Release planning and publication

Use existing milestone/map pairs before creating another release slot. A release
milestone description contains `Map: #N`; the map belongs to that milestone.
Unslotted themes carry no version promise. Consult `docs/RELEASE_PROCESS.md`.

When scope changes, update the milestone, map, affected issue memberships and
supersession notes together. Store before-state receipts and intended changes
in a dated planning artifact. Query child issues and native blockers to find
the frontier; do not maintain a duplicate open-ticket list when native
sub-issues are available.
If using the task-list fallback above, keep that list authoritative and current.

Implementation readiness requires an accepted behavioral brief, not just a
milestone assignment or routing label. See [triage guidance](triage-labels.md).
Explicit human decisions remain open until the human answers. Do not merge PRs, enable auto-merge, cut releases, request new reviews, or discard
dirty work as a consequence of planning alone.
