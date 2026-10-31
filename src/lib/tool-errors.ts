import { z } from 'zod';
import { CircuitBreakerOpenError } from './circuit-breaker.js';
import { readAuthDiagnostics } from './auth-diagnostics.js';

export const ERROR_CODES = [
  'invalid_argument',
  'sources_required',
  'ref_unavailable',
  'unsupported_operation',
  'credentials_missing',
  'auth_failed',
  'quota_exhausted',
  'not_found',
  'provider_unreachable',
  'provider_timeout',
  'provider_error',
  'integrity_mismatch',
  'breaker_open',
  'all_sources_failed',
  'partial_failure',
  'output_refused',
  'cancelled',
  'outcome_unknown',
  'internal',
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];
export type Retry = 'safe' | 'may_spend_quota' | 'no';
export const ErrorEnvelopeSchema = z
  .object({
    error: z
      .object({
        code: z.enum(ERROR_CODES),
        source: z.enum(['libgen', 'annas', 'zlibrary']).optional(),
        operation: z.string(),
        message: z.string().max(199),
        retry: z.enum(['safe', 'may_spend_quota', 'no']),
        retry_after_s: z.number().nonnegative().optional(),
        recovery: z.string(),
        attempts: z.array(z.record(z.string(), z.unknown())),
        detail: z.record(z.string(), z.unknown()),
      })
      .strict(),
  })
  .strict();

export class ToolFailure extends Error {
  constructor(
    public code: ErrorCode,
    message: string,
    public info: Record<string, any> = {},
  ) {
    super(message);
  }
}
const recoveries: Partial<Record<ErrorCode, string>> = {
  invalid_argument: 'Correct the arguments; see the 2.0 migration guide.',
  sources_required: 'Choose sources from detail.available, in the order you want them tried.',
  ref_unavailable: 'Search again and choose the edition again; use its new ref.',
  credentials_missing: 'Use get_limits; configure the named source or choose another source.',
  auth_failed: 'Check the configured account or API key.',
  quota_exhausted: 'Choose another source or wait for the daily quota to reset.',
  output_refused: 'Omit output_dir to use the configured output root.',
  cancelled: 'Call again if you still want it; the receipt check runs first.',
  outcome_unknown:
    'Call download again with the same ref if still wanted; disk receipts are checked first, but a new transfer may spend quota.',
};
export function errorEnvelope(error: unknown, operation: string, source?: string) {
  const e = error instanceof ToolFailure ? error : classifyFailure(error, operation, source);
  const info = e.info;
  const detail = { ...info.detail };
  if ('diagnostics' in detail) {
    const diagnostics = readAuthDiagnostics(detail.diagnostics);
    if (diagnostics) detail.diagnostics = diagnostics;
    else delete detail.diagnostics;
  }
  const attempted = info.source ?? source;
  // Do not expose upstream exception text, URLs, bearer values, or stacks.
  const message = `${attempted ? `${attempted}: ` : ''}${e.message}`.slice(0, 199);
  return ErrorEnvelopeSchema.parse({
    error: {
      code: e.code,
      ...(attempted ? { source: attempted } : {}),
      operation,
      message,
      retry: info.retry ?? 'no',
      ...(Number.isFinite(info.retry_after_s) ? { retry_after_s: info.retry_after_s } : {}),
      recovery:
        info.recovery ??
        (e.code === 'outcome_unknown' && attempted === 'libgen'
          ? 'Call download again with the same ref if still wanted; disk receipts are checked first and LibGen transfers are quota-free.'
          : recoveries[e.code]) ??
        'Try another source or candidate; report persistent provider drift.',
      attempts: info.attempts ?? [],
      detail,
    },
  });
}
export function classifyFailure(error: any, operation: string, source?: string): ToolFailure {
  if (error instanceof ToolFailure) return error;
  const context = error?.context ?? {};
  const breakerError =
    error instanceof CircuitBreakerOpenError
      ? error
      : context.originalError instanceof CircuitBreakerOpenError
        ? context.originalError
        : undefined;
  const diagnostics = readAuthDiagnostics(context.diagnostics);
  const detail = context.details ?? {};
  const candidate = detail.reason ?? context.reason ?? 'unknown';
  const allowedReasons = [
    'aborted',
    'timeout',
    'auth_rejected',
    'auth_backoff',
    'login_failed',
    'configuration_error',
    'quota_exhausted',
    'invalid_argument',
    'output_refused',
    'internal',
    'breaker_open',
    'dns_failure',
    'dns_timeout',
    'connect_timeout',
    'connect_refused',
    'connect_error',
    'tls_error',
    'read_timeout',
    'search_timeout',
    'walk_budget_exhausted',
    'integrity_mismatch',
    'http_error',
    'protocol_error',
    'partial_transfer',
    'not_found',
  ];
  let reason = allowedReasons.includes(candidate) ? candidate : 'unknown';
  // The bridge emits this exact allowlisted token for certificate/SSL failures.
  // Never infer a public TLS cause from arbitrary upstream exception prose.
  if (context.loginToken === 'TLS certificate verification failed') reason = 'tls_error';
  else if (
    [
      'ConnectError',
      'ConnectTimeout',
      'ReadTimeout',
      'NonJSONResponseError',
      'DiamWallError',
      'HTTPStatusError',
    ].includes(context.pythonErrorType)
  )
    reason = (
      {
        ConnectError: 'connect_error',
        ConnectTimeout: 'connect_timeout',
        ReadTimeout: 'read_timeout',
        NonJSONResponseError: 'protocol_error',
        DiamWallError: 'protocol_error',
        HTTPStatusError: 'http_error',
      } as Record<string, string>
    )[context.pythonErrorType];
  // Fill only an unknown cause: explicit timeout/abort/acquisition decisions
  // keep their existing codes and download issuance semantics.
  if (reason === 'unknown' && diagnostics?.failure_kind === 'transport')
    reason =
      (
        {
          WriteTimeout: 'read_timeout',
          PoolTimeout: 'connect_timeout',
          RemoteProtocolError: 'protocol_error',
        } as Record<string, string>
      )[context.pythonErrorType] ?? 'connect_error';
  if (diagnostics && ['login_failed', 'auth_rejected'].includes(reason))
    reason =
      diagnostics.failure_kind === 'auth_rejected'
        ? 'auth_rejected'
        : diagnostics.failure_kind === 'transport'
          ? 'connect_error'
          : diagnostics.http_status === undefined
            ? 'protocol_error'
            : 'http_error';
  const before = detail.request_issued === false;
  const issuedBudget =
    operation === 'download' &&
    reason === 'walk_budget_exhausted' &&
    detail.request_issued === true;
  const sourceConfiguration =
    reason === 'configuration_error' && context.pythonErrorType === 'SourceConfigurationError';
  const deadlineVariable = ['PYTHON_BRIDGE_TIMEOUT', 'PYTHON_BRIDGE_LONG_TIMEOUT'].includes(
    detail.variable,
  )
    ? detail.variable
    : operation === 'download'
      ? 'PYTHON_BRIDGE_LONG_TIMEOUT'
      : 'PYTHON_BRIDGE_TIMEOUT';
  let code: ErrorCode = 'internal';
  if (reason === 'aborted') code = 'cancelled';
  else if (reason === 'auth_backoff') code = 'breaker_open';
  else if (reason === 'not_found') code = 'not_found';
  else if (issuedBudget) code = 'outcome_unknown';
  else if (reason === 'timeout' || error?.code === 'BRIDGE_TIMEOUT')
    code = operation === 'download' ? 'outcome_unknown' : 'provider_timeout';
  else if (operation === 'download' && error?.code === 'BRIDGE_KILLED') code = 'outcome_unknown';
  else if (
    ['auth_rejected', 'login_failed'].includes(reason) ||
    context.pythonErrorType === 'EAPIAuthenticationError'
  )
    code = 'auth_failed';
  else if (['invalid_argument', 'output_refused', 'internal', 'breaker_open'].includes(reason))
    code = reason as ErrorCode;
  else if (reason === 'configuration_error')
    code = sourceConfiguration ? 'invalid_argument' : 'credentials_missing';
  else if (reason === 'quota_exhausted') code = 'quota_exhausted';
  else if (
    [
      'dns_failure',
      'dns_timeout',
      'connect_timeout',
      'connect_refused',
      'connect_error',
      'tls_error',
    ].includes(reason)
  )
    code = 'provider_unreachable';
  else if (['read_timeout', 'search_timeout', 'walk_budget_exhausted'].includes(reason))
    code = 'provider_timeout';
  else if (reason === 'integrity_mismatch') code = 'integrity_mismatch';
  else if (['http_error', 'protocol_error', 'partial_transfer'].includes(reason))
    code = 'provider_error';
  else if (error?.code === 'CIRCUIT_BREAKER_OPEN') code = 'breaker_open';
  else if (['ValueError', 'TypeError'].includes(context.pythonErrorType)) code = 'invalid_argument';
  else if (detail.failures?.length) {
    const children = detail.failures.map((f: any) =>
      classifyFailure({ context: { details: f } }, operation, source),
    );
    const child =
      children.find((f: ToolFailure) => f.code === 'integrity_mismatch') ??
      children[children.length - 1];
    return new ToolFailure(child.code, `${operation} did not complete.`, {
      ...child.info,
      detail: { reason: child.info.detail.reason },
      attempts: children.map((f: ToolFailure, i: number) => ({
        source:
          detail.failures[i].provider === 'annas_archive' ? 'annas' : detail.failures[i].provider,
        outcome: 'failed',
        code: f.code,
        reason: f.info.detail.reason,
      })),
    });
  }
  const retry: Retry = issuedBudget
    ? 'no'
    : [
          'provider_unreachable',
          'provider_timeout',
          'provider_error',
          'integrity_mismatch',
          'breaker_open',
          'cancelled',
          'outcome_unknown',
        ].includes(code)
      ? operation === 'download' && !before && source !== 'libgen'
        ? 'may_spend_quota'
        : 'safe'
      : 'no';
  const occupied = code === 'output_refused' && detail.destination_occupied === true;
  return new ToolFailure(
    code,
    sourceConfiguration
      ? 'Source deadline configuration error; see docs/RETRY_CONFIGURATION.md.'
      : occupied
        ? 'Destination already holds a file with no matching receipt.'
        : `${operation} did not complete (${reason === 'unknown' ? code : reason}).`,
    {
      source,
      retry,
      ...(sourceConfiguration
        ? {
            recovery: `Configure ${deadlineVariable} to at least 10000 ms; see docs/RETRY_CONFIGURATION.md.`,
          }
        : {}),
      ...(issuedBudget
        ? {
            recovery:
              'The transfer may have completed. Check the destination and acquisition receipt before deciding whether to retry.',
          }
        : {}),
      ...(occupied
        ? {
            recovery:
              'The destination already holds a file with no matching receipt; move or rename it or choose another output_dir.' +
              (detail.request_issued === true && source !== 'libgen'
                ? ' This attempt may already have used a download.'
                : ''),
          }
        : {}),
      ...(Number.isFinite(context.retryAfterMs)
        ? { retry_after_s: Math.ceil(context.retryAfterMs / 1000) }
        : {}),
      detail: {
        reason,
        ...(breakerError ? { dependency: breakerError.dependency } : {}),
        ...(diagnostics ? { diagnostics } : {}),
        ...(typeof detail.request_issued === 'boolean'
          ? { request_issued: detail.request_issued }
          : {}),
      },
    },
  );
}
