import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

/** Hash emitted names and bytes, excluding compiler maps and type declarations. */
export function getRuntimeDigest(directory: string): string {
  const hash = createHash('sha256');
  function visit(relative: string): void {
    const entries = readdirSync(join(directory, relative), { withFileTypes: true });
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isSymbolicLink()) throw new Error('Runtime symlinks cannot be stamped');
      if (entry.isDirectory()) visit(name);
      else if (!entry.isFile()) throw new Error('Unsupported runtime file');
      else if (!/(?:\.map|\.d\.(?:ts|mts|cts)|\.tsbuildinfo)$/.test(name)) {
        const bytes = readFileSync(join(directory, name));
        hash.update(`${JSON.stringify(name)}:${bytes.length}:`).update(bytes);
      }
    }
  }
  visit('');
  return hash.digest('hex');
}
