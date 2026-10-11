import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, safeStorage, Tray } from 'electron';
import { join } from 'node:path';
import { unlinkSync, existsSync } from 'node:fs';
import { ChromeController } from './chrome';
import { CommandJournal, PrivateStore } from './journal';
import { Credentials, parsePairing, ProbeError } from './protocol';
import { enroll, ProbeTransport, request } from './transport';

let window: BrowserWindow | undefined, tray: Tray | undefined, transport: ProbeTransport | undefined;
let controller: ChromeController, credentialStore: PrivateStore<Credentials>, credentials: Credentials | undefined;
let state = '未配对', notice = '', operation: string | null = null;
let loginSession: string | undefined;
let quitting = false;
const kinds = new Set(['boss', 'liepin', 'zhaopin', '51job', 'wb', 'xhs', 'dy']);

function publicState() {
  return { configured: !!credentials, state, notice, operation, paused: transport?.paused || false,
    server: credentials?.server || '', device_id: credentials?.device_id || '',
    profiles: transport?.profiles || [], login_active: !!loginSession };
}
function publish() { if (window && !window.isDestroyed()) window.webContents.send('probe:state', publicState()); }
function startTransport() {
  if (!credentials) return;
  transport?.stop();
  const journal = new CommandJournal(new PrivateStore(join(app.getPath('userData'), 'command-journal.enc'), safeStorage));
  transport = new ProbeTransport(credentials, journal, controller);
  transport.on('state', value => { state = value; publish(); });
  transport.on('profiles', () => publish());
  transport.on('paused', () => publish());
  transport.on('notice', value => { notice = value; publish(); });
  transport.on('operation', value => { operation = value; publish(); });
  transport.on('completed', (value: { operation: string; status: string; session_id: string; kind: string }) => {
    if (value.operation === 'browser.open_login' && value.status === 'succeeded') {
      loginSession = value.session_id; notice = 'Chrome已在这台设备打开，请扫码或处理验证码'; publish();
    }
    if (value.operation === 'browser.confirm_login' && value.status === 'succeeded') {
      loginSession = undefined; notice = '本机登录记录已保留，首次采集会核对是否可用'; publish();
    }
  });
  transport.start();
}
async function action<T>(fn: () => Promise<T>) {
  try { return { ok: true, value: await fn() }; }
  catch (error) { return { ok: false, message: error instanceof ProbeError ? error.message : '操作未完成，请检查平台连接和Chrome' }; }
}
function createWindow() {
  window = new BrowserWindow({ width: 780, height: 800, minWidth: 660, minHeight: 650,
    title: '观势采集探针', show: false, webPreferences: {
      preload: join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true,
    } });
  void window.loadFile(join(__dirname, 'ui/index.html'));
  window.once('ready-to-show', () => window?.show());
  window.on('close', event => { if (!quitting && tray) { event.preventDefault(); window?.hide(); } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { window?.show(); window?.focus(); });
  app.whenReady().then(() => {
    if (!['win32', 'darwin'].includes(process.platform)) {
      dialog.showErrorBox('暂不支持这个系统', '本版采集探针支持Windows和macOS。'); app.quit(); return;
    }
    if (!safeStorage.isEncryptionAvailable()) {
      dialog.showErrorBox('系统凭据存储不可用', '请使用正常登录的桌面用户会话启动探针。'); app.quit(); return;
    }
    const root = app.getPath('userData');
    controller = new ChromeController(join(root, 'chrome-profiles'));
    credentialStore = new PrivateStore(join(root, 'device-credentials.enc'), safeStorage);
    try { credentials = credentialStore.read(); } catch { notice = '原设备身份无法解密，请重新配对'; }
    createWindow();
    const pixels = Buffer.alloc(16 * 16 * 4);
    for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) {
      if ((x - 7.5) ** 2 + (y - 7.5) ** 2 < 49) {
        const i = (y * 16 + x) * 4;
        pixels[i] = 0x35; pixels[i + 1] = 0x55; pixels[i + 2] = 0xbc; pixels[i + 3] = 0xff;
      }
    }
    const icon = nativeImage.createFromBitmap(pixels, { width: 16, height: 16 });
    tray = new Tray(icon); tray.setToolTip('观势采集探针');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: '显示探针', click: () => { window?.show(); window?.focus(); } },
      { label: '暂停 / 恢复采集', click: () => transport?.setPaused(!transport.paused) },
      { type: 'separator' }, { label: '退出探针', click: () => app.quit() },
    ])); tray.on('click', () => { window?.show(); window?.focus(); });
    if (credentials) startTransport();
  });
}

ipcMain.handle('probe:status', () => publicState());
ipcMain.handle('probe:register', (_event, input: { config: string; server: string; name: string; consent: boolean }) => action(async () => {
  if (!input || input.consent !== true || typeof input.name !== 'string' || !input.name.trim() || input.name.length > 120)
    throw new ProbeError('consent_required', '请填写设备备注并确认允许平台采集');
  if (credentials) throw new ProbeError('already_paired', '这台探针已配对，请先解除原配对');
  const pairing = parsePairing(input.config, input.server);
  if (process.platform !== 'win32' && process.platform !== 'darwin') throw new ProbeError('platform_unsupported', '当前系统不支持');
  if (!['x64', 'arm64'].includes(process.arch)) throw new ProbeError('architecture_unsupported', '当前处理器架构不支持');
  credentials = await enroll(pairing, input.name.trim(), process.platform, process.arch);
  credentialStore.write(credentials); notice = ''; startTransport(); return publicState();
}));
ipcMain.handle('probe:pause', (_event, paused: boolean) => {
  if (typeof paused !== 'boolean') return;
  transport?.setPaused(paused); publish();
});
ipcMain.handle('probe:reconnect', () => { if (credentials) startTransport(); });
ipcMain.handle('probe:open-login', (_event, input: { kind: string; name: string; profile_id: string }) => action(async () => {
  if (!credentials || !kinds.has(input.kind) || typeof input.name !== 'string' || !input.name.trim() || input.name.length > 120)
    throw new ProbeError('login_invalid', '请连接平台、选择登录平台并填写账号备注');
  const result = await request<{ session_id: string }>(credentials, '/agent/open-login', 'POST', input);
  loginSession = result.session_id; notice = '正在打开Chrome，请在真实浏览器窗口完成扫码或验证码'; publish(); return result;
}));
ipcMain.handle('probe:confirm-login', () => action(async () => {
  if (!credentials || !loginSession) throw new ProbeError('login_missing', '请先打开平台登录窗口');
  const result = await request(credentials, '/agent/sessions/' + loginSession + '/confirm-login', 'POST', {});
  notice = '正在确认Chrome中的登录状态'; publish(); return result;
}));
ipcMain.handle('probe:unpair', () => action(async () => {
  if (credentials) await request(credentials, '/agent/unpair', 'POST', {});
  transport?.stop(); credentials = undefined; loginSession = undefined;
  const path = join(app.getPath('userData'), 'device-credentials.enc'); if (existsSync(path)) unlinkSync(path);
  state = '已解除本机配对'; notice = '请同时在Web撤销旧设备。Chrome资料仍保留在本机。'; publish(); return publicState();
}));
ipcMain.handle('probe:startup', (_event, enabled: boolean) => {
  if (typeof enabled === 'boolean') app.setLoginItemSettings({ openAtLogin: enabled });
  return app.getLoginItemSettings().openAtLogin;
});
app.on('before-quit', event => {
  if (quitting) return;
  if (!controller) { quitting = true; return; }
  event.preventDefault(); quitting = true; transport?.stop();
  void controller?.disconnect().finally(() => app.quit());
});
