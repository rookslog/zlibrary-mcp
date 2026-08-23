/**
 * Simplified venv-manager for UV (v2.0.0)
 *
 * UV automatically creates and manages .venv/ in the project directory.
 * This module simply provides the path to UV's Python executable.
 *
 * MIGRATION NOTES:
 * - Replaces 406 lines of cache venv management with ~45 lines
 * - No cache directory at ~/.cache/zlibrary-mcp/
 * - No .venv_config file
 * - No programmatic pip installation
 * - UV handles all dependency management
 *
 * Setup: Run `uv sync --no-dev` before building (`uv sync` for contributors)
 */

import * as path from 'path';
import { accessSync, constants, existsSync } from 'fs';
import { fileURLToPath } from 'url';

// Recreate __dirname for ESM
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Get path to UV-managed Python executable
 *
 * UV creates .venv/ in project root when you run: uv sync --no-dev
 * This function returns the path to Python in that venv.
 *
 * @returns {Promise<string>} Path to Python executable in .venv
 * @throws {Error} If .venv not found (user needs to run: uv sync --no-dev)
 */
/**
 * Path segments to UV's Python executable inside `.venv`, for a given platform.
 *
 * UV follows the platform convention: `.venv/bin/python` on POSIX,
 * `.venv\Scripts\python.exe` on Windows. Hardcoding the POSIX layout meant the
 * venv was never found on Windows and every invocation failed with the
 * "run uv sync --no-dev" error even after a successful sync.
 *
 * Exported and platform-parameterised so both branches are testable from any
 * host — a platform-conditional that only runs on the platform it is broken for
 * is how this reached users in the first place.
 *
 * @param platform - A `process.platform` value
 */
export function venvPythonSegments(platform: NodeJS.Platform): string[] {
  return platform === 'win32'
    ? ['.venv', 'Scripts', 'python.exe']
    : ['.venv', 'bin', 'python'];
}

/** Shell command for removing a corrupted `.venv`, per platform. */
export function venvRemoveCommand(platform: NodeJS.Platform): string {
  return platform === 'win32' ? 'rmdir /s /q .venv' : 'rm -rf .venv';
}

/** Shell command for installing UV, per platform. */
export function uvInstallCommand(platform: NodeJS.Platform): string {
  return platform === 'win32'
    ? 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    : 'curl -LsSf https://astral.sh/uv/install.sh | sh';
}

export async function getManagedPythonPath(): Promise<string> {
  const projectRoot = path.resolve(__dirname, '..', '..');
  const uvVenvPython = path.join(projectRoot, ...venvPythonSegments(process.platform));

  // Check if UV venv exists
  if (!existsSync(uvVenvPython)) {
    throw new Error(
      'Python virtual environment not found.\n\n' +
      'UV has not initialized the environment. Please run:\n' +
      '  bash setup-uv.sh --no-dev\n' +
      '  # or, equivalently:  uv sync --no-dev\n\n' +
      'This will:\n' +
      '  1. Create .venv/ directory\n' +
      '  2. Install the runtime dependencies from pyproject.toml\n' +
      '  3. Generate uv.lock for reproducibility\n\n' +
      'Contributing to this repo rather than running it? Use\n' +
      '  bash setup-uv.sh\n' +
      'which adds the development group (pytest, Ruff, pip-audit).\n\n' +
      'First time setup? Install UV:\n' +
      `  ${uvInstallCommand(process.platform)}\n` +
      '  # Or: pip install uv\n\n' +
      'See: https://docs.astral.sh/uv/getting-started/installation/'
    );
  }

  // Validate only filesystem state here. Executing `python --version` created
  // a second, synchronous and unbounded subprocess path outside the bridge
  // lifecycle owner. The first real bridge invocation is the execution check.
  try {
    accessSync(uvVenvPython, process.platform === 'win32' ? constants.F_OK : constants.X_OK);
  } catch (error) {
    throw new Error(
      `Python at ${uvVenvPython} is not executable.\n` +
      `This usually means .venv is corrupted. Try:\n` +
      `  ${venvRemoveCommand(process.platform)}\n` +
      `  uv sync --no-dev`,
      { cause: error }
    );
  }

  return uvVenvPython;
}

// MIGRATION NOTE: Removed from v1.x:
// - getCacheDir() - No longer needed (no cache venv)
// - getConfigPath() - No longer needed (no config file)
// - readVenvPathConfig() - No longer needed
// - writeVenvPathConfig() - No longer needed
// - createVenv() - UV handles this
// - installDependencies() - UV handles this
// - ensureVenvReady() - UV handles this
// - checkPackageInstalled() - UV handles this
// - findPythonExecutable() - UV handles this
// - runCommand() - UV handles this
// - VenvManagerDependencies interface - No longer needed
// - defaultDeps - No longer needed
// - All complex error handling - Simplified
//
// Total reduction: 406 lines → 45 lines (89% reduction)
