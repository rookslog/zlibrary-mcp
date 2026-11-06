# Scheduled source-health report, version 1

The scheduled upstream workflow publishes `source-health.v1.json` in artifact
`upstream-source-health-v1-<run_attempt>`, retained for 30 days. This is an
account-sensitive report for a reader, separate from the existing unauthenticated
`upstream-source-report` and `live-integration-log` artifacts. It makes no
whole-service healthy claim. Source identifiers are `zlibrary`, `annas-archive`
and `libgen`.

The `source_health` job uses `always()` after both source reachability and live
tests. It is independent of drift-issue filing. Missing input artifacts are
represented in JSON, including setup failure, cancellation and skipped work.
If the publisher runner, checkout or upload itself cannot finish, the artifact
may be absent; a reader must report unreadable/missing evidence, never fall back
to an older report as current or interpret workflow success as source health.

## Envelope

| Field | Type and meaning |
|---|---|
| `schema_version` | Integer `1`; reject unsupported versions. |
| `generated_at` | Producer creation time in UTC, `YYYY-MM-DDTHH:MM:SSZ`, not a date-only check time or a claim that every check ran at that instant. |
| `run.id` | Positive decimal string identifying the workflow run. |
| `run.attempt` | Positive integer identifying the attempt. |
| `run.head_sha` | Full lowercase 40-hex commit SHA. |
| `run.event` | `schedule` for the published report; the offline CLI also accepts `workflow_dispatch`, which scheduled readers must ignore. |
| `jobs` | `reachability`, `reachability_step`, `live_tests`, `live_step`, each `success`, `failure`, `cancelled`, `skipped` or `unknown`. |
| `inputs` | `probe_artifact` and `live_artifact`: exact attempt-specific names used for this run. |
| `expected_sources` | The complete ordered three-source ID list above. |
| `sources` | An object containing every expected source, including untested sources. |
| `policy_valid` | Whether the alert-policy source list is valid. Invalid policy gives explicit failed reachability evidence and `unknown` alert policy. |
| `probe_contract_complete` | Every expected probe row is present, well formed and attributable to an executed probe, with no extra checks. This describes completeness of evidence, not whether probes passed. |
| `unexpected_probe_checks` | Count of unknown/malformed check identifiers; their raw strings are not copied. |

Readers must bind the JSON run ID, attempt and SHA to the selected run, validate
its timestamp/version and expected source/check sets, and preserve missing or
invalid evidence. They must not infer recovery from an omitted row, a job result
or a different coverage scope. Producer time does not reset the age of a report
when a reader reads it again.

## Per-source outcomes

Each source has `alert_policy` (`relied_on`, `warning_only`, or `unknown`),
`reachability`, and `authenticated`. Warning-only sources remain in the report.
Their alert policy does not alter their observed state.

All outcome states use this closed vocabulary:

| State | Meaning |
|---|---|
| `passed` | Positive evidence the named scope ran and passed; for authenticated coverage this requires the live execution markers, usable JUnit, successful completion and a passing positive authentication check. |
| `no_account` | The dedicated CI test account was absent; no authenticated health claim. |
| `not_run` | The relevant work did not complete or was skipped, cancelled, never started, entirely skipped by pytest, or has no configured authenticated suite. Read its reason and nullable execution evidence. |
| `blocked` | Probe-observed refusal of the named source/scope. This does not establish a global outage. |
| `failed` | An observed failure, or missing/invalid/contradictory required evidence. The reason distinguishes evidence failure from a test/probe failure. |

### Reachability

`status` and `reason` summarize `checks`; failed/incomplete evidence takes
precedence over a block. `evidence_complete` records valid complete input for
that source. `expected_checks` and `missing_checks` contain stable IDs. `checks`
always contains an entry for each expected check with `id`, `status`, a fixed
`reason` code, and `first_seen` (validated date or null).

Expected IDs are:

- Z-Library: `zlibrary:eapi/info/domains`, `zlibrary:eapi/book/search`.
- Anna's Archive: `annas-archive:search`.
- LibGen: `libgen:search`, `libgen:article-search`, `libgen:download`.

A probe's `changed` result maps to `failed` in this report. Example reason codes
include `probe_passed`, `probe_refused`, `probe_timeout`, `probe_tls_failure`,
`probe_dns_failure`, `probe_connection_failed` and `probe_contract_failed`.
Missing, malformed, duplicate or unexpected checks have distinct evidence
reasons. A skipped probe step is `not_run` / `probe_step_not_started`; a completed
probe without its required report is `failed` / `missing_probe_report`.

### Authenticated suite

Z-Library's scope is `zlibrary:python-integration`. It includes:

- `status` and fixed `reason` code;
- `ran`: true when execution and nonempty JUnit cases corroborate it, false for
  a known skip/never-started step, null when execution cannot be established;
- `counts`: null without usable JUnit, otherwise observed `total`, `passed`,
  `failed` and `skipped` case counts;
- `required_check`: `zlibrary:authentication-succeeds`, tied to
  `TestRealAuthentication.test_authentication_succeeds` (parameter variants are
  grouped without copying their identifiers);
- `coverage_complete`: true only for successful observed suite evidence with
  the required positive check and no skipped cases. It does not claim coverage
  of capabilities outside that suite or invent tests absent from the evidence.

A passing subset with skips remains explicitly partial (`coverage_complete`
false, `live_suite_partially_covered`). All-skipped results are `not_run`, and a
missing or entirely skipped required authentication check cannot establish an
authenticated pass. Missing/invalid JUnit after an execution marker is `failed`;
an unstarted live step is `not_run`. Collection/report errors remain failures.
No-account reports use `no_ci_account_configured`. The existing live log and
JUnit artifacts remain available for detailed diagnosis.

Anna's Archive and LibGen have `not_run`, `ran=false`, null scope/counts/required
check and reason `no_authenticated_suite_configured`. A successful unauthenticated
probe never substitutes for an authenticated suite.

## Attempt binding and privacy

The workflow preserves additional immutable input artifacts named
`upstream-source-health-probe-<run_attempt>` and
`upstream-source-health-live-<run_attempt>`. Downloads use those exact names in
the current workflow run. A publisher-only rerun has no fallback to a previous
attempt's inputs: absent evidence is explicit rather than newly timestamped
as current. The final artifact is also attempt-specific.

The stdlib-only producer reads these local files and selected non-secret run
metadata. It performs no network, account or credential operation. Inputs are
bounded to 5 MiB each; malformed JSON/XML and contradictory evidence cannot
produce a pass. Output uses allowlisted source/check identifiers and fixed
reason codes. It does not copy raw upstream messages, hostnames, URLs, account
identifiers, credentials or parameterized JUnit case names. The existing raw
artifacts retain their prior behavior.

The schema and offline tests qualify the producer, not a live source or the
private reader. A real completed scheduled artifact still requires the change
to reach the workflow and that run to complete. No reader or briefing schedule
is installed by this change.
