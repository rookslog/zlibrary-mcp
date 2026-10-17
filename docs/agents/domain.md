# Domain guidance for maintainers and assistants

This guidance can be followed without installing any skills.

This repository has one domain context. Read `CONTEXT.md` for source, adapter,
route, provenance, acquisition and processing terminology. Read `VISION.md`
before architectural proposals and relevant decisions under `docs/adr/` before
changing a contract. Use `AGENTS.md` as the shared agent contract; `CLAUDE.md`
imports it.

ADRs describe decisions, not implementation completion. Check the current code
and tracker before treating an accepted ADR or a closed issue as shipped work.
ADR-011 governs the source-adapter migration. Its opaque source references and
optional capabilities must not be silently replaced by a universal MD5 or by
mandatory Z-Library-only methods.

Keep `CONTEXT.md` a glossary when updating domain terms. Decisions with lasting
trade-offs belong in ADRs; release scope belongs in GitHub milestones/map
issues. Dated material under `claudedocs/` is evidence or a snapshot, not a
second release plan. Preserve alternative readings until the owner resolves
them. Skills must not answer human-in-the-loop decision tickets for the owner.

For current scheduling, consult GitHub milestones and their map issues.
Deferring work from a release does not reject it or amend its architectural
constraints. Source-access boundaries remain in `AGENTS.md` and `CONTEXT.md`;
this guide does not maintain a second copy of the release plan.
