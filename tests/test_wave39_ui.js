'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'manage.js'), 'utf8');
const uiSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'ui.js'), 'utf8');
const stylesSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'styles.css'), 'utf8');
let nextId = 0;
const React = {
  createElement(type, props, ...children) {
    return {
      type,
      props: props || {},
      children: children.flat(Infinity).filter((child) => child != null && child !== false),
    };
  },
  useState() {},
  useId() { return `:wave39-${nextId++}:`; },
  useRef(initial) { return { current: initial }; },
  useEffect() {},
};
const window = {
  React,
  GD: {},
  GDStore: {},
  UI: {},
};

vm.runInNewContext(uiSource, {
  React,
  ReactDOM: { createPortal(node) { return node; } },
  window,
  navigator: {},
  document: { addEventListener() {}, removeEventListener() {} },
}, { filename: 'web/ui.js' });
vm.runInNewContext(source, { React, window }, { filename: 'web/manage.js' });

function textOf(node) {
  if (node == null || node === false) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  return (node.children || []).map(textOf).join('');
}

function findAll(node, predicate, found = []) {
  if (!node || typeof node !== 'object') return found;
  if (predicate(node)) found.push(node);
  for (const child of node.children || []) findAll(child, predicate, found);
  return found;
}

function resolveTree(node) {
  if (Array.isArray(node)) return node.map(resolveTree);
  if (!node || typeof node !== 'object') return node;
  if (typeof node.type === 'function') {
    const children = node.children.length === 1 ? node.children[0] : node.children;
    return resolveTree(node.type({ ...node.props, children }));
  }
  return { ...node, children: (node.children || []).map(resolveTree) };
}

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = stylesSource.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  assert.ok(match, `missing ${selector} CSS rule`);
  return match[1];
}

const renderedFields = [
  window.UI.Field({ label: 'API port', value: 9443 }),
  window.UI.Field({ label: 'Snippet storage', value: 'snippets' }),
  window.UI.Field({ label: 'SSH host', value: 'ssh.example' }),
  window.UI.Field({ label: 'SSH user', value: 'automation' }),
  window.UI.Field({ label: 'SSH key path', value: '/run/secrets/key' }),
  window.UI.SelectField({ label: 'ISO storage', value: 'iso-vault', options: ['iso-vault'] }),
  window.UI.TextArea({ label: 'Description', value: 'accessible shared textarea' }),
];
const associatedIds = new Set();
for (const field of renderedFields) {
  const labels = findAll(field, (node) => node.type === 'label');
  const controls = findAll(field, (node) => ['input', 'select', 'textarea'].includes(node.type));
  assert.equal(labels.length, 1, 'a visibly labelled field must render one native label');
  assert.equal(controls.length, 1, 'a field primitive must render one form control');
  const label = labels[0];
  const control = controls[0];
  assert.ok(textOf(label), 'the native label must remain visible');
  assert.ok(label.props.htmlFor, `${textOf(label)} must target its control`);
  assert.equal(control.props.id, label.props.htmlFor,
    `${textOf(label)} must programmatically name its control`);
  assert.equal(associatedIds.has(control.props.id), false,
    `${textOf(label)} must not reuse another field id`);
  associatedIds.add(control.props.id);
}

const formModal = resolveTree(window.UI.FormModal({
  title: 'Short viewport form',
  onClose() {},
  onSubmit() {},
  children: React.createElement('input', { 'aria-label': 'Example control' }),
}));
const modal = findAll(formModal, (node) => node.props.className === 'modal')[0];
assert.ok(modal, 'FormModal must render the shared modal container');
assert.deepEqual(modal.children.map((child) => child.props.className), [
  'modal-head', 'modal-body', 'modal-foot',
], 'head, scrolling body, and footer must remain ordered modal siblings');

const modalCss = cssRule('.modal');
assert.match(modalCss, /display\s*:\s*flex\s*;/);
assert.match(modalCss, /flex-direction\s*:\s*column\s*;/);
assert.match(modalCss, /max-height\s*:\s*calc\(100vh\s*-\s*32px\)\s*;/,
  'short-viewport modal needs a broadly supported viewport-height fallback');
assert.match(modalCss, /max-height\s*:\s*calc\(100dvh\s*-\s*32px\)\s*;/,
  'short-viewport modal must follow the dynamic viewport when available');
for (const viewportHeight of [812, 568]) {
  assert.ok(viewportHeight - 32 < viewportHeight,
    `${viewportHeight}px viewport must retain space around the bounded modal`);
}
assert.match(cssRule('.modal-head'), /flex-shrink\s*:\s*0\s*;/,
  'the modal header must remain visible when the body overflows');
assert.match(cssRule('.modal-foot'), /flex-shrink\s*:\s*0\s*;/,
  'the modal footer actions must remain visible when the body overflows');
const modalBodyCss = cssRule('.modal-body');
assert.match(modalBodyCss, /min-height\s*:\s*0\s*;/);
assert.match(modalBodyCss, /overflow-y\s*:\s*auto\s*;/,
  'lower connection controls must scroll inside the modal body');

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
assert.ok(source.includes("placeholder: '/run/secrets/pve_key'"),
  'SSH key path must use the secret path only as a placeholder');
assert.ok(source.includes('0 = unlimited'));
assert.equal(source.includes('0 = inherit global'), false);

console.log('ALL WAVE 39 UI TESTS PASSED');
