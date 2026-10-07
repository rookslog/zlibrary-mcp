import baseConfig from './jest.config.js';

export default {
  ...baseConfig,
  testPathIgnorePatterns: baseConfig.testPathIgnorePatterns.filter(
    (pattern) => pattern !== '/__tests__/e2e/' && pattern !== '/__tests__/integration/',
  ),
  collectCoverage: false,
};
