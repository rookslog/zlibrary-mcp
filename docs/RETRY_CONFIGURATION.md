# Retry Logic and Circuit Breaker Configuration

## Overview

The Z-Library MCP server implements comprehensive retry logic with exponential backoff and circuit breaker patterns to handle transient failures in Z-Library's unreliable infrastructure.

## Features

- **Exponential Backoff**: Automatically retries failed operations with increasing delays
- **Circuit Breaker**: Prevents cascading failures by opening circuit after repeated failures
- **Smart Error Classification**: Distinguishes between retryable and fatal errors
- **Configurable Parameters**: All retry behavior can be customized via environment variables

## Environment Variables

### Retry Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RETRY_MAX_RETRIES` | `3` | Maximum number of retry attempts |
| `RETRY_INITIAL_DELAY` | `1000` | Initial retry delay in milliseconds |
| `RETRY_MAX_DELAY` | `30000` | Maximum retry delay in milliseconds (30 seconds) |
| `RETRY_FACTOR` | `2` | Exponential backoff multiplier |

### Circuit Breaker Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CIRCUIT_BREAKER_THRESHOLD` | `5` | Number of failures before opening circuit |
| `CIRCUIT_BREAKER_TIMEOUT` | `60000` | Time in ms before attempting to close circuit (1 minute) |

### Multi-Source Timeout Configuration

Nothing in the multi-source path may run unbounded. Two layers cooperate, and
the outer Node budget must remain larger than the worst-case inner provider walk.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOK_SOURCE_CONNECT_TIMEOUT` | `10` | TCP/TLS connect budget per phase, in seconds |
| `BOOK_SOURCE_READ_TIMEOUT` | `30` | Response-read budget per phase, in seconds |
| `BOOK_SOURCE_TOTAL_TIMEOUT` | `45` | Wall-clock budget for one provider operation, in seconds |
| `BOOK_SOURCE_DOWNLOAD_TIMEOUT` | `1500` | Wall-clock budget for a complete source-file transfer, in seconds |
| `BOOK_SOURCE_PREFLIGHT` | `true` | Probe DNS and TCP before a provider request |
| `BOOK_SOURCE_PREFLIGHT_TIMEOUT` | `5` | Budget per DNS or TCP probe phase, in seconds |
| `PYTHON_BRIDGE_TIMEOUT` | `240000` | Ordinary Python bridge wall-clock budget, in milliseconds |
| `PYTHON_BRIDGE_LONG_TIMEOUT` | `2400000` | Download and document-processing bridge budget, in milliseconds |
| `PYTHON_BRIDGE_KILL_GRACE` | `3000` | Grace between process-tree termination and forced kill, in milliseconds |

Malformed, non-positive, and non-finite values fall back to their defaults rather
than disabling a deadline.

Preflight is skipped when `HTTP_PROXY` or `HTTPS_PROXY` covers the target, with
`NO_PROXY` honored. A direct DNS/TCP socket cannot represent a proxied request;
letting that probe veto the real client would create a false outage.

The preflight budget applies to two phases (DNS, then TCP). At the defaults, one
provider attempt can cost at most `2 × 5s + 45s = 55s`. LibGen can walk three
mirrors and `auto` can add Anna's Archive, so the worst-case search budget is
`4 × 55s = 220s`, below the 240-second ordinary bridge budget. Raise
`PYTHON_BRIDGE_TIMEOUT` whenever provider budgets make that composition larger;
otherwise the outer process-tree kill can preempt legitimate fallback work.

Source-file transfer has a separate 1,500-second default because a valid large
book can exceed the 45-second search and URL-resolution budget. The 2,400-second
long bridge default composes worst-case LibGen resolution
(`3 × (2 × 5s + 45s) = 165s`), transfer (`1,500s`), the OCR subprocess default
(`600s`), and finalization headroom (`135s`). If any component is overridden,
keep `PYTHON_BRIDGE_LONG_TIMEOUT` at least as large as their sum after converting
milliseconds to seconds.

## How It Works

### Exponential Backoff

When an operation fails, the system will:

1. Check if the error is retryable (network errors, timeouts, server errors)
2. Wait for the calculated delay: `min(initialDelay * (factor ^ attempt), maxDelay)`
3. Retry the operation
4. Repeat until success or max retries exhausted

**Example delay progression** (defaults):
- Attempt 1: 1000ms (1 second)
- Attempt 2: 2000ms (2 seconds)
- Attempt 3: 4000ms (4 seconds)
- Attempt 4: 8000ms (8 seconds)

### Circuit Breaker

The circuit breaker protects against cascading failures:

1. **CLOSED** (normal): All operations pass through
2. **OPEN** (failing): After threshold failures, circuit opens and rejects all operations
3. **HALF_OPEN** (testing): After timeout, allows one operation to test if service recovered
4. **Back to CLOSED**: If test succeeds, circuit closes and normal operation resumes

## Error Classification

### Retryable Errors

These errors trigger retry logic:
- Network errors (`NETWORK_ERROR`, `TIMEOUT`, `ECONNREFUSED`, `ETIMEDOUT`)
- Server errors (`SERVER_ERROR`, `5xx` status codes)
- Domain errors (`DOMAIN_ERROR`)
- Transient Python errors (connection/network related)

### Non-Retryable Errors

These errors fail immediately without retry:
- Authentication errors (`AUTH_ERROR`, `FORBIDDEN`)
- Validation errors (`INVALID_INPUT`, `VALIDATION_ERROR`)
- Fatal errors (marked with `fatal: true`)
- Parse errors (malformed responses)

## Usage Examples

### Default Configuration

```typescript
// Uses default retry settings from environment
const books = await searchBooks({ query: 'python' });
```

### Custom Retry Configuration

Set environment variables before starting the server:

```bash
# More aggressive retry strategy
export RETRY_MAX_RETRIES=5
export RETRY_INITIAL_DELAY=500
export RETRY_MAX_DELAY=60000
export RETRY_FACTOR=3

# More sensitive circuit breaker
export CIRCUIT_BREAKER_THRESHOLD=3
export CIRCUIT_BREAKER_TIMEOUT=30000

npm start
```

### Monitoring Retry Behavior

The system logs all retry attempts:

```
Attempt 1 failed, retrying in 1000ms... {
  error: 'Network timeout',
  attempt: 1,
  maxRetries: 3,
  delay: 1000
}
```

Circuit breaker state changes are also logged:

```
Circuit breaker state transition: CLOSED -> OPEN
Circuit breaker opened due to repeated failures {
  failureCount: 5,
  threshold: 5,
  lastFailureTime: '2025-09-30T12:00:00.000Z'
}
```

## Implementation Details

### Core Components

1. **retry-manager.ts**: Exponential backoff implementation
2. **circuit-breaker.ts**: Circuit breaker pattern
3. **errors.ts**: Custom error classes with retry metadata
4. **zlibrary-api.ts**: Integration point for all API calls

### Code Example

```typescript
import { withRetry, isRetryableError } from './retry-manager';
import { CircuitBreaker } from './circuit-breaker';

// Create circuit breaker
const breaker = new CircuitBreaker({
  threshold: 5,
  timeout: 60000
});

// Wrap operation with retry logic
const result = await withRetry(
  async () => {
    return breaker.execute(async () => {
      // Your operation here
      return await fetchData();
    });
  },
  {
    maxRetries: 3,
    initialDelay: 1000,
    shouldRetry: isRetryableError
  }
);
```

### Custom Retry Logic

You can override the retry behavior for specific operations:

```typescript
import { withRetry } from './retry-manager';

const result = await withRetry(
  () => criticalOperation(),
  {
    maxRetries: 5,
    initialDelay: 500,
    maxDelay: 10000,
    factor: 2,
    shouldRetry: (error) => {
      // Custom retry logic
      return error.code === 'CUSTOM_RETRYABLE';
    },
    onRetry: (attempt, error, delay) => {
      // Custom logging or metrics
      console.log(`Retry ${attempt}: ${error.message}`);
    }
  }
);
```

## Performance Considerations

### Default Settings Rationale

- **3 retries**: Balances resilience with responsiveness (max ~15s with defaults)
- **1s initial delay**: Avoids overwhelming failing services
- **30s max delay**: Prevents indefinite waits
- **2x factor**: Reasonable exponential growth

### Tuning Recommendations

**For development/testing**:
```bash
export RETRY_MAX_RETRIES=1
export RETRY_INITIAL_DELAY=100
```

**For production with unstable network**:
```bash
export RETRY_MAX_RETRIES=5
export RETRY_INITIAL_DELAY=2000
export RETRY_MAX_DELAY=60000
```

**For rate-limited APIs**:
```bash
export RETRY_INITIAL_DELAY=5000
export RETRY_MAX_DELAY=120000
export RETRY_FACTOR=3
```

## Troubleshooting

### Circuit Keeps Opening

If circuit breaker keeps opening:
1. Check Z-Library service availability
2. Increase `CIRCUIT_BREAKER_THRESHOLD`
3. Verify network connectivity
4. Review error logs for root cause

### Too Many Retries

If operations retry excessively:
1. Reduce `RETRY_MAX_RETRIES`
2. Check error classification (ensure fatal errors don't retry)
3. Review `shouldRetry` predicate logic

### Slow Response Times

If responses are too slow:
1. Reduce `RETRY_INITIAL_DELAY`
2. Reduce `RETRY_MAX_DELAY`
3. Consider reducing `RETRY_MAX_RETRIES`

## Related Documentation

- [PATTERNS.md](../.claude/PATTERNS.md) - Code patterns and best practices
- [ISSUES.md](../ISSUES.md) - Known issues including ISSUE-005 (resolved)
- [ADR-002](./adr/ADR-002-Download-Workflow-Redesign.md) - Download workflow architecture

## Testing

Comprehensive unit tests cover all retry scenarios:
- `__tests__/retry-manager.test.js`: Retry logic tests (96.96% coverage)
- `__tests__/circuit-breaker.test.js`: Circuit breaker tests (100% coverage)

Run tests:
```bash
npm test -- retry-manager.test.js
npm test -- circuit-breaker.test.js
```

## Version History

- **v1.0.0** (2025-09-30): Initial implementation resolving ISSUE-005
  - Exponential backoff with configurable parameters
  - Circuit breaker pattern
  - Smart error classification
  - Comprehensive test coverage
