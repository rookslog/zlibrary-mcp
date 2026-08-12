# Full-book PDF dependency audit and action plan

**Date:** 2026-08-12
**Scope:** issue [#105](https://github.com/rookslog/zlibrary-mcp/issues/105), current
`docs/105-pdf-fixture-audit` checkout only. This is an audit and plan, not a
cleanup change. No fixture, ground truth, script, Git history, or LFS object was
changed.

## Reader and decision

**Reader:** a maintainer deciding whether to approve a future removal change.

**Post-read action:** decide whether to authorize the staged, non-history-rewrite
cleanup below, with its dependent-data changes, and separately decide whether a
history and remote-LFS purge is warranted.

## Method and coverage boundary

**observed:** this audit ran both `git grep` over tracked text and `rg --hidden`
over the checkout, excluding `.git`, `node_modules`, `.venv`, and `dist`, for the
four exact basenames. The literal search returned 12 tracked text occurrences in
six files. `git ls-files` independently established the four tracked PDF paths.

**corroborated-for-this-decision:** there are no additional *current checkout*
textual references to those exact four basenames outside the inventory below.
The checks would have been weakened by an alias, a generated-at-runtime filename,
an ignored path, a different checkout, or Git history not present in
the local refs; those surfaces remain outside this literal-reference conclusion.

The audit also read the live issue bodies for [#85](https://github.com/rookslog/zlibrary-mcp/issues/85)
and [#105](https://github.com/rookslog/zlibrary-mcp/issues/105), the pytest
configuration, the relevant tests/scripts, current Git/LFS metadata, and an
`npm pack --dry-run --json` manifest. The first pack attempt was blocked because
the sandbox could not write its normal cache or the shared Git config; rerunning
with a temporary npm cache and `HUSKY=0` produced the manifest. That limitation
does not change the tracked-file or LFS measurements below.

## Reference inventory

### Four tracked artifacts

| Full-book PDF | Working-tree bytes | Direct text references | Relationship |
| --- | ---: | ---: | --- |
| `test_files/KantImmanuel_CritiqueOfPureReasonTheCambridgeEditionOfTheWorksOfImmanuelKant_23371882.pdf` | 77,733,907 | `scripts/test_marginalia_detection.py` | Active, manually-invoked analysis script. It is not under pytest's `__tests__/python` discovery root. |
| `test_files/DerridaJacques_OfGrammatology_1268316.pdf` | 22,047,657 | baseline JSON; extraction script; three archived scripts | See relationships below. |
| `test_files/UnknownAuthor_MarginsOfPhilosophy_984933.pdf` | 4,748,766 | baseline JSON | Dynamically exercised by one slow recall check; no direct generator or archive reference found. |
| `test_files/HeideggerMartin_TheQuestionOfBeing_964793.pdf` | 3,354,243 | baseline JSON; extraction script; three archived scripts | See relationships below. |

**observed:** the four total 107,884,573 bytes (102.89 MiB). The order above
matches the actual working-tree sizes, not the rounded figures in #105.

### Textual references, by role

| Role | Current references | Audit finding |
| --- | --- | --- |
| Slow test input (dynamic) | `test_files/ground_truth/body_text_baseline.json` has entries for Derrida, Heidegger, and Margins. `__tests__/python/test_recall_baseline.py` parameterizes over every entry, opens `test_files/<name>`, and is marked `slow`. | **corroborated-for-this-decision:** the statement “no test references any of the four” is false if “references” includes runtime data dependencies. There are no direct full-book filename literals beneath `__tests__/`; three books are indirect inputs. The Kant full book has no baseline entry. |
| Baseline companion data | `test_files/ground_truth/baseline_texts/HeideggerMartin_TheQuestionOfBeing_964793.pdf.txt` is tracked (223,517 bytes). Equivalent tracked companion text files were not found for the Derrida or Margins full books. | **observed:** `test_no_body_text_recall_loss` skips an entry without its companion text; `test_body_text_not_shorter` still processes every baseline entry. Therefore the hydrated slow suite presently processes Heidegger in both checks and Derrida/Margins in the length check. |
| Baseline generator | `scripts/generate_recall_baseline.py` globs every top-level `test_files/*.pdf` and writes both the JSON and a companion text file. | **corroborated-for-this-decision:** retaining this generator unchanged would reintroduce entries for any full-book PDFs present when it runs. It does not establish how an intended replacement should be named or compared. |
| Page-extract generator | `scripts/extraction/extract_specific_pages.py` reads the Derrida full book to create `derrida_pages_110_135.pdf`, and the Heidegger full book to create `heidegger_pages_79-88.pdf`. | **observed:** this is provenance/generation coupling for exactly two derivative fixtures. It does not generate the Kant, Margins, `derrida_footnote_pages_120_125.pdf`, or other page fixtures. |
| Archived manual analyses | `scripts/archive/search_sous_rature.py`, `scripts/archive/test_specific_pages_analysis.py`, and `scripts/archive/test_strikethrough_detection.py` each name the Derrida and Heidegger full books. | **source-supported:** pytest discovery is restricted to `__tests__/python` by `pytest.ini`; these archive scripts are not part of the normal Python suite. They remain manual dependencies if someone runs them. |
| Active manual analysis | `scripts/test_marginalia_detection.py` names the Kant full book under its `__main__` path. | **observed:** this is a direct future break if the Kant file is removed without redirecting or retiring the script. |
| Ground-truth schema/loader claim in #105 | `test_schema_validation.py` explicitly excludes `body_text_baseline.json`; `ground_truth_loader.py` reads per-test ground-truth JSON, not the recall-baseline manifest. | **corroborated-for-this-decision:** #105’s assertion that those two components consume the full-book baseline is not supported by their current source. |
| Production/runtime package | Exact-name search found no match in `src/` or `lib/`. `npm pack --dry-run --json` listed 93 package files and no `test_files/` path. | **corroborated-for-this-decision:** the four PDFs do not ship in the current npm tarball. This says nothing about source clones, forks, or external archives. |

## Storage and distribution: four distinct effects

### A. A future ordinary removal commit

**inference:** a future commit that removes the four paths from the tip would
remove 107,884,573 bytes of hydrated fixture content from that checkout. It must
not be evaluated as a standalone four-file deletion: it would break the direct
scripts and leave the slow recall manifest with three obsolete entries unless
dependent changes are made in the same approved change.

**recommendation:** do not “re-base” a full-book recall entry onto an arbitrary
page extract. The compared texts and length thresholds describe different
documents. Choose explicitly between retiring the three full-book baseline cases,
creating new baseline cases from replacement fixtures, or retaining an approved
external source. The audit does not choose among those product/quality outcomes.

### B. Ordinary fresh clone and checkout

**source-supported:** Git LFS stores pointers in Git and materializes large
objects through those pointers; GitHub documents this model in [About Git Large
File Storage](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage).

**inference:** after an ordinary removal commit becomes the checked-out tip, a
fresh LFS-enabled clone/checkout of that tip has no four matching pointers to
materialize. Its Git history can still contain small historical pointer blobs.
This reduces new checkout materialization; it does not delete previously
downloaded local LFS objects, alter old commit checkouts, or prove a particular
client's LFS/smudge configuration.

### C. Existing local `.git` and reachable history

**observed:** in this checkout's shared Git directory, `du -sh .git` reported
137M and `du -sh .git/lfs` reported 110M. A direct object-file count measured 18
local LFS object files totaling 114,825,340 bytes. `git lfs ls-files --all`
reported the same 18 current-ref LFS entries; the four full books are 93.95% of
that local LFS-byte total. `git log --all -- <four paths>` found two commits that
touch the four paths.

**inference:** a future tip-only deletion leaves those objects in this existing
local `.git/lfs` directory and leaves historical references reachable. Any local
disk reclamation would be a separate, deliberately authorized cleanup with its
own retention/rollback decision; it is not part of #105's proposed commit.

### D. Remote Git LFS storage quota and history rewrite/support

**source-supported:** GitHub’s current [Removing files from Git Large File
Storage](https://docs.github.com/en/repositories/working-with-files/managing-large-files/removing-files-from-git-large-file-storage)
states that removed LFS objects continue to exist remotely and count toward LFS
storage; it directs maintainers who cannot recreate the repository to contact
Support. Its [Git LFS billing documentation](https://docs.github.com/billing/managing-billing-for-your-products/managing-billing-for-git-large-file-storage)
also says storage is based on all associated objects and that a deletion during a
month does not recalculate that month’s accrued use.

**corroborated-for-this-decision:** neither a tip-only deletion nor a local
history rewrite alone demonstrates remote LFS quota reclamation. A history
rewrite is required to remove the paths from reachable Git history, but remote
LFS object disposition and billing recovery require the GitHub-supported
purge/repository-recreation route or a Support outcome. The current remote quota,
account plan, and Support disposition were not inspected; they are
**uncorroborated** for this repository.

## Current measurements and issue-claim comparison

| Surface measured from current local Git/worktree data | Result | Decision use |
| --- | ---: | --- |
| All materialized tracked working-tree files | 123,442,662 bytes (117.72 MiB) | A current-checkout measurement, not a remote quota figure. |
| `test_files/` materialized tree | 116,371,585 bytes (110.98 MiB) | The four books are 92.71% of this directory. |
| Four full books | 107,884,573 bytes (102.89 MiB) | Direct target magnitude for a future tip-only removal. |
| Git blob objects for all tracked files | 7,190,531 bytes (6.86 MiB) | Mostly ordinary Git content and LFS pointers; not LFS payload size. |
| Local LFS object files | 114,825,340 bytes (109.51 MiB), 18 objects | A local cache observation only. |
| `.git` directory reported by `du` | 137M, of which `.git/lfs` is 110M | Existing local storage, shared by this worktree. |

**corroborated-for-this-decision:** #105’s rounded “113 MB of 119 MB tracked
content” and “105 MB four books” figures do not match the present byte-level
measurements under a single stated unit. They may be differently rounded or
measured from a different checkout; that explanation is **uncorroborated**.

**source-supported risk:** the live issue identifies public distribution of
scholarly editions as a concern. This audit did not inspect licenses, provenance,
permissions, or controlling law and makes no legal conclusion. **recommendation:**
obtain an owner/legal-policy decision before treating that concern as the basis
for irreversible history or remote-object actions.

## Staged action plan (not authorized by this audit)

| Stage | Approval gate and action | Expected blast radius | Checks and rollback |
| --- | --- | --- | --- |
| 0. Choose quality scope | **Owner decision required:** retire, replace, or externally retain the three slow full-book recall cases; decide the future status of the active Kant script and the Derrida/Heidegger page-extract provenance script. | Defines whether coverage decreases, is redesigned, or gains a new source. | Record the decision and a source/ground-truth contract. Do not substitute page extracts without new expected outputs and a real-document quality review. |
| 1. Make dependencies coherent | **Implementation approval required:** update/remove the three baseline manifest entries and the Heidegger baseline text as the chosen quality scope requires; redirect/archive/remove direct scripts only as separately approved. | Slow recall tests and manual scripts; no production package impact is expected from the current manifest check. | Run focused slow recall tests with deliberately chosen hydrated/replacement fixtures, inspect generator output, and re-run exact-name search. Revert the cohesive commit to restore tip behavior. |
| 2. Remove only current-tip artifacts | **Explicit deletion approval required:** remove the four files in one reviewable commit after Stage 1 is green. Do not alter `.gitattributes` solely for this: current page fixtures still use LFS. | Fresh checked-out tips no longer materialize the four objects; old commits/local LFS caches remain. | Run the full repository checks, inspect a fresh-clone/checkout result with documented LFS settings, and confirm no direct or dynamic dependency remains. Revert the removal commit to restore the files at the tip; that rollback does not repair a later history rewrite. |
| 3. Decide remote/history remediation separately | **Separate destructive-operation approval and coordination required:** inventory refs, forks, PRs, contributor clones, and a tested recovery bundle before any `filter-repo`/force-push. Obtain GitHub Support guidance for remote LFS objects before promising quota recovery. | Rewrites commit IDs and disrupts collaborators/forks; remote purge may be irreversible. | Dry-run and document affected refs, protect a recovery mirror/bundle, coordinate clone replacement, then request Support with the rewrite’s orphaned-LFS report if applicable. Abort before force-push if the recovery or stakeholder gate is incomplete. |

## Audit disposition

**recommendation:** treat Stage 0–2 as a bounded repository-maintenance proposal
only after an owner approves the quality tradeoff. Treat Stage 3 as a distinct
high-blast-radius remediation, not as a promised consequence of deleting files
from a future commit.

**observed non-actions:** this audit deleted, moved, regenerated, or modified no
PDF, ground-truth data, script, LFS object, Git history, or fixture. No
destructive Git command was run.
