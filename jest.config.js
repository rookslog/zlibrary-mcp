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
  // Ratcheted to just under the actual measurement (85.08 / 79.59 / 77.89 / 87.37
  // at 165 tests) so the gate catches a real regression. The previous floors were
  // set against a 93-test suite and had drifted ~20 points below reality, which
  // meant coverage could halve without CI noticing. Raise these when coverage
  // rises; never lower them to make a change pass.
  coverageThreshold: {
    global: {
      statements: 84,
      branches: 78,
      functions: 76,
      lines: 86,
    },
  },
};
