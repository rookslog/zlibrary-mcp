# Security Policy

## Supported versions

Only the latest release line receives fixes. Check [releases](https://github.com/rookslog/zlibrary-mcp/releases)
for the current version.

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/rookslog/zlibrary-mcp/security/advisories/new)
rather than opening a public issue. Include reproduction steps and the version
you tested. Expect an initial response within a week.

## Handling your credentials

`ZLIBRARY_EMAIL` and `ZLIBRARY_PASSWORD` are read from the environment and used
only to authenticate against Z-Library. Two things worth knowing:

- **Credentials are passed to the Python bridge as a subprocess environment.**
  Acquisition records contain edition metadata and request context, not account
  credentials or session cookies.
- **`LOG_LEVEL=debug` echoes search arguments to stderr**, and MCP clients
  capture stderr into their own logs. Do not enable it when your queries are
  sensitive, and scrub client logs before attaching them to a bug report. At the
  default level, per-request arguments are not logged.

## Dependency vulnerability policy

CI blocks on `pip-audit` and `npm audit`. Any advisory with a published fix must
be resolved by raising the floor in `tool.uv.constraint-dependencies`
(`pyproject.toml`) rather than added to the ignore list. An advisory may be
ignored only when no fix exists, or when the fix is blocked by a pin that is
documented inline with the reason — see the `audit` job in
`.github/workflows/ci.yml`, where each ignore carries its justification.

Dependabot proposes weekly updates so this does not drift back into a
permanently-red gate.

## Scope note

This project scrapes and calls undocumented third-party endpoints (Z-Library
EAPI, Anna's Archive, LibGen). Issues in those upstream services are not
vulnerabilities in this project; the scheduled
[upstream contract check](.github/workflows/upstream-check.yml) tracks their
availability. Run `npm run doctor` to check them yourself.

## CI test account and uploaded output

CI may only use a dedicated test account with nothing else on it. Without an
account, the live suite reports `no_account` and skips authentication.

`scripts/check-workflow-secrets.mjs`, run by CI, parses every workflow and requires
successful output preparation before artifact uploads in jobs using secrets other
than `GITHUB_TOKEN`. Uploads may contain only `live.log` and `live.xml` from the
workspace root, with only artifact-upload steps between filtering and the last
upload. Aggregate and bracket secret access receive the same checks.
Reusable-workflow callers using secrets require a named job exception.
Exceptions require a named entry and a nonempty stated reason in the checker.
The check covers direct `actions/upload-artifact` steps in workflow files only. It does not inspect composite actions, other actions, shell commands, caches, job summaries or job outputs.

Integration credentials use an opaque test helper and an explicit login boundary.
The login helper records session values in a private file outside the workspace.
The output filter consumes and removes that file, replaces configured and captured
values and their encoded representations, and also checks recognized session fields.
Missing required capture or incomplete login records prevent preparation. The
filter rereads the upload files and checks decoded XML. An unsuccessful filter
prevents upload. After successful filtering, the workflow prints a fixed test
summary; raw pytest output stays in the prepared
log file. Live test output is retained for one day; the separate source report
remains at thirty days. Offline canaries exercise this path with synthetic values
in the normal test suite.
