import { createHash } from 'node:crypto';

export const PROTOCOL = 'probe_v1';
export const MAX_BYTES = 4_000_000;
export const OPERATIONS = [
  'browser.open_login', 'browser.confirm_login', 'browser.start_task', 'browser.navigate',
  'browser.read_page', 'browser.locate', 'browser.click', 'browser.scroll',
  'browser.close_session', 'device.status',
] as const;
export type Operation = typeof OPERATIONS[number];
export interface Pairing {
  protocol_version: 'probe_v1'; server: string; code: string; ca_pem: string | null; expires_at: string;
}
export interface Credentials {
  server: string; ca_pem: string | null; device_id: string; token: string; profile_id: string;
}
export interface Command {
  type: 'command'; id: string; connection_id: string; operation: Operation;
  expires_at: string; payload: Record<string, unknown>;
}
export interface Receipt {
  command_id: string; connection_id: string; status: 'succeeded' | 'failed' | 'unknown';
  result: Record<string, unknown>; error_code: string | null;
}
export class ProbeError extends Error {
  constructor(public code: string, message: string) { super(message); }
}
export function uuid(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value);
}
export function parsePairing(text: string, enteredServer?: string): Pairing {
  if (!text.startsWith('hrprobe1.') || text.length > 40_000) throw new ProbeError('pairing_invalid', '请粘贴网页生成的配对配置');
  let value: Pairing;
  try { value = JSON.parse(Buffer.from(text.slice(9), 'base64url').toString('utf8')); }
  catch { throw new ProbeError('pairing_invalid', '配对配置无法识别'); }
  const target = new URL(value.server);
  if (value.protocol_version !== PROTOCOL || target.protocol !== 'https:' || target.username || target.password ||
      target.pathname !== '/' || target.search || target.hash || typeof value.code !== 'string' || value.code.length < 20)
    throw new ProbeError('pairing_invalid', '配对配置需要正确的HTTPS平台地址');
  if (Date.parse(value.expires_at) <= Date.now() || !Number.isFinite(Date.parse(value.expires_at)))
    throw new ProbeError('pairing_expired', '配对配置已过期，请从网页重新获取');
  if (enteredServer && new URL(enteredServer).origin !== target.origin)
    throw new ProbeError('server_mismatch', '填写的平台地址与配对配置不同');
  if (value.ca_pem !== null && (typeof value.ca_pem !== 'string' || !/^-----BEGIN CERTIFICATE-----/m.test(value.ca_pem)))
    throw new ProbeError('certificate_invalid', '平台证书格式无效');
  return value;
}
export function validateCommand(value: unknown): Command {
  const c = value as Command;
  if (!c || c.type !== 'command' || !uuid(c.id) || !uuid(c.connection_id) ||
      !OPERATIONS.includes(c.operation) || !c.payload || Array.isArray(c.payload) ||
      Buffer.byteLength(JSON.stringify(c)) > MAX_BYTES || !Number.isFinite(Date.parse(c.expires_at)))
    throw new ProbeError('command_invalid', '平台浏览器命令无效');
  if (c.operation !== 'device.status' && (!uuid(c.payload.session_id) || !uuid(c.payload.profile_id)))
    throw new ProbeError('command_invalid', '浏览器命令缺少会话归属');
  if (c.payload.page_id !== undefined && !uuid(c.payload.page_id)) throw new ProbeError('command_invalid', '任务标签页无效');
  return c;
}
export function allowedUrl(url: string, roots: readonly string[]): boolean {
  try {
    const u = new URL(url), h = u.hostname.toLowerCase().replace(/\.$/, '');
    return u.protocol === 'https:' && (!u.port || u.port === '443') && !u.username && !u.password &&
      roots.some(r => h === r || h.endsWith('.' + r));
  } catch { return false; }
}
export function safeUrl(value: string): string {
  try {
    const u = new URL(value);
    if (!['https:', 'http:'].includes(u.protocol)) return u.protocol === 'about:' ? 'about:blank' : 'unknown';
    for (const key of [...u.searchParams.keys()]) if (/token|securityid|session|password|code|lid/i.test(key)) u.searchParams.delete(key);
    u.username = ''; u.password = ''; u.hash = ''; return u.toString();
  } catch { return 'unknown'; }
}
export function receiptDigest(r: Pick<Receipt, 'status' | 'result' | 'error_code'>): string {
  return createHash('sha256').update(JSON.stringify(r)).digest('hex');
}
