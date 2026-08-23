# Z-Library MCP

An MCP server that finds books across several catalogues, acquires them, and turns them
into RAG-ready text. Its language centres on *sources* — the catalogues it reads — and the
distinct *routes* by which a source will hand over a file.

## Language

### Sources and routing

**Source**:
A catalogue the server can search and acquire from. Z-Library, LibGen, and Anna's Archive
are sources.
_Avoid_: Provider, backend, mirror (a mirror is a host *within* a source)

**Adapter**:
The per-source implementation satisfying the common source interface, living in
`lib/sources/`. Note that **not every source has one**: Anna's Archive and LibGen are
adapters, while Z-Library predates the interface and keeps a separate path. Migrating it
is tracked in #40.
_Avoid_: Driver, connector, plugin

**Mirror**:
One of several interchangeable hosts serving the same source, identified by the name the
source uses for it. LibGen's `li`, `vg`, and `la` are mirrors of one source, not three
sources.
_Avoid_: Domain, instance

**Host**:
The machine that actually served the bytes, which is often not the mirror that resolved
the link — LibGen's `li` mirror hands off to a `cdnN.booksdl.lc` node. Recorded separately
from the mirror because they fail independently.
_Avoid_: Server, node, mirror

**Route**:
A distinct path by which one source hands over a file. A single source may offer several,
differing in what they require of the operator and what limits they impose.
_Avoid_: Method, channel, path

**Provenance**:
The record of which source, route, mirror, and host served a given result.

Note that provenance is currently **written to the logs, not returned to the caller**.
`DownloadResult` carries `url`, `source`, and quota only. Making it part of the returned
result is an open design decision (#96), so treat this entry as naming the concept rather
than describing a guarantee the server presently offers.

### Anna's Archive routes

These terms exist because "keyless" was ambiguous and the ambiguity caused a real
planning error: work the maintainer had approved was recorded as ruled out.

**Keyed fast_download**:
Anna's download route authenticated by an API key from a paid membership. No browser
verification.

**Operator-cookie slow_download**:
Anna's download route in which a *human* passes the DDoS-Guard browser verification and
supplies the resulting `__ddg*` cookies, which the server then attaches to plain HTTP
requests. Requires no API key and no browser inside the server. Intended to be
rate-limited to personal-use scale.

**Not viable — dead, not deferred** (resolved 2026-08-11, #84). DDoS-Guard binds the
challenge cookie to the client that earned it: the issuing IP is stored inside `__ddg9_`,
so a transplanted cookie is rejected byte-for-byte identically to no cookie at all
(403, 902 bytes, with and without). `AnnasArchiveAdapter.get_download_url` calls only the
keyed `fast_download` endpoint and raises without `ANNAS_SECRET_KEY`. **Anna's therefore
has exactly one supported download route: keyed `fast_download`.** The term is retained
here because the distinction it names is still needed to read #75, #84 and #97 — not
because the route is planned. Its guardrail work, #97, is closed as moot **and has a
successor**: the sanctioned rate-limited route it anticipated did appear, as the
browser-resident session below, and #144 carries the guardrails for it. Read #97 for the
thresholds it settled; do not reopen it.
_Avoid_: Keyless download (ambiguous — say this instead)

**Browser-resident session**:
A route where a *real browser on the operator's machine* holds the DDoS-Guard clearance and
search and download requests are issued from inside it, rate-limited. Nothing is exported to
a different client, so the IP binding that defeated the operator-cookie route above does not
apply. #142 measured that clearance can be held this way — headful only, roughly 20 minutes
per solve — and #143 and #144 build on that answer.

**In scope** (operator ruling; the reasoning and its limits are in `AGENTS.md`
§"Two terms with sharp edges", the measurement in #142). The entry below previously read
"permanently out of scope" and #95 carried that clause forward to every successor map; what
the ruling opened is *this* route, on the condition that #144's rate limiting ships in the
same pass as it rather than after it. Fingerprint work counts as in scope only where it keeps
this route working — a real browser whose automation markers would otherwise get it walled.

**Machine-solved challenge**:
Any route where the *server* defeats the browser verification itself with no browser session
a person established — a headless solver run unattended, spoofing alone in place of a real
browser. Still not the route being built, and the ruling above did not open it: #142
measured that headless fails even holding valid clearance, so this is not a design being
deferred but one that does not work. Read the reversal above as scoped to the
browser-resident session; nothing here is licensed by it.
_Avoid_: Keyless download, automated download

Note that "keyless" alone should not be used. It has meant both *no API key* (a live
requirement) and *no human in the loop* (ruled out), and those are different routes.
Prefer **key-free** for the former when a general term is needed.

### Acquisition

**Acquisition**:
Obtaining a file from a source and placing it on disk. Distinct from *search*, which only
returns candidates.
_Avoid_: Download (use for the raw transfer step only), fetch

**Processing**:
Turning an acquired file into RAG-ready text output. Yields paths on disk rather than the
text itself — never the text itself. A document whose extraction produces no usable text
yields **no path**: `processed_file_path` is null, which callers must handle.
_Avoid_: Extraction, conversion

**Book details**:
The result object from a search, carrying the identifiers acquisition needs. Acquisition
consumes these rather than re-looking-up by id.
_Avoid_: Metadata (broader), book record
