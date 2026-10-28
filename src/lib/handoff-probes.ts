/** Opt-in diagnostics only; never part of the default acquisition interface. */
import { constants } from 'node:fs';
import { open, realpath, stat } from 'node:fs/promises';
import * as path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js';
import type {
  BlobResourceContents,
  CallToolResult,
  Tool,
} from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import { getPythonScriptPath } from './paths.js';
import { pageRenderQueue, PdfQueueBusyError } from './pdf-queue.js';

class ProbeError extends Error {}

// Probe-only: ZLIBRARY_PROBE_MAX_FILE_BYTES raises the cap to find the tunnel's real ceiling (1 B to 32 MiB).
const requestedMax = Number(process.env.ZLIBRARY_PROBE_MAX_FILE_BYTES);
const MAX_FILE =
  Number.isInteger(requestedMax) && requestedMax > 0 && requestedMax <= 32 * 1024 * 1024
    ? requestedMax
    : 6 * 1024 * 1024;
const MAX_FILE_LABEL = `${+(MAX_FILE / 1024 / 1024).toFixed(2)} MiB`;
const MAX_IMAGE = 3 * 1024 * 1024;
const TTL = 30 * 60 * 1000;
const fileInput = z.strictObject({
  path: z.string().min(1),
  mode: z.enum(['resource_link', 'embedded']),
});
const imageInput = z.strictObject({
  path: z.string().min(1),
  page: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
  dpi: z.number().int().optional(),
});
export const PROBE_TOOL_DEFINITIONS: Tool[] = [
  {
    name: 'probe_file_link',
    description:
      'Diagnostic: expose an unchanged local file as an MCP resource. Path is relative to ZLIBRARY_OUTPUT_ROOT; 6 MiB maximum. Reports only name, bytes and SHA-256.',
    inputSchema: {
      type: 'object',
      properties: { path: { type: 'string' }, mode: { enum: ['resource_link', 'embedded'] } },
      required: ['path', 'mode'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'probe_page_image',
    description:
      'Diagnostic: render one PDF page as an image, without extracting text. Path is relative to ZLIBRARY_OUTPUT_ROOT; page is 1-based. Requires ZLIBRARY_PROBE_PYTHON. DPI defaults to 100 and is clamped to 50–200; longest side is at most 1600 px.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string' },
        page: { type: 'integer', minimum: 1 },
        dpi: { type: 'integer', default: 100 },
      },
      required: ['path', 'page'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];

function contained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

async function readContainedFile(
  input: string,
): Promise<{ bytes: Buffer; name: string; mimeType: string }> {
  if (
    input.includes('\0') ||
    path.isAbsolute(input) ||
    path.win32.isAbsolute(input) ||
    /^[a-z]:/i.test(input) ||
    input.split(/[\\/]/).includes('..')
  )
    throw new ProbeError('path must be relative and contained in the output root');
  const configured = process.env.ZLIBRARY_OUTPUT_ROOT;
  if (!configured) throw new ProbeError('ZLIBRARY_OUTPUT_ROOT must be set for probes');
  let root: string, target: string;
  try {
    root = await realpath(configured);
    target = await realpath(path.resolve(root, input));
  } catch {
    throw new ProbeError('file path is unavailable');
  }
  if (!contained(root, target)) throw new ProbeError('file path is outside the output root');
  // O_NONBLOCK keeps a FIFO swapped in after realpath from hanging open().
  const handle = await open(
    target,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  ).catch(() => {
    throw new ProbeError('file path cannot be opened');
  });
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw new ProbeError('path must name a regular file');
    if (info.size > MAX_FILE) throw new ProbeError(`file exceeds ${MAX_FILE_LABEL}`);
    const current = await realpath(target);
    const currentInfo = await stat(current);
    if (!contained(root, current) || info.dev !== currentInfo.dev || info.ino !== currentInfo.ino)
      throw new ProbeError('file path changed while opening');
    // Read from the checked descriptor, with a hard limit even if the file grows.
    const buffer = Buffer.alloc(MAX_FILE + 1);
    let length = 0;
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null);
      if (!bytesRead) break;
      length += bytesRead;
    }
    if (length > MAX_FILE) throw new ProbeError(`file exceeds ${MAX_FILE_LABEL}`);
    const mimeTypes: Record<string, string> = {
      '.pdf': 'application/pdf',
      '.epub': 'application/epub+zip',
      '.txt': 'text/plain',
      '.png': 'image/png',
      '.jpg': 'image/jpeg',
      '.jpeg': 'image/jpeg',
    };
    return {
      bytes: Buffer.from(buffer.subarray(0, length)),
      name: path.basename(target),
      mimeType: mimeTypes[path.extname(target).toLowerCase()] ?? 'application/octet-stream',
    };
  } finally {
    await handle.close();
  }
}

function render(
  bytes: Buffer,
  python: string,
  page: number,
  dpi: number,
  signal?: AbortSignal,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(
      python,
      ['-I', '-B', getPythonScriptPath('probe_page_image.py'), String(page), String(dpi)],
      {
        timeout: 25000,
        killSignal: 'SIGKILL',
        maxBuffer: 4 * 1024 * 1024 + 4096,
        encoding: 'utf8',
        signal,
      },
      (error, stdout) => {
        if (error)
          reject(new ProbeError('PDF renderer could not complete (25 s timeout or process error)'));
        else resolve(stdout);
      },
    );
    child.stdin?.on('error', () => {
      /* Early child exit is handled by the callback. */
    });
    child.stdin?.end(bytes);
  });
}

export class HandoffProbes {
  private readonly files = new Map<string, { resource: BlobResourceContents; expires: number }>();

  private prune() {
    const now = Date.now();
    for (const [id, entry] of this.files) if (entry.expires <= now) this.files.delete(id);
  }

  readResource(uri: string): { contents: BlobResourceContents[] } {
    this.prune();
    const entry = this.files.get(uri);
    if (!entry) throw new McpError(ErrorCode.InvalidParams, 'Unknown or expired probe resource');
    return { contents: [{ ...entry.resource }] };
  }

  async call(name: string, args: unknown, signal?: AbortSignal): Promise<CallToolResult> {
    try {
      if (name === 'probe_file_link') {
        const parsed = fileInput.safeParse(args);
        if (!parsed.success) throw new ProbeError('invalid arguments');
        const { bytes, name: filename, mimeType } = await readContainedFile(parsed.data.path);
        const uri = `zlib-probe://file/${randomUUID()}`;
        const resource = { uri, mimeType, blob: bytes.toString('base64') };
        const facts = {
          type: 'text' as const,
          text: JSON.stringify({
            name: filename,
            bytes: bytes.length,
            sha256: createHash('sha256').update(bytes).digest('hex'),
          }),
        };
        if (parsed.data.mode === 'embedded')
          return { content: [{ type: 'resource', resource }, facts] };
        this.prune();
        while (this.files.size >= 20) this.files.delete(this.files.keys().next().value!);
        this.files.set(uri, { resource, expires: Date.now() + TTL });
        return {
          content: [
            { type: 'resource_link', uri, mimeType, name: filename, size: bytes.length },
            facts,
          ],
        };
      }
      const parsed = imageInput.safeParse(args);
      if (!parsed.success) throw new ProbeError('invalid arguments');
      const { bytes } = await readContainedFile(parsed.data.path);
      const python = process.env.ZLIBRARY_PROBE_PYTHON;
      if (!python || !path.isAbsolute(python))
        throw new ProbeError('ZLIBRARY_PROBE_PYTHON must be an absolute interpreter path');
      {
        const output = JSON.parse(
          await pageRenderQueue.run(
            (queuedSignal) =>
              render(
                bytes,
                python,
                parsed.data.page,
                Math.max(50, Math.min(200, parsed.data.dpi ?? 100)),
                queuedSignal,
              ),
            25000,
            signal,
          ),
        );
        if (output.error === 'page_out_of_range')
          throw new ProbeError('page is outside the PDF page range');
        const result = z
          .strictObject({
            page: z.number().int().positive(),
            pages: z.number().int().positive(),
            width: z.number().int().min(1).max(1600),
            height: z.number().int().min(1).max(1600),
            mimeType: z.enum(['image/png', 'image/jpeg']),
            data: z.string(),
          })
          .safeParse(output);
        if (!result.success) throw new ProbeError('PDF render failed');
        const value = result.data;
        const imageBytes = Buffer.from(value.data, 'base64');
        if (
          !imageBytes.length ||
          imageBytes.length > MAX_IMAGE ||
          imageBytes.toString('base64') !== value.data
        )
          throw new ProbeError('PDF render exceeded the image byte limit');
        return {
          content: [
            { type: 'image', mimeType: value.mimeType, data: value.data },
            {
              type: 'text',
              text: `page ${value.page} of ${value.pages}, ${value.width}x${value.height} px, ${imageBytes.length} bytes`,
            },
          ],
        };
      }
    } catch (error) {
      // Never include helper stderr, PDF bytes, extracted text or raw OS paths.
      const message =
        error instanceof ProbeError || error instanceof PdfQueueBusyError
          ? error.message
          : 'file probe could not complete';
      return { isError: true, content: [{ type: 'text', text: `Probe error: ${message}` }] };
    }
  }
}
