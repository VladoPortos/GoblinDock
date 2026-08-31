'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const manageSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'manage.js'), 'utf8');
const detailSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'vmdetail.js'), 'utf8');
const dashboardSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'dashboard.js'), 'utf8');
const window = { GD: {}, GDStore: { refresh: async () => {}, toast() {} }, UI: {} };
const React = { createElement() {}, useState() {} };
vm.runInNewContext(manageSource, { React, window }, { filename: 'web/manage.js' });

const { connectionPayload } = window.ConnectionUI;
assert.equal(connectionPayload({
  name: 'pve', host: 'pve', port: '', token_id: 'u@p!t', token_secret: 'x',
  verify_tls: true, node: '', storage: '', iso_storage: 'local',
  snippet_storage: 'local', bridge: 'vmbr0', ssh_host: '', ssh_user: 'root',
  ssh_key_path: '', max_cores: '', max_ram_gb: '', max_disk_gb: '',
}, false).port, 8006, 'a blank port must keep the safe Proxmox default');

assert.match(detailSource, /powerUnavailable\s*=\s*live\.status\s*===\s*['"]unknown['"]/,
  'VM detail must explicitly detect unavailable live status');
assert.match(detailSource, /disabled:\s*busy\s*\|\|\s*powerUnavailable/,
  'VM detail power controls must be disabled while status is unavailable');
assert.match(dashboardSource, /vm\.status\s*===\s*['"]unknown['"]/,
  'dashboard actions must recognize unknown VM status');

console.log('ALL WAVE 47 UI TESTS PASSED');
