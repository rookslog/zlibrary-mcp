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
  They are never written to the RAG output bundle or the debug log.
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
