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
`lib/sources/`.
_Avoid_: Driver, connector, plugin

**Mirror**:
One of several interchangeable hosts serving the same source. LibGen's `li`, `vg`, and
`la` are mirrors of one source, not three sources.
_Avoid_: Domain, instance

**Route**:
A distinct path by which one source hands over a file. A single source may offer several,
differing in what they require of the operator and what limits they impose. The route that
served a file is always reported to the caller.
_Avoid_: Method, channel, path

**Provenance**:
The record of which source, route, mirror, and host actually served a given result.
Reported on every acquisition, never inferred by the caller.

### Anna's Archive routes

These three terms exist because "keyless" was ambiguous and the ambiguity caused a real
planning error: work the maintainer had approved was recorded as ruled out.

**Keyed fast_download**:
Anna's download route authenticated by an API key from a paid membership. No browser
verification.

**Operator-cookie slow_download**:
Anna's download route where a *human* passes the DDoS-Guard browser verification and
supplies the resulting `__ddg*` cookies; the server then makes plain HTTP requests with
them. Requires no API key and no browser inside the server. Rate-limited to personal-use
scale.
_Avoid_: Keyless download (ambiguous — say this instead)

**Machine-solved challenge**:
Any route where the *server* defeats the browser verification itself — headless browser,
fingerprint spoofing. Permanently out of scope: it defeats an anti-abuse control Anna's
operates deliberately.
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
Turning an acquired file into RAG-ready text output. Always yields a path on disk, never
the text itself.
_Avoid_: Extraction, conversion

**Book details**:
The result object from a search, carrying the identifiers acquisition needs. Acquisition
consumes these rather than re-looking-up by id.
_Avoid_: Metadata (broader), book record
