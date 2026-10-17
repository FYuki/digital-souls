import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { checkText, checkRepository, checkManifest } from './check-docs.mjs';
import { createHash } from 'node:crypto';

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'core-docs-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

test('manifest detects tampering of exact asset bytes', t => {
  const root = fixture(t);
  writeFileSync(join(root, 'asset.bin'), 'example');
  const manifest = { files: [{ status: 'included', path: 'asset.bin', bytes: 7,
    sha256: createHash('sha256').update('example').digest('hex') }] };
  assert.deepEqual(checkManifest(root, manifest), []);
  writeFileSync(join(root, 'asset.bin'), 'changed');
  assert.match(checkManifest(root, manifest)[0], /mismatch/);
});
test('manifest rejects missing files and escaping paths', t => {
  const root = fixture(t);
  assert.match(checkManifest(root, { files: [{ status: 'included', path: 'missing' }] })[0], /Missing asset/);
  assert.match(checkManifest(root, { files: [{ status: 'included', path: '../outside' }] })[0], /escapes/);
});
test('held assets must not claim a local path', t => {
  const root = fixture(t);
  assert.deepEqual(checkManifest(root, { files: [{ status: 'held' }] }), []);
  assert.match(checkManifest(root, { files: [{ status: 'held', path: 'a.wav' }] })[0], /must not/);
});
test('accepts real local targets and ignores external links and anchors', t => {
  const root = fixture(t);
  writeFileSync(join(root, 'target.md'), '# Target\n');
  assert.deepEqual(checkText(root, 'README.md', '[local](target.md#section) [web](https://example.com) [anchor](#x)\n'), []);
});
test('rejects missing image targets', t => {
  assert.match(checkText(fixture(t), 'a.md', '![image](missing.png)\n')[0], /missing local target/);
});
test('rejects links escaping repository', t => {
  assert.match(checkText(fixture(t), 'a.md', '[escape](../outside.md)\n')[0], /escapes repository/);
});
test('handles malformed URL encoding as validation failure', t => {
  assert.match(checkText(fixture(t), 'a.md', '[bad](%ZZ)\n')[0], /invalid link encoding/);
});
test('checks line endings, whitespace and final newline', t => {
  const errors = checkText(fixture(t), 'a.md', 'text \r\nend');
  assert.equal(errors.length, 3);
});
test('rejects malformed JSON and accepts valid JSON', t => {
  const root = fixture(t);
  assert.match(checkText(root, 'a.json', '{oops}\n')[0], /invalid JSON/);
  assert.deepEqual(checkText(root, 'a.json', '{}\n'), []);
});
test('empty tracked file set is not a pass', t => {
  const root = fixture(t);
  execFileSync('git', ['init', '-q'], { cwd: root });
  assert.throws(() => checkRepository(root), /No tracked/);
});
test('CLI checks tracked files and returns a failing exit code for broken links', t => {
  const root = fixture(t);
  execFileSync('git', ['init', '-q'], { cwd: root });
  writeFileSync(join(root, 'README.md'), '[missing](missing.md)\n');
  execFileSync('git', ['add', 'README.md'], { cwd: root });
  const script = fileURLToPath(new URL('./check-docs.mjs', import.meta.url));
  const failed = spawnSync(process.execPath, [script], { cwd: root, encoding: 'utf8' });
  assert.equal(failed.status, 1);
  assert.match(failed.stderr, /missing local target/);
  writeFileSync(join(root, 'missing.md'), '# Found\n');
  const passed = spawnSync(process.execPath, [script], { cwd: root, encoding: 'utf8' });
  assert.equal(passed.status, 0);
});

function localPostgresCompose() {
  return JSON.parse(readFileSync(new URL('../core/compose.postgresql.json', import.meta.url), 'utf8'));
}

test('local PostgreSQL uses the isolated fixture image and digest', () => {
  const compose = localPostgresCompose();
  const fixtureScript = readFileSync(new URL('../core/tools/with-test-postgres.sh', import.meta.url), 'utf8');
  for (const wrapper of ['test-postgres.sh', 'evaluate-semantic-retrieval.sh']) {
    const script = readFileSync(new URL(`../core/tools/${wrapper}`, import.meta.url), 'utf8');
    assert.match(script, /with-test-postgres\.sh/);
  }
  const image = fixtureScript.match(/image='([^']+)'/)[1];
  assert.deepEqual(Object.keys(compose.services), ['postgres']);
  assert.equal(compose.services.postgres.image, image);
});

test('local PostgreSQL publishes only loopback with an optional host port', () => {
  const service = localPostgresCompose().services.postgres;
  assert.deepEqual(service.ports, [{
    target: 5432, published: '${DSC_POSTGRES_PORT:-5432}', host_ip: '127.0.0.1', protocol: 'tcp'
  }]);
  assert.equal(service.network_mode, undefined);
});

test('local PostgreSQL persists the PostgreSQL 18 data directory in a named volume', () => {
  const compose = localPostgresCompose();
  assert.deepEqual(compose.volumes, { postgres_data: {} });
  assert.deepEqual(compose.services.postgres.volumes, [
    { type: 'volume', source: 'postgres_data', target: '/var/lib/postgresql' }
  ]);
});

test('local PostgreSQL requires explicit credentials and ignores the local env file', () => {
  const service = localPostgresCompose().services.postgres;
  assert.deepEqual(service.environment, {
    POSTGRES_DB: '${DSC_POSTGRES_DATABASE:?Set DSC_POSTGRES_DATABASE}',
    POSTGRES_USER: '${DSC_POSTGRES_USER:?Set DSC_POSTGRES_USER}',
    POSTGRES_PASSWORD: '${DSC_POSTGRES_PASSWORD:?Set DSC_POSTGRES_PASSWORD}'
  });
  const example = readFileSync(new URL('../core/examples/postgresql.env.example', import.meta.url), 'utf8');
  assert.equal(example, 'DSC_POSTGRES_DATABASE=\nDSC_POSTGRES_USER=\nDSC_POSTGRES_PASSWORD=\n');
  const root = fileURLToPath(new URL('../', import.meta.url));
  const ignored = spawnSync('git', ['check-ignore', '.env.postgresql.local'], { cwd: root });
  assert.equal(ignored.status, 0);
});
