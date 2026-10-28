// E-only recovery allowance: preserve terminal/transport failures and task evidence.
import { dataOf, expectedError } from './oracles.js';
export function recoveredSourcesRequired(task, call, trace) {
  const error = dataOf(call.reply ?? {}).error;
  if (
    call.name !== 'search' ||
    call.error ||
    error?.code !== 'sources_required' ||
    (error.detail?.request_issued ?? false) !== false
  )
    return false;
  // E7 permits one discovery correction before provider attempts begin. Counting
  // it as a retry would confuse validation rejection with an issued search.
  if (task.id === 'E7' && trace.slice(0, trace.indexOf(call)).some((c) => c.name === 'search'))
    return false;
  return trace
    .slice(trace.indexOf(call) + 1)
    .some(
      (next) =>
        next.name === 'search' &&
        Array.isArray(next.args?.sources) &&
        next.args.sources.length > 0 &&
        !next.error &&
        ((!next.reply?.isError && !dataOf(next.reply ?? {}).error) || expectedError(task, next)),
    );
}
