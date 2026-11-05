# Scripts

| Script | Use |
|---|---|
| `validate-python-bridge.js` | Asset validation (`npm run validate`) |
| `validate-readme-tools.sh` | Compare registrations with the README tool list |
| `check_upstream.py` | Source contract probes (`npm run doctor`) |
| `release-audit.mjs` | Release-record audit; requires Git history and GitHub access |
| `check_optional_imports.py --core-smoke` | Offline acquisition import trap |
| `get_venv_python_path.mjs` | Locate the project Python interpreter |
| `check-packed-artifact.mjs --offline-providers` | Clean disposable npm install and offline installed-bin smoke (source checkout only) |

Text-processing CLI paths are removed. See
[migration](../docs/text-processing-migration.md). Scripts in `archive/` are
historical experiments, not runnable product features.

The npm package includes validation, upstream, and release-audit scripts.
Build/lint/test/rehearsal commands require a source checkout. The release audit
also needs that checkout's Git release records.

## Packed install check

After `npm run build`, run:

```bash
node scripts/check-packed-artifact.mjs --offline-providers
```

For an interpreter-specific check, set `UV_PYTHON` to its executable path.
That optional selection is forwarded to the disposable package's setup;
without it, uv uses its normal interpreter selection. CI does not pin this job.

Requires Node 22+, Python 3.10+, uv, OpenSSL and tar. Packs once, installs that
tarball into an empty local prefix with install hooks disabled, and runs the
documented `bash setup-uv.sh --no-dev` in the installed package. Registry access
during dependency setup is expected. No global npm
install, publish, live doctor probes or user account configuration is used.
All disposable state is removed in `finally`.

Pack requests lifecycle scripts disabled with both `--ignore-scripts` and
`npm_config_ignore_scripts=true`; `HUSKY=0` also disables Husky's hook setup.
Some npm versions still invoke prepare and emit a Husky skip diagnostic despite
the flag. Tarball discovery therefore requires exactly one `.tgz` file in the
pack destination and never parses npm stdout as JSON. The check does not claim
that every npm version honors lifecycle-script suppression.

Checks every bin plus doctor/validation/start/release-audit script target, the
installed Python bridge and package-local paths, then starts npm's actual bin
link from outside the source tree. Every stdout line must be JSON-RPC. The
2.0 names and argument names must match `__tests__/fixtures/tools-surface-v2.json`.
The installed bin runs `search` → `get_book` → `download` → repeated `download`
against the existing rehearsal HTTPS fake for both credential-free LibGen and
synthetic-account Z-Library. It checks exact fixture bytes, SHA-256, output path,
and the complete `.acq.json` receipt. The repeated call must hit that receipt
without any additional fake-provider calls or quota spend, and must preserve
the artifact and receipt bytes. LibGen's initial acquisition includes a range
probe before the full stream; both requests are counted.

`Rehearsal.start()` supplies fake startup/certificates/cleanup, and its shared
`providerEnvironment()` supplies endpoints, trust and the Python fence. The
LibGen URL seam covers both requests-based search and httpx-based resolution
and streaming; production provider code is unchanged. Node/Python network
fences are tested, then runtime must produce zero non-loopback egress attempts.
Only the fake's exact loopback port is admitted. This is not a live-source
health check. All output, account state and fake-server state is disposable.

The first two controls damage the tarball **before** installing it. The third
allows search/details but makes the fake file endpoint return no bytes. Each
must exit 1:

```bash
node scripts/check-packed-artifact.mjs --offline-providers --control=missing-bridge
node scripts/check-packed-artifact.mjs --offline-providers --control=missing-script
node scripts/check-packed-artifact.mjs --offline-providers --control=drop-transfer
```

The existing pack-check stays; a separate CI job runs this smoke on Ubuntu and
macOS within the rehearsal job's 15-minute budget.

## Upgrade and rollback check

From a built source checkout:

```bash
node scripts/check-install-upgrades.mjs --matrix all --offline-providers
```

Installs **published `zlibrary-mcp@1.4.0` from npm**, runs its documented
`bash setup-uv.sh`, prepares its public NLTK Punkt resources, then starts the
actual npm bin against the rehearsal fake. It downloads a synthetic TXT and
uses the old processing tool to produce real extracted text and metadata.
It replaces that same local-prefix installation with the candidate tarball,
runs `bash setup-uv.sh --no-dev`, verifies existing artifact hashes, and checks
all 13 retired names plus both old download flag values and malformed/format
arguments. Invalid calls must return `invalid_argument` with recovery guidance
and `retry:no`, without changing provider counters or output files. New
LibGen and Z-Library acquisition journeys must succeed. Finally, reinstalling
registry 1.4.0, preparing its environment again, and restarting/searching must
succeed with the original artifacts intact.

`RAG_QUALITY_STRATEGY=technical` is adopted by the old processing configuration,
then retained during candidate calls to show it cannot restore removed tools.
The receipt reports replacement of an environment-choice canary inside `.venv`;
package-owned dependencies and diagnostics are not user-artifact preservation
promises. Files under the disposable user home are hashed, excluding mutable
egress/debug logs. New files are allowed; no preexisting artifact may change
or disappear. The check never touches real user configuration or outputs.

The interpreter defaults to **3.13** for the published processing stack;
`UV_PYTHON` may select an executable. This is separate from the unpinned fresh
packed-install check. Requires Node22+, uv, Python3.10+ for the setup guard,
OpenSSL and tar; registry/Python/NLTK dependency downloads are expected during
setup. Runtime Node/Python fences are self-tested and only admit the fake's
exact loopback port. No global install, publication, real-provider traffic,
or migration alias is used. All disposable state is removed in `finally`.

Each destructive-upgrade control runs a real legacy install/seed and candidate
npm replacement, then simulates a destructive upgrade side effect before the
preservation check. Each must fail with the corresponding preservation error:

```bash
node scripts/check-install-upgrades.mjs --offline-providers --control=delete-output
node scripts/check-install-upgrades.mjs --offline-providers --control=rewrite-output
node scripts/check-install-upgrades.mjs --offline-providers --control=delete-state
```

`--matrix all` means all cases in this acquisition-only upgrade check, not the
historical proposed base/rag/scholar/Text installer matrix. No rag/scholar or
Text installation is supplied. The current npm manifest still says 1.4.0;
the check identifies candidate code by its packed hash, distinct runtime bytes
and exact five-tool 2.0 surface, not a version bump performed by this script.

## Daily source alerts

`check_upstream.py` keeps alert policy separate from connector routing. Set
`UPSTREAM_RELIED_SOURCES` to a comma-separated subset of `zlibrary,annas-archive,libgen`.
The default is `zlibrary,libgen`; Anna's Archive remains probed as a dated warning
unless explicitly listed (for example, `zlibrary,annas-archive,libgen`).
Any blocked or failing probe of a listed source exits 1;
excluded sources produce warnings. `UPSTREAM_RELIED_SOURCES=zlibrary` retains the
old warning-only treatment of Anna's Archive and LibGen. Z-Library blocks also
alert now; excluding a source is the way to suppress its alerts. This setting
never changes the connector's source selection or Anna's fallback without a key.
Unknown source names are configuration errors, detected before provider calls.
An unset, blank or whitespace-only value selects the default of `zlibrary,libgen`,
both in the CLI and in GitHub Actions. Configure a named subset to narrow alerts.

Use `--report-file PATH.txt` to write text plus adjacent JSON, including on an alert
exit. The workflow sets `--report-file upstream-report.txt` and uploads both files.
Without this flag the probe only prints its report; `npm run doctor` leaves no report
files behind. `--json` prints JSON instead of text. Each JSON `results`
row contains `source`, probe `name`, `status` (`passed`, `blocked`, `changed`),
`reason`, `first_seen` (UTC date or null for a pass), and `relied_on`.
`changed` covers a failed contract or transport, not a confirmed provider change.

`--previous-issue PATH` reads the prior rolling issue body. A machine marker in
the body preserves each continuously failing probe's first-observed date, even
if its reason changes. Old bodies without that marker start tracking today;
the issue creation date is not evidence of a particular source's failure date.
The scheduled workflow closes the rolling issue on recovery, clearing dates
for passing probes. It journals each actionable report in a snapshot comment
before updating the body and state. Both lookups paginate exact-title labelled
issues by creation (including closed issues); the next failure reopens the same issue. The next run reads the latest
dated snapshot from `github-actions[bot]`, so a failed issue update cannot lose
the recovery date or cleared probe state. Both date readers reject invalid or
future dates. Open exact-title labelled issues are preferred; if none is open,
the newest created match is reused. A missing probe report carries the previous
status marker forward. Recovery requires an explicit successful probe flag,
successful reachability and live jobs, and live status `passed`. Inconclusive
runs leave the issue unchanged; a healthy already-closed issue receives no
writes. A failed snapshot write stops before changing the issue body or state.
Excluded-source warnings retain first-seen dates too. The workflow uploads both
files as `upstream-source-report` for 30 days when probe report generation
completes, including when a source alerts. No
source artifact is available if the previous-issue read or probe fails before
writing reports; the artifact step warns in that case. Source blocked and
Source changed have separate issue headings. A block from the runner does not establish a worldwide outage.
The credentialed live suite, including collection, requires an explicit
`zlib_blocked=false` from the reachability probe. A true value reports `blocked`;
missing, empty or any other value reports `unknown` and skips logins. A failed
Anna's or LibGen probe does not suppress those tests when Z-Library explicitly
reported healthy on a configured candidate domain. A recognized Z-Library wall
reports `true`; other failed or inconclusive probes report `unknown`. Only a
complete accepted Z-Library probe set emits `false`: both final response hosts
must match the configured domain, domains must be a non-empty list, and books
must be a list when present. A zero success value is rejected except for the
known unauthenticated responses recognized by the runtime's auth helper.
Anna warning-only results also produce a run annotation. Upstream values are
kept on one line before rendering; multi-line workflow outputs use a fresh
random delimiter, and the domain output is limited to configured candidates.
Workflow-output mode prints only the fixed warning annotation, keeping the report
in its output file and optional artifacts. Ordinary CLI reports retain their text
or JSON output with neutral report values. Upstream text is fenced, comment
delimiters are escaped, and
only the last machine marker supplies carried dates. Only the exact rolling issue
title is reused; other issues with the same label are left untouched.

`npm run doctor` runs the release-record audit even when the source check exits
nonzero. The combined command fails if either check fails or cannot start, and
creates no probe reports without an explicit report-file flag. Trailing command
arguments are separated from Node options, and probe startup errors are printed.
Import-time diagnostics can create an ignored debug log; this command does not
promise an otherwise unchanged filesystem.
