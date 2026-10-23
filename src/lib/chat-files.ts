/** Preliminary, explicitly opt-in conversation-file handoff. No network or page text. */
import { spawn } from 'node:child_process';
import * as path from 'node:path';
import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import { getPythonScriptPath } from './paths.js';
import { PdfQueue, pageRenderQueue } from './pdf-queue.js';
import {
  ChatResources,
  atomicWrite,
  boundedRead,
  cacheDirectory,
  cachedBytes,
  maxBytes,
  readMap,
  sha256,
  sourceFile,
} from './chat-files-storage.js';

const delivery =
  'The file arrives as a conversation file after user approval. Delivery is confirmed only by "Tool uploaded file with ID … to: /mnt/data/<name>". If missing, check /mnt/data in Python and request again or request a smaller range.';
export const CHAT_FILE_TOOL_DEFINITIONS: Tool[] = [
  {
    name: 'get_book_map',
    description:
      'Call first for anything large. Reports measured PDF sections and parts, printed page labels, scan/text share and a safe-window planning hint; no page content. Prefer whole sections. ' +
      delivery,
    inputSchema: {
      type: 'object',
      properties: { file: { type: 'string' } },
      required: ['file'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'share_book_file',
    description:
      'Send a local book whole, by section id, or by 1-based inclusive PDF pages (47-66). Call get_book_map first for anything large; prefer whole sections. Oversized ranges send one fitting prefix and report the remainder. ' +
      delivery,
    inputSchema: {
      type: 'object',
      properties: {
        file: { type: 'string' },
        pages: { type: 'string' },
        section: { type: 'string' },
      },
      required: ['file'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'get_page_image',
    description:
      'Look closely at one PDF page: layout, marginalia or columns. Page is 1-based; dpi defaults to 100, clamped to 50–200. Returns an image and printed page label; no extracted text. Call get_book_map first for anything large.',
    inputSchema: {
      type: 'object',
      properties: {
        file: { type: 'string' },
        page: { type: 'integer', minimum: 1 },
        dpi: { type: 'integer', default: 100 },
      },
      required: ['file', 'page'],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];
const mapInput = z.strictObject({ file: z.string().min(1) });
const shareInput = z
  .strictObject({
    file: z.string().min(1),
    pages: z.string().min(1).optional(),
    section: z.string().min(1).optional(),
  })
  .refine((v) => !(v.pages && v.section));
const imageInput = z.strictObject({
  file: z.string().min(1),
  page: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
  dpi: z.number().int().optional(),
});
const pdfQueue = new PdfQueue();
type Source = Awaited<ReturnType<typeof sourceFile>>;
type Facts = Record<string, any>;
const text = (value: unknown): CallToolResult => ({
  content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }],
});

async function pdf(
  command: 'map' | 'excerpt' | 'render',
  source: Source,
  cache: string,
  args: Facts,
  signal?: AbortSignal,
): Promise<Facts> {
  const python = process.env.ZLIBRARY_PDF_PYTHON;
  if (!python || !path.isAbsolute(python))
    throw new Error('ZLIBRARY_PDF_PYTHON must be an absolute interpreter path with PyMuPDF');
  const queue = command === 'render' ? pageRenderQueue : pdfQueue;
  return queue.run(
    (childSignal) =>
      new Promise((resolve, reject) => {
        const child = spawn(python, ['-I', '-B', getPythonScriptPath('chat_files.py'), command], {
          stdio: ['pipe', 'pipe', 'ignore', source.handle.fd],
          signal: childSignal,
          killSignal: 'SIGKILL',
        });
        const chunks: Buffer[] = [];
        let length = 0;
        child.stdout!.on('data', (chunk: Buffer) => {
          length += chunk.length;
          if (length > 5 * 1024 * 1024) child.kill('SIGKILL');
          else chunks.push(chunk);
        });
        child.on('error', () =>
          reject(
            new Error(
              'PDF helper process could not start or request timed out; check ZLIBRARY_PDF_PYTHON',
            ),
          ),
        );
        child.on('close', (code) => {
          if (code !== 0 || length > 5 * 1024 * 1024)
            return reject(
              new Error('PDF helper could not complete within its timeout/output limit'),
            );
          try {
            // The reply is the last line; stray library output before it is ignored.
            const lines = Buffer.concat(chunks).toString().split('\n');
            const value = JSON.parse(lines.filter((line) => line.trim()).pop() ?? '');
            if (value.error) reject(new Error(value.error));
            else resolve(value);
          } catch {
            reject(new Error('PDF helper returned an invalid result'));
          }
        });
        child.stdin!.on('error', () => {});
        child.stdin!.end(
          JSON.stringify({ cache, key: source.key, max_bytes: maxBytes(), ...args }),
        );
      }),
    command === 'map' ? 90000 : command === 'excerpt' ? 60000 : 25000,
    signal,
  );
}
function findSection(nodes: Facts[], id: string): Facts | undefined {
  for (const node of nodes) {
    if (node.id === id) return node;
    const found = findSection(node.children ?? [], id);
    if (found) return found;
  }
}
export class ChatFiles {
  private readonly resources = new ChatResources();
  private readonly pendingMaps = new Map<string, Promise<Facts>>();
  readResource(uri: string) {
    return this.resources.read(uri);
  }
  private async map(source: Source, cache: string, signal?: AbortSignal): Promise<Facts> {
    const format = path.extname(source.name).slice(1).toLowerCase();
    const base = {
      file: source.name,
      format: format === 'pdf' || format === 'epub' ? format : 'other',
      bytes: source.info.size,
      max_bytes: maxBytes(),
      fits_whole: source.info.size <= maxBytes(),
    };
    if (format !== 'pdf')
      return {
        ...base,
        pages: null,
        sections: [],
        suggested_parts: [],
        safe_window_pages: null,
        guidance:
          format === 'epub'
            ? 'Send whole if it fits; EPUB splitting is not supported yet.'
            : 'Send whole if it fits.',
      };
    const cached = await readMap(cache, source.key);
    if (cached) return cached;
    let pending = this.pendingMaps.get(source.key);
    if (!pending) {
      pending = (async () => {
        const result = {
          ...base,
          ...(await pdf('map', source, cache, {}, signal)),
          guidance:
            'Read sections whole when you can. With no outline, request the front matter and read the contents page from its image. Pieces arrive as separate files in /mnt/data and can be joined in Python. Safe windows are planning hints; every send is measured.',
        };
        await atomicWrite(path.join(cache, `map-${source.key}.json`), JSON.stringify(result));
        return result;
      })();
      this.pendingMaps.set(source.key, pending);
      pending.finally(() => this.pendingMaps.delete(source.key)).catch(() => {});
    }
    return pending;
  }
  async call(name: string, args: unknown, signal?: AbortSignal): Promise<CallToolResult> {
    let source: Source | undefined;
    try {
      const schema =
        name === 'get_book_map' ? mapInput : name === 'share_book_file' ? shareInput : imageInput;
      const parsed = schema.safeParse(args);
      if (!parsed.success)
        throw new Error('invalid arguments; pages and section cannot be used together');
      const input = parsed.data as {
        file: string;
        pages?: string;
        section?: string;
        page?: number;
        dpi?: number;
      };
      source = await sourceFile(input.file);
      const cache = await cacheDirectory(source.root);
      const max = maxBytes();
      const format = path.extname(source.name).toLowerCase();
      if (name === 'get_book_map') return text(await this.map(source, cache, signal));
      if (name === 'get_page_image') {
        if (format !== '.pdf') throw new Error('get_page_image requires a PDF');
        const r = await pdf(
          'render',
          source,
          cache,
          { page: input.page, dpi: Math.max(50, Math.min(200, input.dpi ?? 100)) },
          signal,
        );
        const bytes = Buffer.from(r.data, 'base64');
        if (
          !bytes.length ||
          bytes.length > 3 * 1024 * 1024 ||
          bytes.toString('base64') !== r.data ||
          r.width > 1600 ||
          r.height > 1600
        )
          throw new Error('PDF render exceeded the image byte limit');
        return {
          content: [
            { type: 'image', mimeType: r.mimeType, data: r.data },
            {
              type: 'text',
              text: `page ${r.page} of ${r.pages}${r.printed_page ? `, printed page ${r.printed_page}` : ''}, ${r.width}x${r.height} px, ${bytes.length} bytes`,
            },
          ],
        };
      }
      let pages = input.pages;
      if (input.section) {
        if (format !== '.pdf') throw new Error('section requires a PDF');
        const section = findSection(
          (await this.map(source, cache, signal)).sections,
          input.section,
        );
        if (!section) throw new Error('unknown section id; call get_book_map');
        pages = section.pdf_pages;
      }
      let piece: string;
      let facts: Facts;
      let filename = source.name;
      if (pages) {
        if (format !== '.pdf') throw new Error('pages requires a PDF');
        const result = await pdf('excerpt', source, cache, { pages }, signal);
        if (result.too_large_page) return text(result);
        piece = result.piece;
        filename = `${path.parse(source.name).name}__pp${result.pdf_pages}.pdf`;
        facts = { ...result };
        delete facts.piece;
      } else {
        if (source.info.size > max)
          return text(
            format === '.epub'
              ? 'Too large to send; EPUB splitting is not supported yet.'
              : 'Too large to send whole; call get_book_map and request a section or suggested page range.',
          );
        const bytes = await boundedRead(source.handle, max);
        const digest = sha256(bytes);
        piece = `whole-${digest}${format.replace(/[^a-z0-9.]/g, '')}`;
        await atomicWrite(path.join(cache, piece), bytes);
        facts = { bytes: bytes.length, sha256: digest, pdf_pages: null };
      }
      // Final independent size/hash guard on the exact cached bytes served by resources/read.
      const bytes = await cachedBytes(cache, piece, max);
      if (bytes.length !== facts.bytes || sha256(bytes) !== facts.sha256)
        throw new Error('cached piece changed before send');
      const mimeType =
        format === '.pdf'
          ? 'application/pdf'
          : format === '.epub'
            ? 'application/epub+zip'
            : 'application/octet-stream';
      const uri = await this.resources.publish(cache, {
        piece,
        name: filename,
        mimeType,
        bytes: bytes.length,
        sha256: facts.sha256,
      });
      const result = {
        name: filename,
        ...facts,
        ...(facts.remaining
          ? {
              message: `sent PDF pp. ${facts.pdf_pages}${facts.printed_pages ? ` (printed ${facts.printed_pages})` : ''}; remaining: pages ${facts.remaining} — call share_book_file again with pages "${facts.remaining}"`,
            }
          : {}),
      };
      return {
        content: [
          { type: 'resource_link', uri, mimeType, name: filename, size: bytes.length },
          { type: 'text', text: JSON.stringify(result) },
        ],
      };
    } catch (error) {
      // Known messages describe only metadata/validation. Never emit raw OS errors or helper stderr.
      const message =
        error instanceof Error && !('code' in error)
          ? error.message
          : 'file operation could not complete';
      return { isError: true, content: [{ type: 'text', text: `Chat files error: ${message}` }] };
    } finally {
      await source?.handle.close();
    }
  }
}
