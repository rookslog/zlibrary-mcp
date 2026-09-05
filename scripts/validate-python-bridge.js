#!/usr/bin/env node
/**
 * Build Validation Script
 *
 * Validates that all required Python bridge files exist at expected locations.
 * This catches missing files during build time instead of at runtime.
 *
 * Exit codes:
 *   0 - All validations passed
 *   1 - Missing files or validation errors
 */

import { existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const projectRoot = resolve(__dirname, '..');

// Required Python bridge files
const requiredPythonFiles = [
  { path: 'lib/python_bridge.py', critical: true, description: 'Main Python bridge script' },
  { path: 'lib/eapi_session.py', critical: true, description: 'Shared EAPI authentication' },
  { path: 'lib/eapi_accounts.py', critical: true, description: 'EAPI account selection' },
  { path: 'lib/rag_processing.py', critical: true, description: 'RAG document processing' },
  { path: 'lib/enhanced_metadata.py', critical: true, description: 'Enhanced metadata extraction' },
  { path: 'lib/author_tools.py', critical: false, description: 'Author-specific search tools' },
  { path: 'lib/booklist_tools.py', critical: false, description: 'Booklist management' },
  { path: 'lib/term_tools.py', critical: false, description: 'Term-based search tools' },
  { path: 'lib/__init__.py', critical: false, description: 'Python package initialization' }
];

// Required TypeScript compiled files (dist/)
const requiredCompiledFiles = [
  { path: 'dist/index.js', critical: true, description: 'Main MCP server entry point' },
  { path: 'dist/lib/python-bridge.js', critical: true, description: 'Python bridge TypeScript wrapper' },
  { path: 'dist/lib/zlibrary-api.js', critical: true, description: 'Z-Library API wrapper' },
  { path: 'dist/lib/venv-manager.js', critical: true, description: 'Python venv management' }
];

// Configuration files
const requiredConfigFiles = [
  { path: 'package.json', critical: true, description: 'Package configuration' },
  { path: 'tsconfig.json', critical: true, description: 'TypeScript configuration' },
  { path: 'pyproject.toml', critical: true, description: 'Python dependencies (UV)' },
  { path: 'uv.lock', critical: false, description: 'UV lockfile for reproducible builds' }
];

console.log('🔍 Z-Library MCP Build Validation\n');
console.log('=' .repeat(60));

let allValid = true;
let criticalFailures = 0;
let warnings = 0;

/**
 * Validate a list of files
 */
function validateFiles(fileList, category) {
  console.log(`\n📂 ${category}:`);
  console.log('-'.repeat(60));

  for (const file of fileList) {
    const fullPath = resolve(projectRoot, file.path);
    const exists = existsSync(fullPath);

    if (exists) {
      console.log(`✅ ${file.path}`);
      if (file.description) {
        console.log(`   └─ ${file.description}`);
      }
    } else {
      const marker = file.critical ? '❌ CRITICAL' : '⚠️  WARNING';
      console.error(`${marker} ${file.path} NOT FOUND`);
      if (file.description) {
        console.error(`   └─ ${file.description}`);
      }
      console.error(`   └─ Expected: ${fullPath}`);

      if (file.critical) {
        criticalFailures++;
        allValid = false;
      } else {
        warnings++;
      }
    }
  }
}

// Run validations
validateFiles(requiredPythonFiles, 'Python Bridge Files');
validateFiles(requiredCompiledFiles, 'Compiled TypeScript Files');
validateFiles(requiredConfigFiles, 'Configuration Files');

// Summary
console.log('\n' + '='.repeat(60));
console.log('📊 Validation Summary:');
console.log('-'.repeat(60));

const totalFiles = requiredPythonFiles.length + requiredCompiledFiles.length + requiredConfigFiles.length;
const passedFiles = totalFiles - criticalFailures - warnings;

console.log(`Total files checked: ${totalFiles}`);
console.log(`✅ Passed: ${passedFiles}`);
if (warnings > 0) {
  console.log(`⚠️  Warnings: ${warnings} (non-critical files missing)`);
}
if (criticalFailures > 0) {
  console.error(`❌ Critical failures: ${criticalFailures}`);
}

console.log('='.repeat(60));

// Exit with appropriate code
if (!allValid) {
  console.error('\n❌ BUILD VALIDATION FAILED');
  console.error('Missing critical files will cause runtime failures.');
  console.error('Please ensure all required files are present before deployment.\n');
  process.exit(1);
} else if (warnings > 0) {
  console.warn('\n⚠️  BUILD VALIDATION PASSED WITH WARNINGS');
  console.warn('Some non-critical files are missing.');
  console.warn('Full functionality may not be available.\n');
  process.exit(0);
} else {
  console.log('\n✅ BUILD VALIDATION PASSED');
  console.log('All required files are present and accounted for.\n');
  process.exit(0);
}
