import { existsSync } from 'fs';
import type { Options as PythonShellOptions } from 'python-shell';
import { getManagedPythonPath } from './venv-manager.js'; // Import from the TS file
import { getPythonLibDirectory, getPythonScriptPath } from './paths.js';
import { LONG_BRIDGE_TIMEOUT_MS, runPythonBridge } from './python-runner.js';
import type { RunBridgeOptions } from './python-runner.js';
import { PythonBridgeError } from './errors.js';
import { randomUUID } from 'node:crypto';
import { needsEapi } from './eapi-session.js';
import { readAuthDiagnostics, safeEapiFailureEnvelope } from './auth-diagnostics.js';

const BRIDGE_SCRIPT_NAME = 'python_bridge.py';
const LONG_RUNNING_FUNCTIONS = new Set(['download_book']);

export interface BridgeErrorEnvelope {
  error: string;
  type?: string;
  status_code?: number;
  redirect_host?: string;
  details?: any;
  diagnostics?: unknown;
}

const PERMANENT_CALLER_REASONS = new Set(['configuration_error', 'quota_exhausted']);

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
  return details === undefined;
}

/** Extract the final JSON provider-error envelope from mixed stderr logs. */
export function parseBridgeErrorEnvelope(stderr: unknown): BridgeErrorEnvelope | null {
  if (typeof stderr !== 'string' || stderr.length === 0) return null;
  const lines = stderr.split('\n').slice(-32);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = (lines[index] ?? '').trim();
    if (Buffer.byteLength(line) > 2048) continue;
    if (!line.startsWith('{')) continue;
    try {
      const parsed = JSON.parse(line);
      if (parsed && typeof parsed.error === 'string') {
        if ('diagnostics' in parsed)
          return (safeEapiFailureEnvelope(parsed) as BridgeErrorEnvelope) ?? null;
        return parsed;
      }
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
        `Expected location: <project_root>/lib/python_bridge.py`,
    );
  }

  // Serialize arguments as JSON
  const serializedArgs = JSON.stringify(args);
  const options: PythonShellOptions = {
    mode: 'text',
    pythonPath: pythonExecutable,
    scriptPath: getPythonLibDirectory(),
    args: [functionName, serializedArgs],
    ...(needsEapi(functionName, args)
      ? {
          env: {
            ...process.env,
            ZLIBRARY_DIAGNOSTIC_ID: randomUUID(),
            ZLIBRARY_DIAGNOSTIC_EAPI: '1',
            ZLIBRARY_DIAGNOSTIC_ATTEMPT: '1',
            ZLIBRARY_DIAGNOSTIC_REFRESH: '0',
          },
        }
      : {}),
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
      const message = `Python process exited with code ${error.exitCode}`;
      if (envelope) {
        throw new PythonBridgeError(
          message,
          {
            functionName,
            details: envelope.details,
            pythonErrorType: envelope.type,
            httpStatus: envelope.status_code,
            redirectHost: envelope.redirect_host,
            diagnostics: readAuthDiagnostics(envelope.diagnostics),
          },
          isBridgeDetailRetryable(envelope.details),
        );
      }
      throw new PythonBridgeError(message, { functionName }, false);
    }
    throw error;
  }

  try {
    return JSON.parse(output);
  } catch {
    throw new PythonBridgeError('Failed to parse Python result JSON.', { functionName }, false);
  }
}
