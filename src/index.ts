#!/usr/bin/env node
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import { AcquisitionTools, INPUTS } from './lib/acquisition-tools.js';
import type { ToolName } from './lib/acquisition-tools.js';
import { getSessionMode } from './lib/eapi-session.js';
import { invokeAcquisitionBridge } from './lib/zlibrary-api.js';
import { logger } from './lib/logger.js';
import { HandoffProbes, PROBE_TOOL_DEFINITIONS } from './lib/handoff-probes.js';
import { ChatFiles, CHAT_FILE_TOOL_DEFINITIONS } from './lib/chat-files.js';
import { getRuntimeDigest } from './lib/build-info.js';

const browserSearchDescription =
  'Metadata, not book text. Give query and/or author; exact covers both. sources is ordered: libgen needs no account/quota; zlibrary needs an account, downloads spend quota; annas searches without a key; downloads need a key or the opted-in visible browser (ANNAS_BROWSER_ENABLED). Defaults: get_limits. Use refs. Refs last this process and 5,000 recent entries; on ref_unavailable search and choose again. Pass cursor alone: replay fetches the same position; pages are re-queried, not frozen. Anna’s may be slow: its browser handles one page at a time; get_limits shows the queue.';
const descriptions: Record<ToolName, string> = {
  search:
    'Metadata, not book text. Give query and/or author; exact covers both. sources is ordered: libgen needs no account/quota; zlibrary needs an account, downloads spend quota; annas searches without a key, downloads with one. Defaults: get_limits. Use refs. Refs last this process and 5,000 recent entries; on ref_unavailable search and choose again. Pass cursor alone: replay fetches the same position; pages are re-queried, not frozen. Anna’s may be slow: its browser handles one page at a time; get_limits shows the queue.',
  get_book:
    'Edition evidence for a ref. Zlibrary costs one detail call; others return held rows. Description capped at 1,000 characters.',
  download:
    'Download a ref to server disk: path, bytes and record sidecar, not text. Repeats at the same destination re-hash sha256 and skip an intact copy without quota; different output_dir or filename transfers again. Local O_EXCL locks coalesce processes. Anna’s may be slow: its browser handles one page at a time; get_limits shows the queue.',
  get_limits:
    'Capabilities and default source order. mode:local (default) costs no network. mode:live fetches account numbers for named sources; only zlibrary costs an account call. Other clients can change estimates; errors are per source.',
  get_history:
    'Zlibrary account history in source order. Not local files. Refs last this process and 5,000 entries; search again if unavailable. Pass cursor alone: replay fetches the same position; pages are not frozen.',
};
// Wire schemas describe each arm; the richer closed-code validator also runs independently.
const errorShape = {
  required: ['error'],
  properties: {
    error: {
      type: 'object',
      required: ['code', 'operation', 'message', 'retry', 'recovery', 'attempts', 'detail'],
      properties: {
        code: { type: 'string' },
        retry: { enum: ['safe', 'may_spend_quota', 'no'] },
        message: { type: 'string' },
        attempts: { type: 'array', items: { type: 'object' } },
        detail: { type: 'object' },
      },
    },
  },
};
const viaShape = { type: 'array', items: { type: 'object' } };
const successShapes: Record<ToolName, Record<string, any>> = {
  search: {
    required: ['rows', 'routing', 'via'],
    properties: {
      rows: { type: 'array', items: { type: 'object', required: ['ref', 'source'] } },
      via: viaShape,
      routing: { type: 'object', required: ['requested', 'attempts', 'fell_back'] },
      next_cursor: { type: 'string' },
      note: { type: 'string' },
    },
  },
  get_book: {
    required: ['ref', 'source', 'identifiers', 'evidence', 'via'],
    properties: {
      ref: { type: 'string' },
      via: viaShape,
      identifiers: { type: 'object' },
      evidence: { type: 'object' },
      description: { type: 'string', maxLength: 1000 },
    },
  },
  download: {
    required: ['path', 'bytes', 'reused', 'transfer_skipped', 'verified', 'record', 'via'],
    properties: {
      path: { type: 'string' },
      bytes: { type: 'integer', minimum: 0 },
      reused: { type: 'boolean' },
      transfer_skipped: { type: 'boolean' },
      verified: { const: 'sha256' },
      record: { type: 'string' },
      via: viaShape,
      quota_estimate: { type: 'object' },
    },
  },
  get_limits: {
    required: ['sources', 'routes'],
    properties: {
      sources: { type: 'object' },
      routes: { type: 'object' },
      default_order: { type: 'array', items: { type: 'string' } },
    },
  },
  get_history: {
    required: ['rows'],
    properties: {
      rows: { type: 'array', items: { type: 'object' } },
      next_cursor: { type: 'string' },
    },
  },
};
export const TOOL_DEFINITIONS: Tool[] = (Object.keys(INPUTS) as ToolName[]).map((name) => {
  const json = z.toJSONSchema(INPUTS[name]);
  delete json.$schema;
  if ((json.properties as any)?.sources) (json.properties as any).sources.uniqueItems = true;
  return {
    name,
    description: descriptions[name],
    inputSchema: json as Tool['inputSchema'],
    outputSchema: {
      type: 'object',
      oneOf: [{ ...successShapes[name], not: { required: ['error'] } }, errorShape],
    },
    annotations: {
      readOnlyHint: name !== 'download',
      ...(name === 'download' ? { destructiveHint: false } : {}),
      idempotentHint: ['search', 'get_book', 'download'].includes(name),
      openWorldHint: true,
    },
  };
});

/** Lower-level SDK dispatch retains typed errors for invalid schemas and removed names. */
class AcquisitionServer extends Server {
  private definitions: Tool[] = [];
  readonly tools = new AcquisitionTools(invokeAcquisitionBridge);
  constructor() {
    const probes = process.env.ZLIBRARY_PROBE_TOOLS === '1' ? new HandoffProbes() : undefined;
    const chatFiles = process.env.ZLIBRARY_CHAT_FILES === '1' ? new ChatFiles() : undefined;
    super(
      { name: 'book-acquisition', version: getServerVersion() },
      { capabilities: { tools: {}, ...(probes || chatFiles ? { resources: {} } : {}) } },
    );
    this.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [
        ...this.definitions,
        ...(probes ? PROBE_TOOL_DEFINITIONS : []),
        ...(chatFiles ? CHAT_FILE_TOOL_DEFINITIONS : []),
      ],
    }));
    if (probes || chatFiles)
      this.setRequestHandler(ReadResourceRequestSchema, async (request) =>
        chatFiles && request.params.uri.startsWith('zlib-book://')
          ? chatFiles.readResource(request.params.uri)
          : probes
            ? probes.readResource(request.params.uri)
            : chatFiles!.readResource(request.params.uri),
      );
    this.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
      if (chatFiles && CHAT_FILE_TOOL_DEFINITIONS.some((tool) => tool.name === request.params.name))
        return chatFiles.call(request.params.name, request.params.arguments ?? {}, extra.signal);
      if (probes && PROBE_TOOL_DEFINITIONS.some((tool) => tool.name === request.params.name))
        return probes.call(request.params.name, request.params.arguments ?? {}, extra.signal);
      const result = await this.tools.call(request.params.name, request.params.arguments ?? {}, {
        signal: extra.signal,
      });
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(result) }],
        structuredContent: result,
        ...(result.error ? { isError: true } : {}),
      };
    });
  }
  registerTool(name: ToolName) {
    const definition = TOOL_DEFINITIONS.find((t) => t.name === name)!;
    const browserEnabled = ['1', 'true', 'yes'].includes(
      (process.env.ANNAS_BROWSER_ENABLED ?? '').trim().toLowerCase(),
    );
    this.definitions.push(
      name === 'search' && browserEnabled
        ? { ...definition, description: browserSearchDescription }
        : definition,
    );
  }
}
export function createServer(): AcquisitionServer {
  getSessionMode();
  const server = new AcquisitionServer();
  server.registerTool('search');
  server.registerTool('get_book');
  server.registerTool('download');
  server.registerTool('get_limits');
  server.registerTool('get_history');
  return server;
}
function getPackageVersion(): string {
  try {
    return JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;
  } catch {
    return 'unknown';
  }
}
/** Package version plus the commit dist/ was built from (`1.4.0+54e8368`), so clients can tell builds apart. */
export function getServerVersion(
  buildInfoUrl = new URL('../build-info.json', import.meta.url),
): string {
  const version = getPackageVersion();
  try {
    const info = JSON.parse(fs.readFileSync(buildInfoUrl, 'utf8'));
    if (typeof info.commit !== 'string' || !/^[0-9a-f]{7,40}$/.test(info.commit)) return version;
    if (
      typeof info.runtime_sha256 !== 'string' ||
      !/^[0-9a-f]{64}$/.test(info.runtime_sha256) ||
      info.runtime_sha256 !== getRuntimeDigest(fileURLToPath(new URL('./', import.meta.url)))
    )
      return version;
    return `${version}+${info.commit}${info.dirty === true ? '.dirty' : ''}`;
  } catch {
    return version;
  }
}
export async function start(opts: { testing?: boolean } = {}) {
  try {
    if (!opts.testing && (!process.env.ZLIBRARY_EMAIL || !process.env.ZLIBRARY_PASSWORD))
      logger.warn(
        'Z-Library account is not configured; LibGen remains available without an account. See get_limits.',
      );
    const server = createServer();
    const transport = new StdioServerTransport();
    await server.connect(transport);
    logger.info('Book acquisition MCP server is running via Stdio.');
    return { server, transport };
  } catch (error) {
    logger.error('Failed to start book acquisition server', error);
    if (!opts.testing) process.exitCode = 1;
    return null;
  }
}
export function isProcessEntryPoint(moduleUrl: string, argv1: string | undefined): boolean {
  if (!argv1) return false;
  // npm installs the `zlibrary-mcp` bin as a symlink into <prefix>/bin, so
  // argv[1] is the link path while import.meta.url is the real file under the
  // package directory — a plain path.resolve comparison never matches and the
  // server silently refused to auto-start for global-install users. Resolve
  // symlinks when the path exists; fall back to lexical resolution so the
  // comparison still works for paths that do not exist on this filesystem.
  const canonical = (p: string): string => {
    try {
      return fs.realpathSync(p);
    } catch {
      return path.resolve(p);
    }
  };
  try {
    return canonical(fileURLToPath(moduleUrl)) === canonical(argv1);
  } catch {
    // A non-file:// URL (e.g. a bundler's virtual module) is never the entry point.
    return false;
  }
}

if (isProcessEntryPoint(import.meta.url, process.argv[1])) {
  void start();
}
