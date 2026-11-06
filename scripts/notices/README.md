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
Refresh can download those dependency artifacts; it does not contact book providers.
Use `TMPDIR` for the wheel/source scratch cache.

Ordinary generation (without `--refresh`) and `--check` work offline from retained
`metadata.json` and `supplements.json`. Any lock-byte change rejects the old inputs.
The snapshot records versions and original notice text, including CRLF. Missing
notice files fail refresh, except the bs4 alias distribution, whose complete upstream
metadata is retained with an explicit missing-notice label. No grant is invented.

`supplements.json` retains the complete GNU LGPL-2.1 text from the local common-licences
installation and ElementTree's notice from lxml's hash-checked source archive.
Each supplement carries its original text hash. Changing dependencies requires
reviewing these supplements as well as refreshing installed inputs. The notices
inventory describes the application dependency closure; inherited container runtimes,
OS packages and their bundled libraries need the actual image inventory too.

`license-freeze.json` pins root/vendor notices, existing README/docs bytes, and the
literal licence-field bytes to the lane's starting commit. Its checked-in baseline
must not be refreshed as part of dependency notice generation. A later authorized
policy change must explicitly replace or retire that lane guard.

The generator and input snapshots are source/build tooling. The npm payload includes
the generated notices, while dependencies are installed separately. Reconstructing
the package or image requires the matching source checkout and build inputs.
