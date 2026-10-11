/* Linux cross-build check; this cannot replace native Windows/macOS launch tests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

(async () => {
  const asar = await import('@electron/asar');
  const root = path.resolve(__dirname, '..');
  const archives = [
    'release/win-unpacked/resources/app.asar',
    'release/mac/观势采集探针.app/Contents/Resources/app.asar',
    'release/mac-arm64/观势采集探针.app/Contents/Resources/app.asar',
  ];
  const files = fs.readdirSync(path.join(root, 'dist'), { recursive: true })
    .filter(name => fs.statSync(path.join(root, 'dist', name)).isFile()).sort();
  const checks = archives.map(archive => {
    for (const name of files) {
      assert.deepEqual(asar.extractFile(path.join(root, archive), 'dist/' + name),
        fs.readFileSync(path.join(root, 'dist', name)), 'Stale packaged client: ' + name);
    }
    return { archive, compiled_files_matched: files.length };
  });
  const digest = crypto.createHash('sha256');
  for (const name of files) {
    digest.update(name + '\0'); digest.update(fs.readFileSync(path.join(root, 'dist', name)));
  }
  console.log(JSON.stringify({ status: 'passed', compiled_client_sha256: digest.digest('hex'),
    checks, native_validation: 'not_run' }));
})().catch(error => { console.error(error.message); process.exitCode = 1; });
