const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { parsePairing, validateCommand, allowedUrl, safeUrl } = require('../dist/protocol.js');
const { PrivateStore, CommandJournal } = require('../dist/journal.js');

function pairing(changes = {}) {
  return 'hrprobe1.' + Buffer.from(JSON.stringify({ protocol_version: 'probe_v1', server: 'https://platform.example.invalid',
    code: 'fixture-code-not-a-real-token-long-enough', ca_pem: null, expires_at: new Date(Date.now() + 60_000).toISOString(), ...changes })).toString('base64url');
}
test('pairing uses TLS and expected platform origin', () => {
  assert.equal(parsePairing(pairing(), 'https://platform.example.invalid').protocol_version, 'probe_v1');
  for (const server of ['http://platform.example.invalid', 'file:///tmp/example', 'https://u:p@platform.example.invalid']) assert.throws(() => parsePairing(pairing({ server })));
  assert.throws(() => parsePairing(pairing(), 'https://other.example.invalid'));
  assert.throws(() => parsePairing(pairing({ expires_at: '2020-01-01T00:00:00Z' })));
});
test('browser site allowlist rejects suffix and local-resource tricks', () => {
  assert(allowedUrl('https://www.zhipin.com/gongsi/example.html', ['zhipin.com']));
  for (const url of ['file:///tmp/example', 'https://zhipin.com.evil.invalid/', 'http://zhipin.com/', 'https://127.0.0.1/', 'https://u:p@zhipin.com/', 'https://zhipin.com:1234/']) assert(!allowedUrl(url, ['zhipin.com']));
});
test('about:blank and private navigation query parameters are not malformed or exposed', () => {
  assert.equal(safeUrl('about:blank'), 'about:blank');
  const u = safeUrl('https://www.zhipin.com/job_detail/example.html?securityId=fixture-only&lid=x&page=2');
  assert(!u.includes('fixture-only')); assert(!u.includes('lid=')); assert(u.includes('page=2'));
});
test('device commands cannot introduce shell or arbitrary ownership paths', () => {
  const command = { type: 'command', id: randomUUID(), connection_id: randomUUID(), operation: 'browser.start_task',
    expires_at: new Date(Date.now() + 20_000).toISOString(), payload: { session_id: randomUUID(), profile_id: randomUUID() } };
  assert.equal(validateCommand(command).operation, 'browser.start_task');
  assert.throws(() => validateCommand({ ...command, operation: 'shell.exec' }));
  assert.throws(() => validateCommand({ ...command, payload: { ...command.payload, profile_id: '../outside' } }));
});
test('local durable journal recovers completion but never replays unknown actions', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'probe-journal-'));
  // This is an explicit test codec, not an OS-encryption acceptance claim.
  const codec = { encryptString: s => Buffer.from(s).reverse(), decryptString: b => Buffer.from(b).reverse().toString() };
  const store = new PrivateStore(path.join(dir, 'journal.enc'), codec);
  const first = new CommandJournal(store); const id = randomUUID(); first.begin(id);
  const second = new CommandJournal(store); assert.equal(second.recover(id, randomUUID()).status, 'unknown');
  const receipt = { command_id: id, connection_id: randomUUID(), status: 'succeeded', result: { opened: true }, error_code: null };
  second.finish(receipt); const third = new CommandJournal(store), connection = randomUUID();
  assert.deepEqual(third.recover(id, connection), { ...receipt, connection_id: connection });
  assert.equal(third.recover(randomUUID(), connection).error_code, 'execution_unknown');
  assert(!fs.readFileSync(path.join(dir, 'journal.enc'), 'utf8').includes('opened'));
  fs.rmSync(dir, { recursive: true });
});
