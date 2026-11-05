# Deployment Documentation

This directory retains deployment guides and validation reports from the older
interface. Start with the current [README](../../README.md) for installation,
configuration and the five public MCP tools: `search`, `get_book`, `download`,
`get_limits` and `get_history`. See the [interface migration](../interface-migration.md)
for saved-call and download-location changes.

## Historical references

- **DEPLOYMENT_CHECKLIST.md** — Older deployment procedure; compare each step with
  the current README before using it.
- **DEPLOYMENT_READINESS_AND_IMPROVEMENTS.md** — Historical readiness analysis and
  recommendations, not acceptance evidence for the current implementation.
- **ALL_TOOLS_VALIDATION_MATRIX.md** — Historical validation of the former eleven-tool
  interface. It does not establish that the current five tools work.

## Current checks

Use the README and `AGENTS.md` for contributor commands. `npm test` runs Jest with
coverage; pytest is a separate suite. Unit and offline rehearsal tests do not
establish live provider availability. A historical validation matrix is not a
current deployment receipt.
