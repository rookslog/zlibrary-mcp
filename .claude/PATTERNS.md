# Z-Library MCP - Code Patterns & Best Practices

Diagnostics in TypeScript server modules use the shared `logger` from
`src/lib/logger.ts` (adjust the relative import for the calling module). It writes
to stderr; stdout is reserved for MCP JSON-RPC. These recipes are illustrative,
not additional runtime services or public tools. See `AGENTS.md` for the contract.

## Error Handling Patterns

### Pattern: Retry with Exponential Backoff

```typescript
async function withRetry<T>(
  operation: () => Promise<T>,
  options: {
    maxRetries?: number;
    initialDelay?: number;
    maxDelay?: number;
    factor?: number;
    shouldRetry?: (error: any) => boolean;
  } = {}
): Promise<T> {
  const {
    maxRetries = 3,
    initialDelay = 1000,
    maxDelay = 30000,
    factor = 2,
    shouldRetry = (error) => !error.fatal
  } = options;

  let lastError: any;
  let delay = initialDelay;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;

      if (attempt === maxRetries || !shouldRetry(error)) {
        throw error;
      }

      logger.debug(`Attempt ${attempt + 1} failed, retrying in ${delay}ms...`);
      await new Promise(resolve => setTimeout(resolve, delay));
      delay = Math.min(delay * factor, maxDelay);
    }
  }

  throw lastError;
}

// Usage
const result = await withRetry(
  () => searchBooks(params),
  {
    shouldRetry: (error) => error.code === 'NETWORK_ERROR'
  }
);
```

### Pattern: Circuit Breaker

```typescript
class CircuitBreaker {
  private failureCount = 0;
  private lastFailureTime: number | null = null;
  private state: 'CLOSED' | 'OPEN' | 'HALF_OPEN' = 'CLOSED';

  constructor(
    private threshold: number = 5,
    private timeout: number = 60000
  ) {}

  async execute<T>(operation: () => Promise<T>): Promise<T> {
    if (this.state === 'OPEN') {
      if (Date.now() - this.lastFailureTime! > this.timeout) {
        this.state = 'HALF_OPEN';
      } else {
        throw new Error('Circuit breaker is OPEN');
      }
    }

    try {
      const result = await operation();
      this.onSuccess();
      return result;
    } catch (error) {
      this.onFailure();
      throw error;
    }
  }

  private onSuccess() {
    this.failureCount = 0;
    this.state = 'CLOSED';
  }

  private onFailure() {
    this.failureCount++;
    this.lastFailureTime = Date.now();

    if (this.failureCount >= this.threshold) {
      this.state = 'OPEN';
      logger.warn('Circuit breaker opened due to repeated failures');
    }
  }
}
```

### Pattern: Error Context Enrichment

```typescript
class ZLibraryError extends Error {
  constructor(
    message: string,
    public code: string,
    public context?: any,
    public retryable: boolean = true
  ) {
    super(message);
    this.name = 'ZLibraryError';
  }

  static fromError(error: any, context?: any): ZLibraryError {
    if (error instanceof ZLibraryError) {
      return error;
    }

    const message = error.message || String(error);
    const code = error.code || 'UNKNOWN_ERROR';

    return new ZLibraryError(message, code, {
      ...context,
      originalError: error,
      stack: error.stack,
      timestamp: new Date().toISOString()
    });
  }
}

// Usage
try {
  await downloadBook(bookId);
} catch (error) {
  throw ZLibraryError.fromError(error, {
    bookId,
    operation: 'download',
    user: userId
  });
}
```

## Logging Patterns

### Pattern: Structured Logging

```typescript
// Example for a module directly under src/.
import { logger } from './lib/logger.js';

// The shared logger exposes error, warn, info, debug and isEnabled.
// LOG_LEVEL controls verbosity; no file or stdout transport is configured.
logger.info('Search started', { requestId: context.requestId });
logger.debug('Search arguments', { query: params.query, filters: params.filters });
```

At debug level, search arguments can appear in client-captured stderr. Follow
`SECURITY.md`: avoid debug logs for sensitive queries and scrub logs before sharing.

### Pattern: Performance Logging

```typescript
function logPerformance(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
  const originalMethod = descriptor.value;

  descriptor.value = async function (...args: any[]) {
    const start = Date.now();
    const requestId = crypto.randomUUID();

    logger.info(`${propertyKey} started`, {
      requestId,
      argumentCount: args.length
    });

    try {
      const result = await originalMethod.apply(this, args);
      const duration = Date.now() - start;

      logger.info(`${propertyKey} completed`, {
        requestId,
        duration,
        success: true
      });

      return result;
    } catch (error) {
      const duration = Date.now() - start;

      logger.error(`${propertyKey} failed`, {
        requestId,
        duration,
        error: error.message,
        stack: error.stack
      });

      throw error;
    }
  };

  return descriptor;
}

// Usage
class BookService {
  @logPerformance
  async searchBooks(params: SearchParams) {
    // Implementation
  }
}
```

## Python Bridge Patterns

### Pattern: Type-Safe Bridge Communication

```typescript
interface PythonRequest<T = any> {
  function: string;
  args: T;
  timeout?: number;
}

interface PythonResponse<T = any> {
  success: boolean;
  data?: T;
  error?: {
    message: string;
    code: string;
    traceback?: string;
  };
}

async function callPython<TArgs, TResult>(
  request: PythonRequest<TArgs>
): Promise<TResult> {
  const response = await pythonBridge.call(request) as PythonResponse<TResult>;

  if (!response.success) {
    throw new ZLibraryError(
      response.error?.message || 'Python call failed',
      response.error?.code || 'PYTHON_ERROR',
      { traceback: response.error?.traceback }
    );
  }

  return response.data!;
}

// Usage with type safety
interface SearchArgs {
  query: string;
  limit: number;
}

interface SearchResult {
  books: Book[];
  total: number;
}

const result = await callPython<SearchArgs, SearchResult>({
  function: 'search_books',
  args: { query: 'python', limit: 50 }
});
```

### Pattern: Python Exception Mapping

```python
# Python side
class ZLibraryException(Exception):
    def __init__(self, message, code, context=None):
        super().__init__(message)
        self.code = code
        self.context = context or {}

def safe_execute(func):
    """Decorator for safe Python function execution"""
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            return {
                'success': True,
                'data': result
            }
        except ZLibraryException as e:
            return {
                'success': False,
                'error': {
                    'message': str(e),
                    'code': e.code,
                    'context': e.context,
                    'traceback': traceback.format_exc()
                }
            }
        except Exception as e:
            return {
                'success': False,
                'error': {
                    'message': str(e),
                    'code': 'UNKNOWN_ERROR',
                    'traceback': traceback.format_exc()
                }
            }
    return wrapper

@safe_execute
def search_books(query, limit=50):
    # Implementation that might raise exceptions
    pass
```

## Caching Patterns

### Pattern: Multi-Level Cache

```typescript
interface CacheOptions {
  ttl?: number;
  key?: string;
}

class MultiLevelCache {
  private memory = new Map<string, { value: any; expires: number }>();
  private redis?: Redis;

  async get<T>(key: string): Promise<T | null> {
    // L1: Memory cache
    const memoryValue = this.memory.get(key);
    if (memoryValue && memoryValue.expires > Date.now()) {
      return memoryValue.value;
    }

    // L2: Redis cache (if available)
    if (this.redis) {
      const redisValue = await this.redis.get(key);
      if (redisValue) {
        const value = JSON.parse(redisValue);
        // Populate L1
        this.memory.set(key, {
          value,
          expires: Date.now() + 60000 // 1 minute in L1
        });
        return value;
      }
    }

    return null;
  }

  async set<T>(key: string, value: T, ttl: number = 3600000): Promise<void> {
    // Set in L1
    this.memory.set(key, {
      value,
      expires: Date.now() + Math.min(ttl, 60000) // Max 1 minute in L1
    });

    // Set in L2
    if (this.redis) {
      await this.redis.setex(key, Math.floor(ttl / 1000), JSON.stringify(value));
    }
  }
}

// Cache decorator
function cached(options: CacheOptions = {}) {
  return function(target: any, propertyKey: string, descriptor: PropertyDescriptor) {
    const originalMethod = descriptor.value;
    const cache = new MultiLevelCache();

    descriptor.value = async function(...args: any[]) {
      const key = options.key || `${propertyKey}:${JSON.stringify(args)}`;

      const cachedValue = await cache.get(key);
      if (cachedValue !== null) {
        logger.debug('Cache hit', { key });
        return cachedValue;
      }

      const result = await originalMethod.apply(this, args);
      await cache.set(key, result, options.ttl);

      return result;
    };

    return descriptor;
  };
}
```

## Domain Handling Patterns

### Pattern: Dynamic Domain Discovery

```typescript
class DomainManager {
  private domains: string[] = [];
  private currentIndex = 0;
  private lastRefresh = 0;
  private refreshInterval = 3600000; // 1 hour

  async getCurrentDomain(): Promise<string> {
    await this.ensureDomains();
    return this.domains[this.currentIndex];
  }

  async rotateDomain(): Promise<string> {
    await this.ensureDomains();
    this.currentIndex = (this.currentIndex + 1) % this.domains.length;
    return this.domains[this.currentIndex];
  }

  private async ensureDomains(): Promise<void> {
    if (this.domains.length === 0 ||
        Date.now() - this.lastRefresh > this.refreshInterval) {
      await this.refreshDomains();
    }
  }

  private async refreshDomains(): Promise<void> {
    try {
      // Try multiple methods to get domains
      const domains = await Promise.any([
        this.getDomainsFromEmail(),
        this.getDomainsFromKnownList(),
        this.getDomainsFromConfig()
      ]);

      this.domains = domains.filter(d => d && d.length > 0);
      this.lastRefresh = Date.now();

      logger.info('Domains refreshed', {
        count: this.domains.length
      });
    } catch (error) {
      logger.error('Failed to refresh domains', error);
      // Keep existing domains if refresh fails
      if (this.domains.length === 0) {
        throw new Error('No domains available');
      }
    }
  }

  private async getDomainsFromEmail(): Promise<string[]> {
    // Implementation to get domains via email
    // Send email to blackbox@zbox.ph
    throw new Error('Not implemented');
  }

  private async getDomainsFromKnownList(): Promise<string[]> {
    return [
      'singlelogin.me',
      '1lib.fr',
      'z-lib.org'
    ];
  }

  private async getDomainsFromConfig(): Promise<string[]> {
    if (process.env.ZLIBRARY_MIRROR) {
      return [process.env.ZLIBRARY_MIRROR];
    }
    throw new Error('No mirror configured');
  }
}
```

## Testing Patterns

### Pattern: Test Data Builders

```typescript
class BookBuilder {
  private book: Partial<Book> = {
    id: '12345',
    title: 'Test Book',
    author: 'Test Author',
    year: 2024,
    language: 'English',
    extension: 'pdf',
    size: 1024000,
    hash: 'abc123'
  };

  withTitle(title: string): this {
    this.book.title = title;
    return this;
  }

  withAuthor(author: string): this {
    this.book.author = author;
    return this;
  }

  withBookDetails(details: Partial<BookDetails>): this {
    this.book.bookDetails = {
      downloadUrl: 'https://example.com/download',
      mirrorUrl: 'https://mirror.com/download',
      ...details
    };
    return this;
  }

  build(): Book {
    return this.book as Book;
  }

  buildMany(count: number): Book[] {
    return Array.from({ length: count }, (_, i) => ({
      ...this.build(),
      id: `${this.book.id}_${i}`,
      title: `${this.book.title} ${i + 1}`
    }));
  }
}

// Usage in tests
describe('BookService', () => {
  it('should search books', async () => {
    const expectedBooks = new BookBuilder()
      .withTitle('Python Programming')
      .buildMany(10);

    // Mock implementation
    mockPythonBridge.searchBooks.mockResolvedValue(expectedBooks);

    const result = await bookService.search('Python');
    expect(result).toEqual(expectedBooks);
  });
});
```

### Pattern: Integration Test Helpers

```typescript
class TestHelper {
  private static instance: TestHelper;
  private mockServer: MockServer;
  private testDb: TestDatabase;

  static async setup(): Promise<TestHelper> {
    if (!this.instance) {
      this.instance = new TestHelper();
      await this.instance.initialize();
    }
    return this.instance;
  }

  private async initialize() {
    // Set up mock Z-Library server
    this.mockServer = await MockServer.create({
      port: 8080,
      responses: {
        '/eapi/book/search': (req) => ({
          books: new BookBuilder().buildMany(10)
        })
      }
    });

    // Set up test database
    this.testDb = await TestDatabase.create();

    // Set up test environment variables
    process.env.ZLIBRARY_MIRROR = 'http://localhost:8080';
  }

  async cleanup() {
    await this.mockServer.stop();
    await this.testDb.clear();
  }

  createAuthenticatedClient(): ZLibraryClient {
    return new ZLibraryClient({
      email: 'test@example.com',
      password: 'test123',
      domain: 'http://localhost:8080'
    });
  }
}

// Usage
describe('Integration Tests', () => {
  let helper: TestHelper;

  beforeAll(async () => {
    helper = await TestHelper.setup();
  });

  afterAll(async () => {
    await helper.cleanup();
  });

  it('should perform end-to-end search', async () => {
    const client = helper.createAuthenticatedClient();
    const results = await client.search('test');
    expect(results).toHaveLength(10);
  });
});
```

## Performance Patterns

### Pattern: Request Batching

```typescript
class BatchProcessor<T, R> {
  private queue: Array<{
    item: T;
    resolve: (result: R) => void;
    reject: (error: any) => void;
  }> = [];
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private processBatch: (items: T[]) => Promise<R[]>,
    private maxBatchSize: number = 10,
    private maxWaitTime: number = 100
  ) {}

  async process(item: T): Promise<R> {
    return new Promise((resolve, reject) => {
      this.queue.push({ item, resolve, reject });

      if (this.queue.length >= this.maxBatchSize) {
        this.flush();
      } else if (!this.timer) {
        this.timer = setTimeout(() => this.flush(), this.maxWaitTime);
      }
    });
  }

  private async flush() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }

    const batch = this.queue.splice(0, this.maxBatchSize);
    if (batch.length === 0) return;

    try {
      const items = batch.map(b => b.item);
      const results = await this.processBatch(items);

      batch.forEach((b, i) => b.resolve(results[i]));
    } catch (error) {
      batch.forEach(b => b.reject(error));
    }
  }
}

// Usage
const searchBatcher = new BatchProcessor<string, Book[]>(
  async (queries) => {
    // Process multiple searches in one request
    return await pythonBridge.batchSearch(queries);
  }
);

const results = await searchBatcher.process('python programming');
```

## Security Patterns

### Pattern: Input Sanitization

```typescript
class InputSanitizer {
  static sanitizeSearchQuery(query: string): string {
    return query
      .trim()
      .replace(/[<>]/g, '') // Remove potential HTML
      .replace(/[';]/g, '') // Remove potential SQL injection
      .substring(0, 200); // Limit length
  }

  static sanitizeBookId(id: string): string {
    // Allow only alphanumeric and common separators
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
      throw new Error('Invalid book ID format');
    }
    return id;
  }

  static sanitizePath(path: string): string {
    // Prevent directory traversal
    const normalized = path.normalize();
    if (normalized.includes('..')) {
      throw new Error('Path traversal detected');
    }
    return normalized;
  }
}
```

---

_These patterns should be followed consistently throughout the codebase. Update this document when introducing new patterns._
