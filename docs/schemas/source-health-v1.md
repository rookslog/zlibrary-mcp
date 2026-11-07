# Scheduled source-health report, version 1

The scheduled upstream workflow publishes `source-health.v1.json` in artifact
`upstream-source-health-v1-<run_attempt>`, retained for 30 days. This is a
metadata-only report for a reader, separate from the unauthenticated
`upstream-source-report` and authenticated `live-integration-log` diagnostics. It makes no
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
| `job_attempts` | `reachability` and `live_tests`: positive job-output attempt integers or null. Mismatched/unavailable attempts invalidate that job's retained outputs; its `jobs` entries become `unknown`. |
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
| `passed` | Positive evidence the named scope ran and passed; for authenticated coverage this requires the live execution markers, usable counts evidence, successful completion and a passing positive authentication check. |
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
An observed step failure takes precedence over a skipped/cancelled/unknown job
when classifying failure; it cannot be relabelled as work that never started.
Completed step outcomes also prevent job cancellation or skipping from turning
a missing required report into a skip. Genuine setup skips remain `not_run`.

### Authenticated suite

Z-Library's scope is `zlibrary:python-integration`. It includes:

- `status` and fixed `reason` code;
- `ran`: true when execution and nonempty JUnit cases corroborate it, false for
  a known skip/never-started step, null when execution cannot be established;
- `counts`: null without usable counts evidence, otherwise observed `total`, `passed`,
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
authenticated pass. Missing/invalid counts after an execution marker is `failed`;
an explicitly skipped live step corroborated by an available counts envelope is
`not_run`. An absent counts artifact instead leaves execution unknown.
If the live step fails before emitting a marker or status, without counts evidence,
the report is `failed` / `live_collection_error`, even if its job was cancelled
or skipped. `ran=null` preserves uncertainty about suite execution after preliminary collection fails.
A missing marker alone never establishes a skip. Without positive skip or
cancellation evidence or an admission status, unknown/completed step outcomes
are `failed` / `execution_not_recorded`, `ran=null`. For an unmarked live step,
completed step outcomes take precedence over its job's skip/cancellation.
Case counts or a completed-suite status without a marker are contradictory
execution evidence and also fail with `ran=null`, rather than claiming no run.
Collection/report errors remain failures.
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
attempt's inputs or retained job outputs. Each upstream job exports its own
`github.run_attempt`. Missing, invalid, earlier or later output attempts yield
`not_run` / `earlier_attempt_outputs`, `ran=null`, null counts and incomplete
evidence for that job; retained admission statuses cannot establish a current
`no_account` or block. The final artifact is also attempt-specific.

For partial reruns: rerunning only the publisher invalidates both retained job
outputs. Rerunning live tests and the publisher can retain current authenticated
evidence while reachability remains unqualified. Rerunning reachability and the
publisher has the inverse effect. Only jobs with the publisher's attempt can
establish current outcomes; rerun both producers for a fully attributable report.
Input uploads are best effort and cannot change existing source-alert outcomes.

The authenticated input is `live-counts.json`, a closed version-1 envelope
containing counts and fixed authentication/error flags, without case identifiers
or test text. Counts preparation and its upload both require successful output
filtering. The fixed counts command runs immediately after that filter and before
the adjacent uploads. Source
health never uploads or consumes raw XML. The diagnostic files are prepared
locally to keep test output out of uploaded files.

The stdlib-only producer reads these local files and selected non-secret run
metadata. It performs no network, account or credential operation. Inputs are
bounded to 5 MiB each; malformed JSON/XML and contradictory evidence cannot
produce a pass. Output uses allowlisted source/check identifiers and fixed
reason codes. It does not copy raw upstream messages, hostnames, URLs, account
identifiers, credentials or parameterized JUnit case names. Both authenticated uploads require successful local output filtering.
Counts preparation is best effort after successful filtering and contains no
test text. If the counts artifact is absent, authenticated execution is unknown:
`failed` / `missing_live_evidence`, `ran=null`, null counts and incomplete
coverage, even when retained outputs say `no_account`, blocked or skipped. This
differs from an available, valid counts envelope with `problem=missing`: that
envelope can corroborate a known admission skip without any JUnit file. An
unavailable artifact cannot establish such nonexecution.

The schema and offline tests qualify the producer, not a live source or the
reader. A completed scheduled artifact requires the change to reach the
workflow and that run to finish. This change does not install a reader.


## Reason classes and reader behaviour

`observed` describes a probe, suite or collection observation; `evidence` means
required evidence is missing, invalid, contradictory or insufficient;
`not_run` means no completed claim for that scope. A reason class is separate
from outcome state and never overrides `ran`, counts or completeness flags.
For example, collection failure is observed while execution can remain unknown,
and cancellation can leave valid case counts with `ran=true`.

An unknown reason code is an **evidence failure** for that scope. Preserve the
unknown code for diagnosis, withhold pass/recovery and do not guess its meaning
from a prefix or an otherwise successful job. Unknown fields may be ignored;
unsupported versions, missing required fields or invalid types require evidence
failure. This table is the complete version-1 vocabulary.

| Reason | Class | Meaning |
|---|---|---|
| `probe_passed` | `observed` | The named unauthenticated check passed. |
| `probe_refused` | `observed` | The probe observed a refusal; no global-outage inference. |
| `probe_timeout` | `observed` | A typed timeout interrupted the probe. |
| `probe_tls_failure` | `observed` | A typed TLS cause interrupted the probe. |
| `probe_dns_failure` | `observed` | A typed resolver cause interrupted the probe. |
| `probe_connection_failed` | `observed` | A typed connection, transport or protocol failure interrupted the probe. |
| `probe_server_error` | `observed` | A probe received an HTTP 5xx response. |
| `probe_http_failure` | `observed` | A probe received another unsuccessful HTTP response. |
| `probe_contract_failed` | `observed` | The expected response or parser contract failed. |
| `live_suite_failed` | `observed` | Executed tests or their pytest exit reported failure. |
| `live_suite_passed` | `observed` | The observed suite and required authentication check passed without skips. |
| `live_suite_partially_covered` | `observed` | The required check passed but other cases were skipped. |
| `live_collection_error` | `observed` | A collection/report error or a failed preliminary live step was observed; execution may be unknown. |
| `invalid_probe_report` | `evidence` | Probe input or a required row is malformed or contradicts alert policy. |
| `contradictory_probe_evidence` | `evidence` | Probe step failed without a failed relied-on row to explain that outcome. |
| `missing_probe_report` | `evidence` | An attempted probe has no readable report. |
| `probe_execution_not_recorded` | `evidence` | Probe input lacks a completed step outcome. |
| `missing_probe_check` | `evidence` | An expected probe row is missing. |
| `duplicate_probe_check` | `evidence` | An expected probe row appears more than once. |
| `unexpected_probe_check` | `evidence` | A source has an unexpected check identifier. |
| `probe_error_unknown` | `evidence` | Exception metadata is malformed or lacks a supported classification. |
| `invalid_alert_policy` | `evidence` | Alert-policy source identifiers are invalid. |
| `invalid_live_status` | `evidence` | Completed-suite classification is unsupported or absent. |
| `execution_not_recorded` | `evidence` | Execution markers or corroborating outcomes do not establish test execution. |
| `contradictory_live_evidence` | `evidence` | Admission status, test evidence or step outcome disagree. |
| `missing_live_evidence` | `evidence` | The counts artifact is unavailable, or marked execution lacks required counts evidence. |
| `invalid_live_evidence` | `evidence` | Counts evidence or its local JUnit input is malformed. |
| `empty_live_evidence` | `evidence` | Local JUnit input contains no testcases. |
| `live_job_incomplete` | `evidence` | Test evidence exists but successful job and step completion are absent. |
| `required_authentication_check_missing` | `evidence` | No positive authentication anchor exists in the observed scope. |
| `earlier_attempt_outputs` | `not_run` | Job output attempt is absent, malformed or different from the publisher attempt. |
| `probe_step_not_started` | `not_run` | The probe step was skipped. |
| `reachability_not_completed` | `not_run` | Reachability was cancelled, skipped or has no completed job outcome. |
| `live_job_cancelled` | `not_run` | Live job cancellation prevents a completed authenticated claim. |
| `live_job_skipped` | `not_run` | The live job was skipped. |
| `no_ci_account_configured` | `not_run` | Admission recorded no configured dedicated CI test account. |
| `probe_state_unknown` | `not_run` | Admission skipped the suite because probe state was unknown. |
| `live_step_not_started` | `not_run` | The live step was explicitly skipped without execution evidence. |
| `all_live_tests_skipped` | `not_run` | Every observed testcase was skipped. |
| `authentication_check_skipped` | `not_run` | The authentication anchor exists but did not pass. |
| `no_authenticated_suite_configured` | `not_run` | This source has no configured authenticated suite. |


## Freshness and example

The workflow is scheduled daily at 06:17 UTC. A reader should select the newest
completed scheduled run, require its exact run/attempt/SHA, and apply a stated
freshness budget. For example, 36 hours allows one daily cadence and 12 hours of
lateness; that is a reader policy, not an availability guarantee. A missing,
unreadable, future-dated or older-than-budget artifact is evidence failure, even
if the previous report passed. Re-reading or rerunning the publisher must not
make older producer observations current. Artifact retention is not freshness.

[Complete synthetic example](source-health-v1.example.json) shows all three
sources, successful unauthenticated checks and an explicitly absent dedicated CI
test account. Its run ID, SHA and timestamp are fixture values. The offline test
regenerates it through the actual producer and checks exact equality.
