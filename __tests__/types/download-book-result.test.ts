import type { DownloadBookResult } from '../../src/lib/zlibrary-api.js';

const result: DownloadBookResult = {
  file_path: '/tmp/book.pdf',
  processed_file_path: null,
  provenance: {
    source: 'libgen',
    route: 'get.php',
    mirror: null,
    host: null,
  },
};

const mirror: string | null = result.provenance.mirror;

// Provenance follows the Python bridge's `_provenance` contract: each value
// may be absent for a real transfer and is represented as null, not invented.
// @ts-expect-error host is nullable
const hostMustNotBeString: string = result.provenance.host;

export { hostMustNotBeString, mirror, result };
