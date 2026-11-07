export interface ZlibraryAccount {
  readonly slot: number;
  readonly email: string;
  readonly password: string;
}

export interface ZlibraryAccountConfig {
  readonly accounts: readonly ZlibraryAccount[];
  /** Slots with missing, duplicate, or unsupported credentials; never includes credentials. */
  readonly invalidSlots: readonly number[];
}

const MAX_ACCOUNT_SLOTS = 16;

/**
 * Read the legacy primary account and optional numbered accounts from process env.
 * A partial account is ignored so other providers remain available, matching the
 * server's existing credential-optional startup behavior.
 */
export function inspectZlibraryAccounts(
  env: Record<string, string | undefined> = process.env,
): ZlibraryAccountConfig {
  const candidates = new Map<number, { email?: string; password?: string }>();
  const invalidSlots = new Set<number>();
  const primaryEmail = env.ZLIBRARY_EMAIL?.trim();
  const primaryPassword = env.ZLIBRARY_PASSWORD;
  if (primaryEmail || primaryPassword) {
    if (primaryEmail && primaryPassword) candidates.set(1, { email: primaryEmail, password: primaryPassword });
    else invalidSlots.add(1);
  }

  for (const [key, value] of Object.entries(env)) {
    const match = /^ZLIBRARY_(EMAIL|PASSWORD)_(\d+)$/i.exec(key);
    if (!match) continue;
    const slot = Number(match[2]);
    if (!Number.isSafeInteger(slot) || slot < 2 || slot > MAX_ACCOUNT_SLOTS) {
      if (Number.isSafeInteger(slot) && slot > 0) invalidSlots.add(slot);
      continue;
    }
    const row = candidates.get(slot) ?? {};
    if (match[1]!.toUpperCase() === 'EMAIL') row.email = value?.trim();
    else row.password = value;
    candidates.set(slot, row);
  }

  const accounts: ZlibraryAccount[] = [];
  const seenEmails = new Set<string>();
  for (const [slot, row] of [...candidates].sort(([a], [b]) => a - b)) {
    if (!row.email || !row.password) {
      invalidSlots.add(slot);
      continue;
    }
    const normalizedEmail = row.email.toLowerCase();
    if (seenEmails.has(normalizedEmail)) {
      invalidSlots.add(slot);
      continue;
    }
    seenEmails.add(normalizedEmail);
    accounts.push({ slot, email: row.email, password: row.password });
  }

  return {
    accounts,
    invalidSlots: [...invalidSlots].sort((a, b) => a - b),
  };
}

/** Round-robin selector; the cursor is process-local and starts at the first account. */
export class ZlibraryDownloadAccountSelector {
  private cursor = 0;
  private configurationKey = '';

  next(accounts: readonly ZlibraryAccount[]): ZlibraryAccount | undefined {
    if (accounts.length === 0) return undefined;
    const key = JSON.stringify(accounts.map(({ slot, email }) => [slot, email.toLowerCase()]));
    if (key !== this.configurationKey) {
      this.configurationKey = key;
      this.cursor = 0;
    }
    const selected = accounts[this.cursor % accounts.length];
    this.cursor = (this.cursor + 1) % accounts.length;
    return selected;
  }
}

