import { createHash, randomBytes } from 'node:crypto';
import { ToolFailure } from './tool-errors.js';

type Kind = 'r' | 'c' | 'j';
/** Process-local LRU. The monotonic high-water mark is independent of tombstones. */
export class RefStore {
  readonly nonce: string;
  counter: bigint;
  private highWater: bigint;
  private values = new Map<string, unknown>();
  private stable = new Map<string, string>();
  private stableKeys = new Map<string, string>();
  private expired = new Set<string>();
  private capacity: number;
  private tombstones: number;
  constructor(
    options: { nonce?: bigint; counter?: bigint; capacity?: number; tombstones?: number } = {},
  ) {
    const nonce = options.nonce ?? randomBytes(8).readBigUInt64BE();
    if (nonce < 0n || nonce > 0xffffffffffffffffn) throw new Error('nonce must be 64 bits');
    this.nonce = nonce.toString(36).padStart(13, '0');
    this.counter = options.counter ?? 0n;
    this.highWater = this.counter;
    this.capacity = options.capacity ?? 5000;
    this.tombstones = options.tombstones ?? 5000;
  }
  mint<T>(kind: Kind, value: T): string {
    if (this.counter !== this.highWater) throw new Error('Ref counter rewind or reissue refused');
    this.counter += 1n;
    this.highWater = this.counter;
    const ref = kind + this.nonce + this.counter.toString(36);
    this.values.set(ref, value);
    if (this.values.size > this.capacity) {
      const oldest = this.values.keys().next().value!;
      this.values.delete(oldest);
      const key = this.stableKeys.get(oldest);
      if (key) this.stable.delete(key);
      this.stableKeys.delete(oldest);
      this.expired.add(oldest);
      if (this.expired.size > this.tombstones)
        this.expired.delete(this.expired.values().next().value!);
    }
    return ref;
  }
  /** Replaying an unchanged page preserves refs and next_cursor while they are live. */
  mintStable<T>(kind: Kind, value: T): string {
    const canonical = (input: any): any =>
      Array.isArray(input)
        ? input.map(canonical)
        : input && typeof input === 'object'
          ? Object.fromEntries(
              Object.keys(input)
                .sort()
                .map((key) => [key, canonical(input[key])]),
            )
          : input;
    const key =
      kind +
      createHash('sha256')
        .update(JSON.stringify(canonical(value)))
        .digest('hex');
    const existing = this.stable.get(key);
    if (existing && this.values.has(existing)) {
      this.get(existing, kind);
      return existing;
    }
    const ref = this.mint(kind, value);
    this.stable.set(key, ref);
    this.stableKeys.set(ref, key);
    return ref;
  }
  /** Retire an alias when its owning retained search is evicted. */
  discard(ref: string): void {
    if (!this.values.delete(ref)) return;
    const key = this.stableKeys.get(ref);
    if (key) this.stable.delete(key);
    this.stableKeys.delete(ref);
    this.expired.add(ref);
    if (this.expired.size > this.tombstones)
      this.expired.delete(this.expired.values().next().value!);
  }
  get<T = any>(ref: string, kind: Kind): T {
    if (!ref || ref[0] !== kind)
      throw new ToolFailure('invalid_argument', 'Reference has the wrong kind.');
    const info = { detail: { kind: kind === 'c' ? 'cursor' : 'row', reason: 'unknown' } };
    if (/^[rcj][a-z0-9]{14,}$/.test(ref) && ref.slice(1, 14) !== this.nonce)
      info.detail.reason = 'other_process';
    else if (this.expired.has(ref)) info.detail.reason = 'expired';
    else if (this.values.has(ref)) {
      const value = this.values.get(ref);
      this.values.delete(ref);
      this.values.set(ref, value);
      return value as T;
    }
    throw new ToolFailure(
      'ref_unavailable',
      `Reference unavailable (${info.detail.reason}).`,
      info,
    );
  }
}
