import { z } from 'zod';

// Closed values only: failure diagnostics must never carry provider prose/URLs.
export const ViaSchema = z.array(z.object({
  source: z.enum(['libgen', 'annas', 'zlibrary']),
  route: z.enum(['api', 'html', 'browser']),
  ms: z.number().nonnegative(),
  queued_ms: z.number().nonnegative().optional(),
})).max(3);
export type Via = z.infer<typeof ViaSchema>;
export function readVia(value: unknown): Via | undefined {
  const result = ViaSchema.safeParse(value);
  return result.success ? result.data : undefined;
}
