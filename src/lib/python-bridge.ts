import { existsSync } from 'fs';
import type { Options as PythonShellOptions } from 'python-shell';
import { getManagedPythonPath } from './venv-manager.js'; // Import from the TS file
import { getPythonLibDirectory, getPythonScriptPath } from './paths.js';
import { LONG_BRIDGE_TIMEOUT_MS, runPythonBridge } from './python-runner.js';
import type { RunBridgeOptions } from './python-runner.js';
import { PythonBridgeError } from './errors.js';

const BRIDGE_SCRIPT_NAME = 'python_bridge.py';
const LONG_RUNNING_FUNCTIONS = new Set(['download_book', 'process_document']);

export interface BridgeErrorEnvelope {
  error: string;
  type?: string;
  details?: any;
}

const UNREACHABLE_REASONS = new Set([
  'dns_failure',
  'dns_timeout',
  'connect_timeout',
  'connect_refused',
  'connect_error',
  'tls_error',
]);
// Reasons where retrying cannot help, because the next attempt needs the
// caller to change something rather than the provider to recover.
// `challenge_required` is here for a sharper reason than the other two: a
// browser-verification wall is cleared by a *person*, and each generic retry
// spawns a fresh bridge process whose rate limiter has forgotten the backoff —
// so retrying walks straight back into the wall three times, which is the
// behaviour Anna's politeness layer exists to prevent (Codex on #150).
const PERMANENT_CALLER_REASONS = new Set([
  'configuration_error',
  'quota_exhausted',
  'challenge_required',
  // The provider answered correctly and the answer will not change. Retrying
  // spends another Anna's daily slot and settle delay per attempt on a page
  // that already told us the edition is not there (Codex on #150).
  'not_found',
]);

function everyFailureHasReason(details: any, predicate: (reason: unknown) => boolean): boolean {
  if (!details || typeof details !== 'object') return false;
  if (typeof details.reason === 'string') return predicate(details.reason);
  return (
    Array.isArray(details.failures) &&
    details.failures.length > 0 &&
    details.failures.every((failure: any) => predicate(failure?.reason))
  );
}

export function isConfigurationBridgeDetail(details: any): boolean {
  return everyFailureHasReason(details, (reason) => reason === 'configuration_error');
}

export function isPermanentBridgeDetail(details: any): boolean {
  return everyFailureHasReason(details, (reason) => PERMANENT_CALLER_REASONS.has(String(reason)));
}

export function isBridgeDetailRetryable(details: any): boolean {
  if (isPermanentBridgeDetail(details)) return false;
  return !everyFailureHasReason(details, (reason) => UNREACHABLE_REASONS.has(String(reason)));
}

/** Extract the final JSON provider-error envelope from mixed stderr logs. */
export function parseBridgeErrorEnvelope(stderr: unknown): BridgeErrorEnvelope | null {
  if (typeof stderr !== 'string' || stderr.length === 0) return null;
  const lines = stderr.split('\n');
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = (lines[index] ?? '').trim();
    if (!line.startsWith('{')) continue;
    try {
      const parsed = JSON.parse(line);
      if (parsed && typeof parsed.error === 'string') return parsed;
    } catch {
      // Diagnostic line, not the envelope.
    }
  }
  return null;
}

/**
 * Execute a Python function from the python_bridge.py script.
 * @param functionName - Name of the Python function to call.
 * @param args - Arguments to pass to the function.
 * @param runOptions - Optional timeout and abort signal for the owned runner.
 * @returns Promise resolving with the result from the Python function.
 * @throws {Error} If the Python process fails or returns an error.
 */
export async function callPythonFunction(
  functionName: string,
  args: Record<string, any> = {},
  runOptions: RunBridgeOptions = {},
): Promise<any> {
  // Await async setup before creating the Promise (avoids async promise executor)
  const pythonExecutable = await getManagedPythonPath();

  const scriptPath = getPythonScriptPath(BRIDGE_SCRIPT_NAME);

  // Validate script exists before attempting to spawn
  if (!existsSync(scriptPath)) {
    throw new Error(
      `Python bridge script not found at: ${scriptPath}\n` +
      `This usually indicates a build or installation issue.\n` +
      `Expected location: <project_root>/lib/python_bridge.py`
    );
  }

  // Serialize arguments as JSON
  const serializedArgs = JSON.stringify(args);
  const options: PythonShellOptions = {
    mode: 'text',
    pythonPath: pythonExecutable,
    scriptPath: getPythonLibDirectory(),
    args: [functionName, serializedArgs],
  };

  let output: string;
  try {
    const lines = await runPythonBridge(BRIDGE_SCRIPT_NAME, options, {
      ...runOptions,
      timeoutMs:
        runOptions.timeoutMs ??
        (LONG_RUNNING_FUNCTIONS.has(functionName) ? LONG_BRIDGE_TIMEOUT_MS : undefined),
      label: runOptions.label ?? `python_bridge.${functionName}`,
    });
    output = lines.join('\n');
  } catch (error: any) {
    if (typeof error?.exitCode === 'number') {
      const envelope = parseBridgeErrorEnvelope(error.stderr);
      const message = `Python process exited with code ${error.exitCode}: ${error.stderr ?? error.message}. Raw stdout: ${error.stdout ?? ''}`;
      if (envelope) {
        throw new PythonBridgeError(
          message,
          {
            functionName,
            args,
            details: envelope.details,
            pythonErrorType: envelope.type,
            stderr: error.stderr,
            originalError: error,
          },
          isBridgeDetailRetryable(envelope.details),
        );
      }
      throw new Error(message, { cause: error });
    }
    throw error;
  }

  try {
    return JSON.parse(output);
  } catch (error: any) {
    throw new Error(
      `Failed to parse Python result JSON: ${error.message}. Raw output: ${output}. Stderr: `,
      { cause: error },
    );
  }
}
