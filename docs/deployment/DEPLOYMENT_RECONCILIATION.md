# Release-to-runtime reconciliation (design draft)

> Status: **proposal for review**, not a deployed workflow. Originating field signal: [#430](https://github.com/rookslog/zlibrary-mcp/issues/430). Last inspected: 2026-11-23. Do not treat these notes as permission to upgrade, stop services, access credentials, or bypass a provider's verification.

## Why this exists

The project has meaningful CI, source-health checks, a release process, and tag-triggered npm/GHCR publication (see [RELEASE_PROCESS](../RELEASE_PROCESS.md)). **Publication does not establish that a given ChatGPT tunnel or locally spawned MCP process runs the published bytes**. Clients may execute a globally installed older package, while a long-lived tunnel uses a separate pinned worktree. The `1.4.0` package number alone is insufficient as an identity signal.

The November 23 read-only investigation observed:

| Deployment class | Observed source | Lifecycle | Interpretation |
| --- | --- | --- | --- |
| macOS local stdio consumers | `2d4d9c0`, compiled 2026-10-28, package `1.4.0` | Per Claude client; multiple simultaneous children | Multiplicity is **not by itself a leak** |
| Linux local stdio consumers | `2d4d9c0`, compiled 2026-10-28, package `1.4.0` | Per client | Separate package from the tunnel |
| Linux managed secure-tunnel child | `899debf` from `DEPLOYED-COMMIT`, compiled 2026-11-06, package `1.4.0` | systemd user service with watchdog | Different branch history; server `build-info.json` has `commit:null` |
| GitHub `master` at observation | `70eebda` (2026-11-08) | Repository revision, **not deployed state** | Has newer merged-search and member-search work; branch histories diverge |

This snapshot intentionally excludes private file paths, endpoint IDs, credential values, user names and raw environment files. A private operator-maintained inventory must hold host-specific paths and manager references. **Do not commit a live private inventory to this public repository.**

## Proposed source-of-truth contract

Use a private declarative inventory with one record per **deployment role**, *not* one record per process PID. Define `desired` and `observed` separately and expose mismatches without silently editing anything. A schema sketch is in [runtime-inventory.example.yaml](runtime-inventory.example.yaml).

Minimum fields:

1. Stable deployment ID and role (`stdio-client`, `managed-tunnel`, `staging`), ownership, platform and lifecycle manager.
2. Approved **immutable artifact** identifier (npm tarball integrity, image digest, or locked built artifact checksum) and expected commit/version/bridge manifest. A moving branch name is not enough.
3. Observed runtime identity (version, commit, emitted-runtime SHA-256, measured UTC time) from the *running server's* MCP initialization or an accepted bounded diagnostic; capture build-info mismatch explicitly.
4. Client registration / tunnel mapping, with private endpoint identifiers stored outside the public repo; separate local client packages from the remote target.
5. Start time and controlled restart/rollback entrypoint. Process counts are diagnostic, never a desired service replica count for stdio sessions.
6. Per-source availability status distinguished from content coverage: `operational`, `blocked`, `not_configured`, `not_tested`, `empty`.

The version string should not be the sole authority: a copied build with `commit:null` must not be upgraded to an asserted git revision merely because another file contains that value. Check both runtime digest and deployment provenance; if inconsistent, report `identity_unknown`. Do not add a new public tool without a compatibility decision; prefer a secure MCP `serverInfo` version / existing bounded diagnostic if possible.

## Continuous delivery proposal (explicit promotion; no unattended master-to-production updates)

1. **Build:** select an reviewed revision, verify the Python bridge, lockfiles, tests, source-health fixture contracts, package contents, and runtime/build provenance. Produce an immutable, checked artifact. GitHub CI/publication alone is not a deployment receipt.
2. **Preflight:** check the proposed artifact against the deployed role contract; verify the target's manager and credential root without reading credential values. Detect diverged histories such as `899debf` vs `master`, and require an explicit integration/selection decision.
3. **Stage:** start an **isolated** local stdio instance or staged tunnel under a distinct profile/data boundary. Check MCP initialize, build identity, `get_limits(mode="local")` and bounded unmetered test search. Test failure shapes including Anna's `browser_check` without repeated provider challenge attempts. Credential-using/member tests are separately opted in and quota-aware.
4. **Promote:** operator-approved target-by-target switch to the tested artifact, with an exclusive deployment lock, recorded previous version, and no in-place mutation of a live working directory. Managed tunnel updates require tested systemd restart and fresh service PID; local stdio sessions using the old package remain explicitly `pending_client_restart`.
5. **Verify:** establish that the ChatGPT-visible registered connector actually reaches the intended runtime, not just that the service exists. Compare live handshake identity to the desired artifact; verify startup and one bounded operation. Report provider reachability separately from deployment health.
6. **Rollback:** on start failure, integrity mismatch, protocol incompatibility or failed acceptance check, revert to the previous artifact and restart only the affected managed service. Keep a traceable deployment receipt. Never mass-kill Claude children to make a count look clean.
7. **Reconcile:** a scheduled host-local or securely authenticated drift check compares declared vs observed identities; create a measured signal for discrepancies. No public CI job should acquire private host paths, live credentials or raw session data.

## Anna's Archive is a separate availability question

The November 6 tunnel build has browser support, but the Nov 23 ChatGPT search path returned `browser_check`. Its process environment showed the browser path enabled and no `ANNAS_SECRET_KEY` (only boolean presence was checked, not secrets). The newer `master` adds a reusable member-session search pathway behind that key. **An upgrade alone cannot be claimed to fix anonymous browser access.** Provider refusal and unknown outcome must remain distinguishable from a true zero-result search. Follow [#139](https://github.com/rookslog/zlibrary-mcp/issues/139), [#145](https://github.com/rookslog/zlibrary-mcp/issues/145) and [#336](https://github.com/rookslog/zlibrary-mcp/issues/336) for access-boundary decisions.

## Decision register (required before automatic promotion)

| Decision | Suggested default | Owner decision needed |
| --- | --- | --- |
| Delivery trigger | Immutable tagged/reviewed artifact, **not every master commit** | Which approval gate and release candidate? |
| Inventory storage | Private operator configuration; public schema only | Host/local file vs private repository |
| Staging route | Isolated process/profile, no shared writable state | Where and how to provision safely |
| Runtime identity | Verified emitted runtime/build digest + source commit | Existing MCP surface vs compatible extension |
| Live testing | Unmetered and non-invasive by default | Opt-in credentialed/member canary policy |
| Rollback | Retain known-good artifact; bounded verification | Who may promote and revert |
| Client process lifecycle | Session-owned; stale state observed, not killed | What counts as an orphan and its safe cleanup |

## Definition of done

A maintainer can name the desired and **actual running** revisions for each role without SSH archaeology. A version mismatch is visible before and after any promotion; a deliberately mismatched stamp cannot be accepted; a nonworking staged build does not replace production; rollback restores exact tested bytes; and a request through the ChatGPT connector can confirm which deployed artifact served it. The runbook documents how Mac stdio and Linux systemd lifecycles differ. Anna's browser refusal stays a source health failure, never an excuse to retry around provider controls.

See also [#205](https://github.com/rookslog/zlibrary-mcp/issues/205) (qualified tunnel guide), [#377](https://github.com/rookslog/zlibrary-mcp/issues/377) (merged search), and [#430](https://github.com/rookslog/zlibrary-mcp/issues/430) (fleet drift signal).
