/** Closed diagnostics shared by the error envelope and logger; never upstream prose. */
export interface AuthDiagnostics {
  failure_kind: 'auth_rejected' | 'transport' | 'wall' | 'ambiguous_status';
  http_status?: number;
  attempt: number;
  refresh_count: number;
  request_id: string;
}
const kinds = ['auth_rejected', 'transport', 'wall', 'ambiguous_status'];
const keys = ['failure_kind', 'http_status', 'attempt', 'refresh_count', 'request_id'];
export function readAuthDiagnostics(value: unknown): AuthDiagnostics | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const v = value as Record<string, any>;
  if (
    Object.keys(v).some((key) => !keys.includes(key)) ||
    !kinds.includes(v.failure_kind) ||
    !Number.isInteger(v.attempt) ||
    v.attempt < 1 ||
    v.attempt > 65535 ||
    !Number.isInteger(v.refresh_count) ||
    v.refresh_count < 0 ||
    v.refresh_count > 65535 ||
    typeof v.request_id !== 'string' ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      v.request_id,
    ) ||
    (v.http_status !== undefined &&
      (!Number.isInteger(v.http_status) || v.http_status < 100 || v.http_status > 599))
  )
    return;
  const result: AuthDiagnostics = {
    failure_kind: v.failure_kind,
    ...(v.http_status === undefined ? {} : { http_status: v.http_status }),
    attempt: v.attempt,
    refresh_count: v.refresh_count,
    request_id: v.request_id,
  };
  if (Buffer.byteLength(JSON.stringify(result)) <= 512) return result;
}

/** Classify only typed/status evidence, not exception text or response bodies. */
export function authDiagnosticsFor(
  error: any,
  requestId: string,
  attempt: number,
  refresh: number,
): AuthDiagnostics {
  const context = error?.context ?? {};
  const prior = readAuthDiagnostics(context.diagnostics);
  const type = context.pythonErrorType;
  const status = context.httpStatus ?? context.loginStatus;
  return readAuthDiagnostics({
    failure_kind:
      prior?.failure_kind ??
      (context.reason === 'aborted'
        ? 'ambiguous_status'
        : context.reason === 'auth_rejected' || type === 'EAPIAuthenticationError'
          ? 'auth_rejected'
          : type === 'DiamWallError'
            ? 'wall'
            : [
                  'ReadTimeout',
                  'WriteTimeout',
                  'ConnectTimeout',
                  'ConnectError',
                  'PoolTimeout',
                  'RemoteProtocolError',
                ].includes(type) ||
                ['NETWORK_ERROR', 'TIMEOUT', 'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT'].includes(
                  error?.code,
                )
              ? 'transport'
              : 'ambiguous_status'),
    ...(prior?.http_status !== undefined
      ? { http_status: prior.http_status }
      : Number.isInteger(status) && status >= 100 && status <= 599
        ? { http_status: status }
        : {}),
    attempt: Math.min(65535, Math.max(1, attempt)),
    refresh_count: Math.min(65535, Math.max(0, refresh)),
    request_id: requestId,
  })!;
}

/** Closed projection of an EAPI bridge failure, including existing control metadata. */
export function safeEapiFailureEnvelope(value: any): Record<string, any> | undefined {
  const diagnostics = readAuthDiagnostics(value?.diagnostics);
  if (!diagnostics) {
    if (value?.type === 'SourceConfigurationError' && !('diagnostics' in value))
      return {
        error: 'Source deadline configuration error; see docs/RETRY_CONFIGURATION.md',
        type: value.type,
        details: {
          reason: 'configuration_error',
          request_issued: false,
          ...(['PYTHON_BRIDGE_TIMEOUT', 'PYTHON_BRIDGE_LONG_TIMEOUT'].includes(
            value.details?.variable,
          )
            ? { variable: value.details.variable }
            : {}),
        },
      };
    // main validates arguments before authentication. Preserve the typed local
    // verdict only when diagnostics are absent, never when a record was rejected.
    // Do not retain its argument-bearing exception message.
    if (['ValueError', 'TypeError'].includes(value?.type) && !('diagnostics' in value))
      return {
        error: 'Invalid bridge arguments',
        type: value.type,
        details: { reason: 'invalid_argument', request_issued: false },
      };
    // A lazy acquisition can refuse a local receipt/output before requesting EAPI.
    // Keep that original typed decision, but never its arbitrary message/path.
    const messages: Record<string, string> = {
      output_refused: 'Output refused; choose another selection or output path',
      invalid_argument: 'Invalid acquisition arguments',
      configuration_error: 'Missing source configuration',
      breaker_open: 'Acquisition is busy; try again later',
      integrity_mismatch: 'Acquisition integrity check failed',
      walk_budget_exhausted: 'Acquisition deadline exhausted; try again later',
    };
    const reason = value?.details?.reason;
    if (value?.type !== 'AcquisitionError' || typeof reason !== 'string' || !messages[reason])
      return;
    return {
      error:
        reason === 'walk_budget_exhausted' && value.details.request_issued !== false
          ? 'Acquisition deadline exhausted'
          : messages[reason],
      type: 'AcquisitionError',
      details: {
        reason,
        ...(typeof value.details.request_issued === 'boolean'
          ? { request_issued: value.details.request_issued }
          : {}),
        ...(typeof value.details.destination_occupied === 'boolean'
          ? { destination_occupied: value.details.destination_occupied }
          : {}),
      },
    };
  }
  const types = [
    'EAPIAuthenticationError',
    'HTTPStatusError',
    'DiamWallError',
    'NonJSONResponseError',
    'ReadTimeout',
    'WriteTimeout',
    'ConnectTimeout',
    'ConnectError',
    'PoolTimeout',
    'RemoteProtocolError',
    'TimeoutException',
    'ReadError',
    'WriteError',
    'ConnectionError',
    'TimeoutError',
    'RuntimeError',
    'ValueError',
    'TypeError',
    'AcquisitionError',
  ];
  const result: Record<string, any> = {
    error: `EAPI request failed (${diagnostics.failure_kind})`,
    type: types.includes(value.type) ? value.type : 'RuntimeError',
    diagnostics,
    ...(diagnostics.http_status === undefined ? {} : { status_code: diagnostics.http_status }),
  };
  if (value.login_failed)
    result.login_failed = {
      status_code: diagnostics.http_status,
      token:
        value.login_failed.token === 'TLS certificate verification failed'
          ? 'TLS certificate verification failed'
          : diagnostics.failure_kind === 'auth_rejected'
            ? 'Authentication rejected'
            : 'Upstream request failed',
    };
  if (Number.isSafeInteger(value.auth_rejected?.generation))
    result.auth_rejected = { generation: value.auth_rejected.generation };
  if (value.details && typeof value.details === 'object') {
    const detail = value.details;
    const reasons = [
      'auth_rejected',
      'outcome_unknown',
      'http_error',
      'read_timeout',
      'walk_budget_exhausted',
      'connect_timeout',
      'configuration_error',
      'output_refused',
      'invalid_argument',
      'breaker_open',
      'integrity_mismatch',
      'partial_transfer',
      'internal',
      'timeout',
      'aborted',
    ];
    result.details = {
      ...(reasons.includes(detail.reason) ? { reason: detail.reason } : {}),
      ...(typeof detail.request_issued === 'boolean'
        ? { request_issued: detail.request_issued }
        : {}),
      ...(typeof detail.destination_occupied === 'boolean'
        ? { destination_occupied: detail.destination_occupied }
        : {}),
    };
  }
  return result;
}
