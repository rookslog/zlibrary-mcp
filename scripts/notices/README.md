# Dependency notice inputs

Source-checkout commands (Python 3.10+; use the synced project environment):

```sh
npm ci
uv sync --frozen --group dev
uv run --frozen python scripts/generate-third-party-notices.py --refresh
uv run --frozen python scripts/generate-third-party-notices.py --check
```

`--refresh` captures the production Node packages' installed metadata and original
notice files, and the runtime Python version union from `uv.lock`. Installed Python
metadata is used for matching versions. Interpreter-conditional records are read
from hash-checked locked wheels. Pycares' bundled c-ares notice files are additionally
read from its hash-checked locked source archive, including its `LICENSES/` directory.
The same table collects ujson's bundled double-conversion notices. Refresh rejects
unknown files under wheel `*.libs/` directories and mappings without retained texts.
Besides the refresh host, it checks the locked musllinux 1.1/1.2 wheels compatible
with the Docker recipe's CPython version and linux/amd64 (x86_64), including older
CPython abi3 tags. It excludes free-threaded builds and other architectures.
These wheel archives use the same hash-checked PyPI download/cache path as other
refresh artifacts; their URLs, hashes and bundled file lists are retained in metadata.
Other image architectures require separate wheel validation before distribution.
Run refresh on Linux or macOS; Windows wheels may use different upstream line endings.
Refresh can download those dependency artifacts; it does not contact book providers.
Use `TMPDIR` for the wheel/source scratch cache.

Ordinary generation (without `--refresh`) and `--check` work offline from retained
`metadata.json` and `supplements.json`. Fingerprints cover production Node paths,
versions and integrities; runtime Python names, versions and artifact hashes; and
the vendored name, version and licence classifiers. Dev-only bumps, root package
version changes, lock whitespace and unrelated setup descriptions do not stale
notices. Production input changes require the exact commands above.
The snapshot records versions and original notice text, including CRLF. Missing
notice files fail refresh, except the bs4 alias distribution, whose complete upstream
metadata is retained with an explicit missing-notice label. No grant is invented.

`supplements.json` retains the complete GNU LGPL-2.1 text from the local common-licences
installation and ElementTree's notice from lxml's hash-checked source archive.
It also retains libffi's licence, GCC's GPLv3 and runtime exception, and zlib's
licence from the versioned upstream URLs recorded beside their text hashes.
These texts identify the bundled components; their source versions are not an
attestation of every wheel's exact native source/build correspondence.
Each supplement carries its original text hash. Changing dependencies requires
reviewing these supplements as well as refreshing installed inputs. The notices
inventory describes the application dependency closure; inherited container runtimes,
OS packages and their bundled libraries need separate inspection of those artifacts.

`license-freeze.json` pins root/vendor licence-file bytes, the licence badge and
`## License` section in `README.md`, root/vendor licence metadata and root licence
file names to commit `5ff8c0b9`. It does not freeze unrelated README prose or
other documentation, nor scan other Markdown for licence claims. Unrelated
README prose mentioning licences belongs outside the protected `## License` section.
Missing manifest fields fail with an explicit diagnostic. The baseline must not
be refreshed as part of dependency notice generation. A later authorized policy
change must explicitly replace or retire this guard.

The generator and input snapshots are source/build tooling. The npm payload includes
the generated notices, while dependencies are installed separately. Reconstructing
the package or image requires the matching source checkout and build inputs.
