/** Contained descriptor reads and private, restart-safe chat resources. */
import { constants } from 'node:fs';
import { open, realpath, stat, lstat, mkdir, readdir, rename, unlink } from 'node:fs/promises';
import type { FileHandle } from 'node:fs/promises';
import * as path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js';

export const sha256 = (data: Buffer | string): string =>
  createHash('sha256').update(data).digest('hex');
export function maxBytes(): number {
  const value = process.env.ZLIBRARY_CHAT_FILES_MAX_BYTES;
  if (value === undefined) return 7_750_000;
  const max = Number(value);
  if (
    !/^\d+$/.test(value) ||
    !Number.isSafeInteger(max) ||
    max < 1_000_000 ||
    max > 32 * 1024 * 1024
  )
    throw new Error('ZLIBRARY_CHAT_FILES_MAX_BYTES must be an integer from 1000000 to 33554432');
  return max;
}
export function contained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
function privatePath(root: string, target: string): boolean {
  return contained(path.join(root, '.chat-files'), target);
}
export async function sourceFile(input: string) {
  if (
    input.includes('\0') ||
    input.split(/[\\/]/).includes('..') ||
    (!path.isAbsolute(input) && (path.win32.isAbsolute(input) || /^[a-z]:/i.test(input)))
  )
    throw new Error('file path must be contained in ZLIBRARY_OUTPUT_ROOT');
  const configured = process.env.ZLIBRARY_OUTPUT_ROOT;
  if (!configured) throw new Error('ZLIBRARY_OUTPUT_ROOT must be set for chat files');
  const root = await realpath(configured).catch(() => {
    throw new Error('output root is unavailable');
  });
  const lexical = path.resolve(root, input);
  if (!contained(root, lexical) || privatePath(root, lexical))
    throw new Error('file path is outside the output root or names the private cache');
  const target = await realpath(lexical).catch(() => {
    throw new Error('file path is unavailable');
  });
  if (!contained(root, target) || privatePath(root, target))
    throw new Error('file path is outside the output root or names the private cache');
  const handle = await open(
    target,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  ).catch(() => {
    throw new Error('file path cannot be opened');
  });
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw new Error('path must name a regular file');
    const current = await realpath(target);
    const now = await stat(current);
    if (
      !contained(root, current) ||
      privatePath(root, current) ||
      info.dev !== now.dev ||
      info.ino !== now.ino
    )
      throw new Error('file path changed while opening');
    const key = sha256(
      JSON.stringify([target, info.size, info.mtimeMs, info.ctimeMs, maxBytes(), 2]),
    );
    return { root, target, handle, info, key, name: path.basename(target) };
  } catch (error) {
    await handle.close();
    throw error;
  }
}
export async function cacheDirectory(root: string): Promise<string> {
  for (const directory of [
    path.join(root, '.chat-files'),
    path.join(root, '.chat-files', 'cache'),
  ]) {
    await mkdir(directory, { mode: 0o700 }).catch(async (error) => {
      if (error.code !== 'EEXIST') throw new Error('private cache cannot be created');
    });
    const info = await lstat(directory);
    if (!info.isDirectory() || info.isSymbolicLink() || (await realpath(directory)) !== directory)
      throw new Error('private cache path must be a real contained directory');
  }
  return path.join(root, '.chat-files', 'cache');
}
export async function atomicWrite(target: string, data: Buffer | string): Promise<void> {
  const temporary = `${target}.${randomUUID()}.tmp`;
  const file = await open(
    temporary,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
    0o600,
  );
  try {
    await file.writeFile(data);
    await file.sync();
  } finally {
    await file.close();
  }
  await rename(temporary, target);
}
export async function boundedRead(handle: FileHandle, limit: number): Promise<Buffer> {
  const buffer = Buffer.alloc(limit + 1);
  let length = 0;
  while (length < buffer.length) {
    const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null);
    if (!bytesRead) break;
    length += bytesRead;
  }
  if (length > limit) throw new Error('file exceeds max_bytes');
  return Buffer.from(buffer.subarray(0, length));
}
export async function cachedBytes(cache: string, name: string, max: number): Promise<Buffer> {
  if (name !== path.basename(name) || name.includes('\0'))
    throw new Error('cached file unavailable');
  const target = path.join(cache, name);
  const handle = await open(
    target,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > max || (await realpath(target)) !== target)
      throw new Error('cached file unavailable or changed');
    return await boundedRead(handle, max);
  } finally {
    await handle.close();
  }
}
interface ResourceEntry {
  id: string;
  piece: string;
  name: string;
  mimeType: string;
  bytes: number;
  sha256: string;
  created: number;
  expires: number;
}
export class ChatResources {
  private tail: Promise<unknown> = Promise.resolve();
  private serial<T>(job: () => Promise<T>): Promise<T> {
    const result = this.tail.then(job);
    this.tail = result.catch(() => {});
    return result;
  }
  private async prune(cache: string): Promise<ResourceEntry[]> {
    const entries: ResourceEntry[] = [];
    for (const name of await readdir(cache)) {
      if (!/^resource-[a-f0-9-]{36}\.json$/.test(name)) continue;
      try {
        const entry = JSON.parse(
          (await cachedBytes(cache, name, 4096)).toString(),
        ) as ResourceEntry;
        if (entry.expires > Date.now() && entry.id === name.slice(9, -5)) entries.push(entry);
        else await unlink(path.join(cache, name));
      } catch {
        /* An unreadable entry cannot be served. */
      }
    }
    return entries.sort((a, b) => a.created - b.created);
  }
  publish(
    cache: string,
    entry: Omit<ResourceEntry, 'id' | 'created' | 'expires'>,
  ): Promise<string> {
    return this.serial(async () => {
      const entries = await this.prune(cache);
      while (entries.length >= 100)
        await unlink(path.join(cache, `resource-${entries.shift()!.id}.json`));
      const id = randomUUID();
      const value = {
        ...entry,
        id,
        created: Math.max(Date.now(), ...entries.map((e) => e.created + 1)),
        expires: Date.now() + 4 * 60 * 60 * 1000,
      };
      await atomicWrite(path.join(cache, `resource-${id}.json`), JSON.stringify(value));
      return `zlib-book://file/${id}`;
    });
  }
  read(uri: string) {
    return this.serial(async () => {
      try {
        const match = /^zlib-book:\/\/file\/([a-f0-9-]{36})$/.exec(uri);
        if (!match) throw new Error();
        const root = await realpath(process.env.ZLIBRARY_OUTPUT_ROOT ?? '');
        const cache = await cacheDirectory(root);
        const entry = (await this.prune(cache)).find((e) => e.id === match[1]);
        if (!entry) throw new McpError(ErrorCode.InvalidParams, 'Unknown or expired book resource');
        const data = await cachedBytes(cache, entry.piece, maxBytes());
        if (data.length !== entry.bytes || sha256(data) !== entry.sha256)
          throw new Error('integrity');
        return { contents: [{ uri, mimeType: entry.mimeType, blob: data.toString('base64') }] };
      } catch (error) {
        if (error instanceof McpError) throw error;
        throw new McpError(
          ErrorCode.InvalidParams,
          'Unknown or expired book resource, or cached file unavailable/changed integrity',
        );
      }
    });
  }
}
export async function readMap(
  cache: string,
  key: string,
): Promise<Record<string, any> | undefined> {
  try {
    return JSON.parse((await cachedBytes(cache, `map-${key}.json`, 4 * 1024 * 1024)).toString());
  } catch {
    return undefined;
  }
}
