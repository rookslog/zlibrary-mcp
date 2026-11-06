## What this changes

<!-- What behaviour differs after this PR, and why. -->

## Why

<!-- The problem being solved. Link the issue if there is one. -->

## How it was verified

<!--
Say what you actually ran. If a check was skipped, say that too — reviewers can
weigh a known gap, but not an unknown one.
-->

- [ ] `npm run build`
- [ ] `node --experimental-vm-modules node_modules/jest/bin/jest.js`
- [ ] `uv run pytest -m "not slow and not integration and not performance"`
- [ ] Tests added or updated for the change

## Risk

<!-- What could this break, and for which source or client? "None" is a fine answer for a docs fix. -->

## How to undo it

<!-- Usually "revert this PR". Say so if it's harder: saved state, a migration, a published artifact. -->

## Notes for reviewers

<!--
Anything worth flagging: a trade-off you made, a decision you were unsure about,
an area you would like a closer look at.

If you touched any of these, please note it explicitly:
  - `src/` logging — stdout is the JSON-RPC channel; diagnostics must use
    `logger` (stderr). A `console.log` in `src/` breaks stdio clients.
  - Python bridge output shape — the RAG bundle contract is consumed by MCP
    clients; additive fields only.
  - Dependency floors in `pyproject.toml` — see SECURITY.md for the audit policy.
-->
