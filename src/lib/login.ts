import { execFile, spawn } from 'node:child_process';
import { getManagedPythonPath } from './venv-manager.js';
import { getPythonScriptPath } from './paths.js';

export interface LoginMetadata {
  configured: boolean;
  identity: string;
  origin: 'none' | 'saved' | 'environment';
  backend: 'file';
  expiry: 'unknown';
  generation: number;
  notice: string;
}

/** Python owns storage and precedence; this channel carries public metadata only. */
export async function readLoginMetadata(
  env: NodeJS.ProcessEnv = process.env,
  options: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<LoginMetadata> {
  if (options.signal?.aborted) throw new Error('Login metadata lookup aborted.');
  const python = await getManagedPythonPath();
  return new Promise((resolve, reject) => {
    execFile(
      python,
      [getPythonScriptPath('login_store.py')],
      {
        env,
        signal: options.signal,
        timeout: Math.min(10000, options.timeoutMs ?? 10000),
        maxBuffer: 16384,
      },
      (error, stdout) => {
        if (error)
          return reject(
            new Error(
              options.signal?.aborted
                ? 'Login metadata lookup aborted.'
                : 'Login store refused; check setup and private permissions.',
            ),
          );
        try {
          const data = JSON.parse(stdout);
          if (
            !['none', 'saved', 'environment'].includes(data.origin) ||
            typeof data.identity !== 'string' ||
            typeof data.configured !== 'boolean' ||
            data.backend !== 'file' ||
            data.expiry !== 'unknown' ||
            !Number.isSafeInteger(data.generation) ||
            typeof data.notice !== 'string'
          )
            throw new Error();
          // Project fields explicitly; never relay arbitrary child properties.
          resolve({
            configured: data.configured,
            identity: data.identity,
            origin: data.origin,
            backend: 'file',
            expiry: 'unknown',
            generation: data.generation,
            notice: data.notice,
          });
        } catch {
          reject(new Error('Invalid login metadata response.'));
        }
      },
    );
  });
}

export async function runLoginCommand(args: string[]): Promise<number> {
  const command = args[0];
  const allowed = command === 'login' ? ['--json', '--password-stdin'] : ['--json'];
  if (
    !['login', 'status', 'logout'].includes(command ?? '') ||
    new Set(args.slice(1)).size !== args.length - 1 ||
    args.slice(1).some((arg) => !allowed.includes(arg))
  ) {
    process.stderr.write('Invalid command options; use login, status, logout, or mcp.\n');
    return 2;
  }
  try {
    const python = await getManagedPythonPath();
    return await new Promise((resolve) => {
      const child = spawn(python, [getPythonScriptPath('login_cli.py'), ...args], {
        stdio: 'inherit',
      });
      child.once('error', () => resolve(1));
      child.once('close', (code) => resolve(code ?? 1));
    });
  } catch {
    process.stderr.write('Login setup is unavailable; run bash setup-uv.sh.\n');
    return 1;
  }
}
