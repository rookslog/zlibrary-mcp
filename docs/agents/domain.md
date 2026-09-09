# Domain documentation

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

The deferred browser-resident route remains required follow-up work. Deferral
from a release is not rejection of the route. Machine-solved challenges and
cookie transplantation retain their existing boundaries in `AGENTS.md`.
