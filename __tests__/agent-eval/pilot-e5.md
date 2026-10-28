# E5 approval and answer diagnostics

Checked 2026-10-28. Authority: EVAL-E5/BRIEF.md. No model call or paid run in this repair.

Observed baseline: clean evaluator HEAD `057dd9720ccab420e2511b6422e305f27ddfde65`.
The lead must commit these evaluator changes before the admission gate permits a pilot.

## Books-only approval

Source-supported: set `mcp_servers.books.default_tools_approval_mode="approve"`.
Keep `approval_policy="never"`, `sandbox_mode="read-only"`, `web_search="disabled"`
and existing shell, delegation, plugins, memory and code-mode disables.
The server-specific mode skips the tool approval prompt; `never` refuses a prompt
if one is still required. Do not relabel download as read-only to avoid approval.
The proxy still forwards the actual measured tool schemas and annotations.

Sources, fetched 2026-10-28:

- https://developers.openai.com/codex/config-reference (MCP server/tool approval keys).
- https://github.com/openai/codex/blob/rust-v0.165.0/codex-rs/core/config.schema.json (`UntypedMcpServer`, `AppToolApproval`).
- https://github.com/openai/codex/blob/rust-v0.165.0/codex-rs/core/src/mcp_tool_call.rs (`maybe_request_mcp_tool_approval`, `requires_mcp_tool_approval_for_mode`, `request_mcp_tool_approval`).
- https://github.com/openai/codex/blob/rust-v0.165.0/codex-rs/core/src/mcp_tool_call_tests.rs (`custom_mcp_tool_approval_mode_uses_server_default_with_tool_override`).
- https://github.com/openai/codex/blob/rust-v0.165.0/codex-rs/codex-mcp/src/mcp/mod.rs (`mcp_permission_prompt_is_auto_approved`: approve precedes never).
- https://github.com/openai/codex/blob/rust-v0.165.0/codex-rs/codex-mcp/src/server.rs (`McpServerMetadata::tool_approval_mode`: tool override, then server default).
- https://github.com/openai/codex/releases/tag/rust-v0.162.1
- https://github.com/openai/codex/releases/tag/rust-v0.163.0
- https://github.com/openai/codex/releases/tag/rust-v0.164.0
- https://github.com/openai/codex/releases/tag/rust-v0.165.0

Observed: local `codex --version` is 0.165.0; `codex exec --help` documents TOML
`-c` overrides; `codex features list` offers no needed separate approval feature.
Changelogs inspected for 0.162.1 through 0.165.0 do not establish this setting's
introduction version; the tagged schema/handler, rather than changelog silence,
are the version-specific support. Auto mode consults destructive/read-only/open-world
annotations; approve mode returns false for the prompt requirement. A strict automatic
review policy is a source-level exception to that shortcut; managed policy and the
installed binary's behavior remain pilot qualifications.

## Output directory provenance

Observed supplied raw file:
`/home/rookslog/workspace/lanes/zlib-w1023-PILOT-E1/pilot-E1.json.artifacts/0-E1-fbdd92b7-4ec6-44eb-a869-0c41d7e539c1/last-driver.jsonl`.
Lines 5, 7, 9, 11 contain the pre-download replies, none with an absolute directory;
line 12 first contains `/tmp/zlib-rehearsal-mfRpKz/server/downloads` in call arguments.
Line 13 records the client approval refusal. The pilot result's `downloads` field
is that same path, constructed in `EDiagnosticServer.start` from the fake server home.
Source-supported: `runEModelTasks` appends `Use ${server.downloads} for downloads.`
to the actual driver prompt. Inference: the prompt supplied the path; raw events
are outputs, not a complete input capture. The task-only prompt in the receipt
omits this appended instruction. The supplied evidence does not establish a mismatch
with the directory actually sent to the model. The proxy passes tool definitions
unchanged; production's download description has no session-specific absolute path.
Recommendation: retain this canonical, bounded destination in the prompt; acquisition
needs the permitted directory. Do not expose fake-provider internals or an extra path
through schemas. Falsifier checked: earlier raw replies supplying the path (absent);
full sent-input/tool-list capture for that pilot is unavailable.

## Independent answer measures

`answer.status` stays the pass gate: JSON object only, declared keys, typed required
facts. Fences/prose cannot pass. `answer.format` measures that format independently;
`answer.facts` preserves required facts, a normalized numeric-string diagnostic and
expected facts even when the format fails. Duplicate keys/multiple JSON objects are
ambiguous and unavailable. A valid but false typed answer has correct format and
incorrect facts. E1 explicitly allows integer 502 or numeric string "502", matching
E4's id forms; other numeric fields remain typed at the strict gate.
`wrongDecisions` excludes format-only failures; `formatFailures` is separate.
A correct identity answer cannot replace observed acquisition, bytes and receipt.
These are evaluator diagnostics, outside the comparison bar, not general model readiness.

## Lead-only rerun (not executed by E5)

After committing evaluator changes, from the named worktree:

```bash
env -u NODE_OPTIONS -u BOOK_SOURCE_READ_TIMEOUT node __tests__/agent-eval/run.mjs --suite E --e-cases E1 --driver codex --model gpt-6.1-sol --runs 1 --cold-session --paid --out /home/rookslog/workspace/lanes/zlib-w1023-PILOT-E1/pilot-E1-e5.json
```

One-line forwarding check, independent of the final score:

```bash
node --input-type=module -e 'import fs from "node:fs";const r=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));if(!r.results.some(x=>x.trace.some(c=>c.name==="download"&&c.forwarded===true&&!c.clientRejected&&c.driverCallId)))process.exit(1)' /home/rookslog/workspace/lanes/zlib-w1023-PILOT-E1/pilot-E1-e5.json
```

Dry-run argv and tests cannot establish actual approval behavior. Even successful
forwarding does not establish correct bytes/acquisition: inspect the strict score,
provider events and receipt separately. E1 may still fail for its invalid limit,
source choice, format, or acquisition outcome.
