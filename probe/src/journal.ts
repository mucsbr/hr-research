import { existsSync, readFileSync, renameSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { Receipt } from './protocol';

export interface SecretCodec { encryptString(value: string): Buffer; decryptString(value: Buffer): string; }
export class PrivateStore<T> {
  constructor(private path: string, private codec: SecretCodec) {}
  read(): T | undefined {
    if (!existsSync(this.path)) return undefined;
    return JSON.parse(this.codec.decryptString(readFileSync(this.path)));
  }
  write(value: T): void {
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const temp = this.path + '.tmp';
    writeFileSync(temp, this.codec.encryptString(JSON.stringify(value)), { mode: 0o600 });
    renameSync(temp, this.path);
  }
}
interface Entry { state: 'running' | 'finished'; receipt?: Omit<Receipt, 'connection_id'>; updated: number; }
export class CommandJournal {
  private entries: Record<string, Entry>;
  constructor(private store: PrivateStore<Record<string, Entry>>) { this.entries = store.read() || {}; }
  begin(id: string): void { this.entries[id] = { state: 'running', updated: Date.now() }; this.save(); }
  finish(receipt: Receipt): void {
    const { connection_id: _connection, ...stable } = receipt;
    this.entries[receipt.command_id] = { state: 'finished', receipt: stable, updated: Date.now() }; this.save();
  }
  recover(id: string, connection: string): Receipt {
    const entry = this.entries[id];
    if (entry?.state === 'finished' && entry.receipt) return { ...entry.receipt, connection_id: connection };
    return { command_id: id, connection_id: connection, status: 'unknown', result: {}, error_code: 'execution_unknown' };
  }
  has(id: string): boolean { return id in this.entries; }
  private save(): void {
    // Bound storage, while retaining running/unknown IDs so actions are never blindly replayed.
    const cutoff = Date.now() - 7 * 86400_000;
    for (const [id, e] of Object.entries(this.entries)) if (e.state === 'finished' && e.updated < cutoff) delete this.entries[id];
    this.store.write(this.entries);
  }
}
