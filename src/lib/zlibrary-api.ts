import type { Options as PythonShellOptions } from 'python-shell';
import * as path from 'path';
import { getManagedPythonPath } from './venv-manager.js'; // Import ESM style
import { appendFile as appendFileAsyncFS, mkdir as mkdirAsyncFS } from 'fs/promises'; // Import fs/promises for async file operations, aliased
// Removed unused https, http imports
// path is already imported on line 2
import { fileURLToPath } from 'url';
import { withRetry, isRetryableError } from './retry-manager.js';
import { CircuitBreaker } from './circuit-breaker.js';
import { ZLibraryError, PythonBridgeError } from './errors.js';
import { logger } from './logger.js';
import { runPythonBridge, LONG_BRIDGE_TIMEOUT_MS } from './python-runner.js';
import {
  isBridgeDetailRetryable,
  isConfigurationBridgeDetail,
  parseBridgeErrorEnvelope,
} from './python-bridge.js';

// Recreate __dirname for ESM
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Path to the Python bridge script
// Calculate path relative to the compiled JS file location (dist/lib)
// Go up two levels from dist/lib to the project root, then into the source lib dir
const BRIDGE_SCRIPT_PATH = path.resolve(__dirname, '..', '..', 'lib');
const BRIDGE_SCRIPT_NAME = 'python_bridge.py';

// Create a circuit breaker for all Python bridge operations
const pythonBridgeCircuitBreaker = new CircuitBreaker({
  threshold: parseInt(process.env.CIRCUIT_BREAKER_THRESHOLD || '5'),
  timeout: parseInt(process.env.CIRCUIT_BREAKER_TIMEOUT || '60000'),
  onStateChange: (oldState, newState) => {
    logger.info(`Python bridge circuit breaker: ${oldState} -> ${newState}`);
  },
  // A client that cancels its own request has told us nothing about the
  // bridge's health. Counting cancellations would mean five aborted searches
  // open the breaker and fail every unrelated tool for the timeout window —
  // turning a user's own impatience into an outage.
  isFailure: (error) =>
    error?.context?.reason !== 'aborted' &&
    !isConfigurationBridgeDetail(error?.context?.details),
});

/**
 * Bridge functions whose work is bounded by file size and CPU, not by a
 * network round trip. Downloading a large book and OCR-ing a scanned one both
 * routinely outrun the ordinary budget, and killing them would turn a slow
 * success into a hard failure — the opposite of what the budget is for. They
 * are still bounded (PYTHON_BRIDGE_LONG_TIMEOUT), just far more generously.
 */
const LONG_RUNNING_FUNCTIONS = new Set(['download_book', 'process_document']);

/**
 * Default wall-clock budget for a bridge function, in ms.
 *
 * @param functionName - Bridge function being called
 * @returns Budget in ms
 */
function defaultTimeoutFor(functionName: string): number | undefined {
  return LONG_RUNNING_FUNCTIONS.has(functionName) ? LONG_BRIDGE_TIMEOUT_MS : undefined;
}

/**
 * Per-call options for the Python bridge.
 */
export interface CallOptions {
  /**
   * Wall-clock budget in ms. Defaults to PYTHON_BRIDGE_TIMEOUT, or
   * PYTHON_BRIDGE_LONG_TIMEOUT for download/processing calls.
   */
  timeoutMs?: number;
  /** Abort signal from the MCP request, so a cancelled call kills the child. */
  signal?: AbortSignal;
}

/**
 * Execute a Python function from the Z-Library repository
 * @param functionName - Name of the Python function to call
 * @param args - Arguments to pass to the function
 * @param callOptions - Timeout and abort signal for the subprocess
 * @returns Promise resolving with the result from the Python function
 * @throws {ZLibraryError} If the Python process fails or returns an error.
 * @throws {BridgeTimeoutError} If the subprocess exceeds its budget; the child
 *   is killed rather than abandoned.
 */
async function callPythonFunction(
  functionName: string,
  args: Record<string, any> = {},
  callOptions: CallOptions = {},
): Promise<any> {
  // Wrap the entire operation with retry logic and circuit breaker
  return withRetry(
    async () => {
      return pythonBridgeCircuitBreaker.execute(async () => {
        try {
          // Get the python path asynchronously INSIDE the try block
          const venvPythonPath = await getManagedPythonPath();
          // Serialize arguments as JSON *before* creating options
          const serializedArgs = JSON.stringify(args);
          const options: PythonShellOptions = {
            mode: 'text', // Revert back to text mode
            pythonPath: venvPythonPath, // Use the Python from our managed venv
            scriptPath: BRIDGE_SCRIPT_PATH, // Use the calculated path to the source lib dir
            args: [functionName, serializedArgs] // Pass serialized string directly
          };

          // runPythonBridge, not PythonShell.run: the latter hands back a
          // promise with no deadline and no handle on the child, so a hung
          // call can only be abandoned — which leaves the Python process
          // running forever (see src/lib/python-runner.ts).
          const results = await runPythonBridge(BRIDGE_SCRIPT_NAME, options, {
            timeoutMs: callOptions.timeoutMs ?? defaultTimeoutFor(functionName),
            signal: callOptions.signal,
            label: `python_bridge.${functionName}`,
          });

          // Check if results exist and contain at least one element
          if (!results || results.length === 0) {
            throw new PythonBridgeError(`No output received from Python script.`, {
              functionName,
              args
            });
          }

          // Join the lines and parse manually
          const stdoutString = results.join('\n');
          let mcpResponseData: any;
          try {
            // First parse: Get the MCP response object { content: [{ type: 'text', text: '...' }] }
            mcpResponseData = JSON.parse(stdoutString);
          } catch (parseError: any) {
            throw new PythonBridgeError(
              `Failed to parse initial JSON output from Python script: ${parseError.message}`,
              { functionName, args, rawOutput: stdoutString },
              false // Parse errors are not retryable
            );
          }

          // Validate the MCP response structure and extract the nested JSON string
          if (!mcpResponseData || !Array.isArray(mcpResponseData.content) || mcpResponseData.content.length === 0 || typeof mcpResponseData.content[0].text !== 'string') {
            throw new PythonBridgeError(
              `Invalid MCP response structure received from Python script.`,
              { functionName, args, rawOutput: stdoutString },
              false // Structure errors are not retryable
            );
          }

          const nestedJsonString = mcpResponseData.content[0].text;
          let resultData: any;
          try {
            // Second parse: Get the actual result object from the nested string
            resultData = JSON.parse(nestedJsonString);
          } catch (parseError: any) {
            throw new PythonBridgeError(
              `Failed to parse nested JSON result from Python script: ${parseError.message}`,
              { functionName, args, nestedString: nestedJsonString },
              false // Parse errors are not retryable
            );
          }

          // Check if the *actual* Python result contained an error structure
          if (resultData && typeof resultData === 'object' && 'error' in resultData && resultData.error) {
            throw new PythonBridgeError(
              `Python bridge execution failed for ${functionName}: ${resultData.error}`,
              { functionName, args }
            );
          }

          // Return the successful result object from Python
          return resultData;
        } catch (err: any) {
          // Log the full error object from python-shell
          console.error(`[callPythonFunction Error - ${functionName}] Raw error object:`, err);

          // If it's already a ZLibraryError, just rethrow
          if (err instanceof ZLibraryError) {
            throw err;
          }

          // The bridge writes a JSON envelope to stderr on failure. When the
          // failure is a provider outage that envelope carries `details`
          // naming the provider, host and reason (dns_failure vs
          // connect_timeout vs http_error); lead with that instead of a
          // traceback, so the caller can tell "this domain is gone" from
          // "this mirror is dropping packets".
          const bridgeError = parseBridgeErrorEnvelope(err.stderr);
          const stderrOutput = err.stderr ? ` Stderr: ${err.stderr}` : '';

          if (bridgeError) {
            throw new PythonBridgeError(
              `${functionName} failed: ${bridgeError.error}`,
              {
                functionName,
                args,
                details: bridgeError.details,
                pythonErrorType: bridgeError.type,
                stderr: err.stderr,
                originalError: err
              },
              // A provider that is not reachable at all will not become
              // reachable inside the retry window; retrying just re-pays the
              // probe. Response-level failures may be transient.
              isBridgeDetailRetryable(bridgeError.details)
            );
          }

          // Wrap in PythonBridgeError with context
          throw new PythonBridgeError(
            `Python bridge execution failed for ${functionName}: ${err.message || err}.${stderrOutput}`,
            {
              functionName,
              args,
              stderr: err.stderr,
              originalError: err
            }
          );
        }
      });
    },
    {
      maxRetries: parseInt(process.env.RETRY_MAX_RETRIES || '3'),
      initialDelay: parseInt(process.env.RETRY_INITIAL_DELAY || '1000'),
      maxDelay: parseInt(process.env.RETRY_MAX_DELAY || '30000'),
      factor: parseFloat(process.env.RETRY_FACTOR || '2'),
      shouldRetry: isRetryableError
    }
  );
}

// Define interfaces for function arguments for better type safety

interface SearchBooksArgs {
    query: string;
    exact?: boolean;
    fromYear?: number | null;
    toYear?: number | null;
    languages?: string[];
    extensions?: string[];
    content_types?: string[];
    count?: number;
}

interface FullTextSearchArgs extends SearchBooksArgs {
    phrase?: boolean;
    words?: boolean;
}

interface GetDownloadHistoryArgs {
    count?: number;
}

interface DownloadBookToFileArgs {
    // id: string; // Replaced by bookDetails
    // format?: string | null; // Replaced by bookDetails
    bookDetails: Record<string, any>; // Expect the full book details object
    outputDir?: string;
    process_for_rag?: boolean;
    processed_output_format?: string;
}

interface ProcessDocumentForRagArgs {
    filePath: string;
    outputFormat?: string;
}

interface ProcessedDocumentStats {
  word_count: number;
  char_count: number;
  format: string;
}

interface ProcessedDocumentBundle {
  processed_file_path: string | null;
  metadata_file_path?: string | null;
  footnotes_file_path?: string | null;
  endnotes_file_path?: string | null;
  citations_file_path?: string | null;
  stats?: ProcessedDocumentStats | null;
  content_types_produced?: string[];
  output_files?: Record<string, string>;
}

interface DownloadBookResult extends ProcessedDocumentBundle {
  file_path: string;
  processing_error?: string;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return Boolean(value)
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.values(value as Record<string, unknown>).every((item) => typeof item === 'string');
}

function isProcessedDocumentStats(value: unknown): value is ProcessedDocumentStats {
  return Boolean(value)
    && typeof value === 'object'
    && typeof (value as ProcessedDocumentStats).word_count === 'number'
    && typeof (value as ProcessedDocumentStats).char_count === 'number'
    && typeof (value as ProcessedDocumentStats).format === 'string';
}

function validateNullablePathField(result: Record<string, any>, fieldName: string): void {
  if (!(fieldName in result)) {
    return;
  }
  const value = result[fieldName];
  if (value !== null && typeof value !== 'string') {
    throw new Error(`Invalid response from Python bridge: ${fieldName} must be a string or null.`);
  }
}

function validateStructuredDocumentBundle(
  result: Record<string, any>,
  { requireProcessedFilePath = true }: { requireProcessedFilePath?: boolean } = {},
): asserts result is ProcessedDocumentBundle {
  if (!result || typeof result !== 'object') {
    throw new Error('Invalid response from Python bridge: Expected an object.');
  }
  if (requireProcessedFilePath && !('processed_file_path' in result)) {
    throw new Error('Invalid response from Python bridge during processing. Missing processed_file_path key.');
  }
  validateNullablePathField(result, 'processed_file_path');
  validateNullablePathField(result, 'metadata_file_path');
  validateNullablePathField(result, 'footnotes_file_path');
  validateNullablePathField(result, 'endnotes_file_path');
  validateNullablePathField(result, 'citations_file_path');

  if ('content_types_produced' in result && !isStringArray(result.content_types_produced)) {
    throw new Error('Invalid response from Python bridge: content_types_produced must be an array of strings.');
  }
  if ('output_files' in result && !isStringRecord(result.output_files)) {
    throw new Error('Invalid response from Python bridge: output_files must be an object of string paths.');
  }
  if ('stats' in result && result.stats !== null && !isProcessedDocumentStats(result.stats)) {
    throw new Error('Invalid response from Python bridge: stats must include word_count, char_count, and format.');
  }
}


/**
 * Search for books in Z-Library
 */
export async function searchBooks({
  query,
  exact = false,
  fromYear = null,
  toYear = null,
  languages = [],
  extensions = [],
  content_types = [],
  count = 10
}: SearchBooksArgs, options: CallOptions = {}): Promise<any> {
  // Pass arguments as an object matching Python function signature
  // Python bridge main() expects 'language' (singular) and 'content_types'
  const pythonArgs = {
    query: query,
    exact: exact,
    from_year: fromYear,
    to_year: toYear,
    languages: languages,
    extensions: extensions,
    content_types: content_types,
    count: count
  };
  // Moved logging to after pythonArgs is defined
  const searchBooksPythonArgsLog = `[${new Date().toISOString()}] Node.js searchBooks: Sending to callPythonFunction: ${JSON.stringify(pythonArgs)}\n`;
  logger.debug(searchBooksPythonArgsLog.trim());
  try {
    const logFilePath = path.resolve(__dirname, '..', '..', 'logs', 'nodejs_debug.log');
    await mkdirAsyncFS(path.dirname(logFilePath), { recursive: true });
    await appendFileAsyncFS(logFilePath, searchBooksPythonArgsLog);
  } catch (e) { console.error('Failed to write to logs/nodejs_debug.log', e); }
  return await callPythonFunction('search', pythonArgs, options);
}
/**
 * Perform full text search
 */
export async function fullTextSearch({
  query,
  exact = false,
  phrase = true,
  words = false,
  languages = [],
  extensions = [],
  content_types = [],
  count = 10
}: FullTextSearchArgs, options: CallOptions = {}): Promise<any> {
  // Pass arguments as an object matching Python function signature
  // Python bridge main() expects 'language' (singular) and 'content_types'
  const pythonArgsFTS = {
    query: query,
    exact: exact,
    phrase: phrase,
    words: words,
    languages: languages,
    extensions: extensions,
    content_types: content_types,
    count: count
  };
  // Moved logging to after pythonArgsFTS is defined
  const ftsPythonArgsLog = `[${new Date().toISOString()}] Node.js fullTextSearch: Sending to callPythonFunction: ${JSON.stringify(pythonArgsFTS)}\n`;
  logger.debug(ftsPythonArgsLog.trim());
  try {
    const logFilePath = path.resolve(__dirname, '..', '..', 'logs', 'nodejs_debug.log');
    await mkdirAsyncFS(path.dirname(logFilePath), { recursive: true });
    await appendFileAsyncFS(logFilePath, ftsPythonArgsLog);
  } catch (e) { console.error('Failed to write to logs/nodejs_debug.log', e); }
  return await callPythonFunction('full_text_search', pythonArgsFTS, options);
}

/**
 * Get user's download history
 */
export async function getDownloadHistory({ count = 10 }: GetDownloadHistoryArgs, options: CallOptions = {}): Promise<any> {
  // Pass arguments as an object matching Python function signature
  return await callPythonFunction('get_download_history', { count }, options);
}

/**
 * Get user's download limits
 */
export async function getDownloadLimits(options: CallOptions = {}): Promise<any> {
  // Pass arguments as an object matching Python function signature
  return await callPythonFunction('get_download_limits', {}, options);
}


/**
 * Process a downloaded document for RAG
 */
export async function processDocumentForRag({
  filePath,
  outputFormat = 'txt',
}: ProcessDocumentForRagArgs, options: CallOptions = {}): Promise<ProcessedDocumentBundle> {
  if (!filePath) {
    throw new Error("Missing required argument: filePath");
  }
  logger.debug(`Calling Python bridge to process document: ${filePath}`);
  // Ensure the file path is absolute or correctly relative for the Python script
  const absoluteFilePath = path.resolve(filePath);
  // Pass arguments as an object matching Python function signature
  const result = await callPythonFunction('process_document', { file_path_str: absoluteFilePath, output_format: outputFormat }, options);

  // Check if the Python script returned an error structure
  if (result && result.error) {
      throw new Error(`Python processing failed: ${result.error}`);
  }

  validateStructuredDocumentBundle(result, { requireProcessedFilePath: true });
  return result;
}

// Removed unused generateSafeFilename function

/**
 * Download a book directly to a file
 */
export async function downloadBookToFile({
    // id, // Replaced by bookDetails
    // format = null, // Replaced by bookDetails
    bookDetails, // Use bookDetails object
    outputDir = './downloads',
    process_for_rag = false,
    processed_output_format = 'txt'
}: DownloadBookToFileArgs, options: CallOptions = {}): Promise<DownloadBookResult> {
  try {
    // Call the Python function, passing the bookDetails object
    const result = await callPythonFunction('download_book', {
        book_details: bookDetails, // Pass the whole object
        // book_id: id, // Removed
        // format: format, // Removed
        output_dir: outputDir,
        process_for_rag: process_for_rag,
        processed_output_format: processed_output_format
    }, options);

    // Check if the Python script returned an error structure
    if (result && result.error) {
        throw new Error(`Python download/processing failed: ${result.error}`);
    }

    // Validate the response structure
    if (!result || !result.file_path) { // Compat check
        throw new Error("Invalid response from Python bridge: Missing original file_path.");
    }

    if (typeof result.file_path !== 'string') {
        throw new Error("Invalid response from Python bridge: file_path must be a string.");
    }

    if (process_for_rag && !('processed_file_path' in result)) {
        throw new Error("Invalid response from Python bridge: Processing requested but processed_file_path key is missing.");
    }

    if (process_for_rag || 'processed_file_path' in result) {
        validateStructuredDocumentBundle(result, { requireProcessedFilePath: false });
    }

    if ('processing_error' in result && typeof result.processing_error !== 'string' && typeof result.processing_error !== 'undefined') {
        throw new Error('Invalid response from Python bridge: processing_error must be a string when present.');
    }

    return result as DownloadBookResult;

  } catch (error: any) {
    // Keep the normalized bridge context on the public error itself. A generic
    // Error with details hidden under cause made every direct consumer invent
    // its own traversal rule and dropped structuredContent at the handler.
    if (error instanceof ZLibraryError) {
      throw new PythonBridgeError(
        `Failed to download book: ${error.message || 'Unknown error'}`,
        error.context,
        error.retryable,
      );
    }
    throw new Error(
      `Failed to download book: ${error.message || 'Unknown error'}`,
      { cause: error },
    );
  }
}

/**
 * Phase 3 Research Tools - Exported wrappers for advanced search and metadata features
 */

// Core fields always included in metadata response
const METADATA_CORE_FIELDS = new Set([
  'id', 'book_hash', 'book_url',
  'title', 'author', 'authors', 'year', 'publisher', 'language',
  'pages', 'isbn_10', 'isbn_13', 'rating', 'cover',
  'url', 'categories', 'extension', 'filesize', 'series',
]);

// Mapping from include group names to metadata field names
const METADATA_INCLUDE_MAP: Record<string, string[]> = {
  'terms': ['terms'],
  'booklists': ['booklists'],
  'ipfs': ['ipfs_cids'],
  'ratings': ['quality_score'],
  'description': ['description'],
};

function filterMetadataResponse(fullMetadata: any, include?: string[]): any {
  if (!fullMetadata || typeof fullMetadata !== 'object') return fullMetadata;

  const result: Record<string, any> = {};

  // Always include core fields
  for (const key of Object.keys(fullMetadata)) {
    if (METADATA_CORE_FIELDS.has(key)) {
      result[key] = fullMetadata[key];
    }
  }

  // Add requested optional field groups
  if (include && include.length > 0) {
    for (const group of include) {
      const fields = METADATA_INCLUDE_MAP[group];
      if (fields) {
        for (const field of fields) {
          if (field in fullMetadata) {
            result[field] = fullMetadata[field];
          }
        }
      }
    }
  }

  return result;
}

export async function getBookMetadata(bookId: string, bookHash: string, include?: string[], options: CallOptions = {}): Promise<any> {
  const fullMetadata = await callPythonFunction('get_book_metadata_complete', {
    book_id: bookId,
    book_hash: bookHash
  }, options);
  return filterMetadataResponse(fullMetadata, include);
}

export async function searchByTerm(args: {
  term: string;
  yearFrom?: number;
  yearTo?: number;
  languages?: string[];
  extensions?: string[];
  limit?: number;
}, options: CallOptions = {}): Promise<any> {
  return callPythonFunction('search_by_term_bridge', {
    term: args.term,
    year_from: args.yearFrom,
    year_to: args.yearTo,
    languages: args.languages,
    extensions: args.extensions,
    limit: args.limit || 25
  }, options);
}

export async function searchByAuthor(args: {
  author: string;
  exact?: boolean;
  yearFrom?: number;
  yearTo?: number;
  languages?: string[];
  extensions?: string[];
  limit?: number;
}, options: CallOptions = {}): Promise<any> {
  return callPythonFunction('search_by_author_bridge', {
    author: args.author,
    exact: args.exact || false,
    year_from: args.yearFrom,
    year_to: args.yearTo,
    languages: args.languages,
    extensions: args.extensions,
    limit: args.limit || 25
  }, options);
}

export async function fetchBooklist(args: {
  booklistId: string;
  booklistHash: string;
  topic: string;
  page?: number;
}, options: CallOptions = {}): Promise<any> {
  return callPythonFunction('fetch_booklist_bridge', {
    booklist_id: args.booklistId,
    booklist_hash: args.booklistHash,
    topic: args.topic,
    page: args.page || 1
  }, options);
}

export async function searchAdvanced(args: {
  query: string;
  exact?: boolean;
  yearFrom?: number;
  yearTo?: number;
  count?: number;
}, options: CallOptions = {}): Promise<any> {
  return callPythonFunction('search_advanced', {
    query: args.query,
    exact: args.exact || false,
    from_year: args.yearFrom,
    to_year: args.yearTo,
    count: args.count || 10
  }, options);
}

/**
 * Search Anna's Archive / LibGen through the multi-source router.
 *
 * @param args - Query, source selection, and result count
 * @param options - Timeout and abort signal. Passing the MCP request's signal
 *   is what makes a client-side cancellation actually kill the subprocess
 *   instead of leaving it running against an unreachable provider.
 */
export async function searchMultiSource(
  args: {
    query: string;
    source?: 'auto' | 'annas' | 'libgen';
    count?: number;
  },
  options: CallOptions = {},
): Promise<any> {
  return callPythonFunction(
    'search_multi_source',
    {
      query: args.query,
      source: args.source || 'auto',
      count: args.count || 10,
    },
    options,
  );
}

// Removed unused downloadFile helper function
