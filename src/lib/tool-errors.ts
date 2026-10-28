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
export function errorEnvelope(
  error: unknown,
  operation: string,
  source?: string,
  env: Record<string, string | undefined> = process.env,
) {
  const e = error instanceof ToolFailure ? error : classifyFailure(error, operation, source, env);
  const info = e.info;
  const detail = { ...info.detail };
  if ('diagnostics' in detail) {
    const diagnostics = readAuthDiagnostics(detail.diagnostics);
    if (diagnostics) detail.diagnostics = diagnostics;
    else delete detail.diagnostics;
  }
  const attempted = info.source ?? source;
  const sourceFailures = [
    'provider_unreachable',
    'provider_timeout',
    'provider_error',
    'outcome_unknown',
  ];
  const sourceFailed =
    sourceFailures.includes(e.code) ||
    (['all_sources_failed', 'partial_failure'].includes(e.code) &&
      info.attempts?.some(
        (attempt: any) => sourceFailures.includes(attempt.code) || attempt.code === 'internal',
      ));
  const validity =
    e.code === 'outcome_unknown'
      ? 'The request was valid; its outcome is unknown. '
      : sourceFailed
        ? 'The request was valid; the source failed. '
        : e.code === 'internal'
          ? 'The request was valid; the helper failed. '
          : '';
  // Do not expose upstream exception text, URLs, bearer values, or stacks.
  // Keep the full message budget; context belongs outside the bounded message.
  // A cut through a surrogate pair must omit the incomplete character.
  const message = e.message.slice(0, 199).replace(/[\uD800-\uDBFF]$/u, '');
  const recovery =
    info.recovery ??
    (e.code === 'outcome_unknown' && attempted === 'libgen'
      ? 'Call download again with the same ref if still wanted; disk receipts are checked first and LibGen transfers are quota-free.'
      : recoveries[e.code]) ??
    'Try another source or candidate; report persistent provider drift.';
  return ErrorEnvelopeSchema.parse({
    error: {
      code: e.code,
      ...(attempted ? { source: attempted } : {}),
      operation,
      message,
      retry: info.retry ?? 'no',
      ...(Number.isFinite(info.retry_after_s) ? { retry_after_s: info.retry_after_s } : {}),
      recovery: `${validity}${attempted ? `${attempted}: ` : ''}${recovery}`,
      attempts: info.attempts ?? [],
      detail,
    },
  });
}
const nonemptyStatus = (d: { reason: string; http_status?: number }) =>
  d.http_status === undefined ? { reason: d.reason } : d;
export function classifyFailure(
  error: any,
  operation: string,
  source?: string,
  env: Record<string, string | undefined> = process.env,
): ToolFailure {
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
    'browser_check',
    'protocol_error',
    'partial_transfer',
    'not_found',
  ];
  const browserEnabled = ['1', 'true', 'yes'].includes(
    (env.ANNAS_BROWSER_ENABLED ?? '').trim().toLowerCase(),
  );
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
  else if (reason === 'configuration_error') code = 'credentials_missing';
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
  else if (['http_error', 'browser_check', 'protocol_error', 'partial_transfer'].includes(reason))
    code = 'provider_error';
  else if (error?.code === 'CIRCUIT_BREAKER_OPEN') code = 'breaker_open';
  else if (['ValueError', 'TypeError'].includes(context.pythonErrorType)) code = 'invalid_argument';
  else if (detail.failures?.length) {
    const children = detail.failures.map((f: any) =>
      classifyFailure({ context: { details: f } }, operation, source, env),
    );
    const child =
      children.find((f: ToolFailure) => f.code === 'integrity_mismatch') ??
      children[children.length - 1];
    return new ToolFailure(child.code, `${operation} did not complete.`, {
      ...child.info,
      detail: nonemptyStatus({
        reason: child.info.detail.reason,
        http_status: child.info.detail.http_status,
      }),
      attempts: children.map((f: ToolFailure, i: number) => ({
        source:
          detail.failures[i].provider === 'annas_archive' ? 'annas' : detail.failures[i].provider,
        outcome: 'failed',
        code: f.code,
        reason: f.info.detail.reason,
        ...(f.info.detail.http_status ? { http_status: f.info.detail.http_status } : {}),
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
    occupied
      ? 'Destination already holds a file with no matching receipt.'
      : `${operation} did not complete (${reason === 'unknown' ? code : reason}).`,
    {
      source,
      retry,
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
      ...(reason === 'browser_check'
        ? {
            recovery: browserEnabled
              ? "Anna's Archive's visible browser could not clear the check, or its pacing/backoff is still active. A person may need to clear the check on the host; wait for any backoff before retrying."
              : "Anna's Archive is behind a DDoS-Guard browser check; this connector does not pass it. Use another source.",
          }
        : {}),
      detail: {
        reason,
        ...(reason === 'browser_check' && Number.isInteger(detail.status_code)
          ? { http_status: detail.status_code }
          : {}),
        ...(breakerError ? { dependency: breakerError.dependency } : {}),
        ...(diagnostics ? { diagnostics } : {}),
        ...(typeof detail.request_issued === 'boolean'
          ? { request_issued: detail.request_issued }
          : {}),
      },
    },
  );
}
