#!/usr/bin/env node

import type { ZodObject, ZodRawShape } from 'zod';
import { z } from 'zod';
// UV Migration Note: ensureVenvReady removed - user runs `uv sync` before build
import * as fs from 'fs';
import { appendFile as appendFileAsync, mkdir as mkdirAsync } from 'fs/promises';
import * as path from 'path';
import { fileURLToPath } from 'url';

// Import SDK components using ESM syntax
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

// Import API handlers
import * as zlibraryApi from './lib/zlibrary-api.js';
import { logger } from './lib/logger.js';

// Recreate __dirname for ESM
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Define Zod schemas for tool parameters
const SearchBooksParamsSchema = z.object({
  query: z.string().describe('Search query'),
  exact: z.boolean().optional().default(false).describe('Whether to perform an exact match search'),
  fromYear: z.number().int().optional().describe('Filter by minimum publication year'),
  toYear: z.number().int().optional().describe('Filter by maximum publication year'),
  languages: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by languages (e.g., ["english", "russian"])'),
  extensions: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by file extensions (e.g., ["pdf", "epub"])'),
  content_types: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by content types (e.g., ["book", "article"])'),
  count: z.number().int().optional().default(10).describe('Number of results to return per page'),
});

const FullTextSearchParamsSchema = z.object({
  query: z.string().describe('Text to search for in book content'),
  exact: z.boolean().optional().default(false).describe('Whether to perform an exact match search'),
  phrase: z
    .boolean()
    .optional()
    .default(true)
    .describe('Whether to search for the exact phrase (requires at least 2 words)'),
  words: z.boolean().optional().default(false).describe('Whether to search for individual words'),
  languages: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by languages (e.g., ["english", "russian"])'),
  extensions: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by file extensions (e.g., ["pdf", "epub"])'),
  content_types: z
    .array(z.string())
    .optional()
    .default([])
    .describe('Filter by content types (e.g., ["book", "article"])'),
  count: z.number().int().optional().default(10).describe('Number of results to return per page'),
});

const GetDownloadHistoryParamsSchema = z.object({
  count: z.number().int().optional().default(10).describe('Number of results to return'),
});

const SourceNameSchema = z.enum(['annas_archive', 'libgen', 'zlibrary']);

const GetDownloadLimitsParamsSchema = z.object({
  sources: z
    .array(SourceNameSchema)
    .nonempty()
    .optional()
    .describe(
      'Which sources to report. All of them when omitted. Naming only sources whose limits are known from configuration (libgen, annas_archive) avoids the Z-Library profile round-trip entirely.',
    ),
});

const GetRecentBooksParamsSchema = z.object({
  count: z.number().int().optional().default(10).describe('Number of books to return'),
  format: z.string().optional().describe('Filter by file format (e.g., "pdf", "epub")'),
});

const DownloadBookToFileParamsSchema = z.object({
  bookDetails: z
    .object({})
    .loose()
    .describe('The full book details object obtained from search_books'),
  outputDir: z
    .string()
    .optional()
    .default('./downloads')
    .describe('Directory to save the file to (default: "./downloads")'),
  process_for_rag: z
    .boolean()
    .optional()
    .describe('Whether to process the document content for RAG after download'),
  processed_output_format: z
    .string()
    .optional()
    .describe('Desired output format for RAG processing (e.g., "text", "markdown")'),
});

const ProcessDocumentForRagParamsSchema = z.object({
  file_path: z.string().describe('Path to the downloaded file to process'),
  output_format: z.string().optional().describe('Desired output format (e.g., "text", "markdown")'),
});

const GetBookMetadataParamsSchema = z.object({
  bookId: z.string().describe('Z-Library book ID'),
  bookHash: z.string().describe('Book hash (can be extracted from book URL)'),
  include: z
    .array(z.enum(['terms', 'booklists', 'ipfs', 'ratings', 'description']))
    .optional()
    .describe(
      'Optional field groups to include beyond core defaults. Core always includes: title, author, year, publisher, language, pages, isbn, rating, cover, categories, extension, filesize. Use include to add: terms (conceptual keywords), booklists (curated collections), ipfs (IPFS CIDs), ratings (quality_score), description (full text description).',
    ),
});

const SearchByTermParamsSchema = z.object({
  term: z.string().describe('Conceptual term to search for (e.g., "dialectic", "phenomenology")'),
  yearFrom: z.number().int().optional().describe('Filter by minimum publication year'),
  yearTo: z.number().int().optional().describe('Filter by maximum publication year'),
  languages: z.array(z.string()).optional().default([]).describe('Filter by languages'),
  extensions: z.array(z.string()).optional().default([]).describe('Filter by file extensions'),
  count: z.number().int().optional().default(25).describe('Number of results to return'),
});

const SearchByAuthorParamsSchema = z.object({
  author: z.string().describe('Author name (supports "Lastname, Firstname" format)'),
  exact: z.boolean().optional().default(false).describe('Use exact author name matching'),
  yearFrom: z.number().int().optional().describe('Filter by minimum publication year'),
  yearTo: z.number().int().optional().describe('Filter by maximum publication year'),
  languages: z.array(z.string()).optional().default([]).describe('Filter by languages'),
  extensions: z.array(z.string()).optional().default([]).describe('Filter by file extensions'),
  count: z.number().int().optional().default(25).describe('Number of results to return'),
});

const FetchBooklistParamsSchema = z.object({
  booklistId: z.string().describe('Booklist ID from book metadata'),
  booklistHash: z.string().describe('Booklist hash from book metadata'),
  topic: z.string().describe('Booklist topic name'),
  page: z.number().int().optional().default(1).describe('Page number for pagination'),
});

const SearchAdvancedParamsSchema = z.object({
  query: z.string().describe('Search query'),
  exact: z.boolean().optional().default(false).describe('Whether to perform exact match search'),
  yearFrom: z.number().int().optional().describe('Filter by minimum publication year'),
  yearTo: z.number().int().optional().describe('Filter by maximum publication year'),
  count: z.number().int().optional().default(10).describe('Number of results to return'),
});

const SearchMultiSourceParamsSchema = z.object({
  query: z.string().describe('Search query'),
  source: z
    .enum(['auto', 'annas', 'libgen'])
    .optional()
    .default('auto')
    .describe(
      "Source selection: auto (Anna's Archive if key available, else LibGen), annas (force Anna's Archive), or libgen (force LibGen)",
    ),
  count: z.number().int().optional().default(10).describe('Maximum number of results to return'),
});

// ============================================================================
// Tool Annotations (MCP Best Practice - helps AI assistants make better decisions)
// ============================================================================

interface ToolAnnotations {
  readOnlyHint?: boolean;
  destructiveHint?: boolean;
  idempotentHint?: boolean;
  openWorldHint?: boolean;
  title?: string;
}

const toolAnnotations: Record<string, ToolAnnotations> = {
  search_books: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Search Books',
  },
  full_text_search: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Full Text Search',
  },
  get_download_history: {
    readOnlyHint: true,
    idempotentHint: false,
    openWorldHint: true,
    title: 'Download History',
  },
  get_download_limits: {
    readOnlyHint: true,
    idempotentHint: false,
    openWorldHint: true,
    title: 'Download Limits',
  },
  download_book_to_file: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: false,
    openWorldHint: true,
    title: 'Download Book',
  },
  process_document_for_rag: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: false,
    title: 'Process for RAG',
  },
  get_book_metadata: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Book Metadata',
  },
  search_by_term: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Search by Term',
  },
  search_by_author: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Search by Author',
  },
  fetch_booklist: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Fetch Booklist',
  },
  search_advanced: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Advanced Search',
  },
  search_multi_source: {
    readOnlyHint: true,
    idempotentHint: true,
    openWorldHint: true,
    title: 'Multi-Source Search',
  },
};

// ============================================================================
// Tool handler implementations
// ============================================================================

/**
 * Cancellation context for a tool call.
 *
 * Every handler takes it, not just the multi-source ones: the subprocess
 * budget bounds a hung call, but only the client's own signal ends one the
 * client has already stopped waiting for. Without it a cancelled download
 * keeps running for up to PYTHON_BRIDGE_LONG_TIMEOUT with nobody to receive
 * the result.
 */
interface HandlerOptions {
  signal?: AbortSignal;
}

interface HandlerMap {
  [key: string]: (args: any, options?: HandlerOptions) => Promise<any>;
  searchBooks: (args: any, options?: HandlerOptions) => Promise<any>;
  fullTextSearch: (args: any, options?: HandlerOptions) => Promise<any>;
  getDownloadHistory: (args: any, options?: HandlerOptions) => Promise<any>;
  getDownloadLimits: (args: any, options?: HandlerOptions) => Promise<any>;
  downloadBookToFile: (args: any, options?: HandlerOptions) => Promise<any>;
  processDocumentForRag: (args: any, options?: HandlerOptions) => Promise<any>;
  getBookMetadata: (args: any, options?: HandlerOptions) => Promise<any>;
  searchByTerm: (args: any, options?: HandlerOptions) => Promise<any>;
  searchByAuthor: (args: any, options?: HandlerOptions) => Promise<any>;
  fetchBooklist: (args: any, options?: HandlerOptions) => Promise<any>;
  searchAdvanced: (args: any, options?: HandlerOptions) => Promise<any>;
  searchMultiSource: (args: any, options?: HandlerOptions) => Promise<any>;
}

const handlers: HandlerMap = {
  searchBooks: async (
    args: z.infer<typeof SearchBooksParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      const searchBooksReceivedArgsLog = `[${new Date().toISOString()}] [src/index.ts] searchBooks handler received Zod-parsed args: ${JSON.stringify(args)}\n`;
      logger.debug(searchBooksReceivedArgsLog.trim());
      try {
        const logFilePath = path.resolve(__dirname, '..', 'logs', 'nodejs_debug.log');
        await mkdirAsync(path.dirname(logFilePath), { recursive: true });
        await appendFileAsync(logFilePath, searchBooksReceivedArgsLog);
      } catch (e) {
        console.error('Failed to write to logs/nodejs_debug.log', e);
      }
      const apiArgs = {
        query: args.query,
        exact: args.exact,
        fromYear: args.fromYear,
        toYear: args.toYear,
        languages: args.languages,
        extensions: args.extensions,
        content_types: args.content_types,
        count: args.count,
      };
      const searchBooksSendingLog = `[${new Date().toISOString()}] [src/index.ts] searchBooks handler sending to zlibraryApi: ${JSON.stringify(apiArgs)}\n`;
      logger.debug(searchBooksSendingLog.trim());
      try {
        const logFilePath = path.resolve(__dirname, '..', 'logs', 'nodejs_debug.log');
        await appendFileAsync(logFilePath, searchBooksSendingLog);
      } catch (e) {
        console.error('Failed to write to logs/nodejs_debug.log', e);
      }
      return await zlibraryApi.searchBooks(apiArgs, options);
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to search books' } };
    }
  },

  fullTextSearch: async (
    args: z.infer<typeof FullTextSearchParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      const ftsReceivedArgsLog = `[${new Date().toISOString()}] [src/index.ts] fullTextSearch handler received Zod-parsed args: ${JSON.stringify(args)}\n`;
      logger.debug(ftsReceivedArgsLog.trim());
      try {
        const logFilePath = path.resolve(__dirname, '..', 'logs', 'nodejs_debug.log');
        await mkdirAsync(path.dirname(logFilePath), { recursive: true });
        await appendFileAsync(logFilePath, ftsReceivedArgsLog);
      } catch (e) {
        console.error('Failed to write to logs/nodejs_debug.log', e);
      }
      const apiArgsFTS = {
        query: args.query,
        exact: args.exact,
        phrase: args.phrase,
        words: args.words,
        languages: args.languages,
        extensions: args.extensions,
        content_types: args.content_types,
        count: args.count,
      };
      const ftsSendingLog = `[${new Date().toISOString()}] [src/index.ts] fullTextSearch handler sending to zlibraryApi: ${JSON.stringify(apiArgsFTS)}\n`;
      logger.debug(ftsSendingLog.trim());
      try {
        const logFilePath = path.resolve(__dirname, '..', 'logs', 'nodejs_debug.log');
        await appendFileAsync(logFilePath, ftsSendingLog);
      } catch (e) {
        console.error('Failed to write to logs/nodejs_debug.log', e);
      }
      return await zlibraryApi.fullTextSearch(apiArgsFTS, options);
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to perform full text search' } };
    }
  },

  getDownloadHistory: async (
    args: z.infer<typeof GetDownloadHistoryParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.getDownloadHistory(args, options);
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to get download history' } };
    }
  },

  getDownloadLimits: async (
    args: z.infer<typeof GetDownloadLimitsParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.getDownloadLimits({ sources: args?.sources }, options);
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to get download limits' } };
    }
  },

  downloadBookToFile: async (
    args: z.infer<typeof DownloadBookToFileParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.downloadBookToFile(args, options);
    } catch (error: any) {
      const details = error?.context?.details;
      return {
        error: {
          message: error.message || 'Failed to download book',
          ...(details === undefined ? {} : { details }),
        },
      };
    }
  },

  processDocumentForRag: async (
    args: z.infer<typeof ProcessDocumentForRagParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.processDocumentForRag(
        {
          filePath: args.file_path,
          outputFormat: args.output_format,
        },
        options,
      );
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to process document for RAG' } };
    }
  },

  getBookMetadata: async (
    args: z.infer<typeof GetBookMetadataParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.getBookMetadata(args.bookId, args.bookHash, args.include, options);
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to get book metadata' } };
    }
  },

  searchByTerm: async (
    args: z.infer<typeof SearchByTermParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.searchByTerm(
        {
          term: args.term,
          yearFrom: args.yearFrom,
          yearTo: args.yearTo,
          languages: args.languages,
          extensions: args.extensions,
          limit: args.count,
        },
        options,
      );
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to search by term' } };
    }
  },

  searchByAuthor: async (
    args: z.infer<typeof SearchByAuthorParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.searchByAuthor(
        {
          author: args.author,
          exact: args.exact,
          yearFrom: args.yearFrom,
          yearTo: args.yearTo,
          languages: args.languages,
          extensions: args.extensions,
          limit: args.count,
        },
        options,
      );
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to search by author' } };
    }
  },

  fetchBooklist: async (
    args: z.infer<typeof FetchBooklistParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.fetchBooklist(
        {
          booklistId: args.booklistId,
          booklistHash: args.booklistHash,
          topic: args.topic,
          page: args.page,
        },
        options,
      );
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to fetch booklist' } };
    }
  },

  searchAdvanced: async (
    args: z.infer<typeof SearchAdvancedParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.searchAdvanced(
        {
          query: args.query,
          exact: args.exact,
          yearFrom: args.yearFrom,
          yearTo: args.yearTo,
          count: args.count,
        },
        options,
      );
    } catch (error: any) {
      return { error: { message: error.message || 'Failed to perform advanced search' } };
    }
  },

  searchMultiSource: async (
    args: z.infer<typeof SearchMultiSourceParamsSchema>,
    options: HandlerOptions = {},
  ) => {
    try {
      return await zlibraryApi.searchMultiSource(
        {
          query: args.query,
          source: args.source,
          count: args.count,
        },
        { signal: options.signal },
      );
    } catch (error: any) {
      // Provider failures name which source failed and why. The Python bridge
      // puts that on stderr as `details`; surface it rather than flattening
      // everything to one sentence, because "annas could not be resolved" and
      // "libgen did not accept a connection" call for different responses.
      const details = error?.context?.details;
      return {
        error: {
          message: error.message || 'Failed to search multi-source',
          ...(details === undefined ? {} : { details }),
        },
      };
    }
  },
};

// ============================================================================
// Legacy compatibility exports (used by existing tests - will be updated in 03-02)
// ============================================================================

interface ToolRegistryEntry {
  description: string;
  schema: ZodObject<ZodRawShape>;
  handler?: (args: any) => Promise<any>;
}

const toolRegistry: Record<string, ToolRegistryEntry> = {
  search_books: {
    description: 'Search for books in Z-Library',
    schema: SearchBooksParamsSchema,
    handler: handlers.searchBooks,
  },
  full_text_search: {
    description: 'Full text search in book content',
    schema: FullTextSearchParamsSchema,
    handler: handlers.fullTextSearch,
  },
  get_download_history: {
    description: 'Get download history',
    schema: GetDownloadHistoryParamsSchema,
    handler: handlers.getDownloadHistory,
  },
  get_download_limits: {
    description: 'Get per-source download limits',
    schema: GetDownloadLimitsParamsSchema,
    handler: handlers.getDownloadLimits,
  },
  get_recent_books: { description: 'Get recently added books', schema: GetRecentBooksParamsSchema },
  download_book_to_file: {
    description:
      'Download a book to file and optionally return a processed RAG bundle with body text, metadata, and optional sibling outputs.',
    schema: DownloadBookToFileParamsSchema,
    handler: handlers.downloadBookToFile,
  },
  process_document_for_rag: {
    description:
      'Process EPUB, PDF, or TXT into a file-based RAG bundle with processed body text, metadata, and optional footnotes/endnotes/citations.',
    schema: ProcessDocumentForRagParamsSchema,
    handler: handlers.processDocumentForRag,
  },
  get_book_metadata: {
    description: 'Get book metadata',
    schema: GetBookMetadataParamsSchema,
    handler: handlers.getBookMetadata,
  },
  search_by_term: {
    description: 'Search by conceptual term',
    schema: SearchByTermParamsSchema,
    handler: handlers.searchByTerm,
  },
  search_by_author: {
    description: 'Search by author',
    schema: SearchByAuthorParamsSchema,
    handler: handlers.searchByAuthor,
  },
  fetch_booklist: {
    description: 'Fetch booklist',
    schema: FetchBooklistParamsSchema,
    handler: handlers.fetchBooklist,
  },
  search_advanced: {
    description: 'Advanced search',
    schema: SearchAdvancedParamsSchema,
    handler: handlers.searchAdvanced,
  },
  search_multi_source: {
    description: 'Multi-source search',
    schema: SearchMultiSourceParamsSchema,
    handler: handlers.searchMultiSource,
  },
};

// ============================================================================
// Helper functions
// ============================================================================

function getPackageVersion(): string {
  try {
    const packageJsonPath = path.resolve(__dirname, '..', 'package.json');
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
    return packageJson.version || 'unknown';
  } catch (error: any) {
    console.warn('Could not read package.json for version:', error.message);
    return 'unknown';
  }
}

// Helper to wrap handler results in MCP content format
function wrapResult(result: any, toolName: string) {
  if (result && typeof result === 'object' && 'error' in result && result.error) {
    return {
      content: [
        {
          type: 'text' as const,
          text: `Error from tool "${toolName}": ${result.error.message || result.error}`,
        },
      ],
      structuredContent: result,
      isError: true as const,
    };
  }
  return {
    content: [{ type: 'text' as const, text: JSON.stringify(result) }],
    structuredContent: result,
  };
}

// ============================================================================
// Server setup
// ============================================================================

/**
 * Check Z-Library credentials before server startup.
 *
 * Missing credentials are a warning, not a fatal error: since v1.4.0 the
 * LibGen source needs none, so `search_multi_source` and downloads of its
 * results work on a bare install. Exiting here would make a working,
 * unlimited source unreachable because an unrelated one was unconfigured.
 * Z-Library tools still fail with their own clear error when invoked.
 */
function validateCredentials(): void {
  const email = process.env.ZLIBRARY_EMAIL;
  const password = process.env.ZLIBRARY_PASSWORD;

  if (!email || !password) {
    const missing: string[] = [];
    if (!email) missing.push('ZLIBRARY_EMAIL');
    if (!password) missing.push('ZLIBRARY_PASSWORD');

    logger.warn(
      `Missing environment variable(s): ${missing.join(', ')} — ` +
        `Z-Library tools (search_books, full_text_search, download history) ` +
        `will fail when called, and get_download_limits will report ` +
        `Z-Library as unavailable while still answering for the other ` +
        `sources. LibGen is unaffected: use ` +
        `search_multi_source with source="libgen" and pass results to ` +
        `download_book_to_file. To enable Z-Library, set "env": ` +
        `{"ZLIBRARY_EMAIL": "...", "ZLIBRARY_PASSWORD": "..."} in your MCP ` +
        `client configuration. See README.md.`,
    );
  }
}

interface StartOptions {
  testing?: boolean;
}

async function start(
  opts: StartOptions = {},
): Promise<{ server: McpServer; transport: StdioServerTransport } | null> {
  try {
    // Validate credentials before anything else (skip in test mode)
    if (!opts.testing) {
      validateCredentials();
    }

    // Ensure the logs directory exists
    try {
      await mkdirAsync(path.resolve(__dirname, '..', 'logs'), { recursive: true });
      logger.debug("Log directory 'logs/' ensured.");
    } catch (dirError: any) {
      console.error("Failed to create 'logs/' directory:", dirError.message);
    }

    // Instantiate the McpServer
    const server = new McpServer({
      name: 'zlibrary-mcp',
      version: getPackageVersion(),
    });

    // ========================================================================
    // Register all 13 tools via server.tool()
    // ========================================================================

    // Helper to get annotations with proper typing
    const ann = (name: string) => toolAnnotations[name] as ToolAnnotations;

    // Every handler below receives `extra.signal`, which aborts when the client
    // cancels or times out the request. Passing it down is what turns that
    // cancellation into a killed subprocess instead of one still running
    // against a dead provider with nobody left to receive the result.

    // 1. search_books
    server.tool(
      'search_books',
      'Search for books in Z-Library by title, author, or keywords. Returns matching books with metadata including title (string), author (string), name, authors (array), year, format, and file size. Use exact=true for precise title matching. Filter results by year range, language, or file format.',
      SearchBooksParamsSchema.shape,
      ann('search_books'),
      async (args, extra) =>
        wrapResult(
          await handlers.searchBooks(args as any, { signal: extra?.signal }),
          'search_books',
        ),
    );

    // 2. full_text_search
    server.tool(
      'full_text_search',
      'Search for books containing specific text within their content. Returns books with title (string), author (string), name, authors (array), and other metadata. Useful for finding books that discuss particular topics, quotes, or concepts.',
      FullTextSearchParamsSchema.shape,
      ann('full_text_search'),
      async (args, extra) =>
        wrapResult(
          await handlers.fullTextSearch(args as any, { signal: extra?.signal }),
          'full_text_search',
        ),
    );

    // 3. get_download_history
    server.tool(
      'get_download_history',
      "Get the user's Z-Library download history. Returns a list of previously downloaded books with their metadata.",
      GetDownloadHistoryParamsSchema.shape,
      ann('get_download_history'),
      async (args, extra) =>
        wrapResult(
          await handlers.getDownloadHistory(args as any, { signal: extra?.signal }),
          'get_download_history',
        ),
    );

    // 4. get_download_limits
    server.tool(
      'get_download_limits',
      "Report each source's daily download limit. Every entry says which of the three it is reporting — no limit exists, a limit exists but is not known here, or a concrete number. Z-Library is the only source whose answer costs a round-trip, so pass `sources` to ask about the others without paying for it.",
      GetDownloadLimitsParamsSchema.shape,
      ann('get_download_limits'),
      async (args, extra) =>
        wrapResult(
          await handlers.getDownloadLimits(args as any, { signal: extra?.signal }),
          'get_download_limits',
        ),
    );

    // 5. get_recent_books
    server.tool(
      'get_recent_books',
      'Get recently added books to Z-Library. Optionally filter by file format.',
      GetRecentBooksParamsSchema.shape,
      ann('search_books'),
      async (args, extra) => {
        try {
          const result = await (zlibraryApi as any).getRecentBooks(args, {
            signal: extra?.signal,
          });
          return wrapResult(result, 'get_recent_books');
        } catch (error: any) {
          return {
            content: [{ type: 'text' as const, text: `Error: ${error.message}` }],
            isError: true,
          };
        }
      },
    );

    // 6. download_book_to_file
    server.tool(
      'download_book_to_file',
      'Download a book to a local file. Pass the full bookDetails object from search_books results. Optionally process the document for RAG (text extraction) after download. Returns file paths for both the original book and processed text.',
      DownloadBookToFileParamsSchema.shape,
      ann('download_book_to_file'),
      async (args, extra) =>
        wrapResult(
          await handlers.downloadBookToFile(args as any, { signal: extra?.signal }),
          'download_book_to_file',
        ),
    );

    // 7. process_document_for_rag
    server.tool(
      'process_document_for_rag',
      'Process a downloaded document (EPUB, TXT, PDF) to extract clean text content for RAG (Retrieval-Augmented Generation). Extracts text, preserves structure, detects footnotes, and outputs a text file.',
      ProcessDocumentForRagParamsSchema.shape,
      ann('process_document_for_rag'),
      async (args, extra) =>
        wrapResult(
          await handlers.processDocumentForRag(args as any, { signal: extra?.signal }),
          'process_document_for_rag',
        ),
    );

    // 8. get_book_metadata
    server.tool(
      'get_book_metadata',
      'Get metadata for a book. By default returns core fields (title, author, year, publisher, language, pages, isbn, rating, cover, categories). Use the include parameter to add optional field groups: terms (60+ conceptual keywords), booklists (11+ curated collections), ipfs (IPFS CIDs), ratings (quality score), description (full text). Requires bookId and bookHash from search results.',
      GetBookMetadataParamsSchema.shape,
      ann('get_book_metadata'),
      async (args, extra) =>
        wrapResult(
          await handlers.getBookMetadata(args as any, { signal: extra?.signal }),
          'get_book_metadata',
        ),
    );

    // 9. search_by_term
    server.tool(
      'search_by_term',
      'Search for books by conceptual term (e.g., "phenomenology", "dialectic", "epistemology"). Returns books with title (string), author (string), and other metadata. Books in Z-Library are tagged with 60+ conceptual terms.',
      SearchByTermParamsSchema.shape,
      ann('search_by_term'),
      async (args, extra) =>
        wrapResult(
          await handlers.searchByTerm(args as any, { signal: extra?.signal }),
          'search_by_term',
        ),
    );

    // 10. search_by_author
    server.tool(
      'search_by_author',
      'Advanced author search with support for various name formats. Returns books with title (string), author (string), and other metadata. Use exact=true for precise matching. Filter by publication year, language, or file format.',
      SearchByAuthorParamsSchema.shape,
      ann('search_by_author'),
      async (args, extra) =>
        wrapResult(
          await handlers.searchByAuthor(args as any, { signal: extra?.signal }),
          'search_by_author',
        ),
    );

    // 11. fetch_booklist
    server.tool(
      'fetch_booklist',
      'Fetch books from an expert-curated booklist. Z-Library books belong to 11+ booklists with up to 954 books per list. Get booklist IDs from get_book_metadata.',
      FetchBooklistParamsSchema.shape,
      ann('fetch_booklist'),
      async (args, extra) =>
        wrapResult(
          await handlers.fetchBooklist(args as any, { signal: extra?.signal }),
          'fetch_booklist',
        ),
    );

    // 12. search_advanced
    server.tool(
      'search_advanced',
      'Advanced search with automatic separation of exact matches from fuzzy/approximate matches. Returns two arrays: exact_matches and fuzzy_matches, each containing books with title (string), author (string), and other metadata.',
      SearchAdvancedParamsSchema.shape,
      ann('search_advanced'),
      async (args, extra) =>
        wrapResult(
          await handlers.searchAdvanced(args as any, { signal: extra?.signal }),
          'search_advanced',
        ),
    );

    // 13. search_multi_source
    server.tool(
      'search_multi_source',
      "Search for books across Anna's Archive and LibGen. Alternative to Z-Library EAPI. Returns books with md5, title, author, year, extension, size, source, download_url, plus a `routing` block naming what was requested, what served it, whether a fallback happened, and each source's current routes and daily limit. Use source=auto to prefer Anna's Archive with LibGen fallback, or force a specific source.",
      SearchMultiSourceParamsSchema.shape,
      ann('search_multi_source'),
      async (args, extra) =>
        wrapResult(
          await handlers.searchMultiSource(args as any, { signal: extra?.signal }),
          'search_multi_source',
        ),
    );

    // Create and connect the Stdio transport
    const transport = new StdioServerTransport();
    await server.connect(transport);
    logger.info('Z-Library MCP server (ESM/TS) is running via Stdio...');

    return { server, transport };
  } catch (error: any) {
    console.error('Failed to start MCP server:', error);

    if (opts.testing !== true) {
      process.exit(1);
    }
    return null;
  }
}

/**
 * Whether this module is the process entry point (as opposed to being imported).
 *
 * The original check compared `import.meta.url` against a `file://` URL built by
 * string concatenation from `process.argv[1]`. That is never equal on Windows:
 * `argv[1]` is a backslash path (`C:\app\dist\index.js`) while `import.meta.url`
 * is percent-encoded with forward slashes (`file:///C:/app/dist/index.js`), so
 * the server never auto-started and the client saw an immediate exit.
 *
 * Comparing resolved filesystem paths is correct on every platform. Exported so
 * both operands can be exercised in tests without spawning a process.
 */
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

// Auto-start logic
if (isProcessEntryPoint(import.meta.url, process.argv[1])) {
  start().catch((err) => {
    console.error('Fatal error starting server:', err);
    process.exit(1);
  });
}

// Export necessary components for testing
export { start, handlers, toolRegistry };
