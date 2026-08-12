import { existsSync } from 'fs';
import type { Options as PythonShellOptions } from 'python-shell';
import { getManagedPythonPath } from './venv-manager.js'; // Import from the TS file
import { getPythonLibDirectory, getPythonScriptPath } from './paths.js';
import { runPythonBridge } from './python-runner.js';
import type { RunBridgeOptions } from './python-runner.js';

const BRIDGE_SCRIPT_NAME = 'python_bridge.py';

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
      label: runOptions.label ?? `python_bridge.${functionName}`,
    });
    output = lines.join('\n');
  } catch (error: any) {
    if (typeof error?.exitCode === 'number') {
      throw new Error(
        `Python process exited with code ${error.exitCode}: ${error.stderr ?? error.message}. ` +
          `Raw stdout: ${error.stdout ?? ''}`,
        { cause: error },
      );
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
