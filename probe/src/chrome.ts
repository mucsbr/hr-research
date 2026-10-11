import { spawn } from 'node:child_process';
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import net from 'node:net';
import http from 'node:http';
import { chromium, Browser, BrowserContext, Page } from 'playwright-core';
import { allowedUrl, Command, MAX_BYTES, ProbeError, safeUrl, uuid } from './protocol';

const ROOTS: Record<string, readonly string[]> = {
  boss: ['zhipin.com', 'bosszhipin.com', 'kanzhun.com'], liepin: ['liepin.com', 'liepin.cn', 'lietou-static.com'],
  zhaopin: ['zhaopin.com', 'zhaopin.cn'], '51job': ['51job.com', '51jobcdn.com'],
  xhs: ['xiaohongshu.com', 'xhscdn.com'], dy: ['douyin.com', 'douyincdn.com', 'bytedance.com'],
  wb: ['weibo.com', 'weibo.cn', 'sina.com.cn'],
};
interface Profile { browser: Browser; context: BrowserContext; }
interface ResponseValue { url: string; status: number; content_type: string; body: unknown; }
interface TaskPage { page: Page; responses: ResponseValue[]; pending: Set<Promise<void>>; status: number; }
interface Session {
  profile: string; kind: string; mode: 'login' | 'task'; expires: number;
  pages: Map<string, TaskPage>; loginPage?: Page;
}

export function findChrome(platform = process.platform, environment = process.env): string {
  const choices = platform === 'darwin' ? [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    join(environment.HOME || '', 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
  ] : platform === 'win32' ? [
    join(environment.PROGRAMFILES || 'C:\\Program Files', 'Google/Chrome/Application/chrome.exe'),
    join(environment['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google/Chrome/Application/chrome.exe'),
    join(environment.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
  ] : [];
  const found = choices.find(p => existsSync(p));
  if (!found) throw new ProbeError('chrome_missing', '未找到Google Chrome，请先安装Chrome再继续');
  return found;
}

function availablePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer(); server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address() as net.AddressInfo;
      server.close(() => resolvePort(address.port));
    });
  });
}
function versionAt(port: number): Promise<{ Browser: string; webSocketDebuggerUrl: string }> {
  return new Promise((resolveVersion, reject) => {
    const req = http.get({ hostname: '127.0.0.1', port, path: '/json/version', timeout: 800 }, response => {
      let text = ''; response.on('data', chunk => { text += chunk; if (text.length > 32_000) req.destroy(); });
      response.on('end', () => { try { resolveVersion(JSON.parse(text)); } catch { reject(new Error('invalid browser')); } });
    });
    req.on('error', reject); req.on('timeout', () => req.destroy());
  });
}
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export class ChromeController {
  public chromeVersion?: string;
  private profiles = new Map<string, Profile>(); private sessions = new Map<string, Session>();
  private cleanupTimer: ReturnType<typeof setInterval>;
  constructor(private root: string) {
    mkdirSync(root, { recursive: true, mode: 0o700 });
    if (lstatSync(root).isSymbolicLink()) throw new ProbeError('profile_invalid', '浏览器资料目录不能是链接');
    this.cleanupTimer = setInterval(() => {
      for (const [id, s] of this.sessions) if (s.expires <= Date.now()) void this.closeSession(id);
    }, 15_000); this.cleanupTimer.unref();
  }
  private async profile(id: string): Promise<Profile> {
    if (!uuid(id)) throw new ProbeError('profile_invalid', '浏览器资料目录标识无效');
    const existing = this.profiles.get(id);
    if (existing?.browser.isConnected()) return existing;
    const dir = join(this.root, id); mkdirSync(dir, { recursive: true, mode: 0o700 });
    if (lstatSync(dir).isSymbolicLink() || resolve(dir).slice(0, resolve(this.root).length + 1) !== resolve(this.root) + require('node:path').sep)
      throw new ProbeError('profile_invalid', '浏览器资料目录不在探针目录中');
    const statePath = join(dir, 'probe-port.json'); let port = 0;
    try {
      const saved = JSON.parse(readFileSync(statePath, 'utf8'));
      if (Number.isInteger(saved.port) && saved.port > 0 && saved.port < 65536) {
        const version = await versionAt(saved.port);
        if (version.Browser?.startsWith('Chrome/') && saved.browser_path &&
            new URL(version.webSocketDebuggerUrl).pathname === saved.browser_path) port = saved.port;
      }
    } catch { /* Chrome may have been closed; start the same persistent profile. */ }
    if (!port) {
      port = await availablePort();
      const child = spawn(findChrome(), [
        '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + port,
        '--user-data-dir=' + dir, '--no-first-run', '--no-default-browser-check',
      ], { stdio: 'ignore', windowsHide: false, shell: false, detached: true });
      child.on('error', () => {}); child.unref();
      let ready = false;
      for (let n = 0; n < 50; n++) {
        try { if ((await versionAt(port)).Browser?.startsWith('Chrome/')) { ready = true; break; } } catch {}
        await sleep(200);
      }
      if (!ready) throw new ProbeError('chrome_start_failed', 'Chrome未能启动，请检查已有窗口和系统提示');
      const version = await versionAt(port);
      writeFileSync(statePath, JSON.stringify({ port, browser_path: new URL(version.webSocketDebuggerUrl).pathname }), { mode: 0o600 });
    }
    const browser = await chromium.connectOverCDP('http://127.0.0.1:' + port, { timeout: 10_000, noDefaults: true });
    this.chromeVersion = browser.version();
    const context = browser.contexts()[0];
    if (!context) throw new ProbeError('chrome_context_missing', 'Chrome默认会话不可用');
    const profile = { browser, context }; this.profiles.set(id, profile); return profile;
  }
  private session(command: Command): Session {
    const id = String(command.payload.session_id), session = this.sessions.get(id);
    if (!session || session.expires <= Date.now() || session.profile !== command.payload.profile_id || session.kind !== command.payload.kind)
      throw new ProbeError('session_missing', '浏览器任务会话已结束，请从平台重新发起');
    return session;
  }
  private taskPage(command: Command): TaskPage {
    const s = this.session(command), target = s.pages.get(String(command.payload.page_id));
    if (s.mode !== 'task' || !target || target.page.isClosed()) throw new ProbeError('chrome_closed', '任务标签页已关闭');
    return target;
  }
  private async snapshot(target: TaskPage): Promise<Record<string, unknown>> {
    try { await target.page.waitForLoadState('networkidle', { timeout: 1500 }); } catch {}
    await Promise.race([Promise.allSettled([...target.pending]), sleep(4000)]);
    if (target.pending.size) throw new ProbeError('probe_response_timeout', '网站响应仍未完成，请稍后核对任务');
    const data = await target.page.evaluate(() => {
      const copy = document.documentElement.cloneNode(true) as HTMLElement;
      copy.querySelectorAll('script:not([type="application/ld+json"]), input, textarea, form, iframe').forEach(n => n.remove());
      copy.querySelectorAll('[value]').forEach(n => n.removeAttribute('value'));
      // Public structured job state only; never copy authentication/user bootstrap state.
      const state = (window as unknown as { __INITIAL_STATE__?: any }).__INITIAL_STATE__;
      if (state?.jobDetail) {
        const script = document.createElement('script');
        script.textContent = 'window.__INITIAL_STATE__=' + JSON.stringify({ jobDetail: {
          detailedPosition: state.jobDetail.detailedPosition, detailedCompany: state.jobDetail.detailedCompany,
          guestJobDescClamp: state.jobDetail.guestJobDescClamp,
        } }); copy.appendChild(script);
      }
      return { html: '<!doctype html>' + copy.outerHTML, text: document.body?.innerText || '' };
    });
    const responses = target.responses.splice(0);
    const result = { url: safeUrl(target.page.url()), status: target.status, ...data, responses };
    if (Buffer.byteLength(JSON.stringify(result)) > MAX_BYTES - 2000) throw new ProbeError('document_too_large', '页面资料超过任务上限，未截断冒充完整资料');
    return result;
  }
  async execute(command: Command): Promise<Record<string, unknown>> {
    if (Date.parse(command.expires_at) <= Date.now()) throw new ProbeError('command_expired', '任务指令已过期');
    const p = command.payload, kind = String(p.kind), roots = ROOTS[kind];
    if (command.operation === 'device.status') return { chrome_available: !!findChrome() };
    if (!roots || !uuid(p.profile_id) || !uuid(p.session_id)) throw new ProbeError('command_invalid', '浏览器任务归属无效');
    if (command.operation === 'browser.close_session') return this.closeSession(p.session_id);
    if (command.operation === 'browser.open_login' || command.operation === 'browser.start_task') {
      const profile = await this.profile(p.profile_id);
      let s = this.sessions.get(p.session_id);
      if (!s) {
        const expiry = Date.parse(String(p.session_expires_at));
        if (!Number.isFinite(expiry) || expiry <= Date.now()) throw new ProbeError('session_expired', '浏览器会话已过期');
        s = { profile: p.profile_id, kind, mode: command.operation === 'browser.open_login' ? 'login' : 'task', expires: expiry, pages: new Map() };
        this.sessions.set(p.session_id, s);
      }
      if (s.profile !== p.profile_id || s.kind !== kind) throw new ProbeError('session_mismatch', '浏览器归属已改变');
      if (command.operation === 'browser.open_login') {
        const url = String(p.url); if (!allowedUrl(url, roots)) throw new ProbeError('navigation_forbidden', '平台登录地址不匹配');
        const page = await profile.context.newPage(); s.loginPage = page; await page.bringToFront();
        // Login remains interactive even if a redirect/navigation has not settled.
        try { await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 25_000 }); } catch {}
        return { opened: true, url: safeUrl(page.url()), verified: false };
      }
      if (!uuid(p.page_id)) throw new ProbeError('page_invalid', '任务标签页标识无效');
      if (s.pages.has(p.page_id)) return { created: true, page_id: p.page_id };
      const page = await profile.context.newPage();
      const target: TaskPage = { page, responses: [], pending: new Set(), status: 200 }; s.pages.set(p.page_id, target);
      await page.route('**/*', async route => {
        if (route.request().isNavigationRequest() && route.request().frame() === page.mainFrame() && !allowedUrl(route.request().url(), roots)) await route.abort();
        else await route.continue();
      });
      const pattern = typeof p.response_path_pattern === 'string' && p.response_path_pattern.length <= 300 ? new RegExp(p.response_path_pattern, 'i') : null;
      if (pattern) page.on('response', response => {
        const url = response.url();
        if (!allowedUrl(url, roots) || !pattern.test(new URL(url).pathname) || target.responses.length >= 4 || target.pending.size >= 4) return;
        if (!response.headers()['content-type']?.includes('json')) return;
        const pending = (async () => {
          try {
            if (Number(response.headers()['content-length'] || 0) > 2_000_000) return;
            const raw = await Promise.race([response.body(), sleep(5000).then(() => { throw new Error('response timeout'); })]);
            if (raw.length > 2_000_000) return;
            target.responses.push({ url: safeUrl(url), status: response.status(), content_type: 'application/json', body: JSON.parse(raw.toString('utf8')) });
          } catch {}
        })(); target.pending.add(pending);
        void pending.finally(() => target.pending.delete(pending));
      });
      return { created: true, page_id: p.page_id };
    }
    const session = this.session(command);
    if (command.operation === 'browser.confirm_login') {
      const profile = await this.profile(session.profile);
      const page = profile.context.pages().find(page => !page.isClosed() && allowedUrl(page.url(), roots));
      if (!page) throw new ProbeError('waiting_login', '请先在Chrome中打开并完成这个平台的登录');
      this.sessions.delete(p.session_id);
      return { saved: true, verified: false, url: safeUrl(page.url()) };
    }
    const target = this.taskPage(command);
    if (command.operation === 'browser.navigate') {
      const url = String(p.url); if (!allowedUrl(url, roots)) throw new ProbeError('navigation_forbidden', '采集地址不属于登记平台');
      const response = await target.page.goto(url, { waitUntil: 'domcontentloaded', timeout: 25_000 });
      target.status = response?.status() || 200;
      if (!allowedUrl(target.page.url(), roots)) throw new ProbeError('waiting_login', '页面跳转离开来源平台，请人工检查Chrome');
      return this.snapshot(target);
    }
    if (command.operation === 'browser.read_page') return this.snapshot(target);
    if (command.operation === 'browser.locate' || command.operation === 'browser.click') {
      if (typeof p.selector !== 'string' || p.selector.length > 500) throw new ProbeError('selector_invalid', '页面选择器无效');
      const locator = target.page.locator(p.selector); const count = await locator.count();
      const nth = p.nth === -1 ? count - 1 : Number(p.nth || 0);
      const item = locator.nth(nth);
      if (command.operation === 'browser.click') {
        if (!count || nth < 0 || nth >= count) throw new ProbeError('source_contract_changed', '页面操作目标已变化');
        await item.click({ timeout: 8000 }); return this.snapshot(target);
      }
      if (!count || nth < 0 || nth >= count) return { count: 0, text: '', visible: false, disabled: true, attributes: {} };
      const text = await item.innerText({ timeout: 5000 });
      if (Buffer.byteLength(text) > 3_000_000) throw new ProbeError('document_too_large', '页面文字超过上限');
      const attributes: Record<string, string | null> = {};
      for (const name of ['disabled', 'aria-disabled', 'class', 'href']) attributes[name] = await item.getAttribute(name);
      return { count, text, visible: await item.isVisible(), disabled: await item.isDisabled(), attributes };
    }
    if (command.operation === 'browser.scroll') {
      await target.page.mouse.wheel(0, p.direction === 'up' ? -650 : 650); return this.snapshot(target);
    }
    throw new ProbeError('operation_forbidden', '该操作未开放');
  }
  private async closeSession(id: string): Promise<Record<string, unknown>> {
    const s = this.sessions.get(id); this.sessions.delete(id);
    if (s) for (const target of s.pages.values()) if (!target.page.isClosed()) await target.page.close().catch(() => {});
    // Native login pages and the user's persistent Chrome remain open.
    return { closed: true };
  }
  async disconnect(): Promise<void> {
    clearInterval(this.cleanupTimer);
    for (const id of [...this.sessions.keys()]) await this.closeSession(id);
    for (const p of this.profiles.values()) await p.browser.close().catch(() => {});
    this.profiles.clear();
  }
}
