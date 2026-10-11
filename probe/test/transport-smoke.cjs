/* QA-only: real TLS + both outbound transports, explicit simulated browser executor. */
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const { enroll, ProbeTransport, request } = require('../dist/transport.js');
const { PrivateStore, CommandJournal } = require('../dist/journal.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let cookie = '';
async function api(route, body) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : JSON.stringify(body);
    const req = https.request(new URL('/api/v1' + route, input.server), { method: data ? 'POST' : 'GET', ca: input.ca_pem,
      rejectUnauthorized: true, headers: { 'Content-Type': 'application/json', ...(cookie ? { Cookie: cookie } : {}) } }, r => {
      if (r.headers['set-cookie']) cookie = r.headers['set-cookie'].map(c => c.split(';')[0]).join('; ');
      let text = ''; r.on('data', c => text += c); r.on('end', () => {
        if (r.statusCode >= 400) { reject(new Error('QA HTTP status ' + r.statusCode)); return; }
        resolve(JSON.parse(text));
      });
    }); req.on('error', reject); if (data) req.write(data); req.end();
  });
}
async function until(predicate, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise(r => setTimeout(r, 100)); }
  throw new Error('QA condition timed out');
}
(async () => {
  await api('/auth/login', { username: input.username, password: input.password });
  const outcomes = [];
  for (const mode of ['websocket', 'https_poll']) {
    const bundle = await api('/probes/pairings', {});
    const pairing = JSON.parse(Buffer.from(bundle.pairing_config.slice(9), 'base64url'));
    const credentials = await enroll(pairing, 'QA protocol transport ' + mode, mode === 'websocket' ? 'win32' : 'darwin', 'x64');
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'probe-smoke-'));
    const testCodec = { encryptString: s => Buffer.from(s).reverse(), decryptString: b => Buffer.from(b).reverse().toString() };
    const journal = new CommandJournal(new PrivateStore(path.join(folder, 'journal.enc'), testCodec));
    const calls = [];
    const executor = { execute: async c => {
      calls.push(c.operation);
      if (c.operation === 'browser.open_login') return { opened: true, verified: false, url: 'https://www.zhipin.com/' };
      if (c.operation === 'browser.confirm_login') return { saved: true, verified: false, url: 'https://www.zhipin.com/' };
      throw new Error('Unexpected QA operation');
    } };
    const transport = new ProbeTransport(credentials, journal, executor);
    if (mode === 'https_poll') transport.websocketFailures = 3;
    transport.start();
    try {
      await until(() => transport.profiles.length === 1);
      const login = await request(credentials, '/agent/open-login', 'POST', { profile_id: credentials.profile_id, kind: 'boss', name: 'QA simulated login' });
      await until(async () => (await api('/probes/commands/' + login.command_id)).status === 'succeeded');
      const confirmation = await request(credentials, '/agent/sessions/' + login.session_id + '/confirm-login', 'POST', {});
      await until(async () => (await api('/probes/commands/' + confirmation.command_id)).status === 'succeeded');
      assert.deepEqual(calls, ['browser.open_login', 'browser.confirm_login']);
      const accounts = await api('/source-accounts');
      assert(accounts.some(a => a.id === login.account_id && a.health === 'saved' && a.probe));
      outcomes.push({ mode, tls_verification: 'passed', commands: calls.length, native_browser: false, model_requests: 0 });
    } finally { transport.stop(); fs.rmSync(folder, { recursive: true, force: true }); }
  }
  await api('/auth/logout', {});
  console.log(JSON.stringify({ outcomes, scope: 'isolated QA, browser and OS storage simulated' }));
})().catch(() => { console.error('QA transport smoke failed; credentials suppressed'); process.exitCode = 1; });
