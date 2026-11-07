# Vision

zlibrary-mcp is the **acquisition layer** for research corpora: it finds books,
retrieves them, and turns them into files a pipeline can use. It serves a
researcher assembling a corpus — and any downstream tool that needs books —
through the Model Context Protocol.

This document states the commitments that make the project what it is.
Implementations, sources, and even the project's name are revisable; the
invariants below change only deliberately (see [Amending this
document](#amending-this-document)). Everything else — the current roadmap, the
current adapters, the current endpoints — is contingent and expected to change.

## Invariants

1. **Files, not payloads.** Output is paths to durable artifacts on disk, never
   raw document text pushed through the context window.
2. **stdout is the protocol.** Under the stdio transport, stdout carries
   JSON-RPC and nothing else; all diagnostics go to stderr. (Enforced by
   `__tests__/stdio-purity.test.js`.)
3. **Per-user credentials and quotas.** Accounts, limits, and downloaded
   content belong to the user running the server. A local user may configure
   multiple personally controlled accounts and rotate acquisitions among them.
4. **Sources are adapters; no source is privileged.** Z-Library is one adapter
   among several, not the essence of the project. The adapter contract must
   never be secretly shaped like any single source.
5. **Acquisition is idempotent.** The server knows what it has already acquired
   and processed, and does not re-fetch it. A corpus accretes; asking twice
   costs nothing. *(Partially realized today — the downloads directory exists,
   a queryable manifest does not yet.)*
6. **Drift is detected, not assumed.** Every upstream surface that can break
   silently gets a probe that notices (scheduled upstream checks, `npm run
   doctor`). Green unit tests are not evidence that the world hasn't moved.
7. **Heavy dependencies stay optional.** OCR models and similar machinery must
   never be the price of basic search-and-download.

## Non-goals

Identity is sharpest at its boundary. This project deliberately does **not** do:

- **Library management.** Organizing, curating, browsing, and reading a
  collection belong downstream, in a dedicated library manager. This server
  maintains a *manifest* of what it acquired — enough for idempotence and
  offline reuse — but a manifest is not a library.
- **A hosted or shared instance.** Credentials and quotas are per-user
  (invariant 3); sharing or pooling credentials across users remains out of scope.
- **Bundled OCR models.** OCR remains an optional integration (invariant 7).
- **Direct ID lookup against unstable upstream IDs.** Deprecated by
  [ADR-003](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/adr/ADR-003-Handle-ID-Lookup-Failure.md) for reasons that
  have not changed.

## How releases relate to this document

Each release has a theme, and the theme is a concrete expression of one or more
invariants. The milestone description opens by naming which. For example, v1.4
("source layer as a first-class citizen") realizes invariant 4; a future
manifest/resources release would realize invariant 5.

A proposed feature that serves no invariant — or that serves a non-goal — needs
either a rejection or an amendment, not a quiet merge.

## Amending this document

By pull request only. The PR description must name which invariant or non-goal
is being revised and explain why the reason it was adopted no longer holds.
Drift by silence is not amendment.
