// Environment-only metadata fixture for tests that replace the Python runtime.
// Disk resolution and subprocess transport are exercised in login.test.js.
import { jest } from '@jest/globals';
jest.unstable_mockModule('../../dist/lib/login.js', () => ({
  readLoginMetadata: async (env = process.env) => ({
    configured: Boolean(env.ZLIBRARY_EMAIL && env.ZLIBRARY_PASSWORD),
    identity: env.ZLIBRARY_EMAIL || '',
    origin: env.ZLIBRARY_EMAIL && env.ZLIBRARY_PASSWORD ? 'environment' : 'none',
    backend: 'file',
    expiry: 'unknown',
    generation: 0,
    notice: '',
  }),
  runLoginCommand: async () => {
    throw new Error('CLI must use the real Python runtime');
  },
}));
