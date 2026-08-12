# CLAUDE.md

The working contract for this repo is [AGENTS.md](AGENTS.md), shared by every coding
agent. It is imported below rather than restated — this file used to carry its own copy
of the architecture, the commands and the roadmap, and that copy is what drifted. One
canonical version, or it happens again.

@AGENTS.md

---

## Claude Code specifics

Everything above applies. These are the parts that only matter inside Claude Code.

### Reading order for a cold start

`AGENTS.md` covers the working contract. When a task needs more depth, go in this order
and stop when you have enough:

1. `VISION.md` — invariants and non-goals
2. `.claude/ARCHITECTURE.md` — components and their status
3. `ISSUES.md` — known problems, by severity
4. `.claude/PATTERNS.md` — error handling, logging, caching, testing patterns
5. `.claude/DEBUGGING.md` — diagnostics and common fixes

For RAG pipeline work, `.claude/TDD_WORKFLOW.md` and
`.claude/RAG_QUALITY_FRAMEWORK.md` are mandatory rather than optional.

### Worktrees

Background agents check out worktrees under `.claude/worktrees/`. They are gitignored,
and `jest.config.js` ignores them explicitly — a leftover worktree once produced 111
phantom test failures because Jest discovered a second copy of every test file. Sweep
them when the work they hold has landed: `git worktree list` shows what is registered.

### Useful MCP servers here

- **Playwright** — E2E against the few remaining HTML-scraped surfaces (Anna's Archive)
- **Filesystem** — download directory management

### Quick status

```bash
git status && git branch --show-current
npm run doctor              # upstream drift, then release-record audit
grep "CRITICAL\|HIGH" ISSUES.md
```
