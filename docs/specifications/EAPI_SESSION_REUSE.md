# EAPI session reuse

## Incident and scope

On 2026-09-05, consecutive Trivet searches/downloads failed at login with HTTP 400 and `Too many logins #2. Try again later.` Each Node tool call starts a fresh Python bridge; its module-level client does not survive the call.

Persist only EAPI session cookies in a private directory outside download artifacts. Keep the existing Python process lifecycle, domain probing, source routing, and download behavior.

## Acceptance

- Sequential and concurrent bridge processes using the same credentials perform one successful login and reuse its cookies.
- Validate cached cookies with `/eapi/user/profile`; only an explicit authentication rejection causes one fresh login. Network errors, walls, and unrelated business errors must not cause logins.
- Credential or explicit-domain changes cannot reuse a previous session.
- The session directory and files are private; writes are atomic; credentials and cookies never appear in diagnostics.
- An upstream login-limit rejection is readable, and a short local cooldown prevents subsequent calls from immediately repeating it. This cooldown is not an estimate of upstream recovery time.
- Missing credentials and credential-free source behavior remain unchanged.

## Verification and delivery

1. Python bridge/session helper and behavioral tests: run session, domain-resilience, and bridge regressions, including separate-process concurrency.
2. Publish a PR with evidence; stage a separate deployment directory and verify two real calls without a second login before reporting runtime reuse verified. Keep the old deployment available for rollback.

No upstream merge or account changes are included. If the existing login limit persists, report that limitation without attempting repeated logins.

## Automatic account selection (requested in the same task)

`ZLIBRARY_ACCOUNT_CREDENTIALS` accepts a JSON array of `{email, password}` objects. It takes precedence over the legacy single-account pair, which remains supported. Trivet already encrypts environment settings and masks credential-named entries. Do not put account values in tool arguments or logs.

For Z-Library downloads with a configured pool, read real `downloads_limit` and `downloads_today` values and choose the first account with positive remaining quota. Serialize selection plus download across bridge processes sharing the session directory. Search can use the first account even if its download quota is spent. Do not rotate on login errors, unknown quota, network failures, or uncertain download results. Do not retry an already-dispatched download on another account. All exhausted accounts produce an actionable error; no quota values or reset time are assumed.
