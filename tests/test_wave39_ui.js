'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'manage.js'), 'utf8');
const React = {
  createElement() {},
  useState() {},
};
const window = {
  React,
  GD: {},
  GDStore: {},
  UI: {},
};

vm.runInNewContext(source, { React, window }, { filename: 'web/manage.js' });

assert.ok(window.ConnectionUI, 'manage.js must export window.ConnectionUI');
const { connectionDraft, connectionPayload } = window.ConnectionUI;
assert.equal(typeof connectionDraft, 'function');
assert.equal(typeof connectionPayload, 'function');

const draft = connectionDraft({
  name: 'Production PVE',
  host: 'pve.example',
  port: 9443,
  tokenId: 'automation@pve!goblindock',
  verifyTls: false,
  node: 'pve-a',
  storage: 'local-zfs',
  isoStorage: 'iso-vault',
  snippetStorage: 'snippets',
  bridge: 'vmbr9',
  sshHost: 'ssh.example',
  sshUser: 'automation',
  sshKeyPath: '/run/secrets/pve_key',
  maxCores: 0,
  maxRamGb: 0,
  maxDiskGb: 0,
});

assert.deepEqual(JSON.parse(JSON.stringify(draft)), {
  name: 'Production PVE',
  host: 'pve.example',
  port: 9443,
  token_id: 'automation@pve!goblindock',
  token_secret: '',
  verify_tls: false,
  node: 'pve-a',
  storage: 'local-zfs',
  iso_storage: 'iso-vault',
  snippet_storage: 'snippets',
  bridge: 'vmbr9',
  ssh_host: 'ssh.example',
  ssh_user: 'automation',
  ssh_key_path: '/run/secrets/pve_key',
  max_cores: 0,
  max_ram_gb: 0,
  max_disk_gb: 0,
});

const createPayload = connectionPayload({ ...draft, token_secret: 'new-secret' }, false);
assert.deepEqual(JSON.parse(JSON.stringify(createPayload)), {
  name: 'Production PVE',
  host: 'pve.example',
  port: 9443,
  token_id: 'automation@pve!goblindock',
  token_secret: 'new-secret',
  verify_tls: false,
  node: 'pve-a',
  storage: 'local-zfs',
  iso_storage: 'iso-vault',
  snippet_storage: 'snippets',
  bridge: 'vmbr9',
  ssh_host: 'ssh.example',
  ssh_user: 'automation',
  ssh_key_path: '/run/secrets/pve_key',
  max_cores: 0,
  max_ram_gb: 0,
  max_disk_gb: 0,
});

const editPayload = connectionPayload(draft, true);
assert.equal(Object.hasOwn(editPayload, 'token_secret'), false,
  'editing with a blank token secret must preserve the stored secret');
assert.equal(editPayload.max_cores, 0);
assert.equal(editPayload.max_ram_gb, 0);
assert.equal(editPayload.max_disk_gb, 0);

assert.match(source, /useState\(\(\) => connectionDraft\(conn\)\)/,
  'the real connection form must initialize through connectionDraft');
assert.match(source, /connectionPayload\(f, editing\)/,
  'the real connection form must submit through connectionPayload');
for (const label of [
  'API port', 'ISO storage', 'Snippet storage', 'SSH host', 'SSH user', 'SSH key path',
]) {
  assert.ok(source.includes(`label: '${label}'`), `missing labelled ${label} field`);
}
assert.ok(source.includes("placeholder: '/run/secrets/pve_key'"),
  'SSH key path must use the secret path only as a placeholder');
assert.ok(source.includes('0 = unlimited'));
assert.equal(source.includes('0 = inherit global'), false);

console.log('ALL WAVE 39 UI TESTS PASSED');
