# Maintainer triage guidance

Maintainers organize issues and PRs; contributors do not need to apply labels
or run a triage skill. Use categories such as `bug`, `enhancement`,
`documentation` or `question` when useful. A small fix does not need a complete
label set or a separate planning ticket.

Describe task maturity independently of who will do the work:

| State | Meaning |
|---|---|
| Needs triage | The request still needs evaluation |
| Needs information | Specific missing evidence is needed; say what |
| Needs decision | A scope or design choice remains; name the decision-maker |
| Ready | The intended behavior, scope and relevant acceptance checks are agreed |

These are plain-language states, not a requirement to install new labels.
Existing `needs-triage` and `needs-info` labels can help surface waiting work;
record the specific decision or missing evidence in the issue. Use `wontfix`
only for explicit rejection, not deferral. Keep assignment and PR review status
separate from task maturity. Readiness is not merge approval.

## Compatibility with existing automation

Existing `ready-for-agent` and `ready-for-human` labels are legacy routing hints,
not separate contribution tracks. `ready-for-agent` requires an accepted brief;
`ready-for-human` identifies work needing human involvement, not review or merge
clearance. Inspect the issue and GitHub reviews rather than inferring acceptance
from either label. Do not bulk relabel existing work as part of routine triage.

Optional Wayfinder maps use `wayfinder:map`; children use `wayfinder:grilling`,
`wayfinder:prototype`, `wayfinder:research` or `wayfinder:task`. Explain each
question or task in ordinary language. Dependencies identify blockers, while
assignment records who is working on it. A decision ticket is not a build slice.

When authorized to publish automated triage, identify it as an automated
assessment and distinguish recommendations from maintainer decisions. No
external skill-specific prefix is required. Keep sensitive findings in the
private reporting channel described by [SECURITY.md](../../SECURITY.md).
For PRs, preserve actionable review findings and check reviews against the
current head; missing review output is not clearance.
