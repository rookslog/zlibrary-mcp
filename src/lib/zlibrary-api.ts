import type { SessionInput } from './eapi-session.js';
import type { Options as PythonShellOptions } from 'python-shell';
import { randomUUID } from 'node:crypto';
import { authDiagnosticsFor, readAuthDiagnostics } from './auth-diagnostics.js';
import { getManagedPythonPath } from './venv-manager.js'; // Import ESM style
import { withRetry, isRetryableError, isTransientDependencyError } from './retry-manager.js';
import {
  EapiSessionOwner,
  needsEapi,
  sanitizeSessionError,
  getSessionMode,
  authRejected,
} from './eapi-session.js';
import { CircuitBreaker, type Admission } from './circuit-breaker.js';
import { ZLibraryError, PythonBridgeError, BridgeTimeoutError } from './errors.js';
import { logger } from './logger.js';
import { getPythonLibDirectory } from './paths.js';
import { runPythonBridge, LONG_BRIDGE_TIMEOUT_MS } from './python-runner.js';
import { isBridgeDetailRetryable, parseBridgeErrorEnvelope } from './python-bridge.js';

// Path to the Python bridge script
// Calculate path relative to the compiled JS file location (dist/lib)
// Go up two levels from dist/lib to the project root, then into the source lib dir
const BRIDGE_SCRIPT_PATH = getPythonLibDirectory();
const BRIDGE_SCRIPT_NAME = 'python_bridge.py';

const eapiSessions = new EapiSessionOwner();

/**
 * A lazy waiter's view of a shared owner denial it did not trigger. The waiter
 * that ran the failing control child accounts it; the rest carry no verdict.
 */
class SharedOwnerDenial {
  constructor(readonly error: unknown) {}
}
/** Owner control-child (login/validate) failures, which the owner hands to every waiter. */
const controlFailures = new WeakSet<object>();
function markControl<T>(work: Promise<T>): Promise<T> {
  return work.catch((error) => {
    if (error && typeof error === 'object') controlFailures.add(error);
    throw error;
  });
}

// One scope per provider (ADR-011): a 2.0 bridge call names one source, so an
// Anna's outage must not refuse LibGen. `auto` is one Python-owned walk over
// both and keeps its own verdict; no 2.0 tool sends it.
const dependencyBreakers = Object.fromEntries(
  ['eapi', 'libgen', 'annas', 'auto'].map((scope) => [
    scope,
    new CircuitBreaker({
      dependency: scope === 'eapi' ? 'eapi' : 'source_walk',
      threshold: parseInt(process.env.CIRCUIT_BREAKER_THRESHOLD || '5'),
      timeout: parseInt(process.env.CIRCUIT_BREAKER_TIMEOUT || '60000'),
      onStateChange: (oldState, newState) =>
        logger.info(`${scope} circuit breaker: ${oldState} -> ${newState}`),
      isFailure: (error: any) => {
        const details = error?.context?.details;
        // Issuance metadata is not a provider failure verdict. Keep it in the
        // reply, but let master classify the original typed/status evidence.
        // Empty details are likewise no verdict and intentionally stripped.
        const evidence =
          details && Object.keys(details).every((key) => key === 'request_issued')
            ? Object.assign(Object.create(error), {
                context: { ...error.context, details: undefined },
              })
            : error;
        return isTransientDependencyError(evidence);
      },
      // A verified receipt never contacts a provider, so it neither heals nor
      // probes (#267); a waiter's shared owner denial is not its verdict (#272).
      isNeutral: (result: any) =>
        result instanceof SharedOwnerDenial || result?.transfer_skipped === true,
    }),
  ]),
);

/**
 * Breaker scope for a call that does not use EAPI: the source it selects.
 *
 * @param functionName - Bridge function being called
 * @param args - Its arguments
 * @returns `libgen`, `annas`, or `auto` for a search with no named source
 */
function sourceScope(functionName: string, args: Record<string, any>): string {
  if (functionName === 'search_multi_source') {
    // As lib/sources/router.py routes it: exact `auto` walks both providers,
    // exact `annas` reaches Anna's, and every other spelling reaches LibGen.
    const source = args.source === undefined ? 'auto' : args.source;
    return source === 'auto' ? 'auto' : source === 'annas' ? 'annas' : 'libgen';
  }
  const source = String(args.book_details?.source || '').toLowerCase();
  if (source === 'libgen') return 'libgen';
  return source === 'annas' || source === 'annas_archive' ? 'annas' : 'auto';
}

/**
 * Bridge functions whose work is bounded by file size, not by a single
 * network round trip. Downloading a large book can routinely outrun the
 * ordinary budget, and killing it would turn a slow
 * success into a hard failure — the opposite of what the budget is for. They
 * are still bounded (PYTHON_BRIDGE_LONG_TIMEOUT), just far more generously.
 */
const LONG_RUNNING_FUNCTIONS = new Set(['download_book', 'acquire_book']);

/**
 * The ordinary bridge budget, parsed as python-runner parses
 * PYTHON_BRIDGE_TIMEOUT. Every HALF_OPEN probe holds the breaker exclusively
 * for at most this long, even if its transfer or retry sequence takes longer.
 */
const ORDINARY_BRIDGE_TIMEOUT_MS = (() => {
  const raw = process.env.PYTHON_BRIDGE_TIMEOUT?.trim();
  const value = raw && /^\d+$/.test(raw) ? Number(raw) : NaN;
  return Number.isSafeInteger(value) && value > 0 && value <= 2147483647 ? value : 240000;
})();

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
   * PYTHON_BRIDGE_LONG_TIMEOUT for download calls.
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
  const configuredEnv = { ...process.env };
  const identity = configuredEnv.ZLIBRARY_EMAIL || '';
  const memory = getSessionMode() === 'memory';
  const reuse = needsEapi(functionName, args) && memory;
  const acquisitionUsesEapi =
    functionName === 'acquire_book' &&
    !['libgen', 'annas', 'annas_archive'].includes(
      String(args.book_details?.source || '').toLowerCase(),
    );
  const lazyAcquisition = memory && acquisitionUsesEapi;
  const diagnosticEapi = needsEapi(functionName, args) || acquisitionUsesEapi;
  const requestId = randomUUID();
  let bridgeAttempted = false;
  // A diagnostic describes a request attempt, not merely an EAPI-capable tool.
  // Explicit child evidence wins; local refusals never invent attempt 1.
  const hasRequestEvidence = (error: any) =>
    !!readAuthDiagnostics(error?.context?.diagnostics) ||
    (bridgeAttempted &&
      error?.code !== 'CIRCUIT_BREAKER_OPEN' &&
      error?.code !== 'BRIDGE_SPAWN' &&
      error?.context?.details?.request_issued !== false &&
      !['ValueError', 'TypeError', 'AcquisitionError'].includes(error?.context?.pythonErrorType));
  const finishCall = (work: Promise<any>) =>
    work.catch((error: any) => {
      if (controlFailures.has(error)) {
        const shared = error;
        error = Object.create(
          Object.getPrototypeOf(shared),
          Object.getOwnPropertyDescriptors(shared),
        );
        Object.defineProperty(error, 'stack', {
          value: shared.stack,
          writable: true,
          configurable: true,
        });
        error.context = { ...shared.context };
      }
      if (diagnosticEapi && hasRequestEvidence(error)) {
        const prior = readAuthDiagnostics(error?.context?.diagnostics);
        const diagnostics = authDiagnosticsFor(
          error,
          requestId,
          prior?.attempt ?? 1,
          prior?.refresh_count ?? 0,
        );
        if (error && typeof error === 'object') (error.context ??= {}).diagnostics = diagnostics;
        logger.warn('EAPI failure', JSON.stringify(diagnostics));
      }
      throw error;
    });
  const receiptOnly = functionName === 'acquire_book' && args.receipt_only === true;
  const breaker =
    dependencyBreakers[
      needsEapi(functionName, args) || acquisitionUsesEapi
        ? 'eapi'
        : sourceScope(functionName, args)
    ]!;
  const boundProbe = (admission: Admission) => {
    admission.holdFor(ORDINARY_BRIDGE_TIMEOUT_MS);
  };
  const invokeWithRetry = (transport?: SessionInput, signal = callOptions.signal) =>
    withRetry(
      async (attempt) => {
        const execute = async () => {
          const lazy = lazyAcquisition && !transport;
          let activeTransport = transport;
          const lifetime = new AbortController();
          const lazySignal = signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal;
          let ownerWork: Promise<any> | undefined;
          const controlWork: Promise<any>[] = [];
          let denied: unknown;
          let completeOperation!: (value: any) => void;
          let rejectOperation!: (error: any) => void;
          const operation = new Promise<any>((resolve, reject) => {
            completeOperation = resolve;
            rejectOperation = reject;
          });
          void operation.catch(() => {}); // raw child may fail before a grant exists
          const session = transport ? eapiSessions.get(identity) : undefined;
          let sessionUpdate: unknown;
          const adoptUpdate = () => {
            if (activeTransport && sessionUpdate && !signal?.aborted && !lifetime.signal.aborted) {
              eapiSessions.consume(sessionUpdate, identity, activeTransport.generation);
              sessionUpdate = undefined;
            }
          };
          try {
            // Get the python path asynchronously INSIDE the try block
            const venvPythonPath = await getManagedPythonPath();
            // Serialize arguments as JSON *before* creating options
            const serializedArgs = JSON.stringify(args);
            const childEnv = { ...configuredEnv };
            for (const key of [
              'ZLIBRARY_DIAGNOSTIC_ID',
              'ZLIBRARY_DIAGNOSTIC_ATTEMPT',
              'ZLIBRARY_DIAGNOSTIC_REFRESH',
              'ZLIBRARY_DIAGNOSTIC_EAPI',
            ])
              delete childEnv[key];
            if (diagnosticEapi) {
              childEnv.ZLIBRARY_DIAGNOSTIC_ID = requestId;
              childEnv.ZLIBRARY_DIAGNOSTIC_ATTEMPT = String(Math.min(65535, attempt));
              childEnv.ZLIBRARY_DIAGNOSTIC_REFRESH = transport?.authRetry ? '1' : '0';
              childEnv.ZLIBRARY_DIAGNOSTIC_EAPI = '1';
            }
            delete childEnv.ZLIBRARY_EAPI_SESSION;
            delete childEnv.ZLIBRARY_EAPI_CHANNEL;
            delete childEnv.ZLIBRARY_EAPI_HANDOFF_TIMEOUT_MS;
            if (transport) childEnv.ZLIBRARY_EAPI_CHANNEL = '1';
            const options: PythonShellOptions = {
              env: childEnv,
              mode: 'text', // Revert back to text mode
              pythonPath: venvPythonPath, // Use the Python from our managed venv
              scriptPath: BRIDGE_SCRIPT_PATH, // Use the calculated path to the source lib dir
              args: [functionName, serializedArgs], // Pass serialized string directly
            };

            // runPythonBridge, not PythonShell.run: the latter hands back a
            // promise with no deadline and no handle on the child, so a hung
            // call can only be abandoned — which leaves the Python process
            // running forever (see src/lib/python-runner.ts).
            bridgeAttempted = true;
            const results = await runPythonBridge(BRIDGE_SCRIPT_NAME, options, {
              timeoutMs: callOptions.timeoutMs ?? defaultTimeoutFor(functionName),
              signal: lazy ? lazySignal : signal,
              label: `python_bridge.${functionName}`,
              sessionInput: transport ? { ...transport } : undefined,
              onSession:
                transport || lazy
                  ? (update) => {
                      if (!signal?.aborted) sessionUpdate = update;
                    }
                  : undefined,
              onSessionNeeded: lazy
                ? (channel) => {
                    // One admission and one health verdict per lazy acquisition. A
                    // deadline spent before the grant aborts the owner work and is
                    // not counted, as on the non-lazy shared-session path.
                    // A shared owner control failure counts once, for the waiter
                    // whose child produced it; other waiters carry no verdict (#272).
                    const produced = new Set<unknown>();
                    // As a HALF_OPEN probe it settles at the grant, when the owner
                    // hands over a session; the transfer then counts as a CLOSED call.
                    ownerWork = breaker
                      .execute((admission) => {
                        boundProbe(admission);
                        return eapiSessions
                          .run(
                            identity,
                            async (input, authSignal) => {
                              try {
                                if (input.action !== 'operation') {
                                  const work = markControl(invoke(input, authSignal));
                                  controlWork.push(work);
                                  return await work;
                                }
                                const handoff = () => {
                                  if (activeTransport)
                                    throw new PythonBridgeError(
                                      'Duplicate private handoff',
                                      {},
                                      false,
                                    );
                                  activeTransport = Object.freeze({ ...input });
                                  admission.confirm();
                                  channel.grant({ ...activeTransport });
                                  // Decode independently; never await owner completion in the raw runner.
                                  return operation;
                                };
                                return await handoff();
                              } catch (error) {
                                produced.add(error);
                                throw error;
                              }
                            },
                            {
                              signal: lazySignal,
                              timeoutMs: callOptions.timeoutMs ?? defaultTimeoutFor(functionName),
                              operationMode: 'at-most-once',
                            },
                          )
                          .catch((error) => {
                            if (produced.has(error) || !controlFailures.has(error)) throw error;
                            return new SharedOwnerDenial(error);
                          });
                      })
                      .then((result) => {
                        if (result instanceof SharedOwnerDenial) throw result.error;
                        return result;
                      });
                    void ownerWork.catch((error) => {
                      denied = error;
                      if (!activeTransport) channel.deny(error);
                    });
                  }
                : undefined,
            });

            // Check if results exist and contain at least one element
            if (!results || results.length === 0) {
              throw new PythonBridgeError(`No output received from Python script.`, {
                functionName,
              });
            }

            // Join the lines and parse manually
            let stdoutString = results.join('\n');
            let mcpResponseData: any;
            try {
              // First parse: Get the MCP response object { content: [{ type: 'text', text: '...' }] }
              mcpResponseData = JSON.parse(stdoutString);
              // Defensive stripping of the retired stdout carrier; never accept it.
              if (mcpResponseData && typeof mcpResponseData === 'object')
                delete mcpResponseData._eapi_session;
              stdoutString = JSON.stringify(mcpResponseData);
            } catch {
              throw new PythonBridgeError(
                `Failed to parse initial JSON output from Python script.`,
                { functionName },
                false, // Parse errors are not retryable
              );
            }

            // Validate the MCP response structure and extract the nested JSON string
            if (
              !mcpResponseData ||
              !Array.isArray(mcpResponseData.content) ||
              mcpResponseData.content.length === 0 ||
              typeof mcpResponseData.content[0].text !== 'string'
            ) {
              throw new PythonBridgeError(
                `Invalid MCP response structure received from Python script.`,
                { functionName },
                false, // Structure errors are not retryable
              );
            }

            const nestedJsonString = mcpResponseData.content[0].text;
            let resultData: any;
            try {
              // Second parse: Get the actual result object from the nested string
              resultData = JSON.parse(nestedJsonString);
            } catch {
              throw new PythonBridgeError(
                'Failed to parse nested JSON result from Python script.',
                { functionName },
                false, // Parse errors are not retryable
              );
            }

            if (activeTransport?.action === 'login' && resultData?.login_failed) {
              const failure = resultData.login_failed;
              throw new PythonBridgeError(
                String(resultData.error || 'Z-Library login failed'),
                {
                  reason: 'login_failed',
                  loginStatus: failure.status_code,
                  pythonErrorType: resultData.type,
                  loginToken: failure.token,
                  diagnostics: readAuthDiagnostics(resultData.diagnostics),
                },
                false,
              );
            }
            if (
              activeTransport &&
              resultData?.auth_rejected?.generation === activeTransport.generation
            ) {
              const rejected = authRejected(
                activeTransport.generation,
                String(resultData.error || 'EAPI authentication rejected'),
              );
              rejected.context!.diagnostics = readAuthDiagnostics(resultData.diagnostics);
              throw rejected;
            }
            // Check if the *actual* Python result contained an error structure
            if (
              resultData &&
              typeof resultData === 'object' &&
              'error' in resultData &&
              resultData.error
            ) {
              throw new PythonBridgeError(
                `Python bridge execution failed for ${functionName}: ${resultData.error}`,
                {
                  functionName,
                  details: resultData.details,
                  pythonErrorType: resultData.type,
                  httpStatus: resultData.status_code,
                  diagnostics: readAuthDiagnostics(resultData.diagnostics),
                },
                isBridgeDetailRetryable(resultData.details),
              );
            }

            // Accept private metadata only after excluding a matching auth rejection.
            adoptUpdate();
            if (lazy) {
              completeOperation(resultData);
              if (ownerWork) await ownerWork;
            }
            return resultData;
          } catch (caught: any) {
            let err = caught;
            // The acquisition owner shares control failures with other waiters.
            // Add caller diagnostics to a copy, never to the producing error.
            if (
              transport &&
              transport.action !== 'operation' &&
              caught &&
              typeof caught === 'object'
            ) {
              err = Object.create(
                Object.getPrototypeOf(caught),
                Object.getOwnPropertyDescriptors(caught),
              );
              Object.defineProperty(err, 'stack', {
                value: caught.stack,
                writable: true,
                configurable: true,
              });
              err.context = { ...caught.context };
            }
            // Capture runner's internal corruption identity before owner replacement.
            const protocolCorrupted = caught?.privateSessionProtocolCorruption === true;
            if (lazy) {
              rejectOperation(err);
              // An owner denial wins over the consequent child failure, but our
              // own cleanup abort must not hide an earlier protocol/child error.
              const ownerDenial = denied;
              if (!activeTransport) lifetime.abort();
              if (ownerWork) {
                try {
                  await ownerWork;
                } catch (ownerError) {
                  if (activeTransport) err = denied ?? ownerError;
                  else if (ownerDenial || signal?.aborted) err = ownerDenial ?? ownerError;
                }
              }
              if (err && typeof err === 'object') {
                // Owner acquisition denials can be shared by concurrent waiters.
                // Preserve the error's type/own properties without changing theirs.
                const shared = err;
                err = Object.create(
                  Object.getPrototypeOf(shared),
                  Object.getOwnPropertyDescriptors(shared),
                );
                // V8's own `stack` accessor reads undefined on a copy; keep the value.
                Object.defineProperty(err, 'stack', {
                  value: shared.stack,
                  writable: true,
                  configurable: true,
                });
                err.context = {
                  ...err.context,
                  details: { ...err.context?.details, request_issued: !!activeTransport },
                };
              }
            }
            if (protocolCorrupted) {
              sessionUpdate = undefined; // No metadata adoption from corrupt fd3/fd4 streams.
              err = new PythonBridgeError(
                'Invalid private session protocol',
                {
                  functionName,
                  reason: 'internal',
                  details: { request_issued: !!activeTransport },
                },
                false,
              );
            } else if (!['auth_rejected', 'login_failed'].includes(err?.context?.reason))
              adoptUpdate();
            // Never log or retain subprocess environments or internal bearer output.
            sanitizeSessionError(err, [
              configuredEnv.ZLIBRARY_EMAIL || '',
              configuredEnv.ZLIBRARY_PASSWORD || '',
              activeTransport?.remix_userkey || '',
              session?.remix_userkey || '',
              (sessionUpdate as any)?.remix_userkey || '',
              eapiSessions.get(identity)?.remix_userkey || '',
            ]);
            const annotated = (failure: any) => {
              if (diagnosticEapi && hasRequestEvidence(failure))
                (failure.context ??= {}).diagnostics = authDiagnosticsFor(
                  failure,
                  requestId,
                  attempt,
                  activeTransport?.authRetry ? 1 : 0,
                );
              return failure;
            };

            // If it's already a ZLibraryError, just rethrow
            if (err instanceof ZLibraryError) {
              throw annotated(err);
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
              throw annotated(
                new PythonBridgeError(
                  `${functionName} failed: ${bridgeError.error}`,
                  {
                    functionName,
                    details: {
                      ...err.context?.details,
                      ...bridgeError.details,
                      // Lazy transport is assigned only at this call's handoff;
                      // that grant wins a child false, mere availability does not.
                      ...(activeTransport &&
                      (lazy || typeof bridgeError.details?.request_issued !== 'boolean')
                        ? { request_issued: true }
                        : {}),
                    },
                    pythonErrorType: bridgeError.type,
                    httpStatus: bridgeError.status_code,
                    diagnostics: readAuthDiagnostics(bridgeError.diagnostics),
                    stderr: err.stderr,
                    originalError: err,
                  },
                  // A details-bearing envelope has already used Python failover.
                  isBridgeDetailRetryable(bridgeError.details),
                ),
              );
            }

            // Wrap in PythonBridgeError with context
            throw annotated(
              new PythonBridgeError(
                `Python bridge execution failed for ${functionName}: ${err.message || err}.${stderrOutput}`,
                {
                  functionName,
                  stderr: err.stderr,
                  details: err.context?.details,
                  retryAfterMs: err.context?.retryAfterMs,
                  originalError: err,
                },
              ),
            );
          } finally {
            if (lazy) {
              lifetime.abort();
              // Owner waiters can settle before their cancelled control child.
              // This call still owns that child's actual pipe/exit cleanup.
              await Promise.allSettled(controlWork);
            }
          }
        };
        return execute();
      },
      {
        signal,
        maxRetries:
          functionName === 'acquire_book' || transport?.action === 'login' || transport?.authRetry
            ? 0
            : parseInt(process.env.RETRY_MAX_RETRIES || '3'),
        initialDelay: parseInt(process.env.RETRY_INITIAL_DELAY || '1000'),
        maxDelay: parseInt(process.env.RETRY_MAX_DELAY || '30000'),
        factor: parseFloat(process.env.RETRY_FACTOR || '2'),
        shouldRetry: isRetryableError,
      },
    );
  const invoke = async (transport?: SessionInput, signal = callOptions.signal) => {
    if (signal?.aborted) {
      const label = `python_bridge.${functionName}`;
      throw new BridgeTimeoutError(`${label} aborted before it started`, {
        label,
        reason: 'aborted',
      });
    }
    // Account the logical call's final verdict, after its retry budget. A lazy
    // child is admitted once at its session request above, including owner
    // control children; receipt-only and intentional auth retries stay outside.
    return lazyAcquisition || receiptOnly || transport?.authRetry
      ? invokeWithRetry(transport, signal)
      : breaker.execute((admission) => {
          boundProbe(admission);
          return invokeWithRetry(transport, signal);
        });
  };
  if (reuse)
    return finishCall(
      eapiSessions.run(
        identity,
        (input, signal) =>
          input.action === 'operation' ? invoke(input, signal) : markControl(invoke(input, signal)),
        {
          signal: callOptions.signal,
          timeoutMs: callOptions.timeoutMs ?? defaultTimeoutFor(functionName),
        },
      ),
    );
  if (functionName !== 'acquire_book' || lazyAcquisition || receiptOnly)
    return finishCall(invoke());
  // #267: a verified receipt is local, so an open breaker must not hide it. Only
  // a refused acquisition pays for the receipt-only child, and any non-hit there
  // (miss, refusal, its own failure) is answered with the original refusal.
  return finishCall(
    invoke().catch(async (refusal) => {
      if (refusal?.code !== 'CIRCUIT_BREAKER_OPEN') throw refusal;
      const hit = await callPythonFunction(
        functionName,
        { ...args, receipt_only: true },
        callOptions,
      ).catch(() => undefined);
      if (hit?.transfer_skipped === true) return hit;
      throw refusal;
    }),
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

export interface GetRecentBooksArgs {
  count?: number;
  format?: string;
}

interface DownloadBookToFileArgs {
  // id: string; // Replaced by bookDetails
  // format?: string | null; // Replaced by bookDetails
  bookDetails: Record<string, any>; // Expect the full book details object
  outputDir?: string;
}

interface DownloadBookResult {
  file_path: string;
}

const retiredProcessFlag = 'process_for_' + 'r' + 'ag';
/** Admit saved calls before any session acquisition or subprocess launch. */
function admitDownloadArgs(args: Record<string, unknown>): void {
  if (
    (retiredProcessFlag in args && args[retiredProcessFlag] !== false) ||
    'processed_output_format' in args
  ) {
    throw new Error(
      'Text processing was removed in 2.0. Use a separate 1.4.x installation for text processing.',
    );
  }
  const unknown = Object.keys(args).filter(
    (key) => !['bookDetails', 'outputDir', retiredProcessFlag].includes(key),
  );
  if (unknown.length) throw new Error(`Unknown download argument(s): ${unknown.join(', ')}`);
}

/**
 * Search for books in Z-Library
 */
export async function searchBooks(
  {
    query,
    exact = false,
    fromYear = null,
    toYear = null,
    languages = [],
    extensions = [],
    content_types = [],
    count = 10,
  }: SearchBooksArgs,
  options: CallOptions = {},
): Promise<any> {
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
    count: count,
  };
  return await callPythonFunction('search', pythonArgs, options);
}
/**
 * Perform full text search
 */
export async function fullTextSearch(
  {
    query,
    exact = false,
    phrase = true,
    words = false,
    languages = [],
    extensions = [],
    content_types = [],
    count = 10,
  }: FullTextSearchArgs,
  options: CallOptions = {},
): Promise<any> {
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
    count: count,
  };
  return await callPythonFunction('full_text_search', pythonArgsFTS, options);
}

/**
 * Get user's download history
 */
export async function getDownloadHistory(
  { count = 10 }: GetDownloadHistoryArgs,
  options: CallOptions = {},
): Promise<any> {
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
 * Get recently added books from Z-Library.
 *
 * Calls the Python bridge `get_recent_books` operation with the requested count
 * (default 10) and optional cancellation signal. When format filtering is specified,
 * filters the returned books by file extension in TypeScript to preserve the advertised
 * format contract while respecting Python's single-argument `count` signature.
 */
export async function getRecentBooks(
  args: GetRecentBooksArgs = {},
  options: CallOptions = {},
): Promise<any> {
  const count = args.count ?? 10;
  // Keep direct facade consumers aligned with the MCP schema. Broader limits
  // across tools remain #173; this only prevents invalid Python slice counts.
  if (!Number.isInteger(count) || count <= 0) {
    throw new RangeError('count must be a positive integer');
  }
  const format = args.format;

  const result = await callPythonFunction('get_recent_books', { count }, options);

  if (format && result && Array.isArray(result.books)) {
    const targetFormat = format.trim().toLowerCase().replace(/^\./, '');
    const filtered = result.books.filter((book: any) => {
      const ext = (book.extension || '').trim().toLowerCase().replace(/^\./, '');
      return ext === targetFormat;
    });
    return {
      ...result,
      books: filtered,
    };
  }

  return result;
}

/**
 * Download a book directly to a file
 */
export async function downloadBookToFile(
  args: DownloadBookToFileArgs,
  options: CallOptions = {},
): Promise<DownloadBookResult> {
  admitDownloadArgs(args as unknown as Record<string, unknown>);
  const { bookDetails, outputDir } = args;
  try {
    // Call the Python function, passing the bookDetails object
    const result = await callPythonFunction(
      'download_book',
      {
        book_details: bookDetails, // Pass the whole object
        // book_id: id, // Removed
        // format: format, // Removed
        output_dir: outputDir,
      },
      options,
    );

    // Check if the Python script returned an error structure
    if (result && result.error) {
      throw new Error(`Python download failed: ${result.error}`);
    }

    // Validate the response structure
    if (!result || !result.file_path) {
      // Compat check
      throw new Error('Invalid response from Python bridge: Missing original file_path.');
    }

    if (typeof result.file_path !== 'string') {
      throw new Error('Invalid response from Python bridge: file_path must be a string.');
    }

    const processingKeys = [
      'processed_file_path',
      'metadata_file_path',
      'footnotes_file_path',
      'endnotes_file_path',
      'citations_file_path',
      'stats',
      'content_types_produced',
      'output_files',
      'processing_error',
    ];
    if (processingKeys.some((key) => key in result)) {
      throw new Error('Invalid response from Python bridge: unexpected text-processing bundle.');
    }
    return result as DownloadBookResult;
  } catch (error: any) {
    // Keep the normalized bridge context on the public error itself. A generic
    // Error with details hidden under cause made every direct consumer invent
    // its own traversal rule and dropped structuredContent at the handler.
    if (error instanceof ZLibraryError) {
      error.message = `Failed to download book: ${error.message || 'Unknown error'}`;
      throw error;
    }
    throw new Error(`Failed to download book: ${error.message || 'Unknown error'}`, {
      cause: error,
    });
  }
}

/**
 * Phase 3 Research Tools - Exported wrappers for advanced search features
 */

export async function searchByTerm(
  args: {
    term: string;
    yearFrom?: number;
    yearTo?: number;
    languages?: string[];
    extensions?: string[];
    limit?: number;
  },
  options: CallOptions = {},
): Promise<any> {
  return callPythonFunction(
    'search_by_term_bridge',
    {
      term: args.term,
      year_from: args.yearFrom,
      year_to: args.yearTo,
      languages: args.languages,
      extensions: args.extensions,
      limit: args.limit || 25,
    },
    options,
  );
}

export async function searchByAuthor(
  args: {
    author: string;
    exact?: boolean;
    yearFrom?: number;
    yearTo?: number;
    languages?: string[];
    extensions?: string[];
    limit?: number;
  },
  options: CallOptions = {},
): Promise<any> {
  return callPythonFunction(
    'search_by_author_bridge',
    {
      author: args.author,
      exact: args.exact || false,
      year_from: args.yearFrom,
      year_to: args.yearTo,
      languages: args.languages,
      extensions: args.extensions,
      limit: args.limit || 25,
    },
    options,
  );
}

export async function searchAdvanced(
  args: {
    query: string;
    exact?: boolean;
    yearFrom?: number;
    yearTo?: number;
    count?: number;
  },
  options: CallOptions = {},
): Promise<any> {
  return callPythonFunction(
    'search_advanced',
    {
      query: args.query,
      exact: args.exact || false,
      from_year: args.yearFrom,
      to_year: args.yearTo,
      count: args.count || 10,
    },
    options,
  );
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

/** Internal acquisition boundary; public tools validate and translate their own arguments. */
export { callPythonFunction as invokeAcquisitionBridge };
