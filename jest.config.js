export default {
  // Basic Node environment
  testEnvironment: 'node',

  // Match test files in __tests__
  testMatch: [
    '**/__tests__/**/*.test.js', // Assuming tests remain JS files
  ],

  // Ignore node_modules and dist (except for moduleNameMapper resolution)
  testPathIgnorePatterns: [
    '/node_modules/',
    '/dist/', // Ignore compiled output for test discovery
    '/__tests__/e2e/', // E2E tests require running Docker container
    '/__tests__/integration/', // Integration tests require live services
    // Agent worktrees check out a full copy of the repo under the project
    // root, so Jest discovers a second copy of every test file and runs it
    // against a tree with no node_modules and no dist/. Observed 2026-08-12:
    // one leftover worktree produced 111 phantom failures on a suite that is
    // green in CI. .gitignore does not help — Jest walks the filesystem.
    '/\\.claude/worktrees/',
  ],

  // Crucial: Map imports from __tests__ to compiled dist/ files
  moduleNameMapper: {
    // Map relative paths from __tests__ to the compiled files in dist/
    // Match imports like '../lib/module.js' from '__tests__/...'
    '^../lib/(.*)\\.js$': '<rootDir>/dist/lib/$1.js',
    // Match imports like '../index.js' or '../dist/index.js' from '__tests__/...'
    '^../(dist/)?index\\.js$': '<rootDir>/dist/index.js',
    // Keep the SDK mock if still needed, otherwise remove
    // '^@modelcontextprotocol/server$': '<rootDir>/__mocks__/@modelcontextprotocol/server.js',
  },

  // Use --forceExit in test script instead of globalTeardown
  // (globalTeardown's process.exit(0) masks coverage threshold failures)

  // Explicitly disable transformations to prevent Jest from interfering with ESM
  transform: {},

  // Coverage configuration
  collectCoverage: true,
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov', 'json-summary'],
  collectCoverageFrom: ['dist/**/*.js', '!dist/**/*.test.js', '!dist/**/*.d.ts'],
  // Ratcheted to just under the actual measurement so the gate catches a real
  // regression. The floors were once set against a 93-test suite and had
  // drifted ~20 points below reality, which meant coverage could halve without
  // CI noticing. Raise these when coverage rises; never lower them to make a
  // change pass.
  //
  // Measured 85.89 / 79.68 / 79.67 / 87.61 at 206 tests. Branches and functions
  // are ratcheted to that; statements and lines are deliberately left lower,
  // because six venv-manager tests pass only where a .venv exists and their
  // absence costs ~0.5 points on those two metrics — a floor that assumes them
  // would fail on a checkout that has not run `uv sync` yet.
  coverageThreshold: {
    global: {
      statements: 84,
      branches: 79,
      functions: 79,
      lines: 86,
    },
  },
};
