import https from 'node:https';
import WebSocket from 'ws';
import { EventEmitter } from 'node:events';
import { CommandJournal } from './journal';
import { Credentials, MAX_BYTES, OPERATIONS, Pairing, PROTOCOL, ProbeError, Receipt, validateCommand } from './protocol';

export async function request<T>(config: Pick<Credentials, 'server' | 'ca_pem'> & { token?: string }, path: string,
                                  method = 'GET', body?: unknown, connection?: string): Promise<T> {
  const url = new URL('/api/v1/probes' + path, config.server);
  if (url.protocol !== 'https:') throw new ProbeError('tls_required', '探针仅连接可信HTTPS平台');
  const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = https.request(url, { method, ca: config.ca_pem || undefined, rejectUnauthorized: true,
      headers: { 'Content-Type': 'application/json', ...(config.token ? { Authorization: 'Bearer ' + config.token } : {}),
        ...(connection ? { 'X-Probe-Connection': connection } : {}) }, timeout: 30_000 }, response => {
      const chunks: Buffer[] = []; let bytes = 0;
      response.on('data', chunk => { bytes += chunk.length; if (bytes > MAX_BYTES) req.destroy(new ProbeError('response_too_large', '平台响应超过上限')); else chunks.push(chunk); });
      response.on('end', () => {
        let value: any;
        try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
        catch { reject(new ProbeError('response_invalid', '平台响应无法识别')); return; }
        if ((response.statusCode || 500) >= 400) {
          const code = response.statusCode === 401 ? 'device_revoked' : 'request_failed';
          reject(new ProbeError(code, typeof value.detail === 'string' ? value.detail : value.detail?.message || '平台请求未完成'));
        } else resolve(value);
      });
    });
    req.on('error', () => reject(new ProbeError('connection_failed', '无法连接平台，请检查地址、证书和网络')));
    req.on('timeout', () => req.destroy(new ProbeError('connection_timeout', '平台连接超时')));
    if (data) req.write(data); req.end();
  });
}

export async function enroll(pairing: Pairing, name: string, platform: 'win32' | 'darwin', architecture: string): Promise<Credentials> {
  const result = await request<{ device_id: string; token: string; profile_id: string }>(pairing, '/agent/enroll', 'POST', {
    code: pairing.code, name, platform, architecture, version: '0.1.0', protocol_version: PROTOCOL, capabilities: OPERATIONS,
  });
  return { server: pairing.server, ca_pem: pairing.ca_pem, ...result };
}

export interface Executor { chromeVersion?: string; execute(command: ReturnType<typeof validateCommand>): Promise<Record<string, unknown>>; }
export class ProbeTransport extends EventEmitter {
  private socket?: WebSocket; private stopped = true; private connection?: string;
  private heartbeat?: ReturnType<typeof setInterval>; private reconnect?: ReturnType<typeof setTimeout>;
  private backoff = 1000; private queue = Promise.resolve(); public paused = false;
  private websocketFailures = 0; private polling = false;
  public profiles: { id: string; name: string }[] = [];
  constructor(public config: Credentials, private journal: CommandJournal, private executor: Executor) { super(); }
  start(): void { this.stopped = false; this.connect(); }
  stop(): void {
    this.stopped = true; clearTimeout(this.reconnect); clearInterval(this.heartbeat);
    this.socket?.close(); this.emit('state', '已暂停连接');
  }
  setPaused(value: boolean): void { this.paused = value; this.sendHeartbeat(); this.emit('paused', value); }
  private connect(): void {
    if (this.stopped) return;
    if (this.websocketFailures >= 3) { void this.startPolling(); return; }
    const url = new URL('/api/v1/probes/agent/ws', this.config.server); url.protocol = 'wss:';
    this.emit('state', '正在连接平台');
    const socket = new WebSocket(url, { headers: { Authorization: 'Bearer ' + this.config.token },
      ca: this.config.ca_pem || undefined, rejectUnauthorized: true, maxPayload: MAX_BYTES, handshakeTimeout: 15_000 });
    this.socket = socket;
    socket.on('open', () => socket.send(JSON.stringify({ type: 'hello', protocol_version: PROTOCOL, paused: this.paused, chrome_version: this.executor.chromeVersion || null })));
    socket.on('message', raw => {
      try {
        const message = JSON.parse(raw.toString());
        if (message.type === 'hello') {
          this.connection = message.connection_id; this.profiles = message.profiles; this.backoff = 1000; this.websocketFailures = 0;
          clearInterval(this.heartbeat); this.heartbeat = setInterval(() => this.sendHeartbeat(), 20_000);
          this.emit('state', '已连接'); this.emit('profiles', this.profiles);
          for (const id of message.unresolved_commands || []) this.sendReceipt(this.journal.recover(id, this.connection!));
        } else if (message.type === 'command') {
          const command = validateCommand(message);
          this.queue = this.queue.then(async () => {
            if (command.connection_id !== this.connection) return;
            if (this.journal.has(command.id)) { this.sendReceipt(this.journal.recover(command.id, this.connection!)); return; }
            if (this.paused || this.stopped) { this.sendReceipt({ command_id: command.id, connection_id: this.connection!, status: 'failed', result: {}, error_code: 'device_paused' }); return; }
            this.journal.begin(command.id); this.emit('operation', command.operation);
            let receipt: Receipt;
            try {
              const result = await this.executor.execute(command);
              receipt = { command_id: command.id, connection_id: this.connection!, status: 'succeeded', result, error_code: null };
            } catch (error) {
              const code = error instanceof ProbeError ? error.code : 'browser_operation_failed';
              receipt = { command_id: command.id, connection_id: this.connection!, status: 'failed', result: {}, error_code: code };
              this.emit('notice', error instanceof ProbeError ? error.message : '浏览器操作未完成，请查看Chrome');
            }
            this.journal.finish(receipt); this.sendReceipt(receipt);
            this.emit('completed', { operation: command.operation, status: receipt.status,
              session_id: command.payload.session_id, kind: command.payload.kind });
            this.emit('operation', null);
          }).catch(() => { this.emit('notice', '任务结果需要核实，未自动重放操作'); });
        }
      } catch { socket.close(1008, 'invalid message'); }
    });
    socket.on('error', () => this.emit('state', '平台连接失败，正在等待网络恢复'));
    socket.on('unexpected-response', (_req, response) => {
      if (response.statusCode === 401 || response.statusCode === 403) {
        this.stopped = true; this.emit('state', '设备身份已撤销或需要重新配对');
      }
      response.resume();
    });
    socket.on('close', code => {
      clearInterval(this.heartbeat); this.connection = undefined;
      if (code === 1008) { this.stopped = true; this.emit('state', '平台拒绝了连接，请检查配对和设备权限'); }
      if (!this.stopped) {
        this.websocketFailures++;
        this.emit('state', '设备离线，已保存任务回执');
        const delay = Math.min(this.backoff, 30_000) + Math.random() * 500; this.backoff *= 2;
        this.reconnect = setTimeout(() => this.connect(), delay);
      }
    });
  }
  private sendHeartbeat(): void {
    if (this.polling && this.connection) {
      void request(this.config, '/agent/heartbeat', 'POST', { protocol_version: PROTOCOL,
        paused: this.paused, chrome_version: this.executor.chromeVersion || null }, this.connection).catch(() => {});
      return;
    }
    if (this.connection && this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(JSON.stringify({ type: 'heartbeat', connection_id: this.connection, protocol_version: PROTOCOL, paused: this.paused, chrome_version: this.executor.chromeVersion || null }));
  }
  private sendReceipt(receipt: Receipt): void {
    if (this.polling && this.connection) {
      void request(this.config, '/agent/receipts', 'POST', { ...receipt, connection_id: this.connection }).catch(() => {
        this.emit('notice', '回执已保存在本机，恢复连接后核对结果');
      });
      return;
    }
    if (this.socket?.readyState === WebSocket.OPEN && this.connection)
      this.socket.send(JSON.stringify({ type: 'receipt', ...receipt, connection_id: this.connection }));
  }
  private async startPolling(): Promise<void> {
    if (this.polling || this.stopped) return;
    this.polling = true;
    try {
      const hello = await request<{ connection_id: string; profiles: { id: string; name: string }[]; unresolved_commands: string[] }>(this.config,
        '/agent/hello', 'POST', { protocol_version: PROTOCOL, paused: this.paused, chrome_version: this.executor.chromeVersion || null });
      this.connection = hello.connection_id; this.profiles = hello.profiles;
      this.emit('profiles', this.profiles); this.emit('state', '已连接 · 企业网络兼容模式');
      for (const id of hello.unresolved_commands) await request(this.config, '/agent/receipts', 'POST', this.journal.recover(id, this.connection));
      this.heartbeat = setInterval(() => this.sendHeartbeat(), 20_000);
      while (!this.stopped) {
        const packet = await request<{ commands: unknown[] }>(this.config, '/agent/commands?wait=20', 'GET', undefined, this.connection);
        for (const value of packet.commands) {
          const command = validateCommand(value);
          if (this.journal.has(command.id)) { this.sendReceipt(this.journal.recover(command.id, this.connection)); continue; }
          this.journal.begin(command.id); this.emit('operation', command.operation);
          let receipt: Receipt;
          try {
            if (this.paused || this.stopped) throw new ProbeError('device_paused', '探针已暂停，未执行新操作');
            const result = await this.executor.execute(command);
            receipt = { command_id: command.id, connection_id: this.connection, status: 'succeeded', result, error_code: null };
          } catch (error) {
            receipt = { command_id: command.id, connection_id: this.connection, status: 'failed', result: {}, error_code: error instanceof ProbeError ? error.code : 'browser_operation_failed' };
            this.emit('notice', error instanceof ProbeError ? error.message : 'Chrome操作未完成，请查看浏览器');
          }
          this.journal.finish(receipt); await request(this.config, '/agent/receipts', 'POST', receipt);
          this.emit('completed', { operation: command.operation, status: receipt.status, session_id: command.payload.session_id, kind: command.payload.kind });
          this.emit('operation', null);
        }
      }
    } catch (error) {
      if (error instanceof ProbeError && error.code === 'device_revoked') { this.stopped = true; this.emit('state', '设备身份已撤销，请重新配对'); }
      else this.emit('state', '兼容连接中断，正在等待网络恢复');
    } finally {
      clearInterval(this.heartbeat); this.polling = false; this.connection = undefined;
      if (!this.stopped) this.reconnect = setTimeout(() => { void this.startPolling(); }, 5000);
    }
  }
}
