# Acquisition receipt format v1

This contract is for downstream library and corpus readers consuming `.acq.json`
sidecars. It describes the persisted record, rather than a tool response or an
account-history entry. The [JSON Schema](schemas/acq-record-v1.schema.json) uses
JSON Schema draft 2020-12. A valid receipt is evidence to check against the file,
not an authorization token or a guarantee that its bytes still exist unchanged.

## States and publication

1. **Initial intent:** before authentication or provider contact, the server
   writes `state: "intent"` with `bytes: null` and `sha256: null`. `path` is the
   predicted absolute destination. All fields in the table below are present.
2. **Hashed intent:** before publishing staged bytes, the checkpoint records
   their byte count and SHA-256, corrects `path` and `format` to the canonical
   destination, and persists the intent. A format correction can create both a
   canonical sidecar and an alias at the originally requested destination; the
   canonical hashed intent is written first. Both records point at the canonical
   artifact. An intent can survive a crash before or after file publication.
3. **Complete:** after checking published bytes against the checkpoint, the
   server writes `state: "complete"`, with non-null `bytes` and `sha256` and the
   transfer's provenance. A later call can recover a published hashed intent by
   verifying the file and promoting it to complete without provider contact.

Normal receipt reuse re-hashes the file and leaves a complete receipt unchanged.
The current call's `transfer_skipped` can therefore differ from the value stored
in the receipt. Recovery can persist `transfer_skipped: true`; ordinary reads do
not refresh a receipt's timestamp or request context. Compatible existing
canonical receipts remain authoritative, including their original provenance.
When a breaker permits only a read-only receipt hit, an intent or alias may be
returned without repair until the next ordinary call.

## Fields

All top-level fields listed here are written in both states. Unknown catalogue
metadata is explicitly null. The writer does not coerce catalogue or request
metadata to a common scalar type: **JSON value** below means any JSON value,
including null, arrays and objects. Readers must check the type they need rather
than assuming, for example, that a year is always a string. The schema preserves
this existing behavior instead of imposing new producer validation.

| Key | JSON type | Meaning and presence/value rules |
|---|---|---|
| `record_version` | integer | Always `1` for this contract. |
| `state` | string | `intent` or `complete`, as described above. |
| `path` | string | Absolute server-side artifact path. After the checkpoint it is canonical, including in an alias receipt. It is not necessarily a path on the reader's host. |
| `bytes` | integer or null | Null in the initial intent; nonnegative staged size after the checkpoint and in complete records. |
| `sha256` | string or null | Null in the initial intent; 64 lowercase hexadecimal characters after the checkpoint and in complete records. Used with `bytes` for local verification. |
| `md5` | JSON value | Catalogue MD5 as supplied, usually a string; null when absent. It is not a claim that the receipt writer verified the file's MD5, and is distinct from normalized source identity. |
| `reused` | boolean | Initially false. Normally records whether transfer publication adopted identical existing bytes; promotion of an existing verified canonical intent records false even when the current call transferred (see below). Retained on later ordinary receipt reuse. |
| `transfer_skipped` | boolean | Initially false. Can become true during recovery or promotion of an existing verified canonical intent, even when the current call transferred (see below). This persisted value is not the result of every later call. |
| `downloaded_at` | string | UTC ISO 8601 timestamp ending in `Z`, set when the original intent is created. Retained at completion, recovery and reuse; not a completion-time measurement. |
| `source` | string | Normalized source identity, normally `zlibrary`, `libgen` or `annas`; `annas_archive` becomes `annas`. Separate from the transport route. |
| `provenance` | object | Route/mirror/host evidence, described below. Initialized before transfer; supplied transfer provenance replaces it on fresh completion. |
| `title` | JSON value | Catalogue title, or null if absent/falsy. Usually a string. |
| `author` | JSON value | Catalogue author, or null if absent/falsy. Usually a string. |
| `year` | JSON value | Catalogue publication year, or null if absent/falsy. May be a string or number. |
| `language` | JSON value | Catalogue language, or null if absent/falsy. Usually a string. |
| `publisher` | JSON value | Catalogue publisher, or null if absent/falsy. Usually a string. |
| `pages` | JSON value | Catalogue page count, or null if absent/falsy. May be a string or number. |
| `isbn` | JSON value | Catalogue ISBN information, or null if absent/falsy; no scalar normalization is applied. |
| `series` | JSON value | Catalogue series, or null if absent/falsy. |
| `edition` | JSON value | Catalogue edition, or null if absent/falsy. |
| `translator` | JSON value | Catalogue translator, or null if absent/falsy. |
| `format` | JSON value initially; string or null after checkpoint | Catalogue `extension`, or null if absent/falsy, in the initial intent. The checkpoint replaces it with the normalized canonical filename suffix, or null if unrecognized. |
| `identifiers` | object | Source-specific identity used for acquisition/reuse, described below. |
| `request` | object | Original caller context, described below. Missing context values are null. |

When the publication checkpoint promotes an existing verified canonical intent,
it records `transfer_skipped: true, reused: false`, even when the current call
transferred and may have spent quota. These two persisted fields must not be
used to count quota spend.

### Provenance

The initial object has `route` equal to the normalized source and null `mirror`
and `host`. Fresh completion uses transfer-supplied provenance when available;
otherwise that initial object remains. The acquisition writer passes supplied
provenance through, so readers must tolerate absent members as well as unknown
members in that object.

| Nested key | JSON type | Meaning |
|---|---|---|
| `provenance.route` | string | Present in the default object; transfer routes currently include `eapi`, `mirror_download` and `fast_download`. Treat routes as descriptive strings, not a closed enum. |
| `provenance.mirror` | string or null | Present and null in the default object. The standard transfer supplies the configured or resolved mirror hostname when known. |
| `provenance.host` | string or null | Present and null in the default object. The standard transfer supplies the observed final response hostname when known. |

Provenance members describe observations, not downloadable URLs. A recovered
hashed intent can retain default provenance because the interrupted transfer
never supplied its final result.

### Identifiers

| Nested key | JSON type | Presence and meaning |
|---|---|---|
| `identifiers.id` | string | Z-Library only; selection ID converted to a string. Not an input for a direct-ID public lookup. |
| `identifiers.hash` | JSON value | Z-Library only; selection `hash`, falling back to `book_hash`. Usually a nonempty string; the writer does not coerce its type. Not the file SHA-256. |
| `identifiers.md5` | string | Other sources; selection MD5 converted to a lowercase string. The receipt layer does not enforce a 32-hex-character shape. |

### Request context

All four keys are present, even when null. Values are retained without coercion.
The five-tool interface currently supplies `ref` and the originating `query`
when available. `item_id` and `why_chosen` can be supplied by internal callers;
they are not additional public `download` arguments.

| Nested key | JSON type | Meaning |
|---|---|---|
| `request.ref` | JSON value | Usually a string: the process-local selection ref, or null. It is provenance, not a durable replay handle. |
| `request.query` | JSON value | Usually a string: originating query, or null. |
| `request.item_id` | JSON value | Caller-assigned correlation identifier, or null. |
| `request.why_chosen` | JSON value | Caller-supplied selection rationale, or null. |

The tool response fields `record`, `verified` and `quota_estimate` are not stored
receipt keys. `record` locates the sidecar; there is no self-location field in
its JSON object.

## Compatibility and reading

- Adding a key does not change `record_version`. Readers ignore unknown keys,
  including nested keys. The schema sets `additionalProperties: true`. Added
  fields must be optional when reading existing v1 records.
- Renaming or removing a key, or changing its meaning, requires a new
  `record_version`.
- A reader encountering a higher version than it understands must refuse that
  receipt rather than guess. This v1 schema accepts only version 1. Missing or
  otherwise unsupported versions must not silently be interpreted as v1.

A downstream reader first checks the version, then validates the known fields.
For a complete artifact, compare the file's current size and SHA-256 with the
receipt before trusting its contents. Schema validation alone does not do that
filesystem check. An intent is not a completed acquisition: let the server
recover or finish it before treating it as complete. An alias sidecar can name
a different canonical filename, so use the validated `path` field to locate the
artifact. Existing 1.4.0 files have no receipts and are not adopted automatically.
