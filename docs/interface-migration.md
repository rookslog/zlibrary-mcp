# 2.0 tool interface migration

Implementation date: 2026-10-19. This is the one breaking tool-interface change;
package/executable/repository names and MCP client keys do not change.
`serverInfo.name` is now `book-acquisition`. No old tool is an alias: each returns
an `invalid_argument` envelope with a helpful replacement and no provider call.
See [API reference](api.md) and [ADR-012](https://github.com/rookslog/zlibrary-mcp/blob/master/docs/adr/ADR-012-Acquisition-Tool-Interface.md).

| Old tool | Replacement and arguments |
|---|---|
| `search_books` | `search`: retain `query`, `exact`, `languages`; `fromYear`/`toYear` → `year_from`/`year_to`; `extensions` → `formats`; `count` → `limit`; add `sources:["zlibrary"]`; `content_types` removed |
| `full_text_search` | Removed without a text-search equivalent. It searched metadata with a quoted-query fallback, not book contents. Use `search` for metadata; `phrase` and `words` removed |
| `get_download_history` | `get_history`: `count` → `limit`; use `cursor` alone to continue |
| `get_download_limits` | `get_limits`: default local; `mode:"live"` for account numbers |
| `get_recent_books` | Removed; choose a metadata query with `search` |
| `download_book_to_file` | `download`: search again, choose a row, pass its `ref`; `outputDir` → `output_dir`; `bookDetails` is not accepted |
| `get_book_metadata` | `get_book` with `ref`; `bookId`, `bookHash`, `include` removed |
| `search_by_term` | Removed; put the term in `search.query` and name sources |
| `search_by_author` | `search.author` without query; retain `exact`, `languages`; `yearFrom`/`yearTo` → `year_from`/`year_to`; `extensions` → `formats`; `count` → `limit`; old default 25 becomes 10 |
| `fetch_booklist` | Removed; use catalogue metadata search |
| `search_advanced` | Removed; make `search` calls with `exact:true`, then `exact:false` if needed |
| `search_multi_source` | `search`: `count` → `limit`; `source:annas/libgen` → `sources:[...]`; `auto` requires explicit order or operator `BOOK_SOURCE_DEFAULT_ORDER` |
| `process_document_for_rag` | Removed; use an isolated 1.4.x installation for text processing |

All new tools reject unknown argument names before provider contact. Both
`process_for_rag` (including `false`) and `processed_output_format` are invalid
on the 2.0 MCP download tool. The internal legacy API façade retains its
already-parked false no-op for non-MCP callers; it is not registered as a tool.

Author-only exact queries retain quoted-author behavior. With both `query` and
`author`, `exact` covers the **whole combined query**, not the title alone.
No author post-filter is promised. Unsupported source filters are reported in
`routing.attempts[].ignored` rather than silently claimed as applied.

## Result shapes

| Before | 2.0 |
|---|---|
| `books[]` | `rows[]` with process-local `ref`, not full upstream records |
| `file_path` | `path` plus `bytes`, `reused`, `transfer_skipped`, `verified`, `record` |
| `daily_limit`, `daily_remaining` | `sources.zlibrary.limit.per_day`, `remaining` |
| `sources_used` | `routing.served_by` plus ordered attempts |
| full-text `search_type` | Removed with the misleading text-search tool |
| unstructured tool errors | Closed `{error}` envelope and three-way `retry` |

`routing.served_by` is absent when a search returns no rows; `fell_back` is
false on that empty page. `get_limits.operations` describes supported operations
even for unconfigured sources. Use `search_available` and `download_available`
for current local availability. A known daily cap without known spend does not
produce a `remaining` count.

Saved full records/IDs must not be replayed as download inputs. Search again and
choose the edition, then use its current ref. Refs do not survive process
restart. Durable library integration reads `.acq.json` records, not refs or
account history. Existing files are not deleted or converted automatically.

## Download location

2.0 contains downloads in one folder by default. The root is
`${XDG_DATA_HOME:-~/.local/share}/zlibrary-mcp/downloads` on Linux/macOS or
`%LOCALAPPDATA%\zlibrary-mcp\downloads` on Windows. Set `ZLIBRARY_OUTPUT_ROOT`
to an absolute path to choose it (home expansion is allowed); relative values
fail startup. To keep the old place, start the server with:

```bash
export ZLIBRARY_OUTPUT_ROOT="$PWD/downloads"
```

Relative `output_dir` values now resolve against the root, never the working
directory. Absolute paths inside the root work. Outside paths, including
outside-pointing symlinks, return `output_refused` with `request_issued: false`
before provider contact. To keep writing to unrelated absolute folders, set
`ZLIBRARY_UNRESTRICTED_OUTPUT=1`; it lifts containment only. Any `..` component
or leading `~` in caller `output_dir` stays refused in every mode. The flag
accepts only `1`, `0` or unset; invalid values fail startup, and `1` logs one
warning on stderr. With an unset root and an existing `./downloads`, startup
reports the new location on stderr.

On POSIX, new directory levels are 0700; existing directory permissions stay unchanged.
New artifacts and receipts are 0600. Artifacts already present keep their mode
on reuse; a receipt the server repairs is rewritten as 0600.
On Windows, chmod only toggles the read-only attribute, not access-control
permissions. Docker pins the root to `/app/downloads`, preserving the mounted
download volume without user action.
On Linux bind mounts, new directories (0700) and files (0600) belong to the
container user; use Docker `--user` or Compose `user:` to match the host UID/GID,
or use chmod as the owner to grant host readers access; reuse preserves artifact modes.
Tunnel deployers already setting a root keep that root; relative paths now
resolve under it even if the wrapper does not change its working directory.

## Existing 1.4.0 downloads

Keep the books already in `./downloads`; upgrading does not delete or convert
those files or change their permissions. They have no acquisition receipts.
If an old file occupies the predicted 2.0 destination, `download` returns `output_refused` with
`destination_occupied: true` before authentication or transfer, even when the
bytes are identical. A file found only at a format-corrected destination can
instead be refused after a transfer. Neither case adopts the old file.

To keep using an old book without spending quota, use that file directly outside
the MCP tool. If you want a 2.0 acquisition record, first move or rename the old
file out of the destination, or choose a separate `output_dir`. Then search again,
select the edition's current `ref`, and call `download`. This is a fresh transfer;
a Z-Library download may count against the account quota. Moving the old file or
choosing another directory does not make the transfer quota-free. There is no
zero-quota way to adopt an old file into the receipt system.

## Receipt implementation choices

Full 64-bit nonce strength and monotonic non-reuse take precedence over the
spec's incompatible short/fixed handle examples. Handles can grow beyond 16
characters. Equal nonce collisions retain the explicitly stated probabilistic
bound; tests do not claim impossible protection when both nonce and counter alias.

An intent receipt gains a staged-file SHA-256 checkpoint before publication.
That checkpoint makes recovery after publication/before finalization testable.
On a fresh acquisition the implementation hashes staging and the published file;
this favors correctness over the spec's single-write-loop optimization. The
fresh result's checksum claim refers to bytes hashed in that call.

Lock creation failures are refused, not silently downgraded to per-process
idempotence. Active local locks are not stolen due only to elapsed time; dead
holders are reclaimed. This preserves the advertised cross-process boundary.
Local macOS tests and a disposable DIONYSUS Linux/ext4 run exercise two real
processes, one transfer, and intent recovery after process death. Network mounts
and other filesystem configurations remain outside those checks.
