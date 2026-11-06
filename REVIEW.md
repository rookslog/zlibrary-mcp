# Review guide

For anyone reviewing a change here, person or agent. `AGENTS.md` says how the project works; this says what a review should look for.

## What failure looks like here

- **A shared path gets worse for another source.** Download, retry and limit code serves every source. A fix for one source must not slow, block or misreport another.
- **A limit stops holding.** A retry spends quota twice, a breaker never opens, a deadline doesn't reach every layer, or repeated bytes count as progress.
- **A reply lies.** A status, retry value or reason that doesn't match what happened. A chat acts on these fields, so a wrong one is a bug even if nothing crashes.
- **A contract moves silently.** A tool's reply shape, an error reason or a setting changes without its fixture, docs and changelog.
- **The docs claim too much.** When a document and the code disagree, the document is the bug.
- **Stdout gets noise.** Stdout is the JSON-RPC channel; diagnostics go to stderr.
- **A test changes to make a failure go away.** A changed expectation needs a contract reason.
- **Private details leak.** Commits, comments, test names and fixtures carry no account details.

## How to report

- Start with a count, and say plainly when there's nothing to fix.
- Give a file and line for every finding, never a guess from a name.
- Tag each finding **Important**, **Nit** or **Pre-existing**. A bug the change didn't introduce isn't the change's fault.
- At most three nits.
- Name the class of bug and every place it occurs, not one line.
- Don't repeat a finding already raised and answered. After the first few rounds, read the new diff only.

## How deep to go

Depth follows the change, not its author or its size.

| Depth | For | What it adds |
|---|---|---|
| Quick | Docs, comments, additive changes behind a setting that's off by default | Read the diff against this guide |
| Standard | Ordinary changes | Run the affected tests |
| Deep | Shared download path, limits and quota, startup, install, release, `.github/**` | Try to break it: mutate each change, run it against a misbehaving local server, check every layer agrees |

## Skip

Lock files, generated output, and `claudedocs/` unless the change is about them.

## When a review keeps finding the same thing

Fix it at the source: a test, a lint rule, or a line in this file.
