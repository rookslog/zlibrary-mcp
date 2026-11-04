# Documentation

Start with the task you need to complete. Package-local guides are linked
relatively; contributor records that do not ship in npm link to the public
source repository.

| Need | Start here | More detail |
|---|---|---|
| Install and configure | [Quick start](../README.md#quick-start), [installation](../README.md#installation) and [configuration](../README.md#configuration) | [Optional dependencies](optional-dependencies.md); [deployment guide](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/DEPLOYMENT.md) |
| Troubleshoot | [Troubleshooting](TROUBLESHOOTING.md) | [Retry and circuit-breaker configuration](RETRY_CONFIGURATION.md) |
| Use the tools | [Tool reference](api.md) | [Interface migration](interface-migration.md), [receipt format](receipt-format.md) and [receipt schema](schemas/acq-record-v1.schema.json) |
| Contribute | [Contributing](../CONTRIBUTING.md) | [Working agreements](https://github.com/rookslog/zlibrary-mcp/blob/master/AGENTS.md) and [scripts](../scripts/README.md) |
| Understand the architecture | [Component map](https://github.com/rookslog/zlibrary-mcp/blob/master/.claude/ARCHITECTURE.md) and [project invariants](../VISION.md) | [Architecture decisions](https://github.com/rookslog/zlibrary-mcp/tree/master/docs/adr) and [processing boundary](text-processing-migration.md) |
| Follow releases | [Release process](RELEASE_PROCESS.md) | [Changelog](https://github.com/rookslog/zlibrary-mcp/blob/master/CHANGELOG.md), [public milestones](https://github.com/rookslog/zlibrary-mcp/milestones) and [releases](https://github.com/rookslog/zlibrary-mcp/releases) |

## Historical work

Old processing plans, experiments and session reports describe earlier designs;
they are not current setup instructions, accepted priorities or release evidence.
Contributor lessons from moved plans remain in the
[processing migration guide](text-processing-migration.md#rationale-retained-from-historical-processing-plans).

The [pre-trim documentation tree](https://github.com/rookslog/zlibrary-mcp/tree/c8798addc98d83e50bb743b98b48d2490476dcb0/docs)
and [session records](https://github.com/rookslog/zlibrary-mcp/tree/c8798addc98d83e50bb743b98b48d2490476dcb0/claudedocs)
preserve the removed documents publicly at their original paths. See the
[preserved processing implementation](text-processing-migration.md#preserved-implementation-and-unresolved-work)
for separately parked code and unresolved findings. These pointers do not need
private planning access.

### Removed reference entries

These links preserve the historical entries previously indexed here:

- [ARCHITECTURE_ANALYSIS.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/ARCHITECTURE_ANALYSIS.md)
- [CITATION_INFERENCE_ARCHITECTURE.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/CITATION_INFERENCE_ARCHITECTURE.md)
- [HYBRID_TOC_EXTRACTION.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/HYBRID_TOC_EXTRACTION.md)
- [IMPLEMENTATION_SUMMARY_HYBRID_TOC.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/IMPLEMENTATION_SUMMARY_HYBRID_TOC.md)
- [MARGINALIA_DESIGN.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/MARGINALIA_DESIGN.md)
- [TESTING_LESSONS_LEARNED.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/TESTING_LESSONS_LEARNED.md)
- [history/MIGRATION_V2.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/history/MIGRATION_V2.md)
- [pdf-processing-implementation-spec.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/pdf-processing-implementation-spec.md)
- [rag-markdown-generation-spec.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/rag-markdown-generation-spec.md)
- [rag-output-qa-report-rerun-20250429.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/rag-output-qa-report-rerun-20250429.md)
- [rag-output-quality-spec.md](https://github.com/rookslog/zlibrary-mcp/blob/c8798addc98d83e50bb743b98b48d2490476dcb0/docs/rag-output-quality-spec.md)
