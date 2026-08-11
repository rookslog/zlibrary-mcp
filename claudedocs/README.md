# claudedocs/ — Working Documentation

Internal working notes for development sessions: architecture analyses, session
handoffs, and research findings. Kebab-case naming; timestamped when the content
is a point-in-time record (see the documentation-guidelines table in
[CLAUDE.md](../CLAUDE.md) for what goes where).

## Current contents

- [architecture/repo-health-and-roadmap-2026-07-24.md](architecture/repo-health-and-roadmap-2026-07-24.md)
  — the live health assessment and forward roadmap (the "current plan" document)
- [architecture/phase-20-21-review-2026-07-24.md](architecture/phase-20-21-review-2026-07-24.md)
  — acceptance criteria for the pending RAG quality-scoring work
  (referenced by issue [#39](https://github.com/rookslog/zlibrary-mcp/issues/39))
- [milestone-history.md](milestone-history.md) — condensed record of all
  development milestones from v1.0 through v1.3
- [session-notes/2025-10-28-footnote-continuation-state-machine.md](session-notes/2025-10-28-footnote-continuation-state-machine.md)
  — design notes behind the footnote-continuation detector
  (referenced by `docs/FOOTNOTE_CONTINUATION_QUICKSTART.md`)

## Where did everything else go?

This directory was pruned on 2026-07-24: ~170 files of dated session notes,
phase reports, exploration logs, and research from 2025 were removed from the
working tree. Nothing is lost — retrieve any of it from git history
(`git log --all --oneline -- claudedocs/` and `git show <commit>:<path>`), or
from the maintainer's offline archive.

New session notes and research go here as before; expect documents in this
directory to be pruned once they stop being load-bearing.
