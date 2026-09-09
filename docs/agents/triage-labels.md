# Triage labels

Each triaged issue has one category and one state. Keep other useful labels.

| Canonical role | GitHub label | Meaning |
|---|---|---|
| bug | bug | Existing behavior is broken |
| enhancement | enhancement | New behavior or an improvement |
| needs-triage | needs-triage | Evaluation or a design decision remains |
| needs-info | needs-info | Waiting for specific missing information |
| ready-for-agent | ready-for-agent | A complete behavioral brief permits bounded implementation |
| ready-for-human | ready-for-human | Human implementation, judgment or manual testing is required |
| wontfix | wontfix | Explicitly rejected, not merely deferred |

Wayfinder maps also use `wayfinder:map`; their decision children use
`wayfinder:grilling`, `wayfinder:prototype`, `wayfinder:research`, or
`wayfinder:task`. A decision ticket is not a build slice. Native issue
dependencies govern the frontier independently of triage labels.

Follow the triage skill's disclosure prefix for tracker content created during
triage. Keep sensitive security findings in the private reporting channel
described by `SECURITY.md`; a planning invocation does not settle disclosure.

For external PRs, `ready-for-human` can indicate human review/merge readiness;
it never grants an agent permission to merge. Missing review output is no
clearance. Preserve existing review findings and exact-head gates.
