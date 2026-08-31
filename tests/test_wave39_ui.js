'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'manage.js'), 'utf8');
const imagesSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'images.js'), 'utf8');
const templatesSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'extra.js'), 'utf8');
const uiSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'ui.js'), 'utf8');
const jobSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'job.js'), 'utf8');
const historySource = fs.readFileSync(path.join(__dirname, '..', 'web', 'history.js'), 'utf8');
const shellSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'shell.js'), 'utf8');
const builderSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'builder.js'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const iconsSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'icons.js'), 'utf8');
const dashboardSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'dashboard.js'), 'utf8');
const vmDetailSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'vmdetail.js'), 'utf8');
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
vm.runInNewContext(imagesSource, { React, window }, { filename: 'web/images.js' });

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

function controlForLabel(node, labelText) {
  const label = findAll(node, (candidate) => candidate.type === 'label'
    && textOf(candidate) === labelText)[0];
  assert.ok(label && label.props.htmlFor, `missing labelled ${labelText} control`);
  return findAll(node, (candidate) => candidate.props.id === label.props.htmlFor)[0];
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

function sourceSection(fileSource, startMarker, endMarker) {
  const start = fileSource.indexOf(startMarker);
  assert.notEqual(start, -1, `missing source section ${startMarker}`);
  const end = fileSource.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `missing source section end ${endMarker}`);
  return fileSource.slice(start, end);
}

function templateListHarness(templates) {
  const HarnessReact = {
    createElement: React.createElement,
    useState(initial) {
      return [typeof initial === 'function' ? initial() : initial, () => {}];
    },
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) {
      return HarnessReact.createElement('span', { 'data-icon': props.name });
    },
    GD: { TEMPLATES: templates, me: {} },
    GDStore: { signOut() {} },
    API: {},
    UI: {
      Menu(props) {
        return HarnessReact.createElement('div', { 'data-template-menu': true },
          props.children,
          (props.items || []).map((item) => HarnessReact.createElement(
            'button', { key: item.label, onClick: item.onClick }, item.label,
          )),
        );
      },
      ConfirmModal() {}, Field() {}, CopyField() {},
      OSGlyph(props) {
        return HarnessReact.createElement('span', { 'data-os': props.os });
      },
      copyToClipboard() {},
    },
  };
  vm.runInNewContext(templatesSource, { React: HarnessReact, window: harnessWindow }, {
    filename: 'web/extra.js',
  });
  const tree = resolveTree(harnessWindow.TemplatesList({ go() {} }));
  return { window: harnessWindow, tree };
}

const templateFixtures = [
  {
    id: 't-owner', templateId: 3911, name: 'Owner allowed', os: 'ubuntu', desc: '',
    cpu: 2, mem: 4, disk: 20, used: 0, public: false, blocks: [], base: 'Ubuntu',
    location: 'pve-a', deployable: true, canEdit: true, canDelete: true,
  },
  {
    id: 't-admin', templateId: 3912, name: 'Admin allowed', os: 'ubuntu', desc: '',
    cpu: 2, mem: 4, disk: 20, used: 0, public: true, blocks: [], base: 'Ubuntu',
    location: 'pve-a', deployable: true, canEdit: true, canDelete: true,
  },
  {
    id: 't-viewer', templateId: 3913, name: 'Public viewer', os: 'ubuntu', desc: '',
    cpu: 2, mem: 4, disk: 20, used: 0, public: true, blocks: [], base: 'Ubuntu',
    location: 'pve-a', deployable: true, canEdit: false, canDelete: false,
  },
  {
    id: 't-referenced', templateId: 3914, name: 'Referenced owner', os: 'ubuntu', desc: '',
    cpu: 2, mem: 4, disk: 20, used: 1, public: false, blocks: [], base: 'Ubuntu',
    location: 'pve-a', deployable: false, canEdit: true, canDelete: false,
  },
];
const templateHarness = templateListHarness(templateFixtures);
function templateCard(name) {
  const cards = findAll(templateHarness.tree, (node) => node.props.className === 'card'
    && textOf(node).includes(name));
  assert.equal(cards.length, 1, `missing rendered ${name} template card`);
  return cards[0];
}

function templateActionState(name) {
  const card = templateCard(name);
  const buttons = findAll(card, (node) => node.type === 'button');
  const buttonLabels = buttons.map(textOf);
  return {
    card,
    buttonLabels,
    deploy: buttons.find((button) => textOf(button) === 'Deploy'),
    menus: findAll(card, (node) => node.props['data-template-menu'] === true),
  };
}

const allowedTemplateStates = [];
for (const name of ['Owner allowed', 'Admin allowed']) {
  const state = templateActionState(name);
  allowedTemplateStates.push({ name, state });
  assert.ok(state.deploy, `${name} must retain Deploy`);
  assert.equal(state.deploy.props.disabled, false);
  assert.ok(state.buttonLabels.includes('Edit'), `${name} must show Edit`);
  assert.ok(state.buttonLabels.includes('Delete'), `${name} must show Delete`);
  assert.equal(state.menus.length, 1, `${name} must show the actions menu`);
}

const viewerActions = templateActionState('Public viewer');
assert.ok(viewerActions.deploy, 'a non-owner public template must retain Deploy');
assert.equal(viewerActions.deploy.props.disabled, false);
assert.equal(viewerActions.buttonLabels.includes('Edit'), false);
assert.equal(viewerActions.buttonLabels.includes('Delete'), false);
assert.equal(viewerActions.menus.length, 0, 'an empty actions menu must be omitted');

const referencedActions = templateActionState('Referenced owner');
assert.ok(referencedActions.deploy, 'a referenced owned template must retain Deploy');
assert.equal(referencedActions.deploy.props.disabled, true,
  'Deploy disabled state must remain controlled only by deployable');
assert.ok(referencedActions.buttonLabels.includes('Edit'));
assert.equal(referencedActions.buttonLabels.includes('Delete'), false);
assert.equal(referencedActions.menus.length, 0, 'a non-deletable template has no menu');
assert.equal(findAll(templateHarness.tree, (node) => textOf(node) === 'Fork').length, 0,
  'template cards must not add a Fork action');

for (const { state } of allowedTemplateStates) {
  const menuTrigger = findAll(state.menus[0], (node) => node.type === 'button'
    && textOf(node) === '')[0];
  assert.equal(menuTrigger.props['aria-label'], 'Template actions',
    'the icon-only template menu trigger needs an accessible name');
}

assert.ok(templateHarness.window.TemplateUI,
  'extra.js must export window.TemplateUI');
assert.equal(typeof templateHarness.window.TemplateUI.templateActionFlags, 'function');
const { templateActionFlags } = templateHarness.window.TemplateUI;
assert.deepEqual(JSON.parse(JSON.stringify(templateActionFlags({
  canEdit: true, canDelete: true,
}))), { canEdit: true, canDelete: true });
for (const unsafe of [
  {}, null, { canEdit: 1, canDelete: 'yes' }, { canEdit: false, canDelete: true },
]) {
  assert.deepEqual(JSON.parse(JSON.stringify(templateActionFlags(unsafe))), {
    canEdit: false, canDelete: false,
  }, 'template capabilities must fail closed and remain strict booleans');
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

assert.ok(window.ImageUI, 'images.js must export window.ImageUI');
const { checksumMeta } = window.ImageUI;
assert.equal(typeof checksumMeta, 'function');

assert.deepEqual(JSON.parse(JSON.stringify(checksumMeta('  '))), {
  normalized: '', valid: true, algorithm: 'Optional', message: 'Optional',
});
for (const [length, algorithm] of [
  [32, 'MD5'], [40, 'SHA-1'], [64, 'SHA-256'], [96, 'SHA-384'], [128, 'SHA-512'],
]) {
  const meta = JSON.parse(JSON.stringify(checksumMeta(`  ${'A'.repeat(length)}\t`)));
  assert.equal(meta.normalized, 'a'.repeat(length));
  assert.equal(meta.valid, true);
  assert.equal(meta.algorithm, algorithm);
  assert.match(meta.message, new RegExp(algorithm.replace('-', '[-]?')));
}
for (const malformed of [
  'f'.repeat(31),
  'g'.repeat(32),
  `${'a'.repeat(16)} ${'a'.repeat(16)}`,
  `sha256:${'a'.repeat(64)}`,
]) {
  const meta = checksumMeta(malformed);
  assert.equal(meta.valid, false, `${malformed.slice(0, 16)} must be invalid`);
  assert.match(meta.message, /hexadecimal/i);
  assert.match(meta.message, /32.*40.*64.*96.*128/);
}

function imageModalHarness(img) {
  const state = [];
  let cursor = 0;
  let generatedId = 0;
  const apiCalls = [];
  const toasts = [];
  const ModalReact = {
    createElement: React.createElement,
    useState(initial) {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) {
        state[index] = typeof initial === 'function' ? initial() : initial;
      }
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
    useId() {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) state[index] = `:image-${generatedId++}:`;
      return state[index];
    },
    useRef(initial) { return { current: initial }; },
    useEffect() {},
  };
  const modalWindow = {
    React: ModalReact,
    Icon() {},
    GD: {},
    GDStore: {
      refresh() { return Promise.resolve(); },
      toast(message, tone) { toasts.push({ message, tone }); },
    },
    API: {
      addBaseImage(payload) { apiCalls.push({ action: 'add', payload }); return Promise.resolve(); },
      editImage(id, payload) { apiCalls.push({ action: 'edit', id, payload }); return Promise.resolve(); },
    },
    UI: {
      OSGlyph() {}, Menu() {}, ConfirmModal() {},
      FormModal(props) {
        return ModalReact.createElement('form', {
          onSubmit: props.onSubmit, busy: props.busy, 'aria-label': props.title,
        }, props.children);
      },
      Field(props) {
        return ModalReact.createElement('input', {
          'aria-label': props.label,
          value: props.value,
          onChange: (event) => props.onChange(event.target.value),
        });
      },
      SelectField(props) {
        return ModalReact.createElement('select', {
          'aria-label': props.label,
          value: props.value,
          onChange: (event) => props.onChange(event.target.value),
        });
      },
      useFetched() { return { online: false, cached: {} }; },
    },
  };
  vm.runInNewContext(imagesSource, { React: ModalReact, window: modalWindow }, {
    filename: 'web/images.js',
  });

  function render() {
    cursor = 0;
    return resolveTree(modalWindow.ImageUI.IsoModal({
      img,
      onClose() {},
      onDone() {},
    }));
  }

  function renderIsos() {
    cursor = 0;
    return resolveTree(modalWindow.Isos({ go() {} }));
  }

  return { render, renderIsos, gd: modalWindow.GD, apiCalls, toasts };
}

const cardHarness = imageModalHarness(null);
Object.assign(cardHarness.gd, {
  me: { isAdmin: false },
  BASE_IMAGES: [{
    id: 'img-legacy', imgId: 391, name: 'Legacy image', os: 'generic', size: '',
    source_url: '', checksum: '',
  }],
  CONNECTIONS: [],
  JOBS: [],
});
const legacyImageCard = cardHarness.renderIsos();
const legacySource = findAll(legacyImageCard,
  (node) => node.props.className === 'copy mono')[0];
assert.equal(textOf(legacySource), 'Not provided',
  'the UI, rather than the serializer, must own friendly empty source copy');

function jobSurfaceHarness({ stateValues = [], gd = {} } = {}) {
  let stateCursor = 0;
  const HarnessReact = {
    createElement: React.createElement,
    Fragment: 'fragment',
    useState(initial) {
      const index = stateCursor++;
      const value = Object.hasOwn(stateValues, index)
        ? stateValues[index]
        : (typeof initial === 'function' ? initial() : initial);
      return [value, () => {}];
    },
    useEffect() {},
    useRef(initial) { return { current: initial }; },
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) {
      return HarnessReact.createElement('span', { 'data-icon': props.name });
    },
    GD: gd,
    GDStore: {
      nav: {},
      refresh() { return Promise.resolve(); },
      toast() {},
      signOut() {},
    },
    API: {
      jobsHistory() { return Promise.resolve([]); },
      jobRetentionGet() { return Promise.resolve({ days: 0 }); },
      deleteJob() { return Promise.resolve(); },
      clearJobs() { return Promise.resolve(); },
    },
    UI: {
      Menu() {},
      ConfirmModal() {},
      copyToClipboard() {},
    },
  };
  vm.runInNewContext(shellSource, { React: HarnessReact, window: harnessWindow }, {
    filename: 'web/shell.js',
  });
  vm.runInNewContext(jobSource, { React: HarnessReact, window: harnessWindow }, {
    filename: 'web/job.js',
  });
  vm.runInNewContext(historySource, { React: HarnessReact, window: harnessWindow }, {
    filename: 'web/history.js',
  });
  return {
    window: harnessWindow,
    render(component) {
      stateCursor = 0;
      return resolveTree(component());
    },
  };
}

function jobDetailFixture(rawStatus, status, title, error) {
  return {
    id: 3900,
    title,
    type: 'deploy',
    rawStatus,
    status,
    pct: 67,
    phase: `${title} phase`,
    phases: ['Allocate', 'Create'],
    elapsed: '12s',
    error,
    steps: [],
    log: [],
  };
}

function builderHarness({ persistent = false } = {}) {
  let stateCursor = 0;
  const state = [];
  const stateChanges = [];
  const HarnessReact = {
    createElement: React.createElement,
    useState(initial) {
      const index = stateCursor++;
      let value;
      if (persistent && Object.hasOwn(state, index)) value = state[index];
      else {
        value = typeof initial === 'function' ? initial() : initial;
        if (persistent) state[index] = value;
      }
      return [value,
        (value) => {
          stateChanges.push({ index, value });
          if (persistent) state[index] = typeof value === 'function' ? value(state[index]) : value;
        }];
    },
    useEffect() {},
    useRef(initial) { return { current: initial }; },
  };
  const paletteBlock = {
    id: 'package-install', name: 'Install packages', cat: 'Install', section: 'Install',
    desc: 'Install selected packages', icon: 'package', builtin: true, phase: 'ansible',
    schema: [],
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) {
      return HarnessReact.createElement('span', { 'data-icon': props.name });
    },
    GD: {
      PALETTE: [paletteBlock],
      TEMPLATES: [{
        templateId: 3950, name: 'Keyboard template', cpu: 2, mem: 4, disk: 20,
        os: 'ubuntu', public: false, recipe: [{
          id: 's-inst', name: 'Install', blocks: [{
            ref: paletteBlock.id, name: paletteBlock.name, inputs: {}, ask: [],
          }],
        }],
      }],
      BASE_IMAGES: [], CONNECTIONS: [], NETWORKS: [], SECRETS: [],
    },
    GDStore: { nav: { templateId: 3950 }, refresh() { return Promise.resolve(); }, toast() {} },
    API: { compile() { return Promise.resolve({ yaml: '' }); } },
    UI: {
      Modal(props) { return HarnessReact.createElement('div', { className: 'modal' }, props.children); },
      OSGlyph() {}, Field() {}, TextArea() {}, SelectField() {}, Toggle() {}, TagInput() {},
      FormModal(props) { return HarnessReact.createElement('form', null, props.children); },
    },
  };
  vm.runInNewContext(builderSource, { React: HarnessReact, window: harnessWindow, setTimeout() {} }, {
    filename: 'web/builder.js',
  });
  return {
    window: harnessWindow,
    render(component) {
      stateCursor = 0;
      return resolveTree(component());
    },
    stateChanges,
    state,
  };
}

const navigationHarness = jobSurfaceHarness({ gd: { me: { isAdmin: true }, VMS: [] } });
function renderSidebar(collapsed, setCollapsed = () => {}) {
  return navigationHarness.render(() => navigationHarness.window.Shell.Sidebar({
    route: 'dashboard', go() {}, collapsed, setCollapsed,
  }));
}
const expandedSidebar = renderSidebar(false);
const expandedNavButtons = findAll(expandedSidebar, (node) => node.type === 'button'
  && String(node.props.className || '').includes('nav-item'));
assert.ok(expandedNavButtons.length > 1, 'sidebar destinations and collapse must be native buttons');
for (const button of expandedNavButtons) {
  assert.equal(button.props.type, 'button', 'sidebar buttons must not submit an enclosing form');
}
const activeDestination = expandedNavButtons.find((button) => textOf(button).includes('Dashboard'));
assert.ok(activeDestination, 'the active dashboard destination must render');
assert.equal(activeDestination.props['aria-current'], 'page');
const inactiveDestination = expandedNavButtons.find((button) => textOf(button).includes('Templates'));
assert.equal(inactiveDestination.props['aria-current'], undefined);
const collapseButton = expandedNavButtons.find((button) => button.props['aria-label'] === 'Collapse');
assert.ok(collapseButton, 'expanded sidebar collapse control needs an accessible name');
assert.equal(collapseButton.props.title, 'Collapse');
let nextCollapsed;
const operableCollapseButton = findAll(renderSidebar(false, (update) => {
  nextCollapsed = update(false);
}), (node) => node.type === 'button' && node.props['aria-label'] === 'Collapse')[0];
operableCollapseButton.props.onClick();
assert.equal(nextCollapsed, true, 'the native Collapse button must invoke the sidebar state change');

const collapsedSidebar = renderSidebar(true);
const expandButton = findAll(collapsedSidebar, (node) => node.type === 'button'
  && node.props['aria-label'] === 'Expand')[0];
assert.ok(expandButton, 'collapsed sidebar control must change its accessible name to Expand');
assert.equal(expandButton.props.type, 'button');
assert.equal(expandButton.props.title, 'Expand');
const collapsedDashboard = findAll(collapsedSidebar, (node) => node.type === 'button'
  && node.props['aria-current'] === 'page')[0];
assert.equal(collapsedDashboard.props['aria-label'], 'Dashboard',
  'collapsed destinations must retain an accessible name');

assert.match(appSource,
  /const \[mobileNavOpen, setMobileNavOpen\] = useState\(false\)/,
  'App must own closed-by-default mobile navigation state');
assert.equal((appSource.match(/setMobileNavOpen\(false\)/g) || []).length, 1,
  'all mobile navigation closes must converge on one close path');
assert.match(appSource, /const go = \(r, params\) => \{[\s\S]*?closeMobileNav\(false\)/,
  'every App navigation route must close the mobile drawer through that path');
assert.match(appSource, /mobileNavKeydown\(event, closeMobileNav\)/,
  'the App Escape listener must use the same close path');
assert.match(appSource, /mobileNavOpen\s*&&\s*h\(SidebarScrim/,
  'the App must render the scrim only while navigation is open');
assert.match(appSource, /requestAnimationFrame\(restore\)/,
  'focus restoration must wait until the closing render can expose the toggle');
assert.match(appSource, /mobileNavToggleRef\.current\.focus\(\)/,
  'focus-restoring closes must return keyboard focus to the top-bar toggle');

assert.equal(typeof navigationHarness.window.Shell.mobileNavKeydown, 'function',
  'shell.js must expose the Escape behavior used by App');
let escapeCloses = 0;
let escapeRestoreFocus;
navigationHarness.window.Shell.mobileNavKeydown({ key: 'Escape' }, (restoreFocus) => {
  escapeCloses += 1;
  escapeRestoreFocus = restoreFocus;
});
navigationHarness.window.Shell.mobileNavKeydown({ key: 'Enter' }, () => {
  throw new Error('non-Escape keys must not close mobile navigation');
});
assert.equal(escapeCloses, 1, 'Escape must invoke exactly one close');
assert.equal(escapeRestoreFocus, true, 'Escape close must request toggle focus restoration');

let mobileToggleDispatches = 0;
const toggleRef = { current: null };
const mobileTopBar = navigationHarness.render(() => navigationHarness.window.Shell.TopBar({
  route: 'dashboard', go() {}, theme: 'dark', setTheme() {}, openDrawer() {},
  mobileNavOpen: false,
  mobileNavToggleRef: toggleRef,
  openMobileNav() { mobileToggleDispatches += 1; },
}));
const mobileNavToggle = findAll(mobileTopBar, (node) => node.type === 'button'
  && node.props.className === 'icon-btn mobile-nav-toggle')[0];
assert.ok(mobileNavToggle, 'TopBar must render the native mobile navigation toggle');
assert.equal(mobileNavToggle.props.type, 'button');
assert.equal(mobileNavToggle.props['aria-label'], 'Open navigation');
assert.equal(mobileNavToggle.props['aria-controls'], 'primary-navigation');
assert.equal(mobileNavToggle.props['aria-expanded'], false);
assert.equal(mobileNavToggle.props.ref, toggleRef);
mobileNavToggle.props.onClick();
assert.equal(mobileToggleDispatches, 1, 'the top-bar toggle must dispatch the open path once');
const openMobileTopBar = navigationHarness.render(() => navigationHarness.window.Shell.TopBar({
  route: 'dashboard', go() {}, theme: 'dark', setTheme() {}, openDrawer() {},
  mobileNavOpen: true, mobileNavToggleRef: toggleRef, openMobileNav() {},
}));
assert.equal(findAll(openMobileTopBar, (node) => node.props.className === 'icon-btn mobile-nav-toggle')[0]
  .props['aria-expanded'], true, 'the toggle must expose the open drawer state');

function renderMobileSidebar(mobileOpen, collapsed) {
  return navigationHarness.render(() => navigationHarness.window.Shell.Sidebar({
    route: 'dashboard', go() {}, collapsed, setCollapsed() {}, mobileOpen,
  }));
}
const closedMobileSidebar = renderMobileSidebar(false, true);
assert.equal(closedMobileSidebar.props.id, 'primary-navigation');
assert.equal(closedMobileSidebar.props['aria-label'], 'Primary navigation');
assert.match(closedMobileSidebar.props.className, /\bcollapsed\b/);
assert.doesNotMatch(closedMobileSidebar.props.className, /\bmobile-open\b/);
const openMobileSidebar = renderMobileSidebar(true, true);
assert.match(openMobileSidebar.props.className, /\bmobile-open\b/);
assert.doesNotMatch(openMobileSidebar.props.className, /\bcollapsed\b/,
  'an open mobile drawer must ignore the desktop collapsed presentation');
assert.match(textOf(openMobileSidebar), /GoblinDock/,
  'an open mobile drawer must render expanded branding even when desktop is collapsed');
assert.ok(findAll(openMobileSidebar, (node) => node.type === 'div'
  && node.props.className === 'nav-label').length,
  'an open mobile drawer must render expanded group labels');

assert.equal(typeof navigationHarness.window.Shell.SidebarScrim, 'function');
let scrimCloses = 0;
const scrim = navigationHarness.render(() => navigationHarness.window.Shell.SidebarScrim({
  onClose(restoreFocus) {
    scrimCloses += 1;
    assert.equal(restoreFocus, true);
  },
}));
assert.equal(scrim.type, 'button');
assert.equal(scrim.props.type, 'button');
assert.equal(scrim.props.className, 'sidebar-scrim');
assert.equal(scrim.props['aria-label'], 'Close navigation');
assert.equal(scrim.props['aria-controls'], 'primary-navigation');
scrim.props.onClick();
assert.equal(scrimCloses, 1, 'the native scrim must dispatch one focus-restoring close');

assert.match(iconsSource, /menu\s*:/, 'the existing icon map must add a menu icon');

const keyboardBuilder = builderHarness();
assert.ok(keyboardBuilder.window.BuilderUI, 'builder.js must export window.BuilderUI');
assert.equal(typeof keyboardBuilder.window.BuilderUI.activatePlacedBlock, 'function');
const { activatePlacedBlock } = keyboardBuilder.window.BuilderUI;
let selectedBlocks = 0;
let preventedSpaces = 0;
const placedTarget = {};
activatePlacedBlock({
  type: 'keydown', key: ' ', target: placedTarget, currentTarget: placedTarget,
  preventDefault() { preventedSpaces += 1; },
}, () => { selectedBlocks += 1; });
assert.equal(selectedBlocks, 1, 'Space must invoke placed-block selection');
assert.equal(preventedSpaces, 1, 'Space must prevent page scrolling');
activatePlacedBlock({
  type: 'keydown', key: 'Enter', target: placedTarget, currentTarget: placedTarget,
  preventDefault() { throw new Error('Enter must not require preventDefault'); },
}, () => { selectedBlocks += 1; });
assert.equal(selectedBlocks, 2, 'Enter must invoke placed-block selection');
activatePlacedBlock({
  type: 'keydown', key: 'Escape', target: placedTarget, currentTarget: placedTarget,
  preventDefault() { throw new Error('irrelevant keys must not be canceled'); },
}, () => { selectedBlocks += 1; });
assert.equal(selectedBlocks, 2, 'irrelevant keys must not select a placed block');
const nestedButton = {};
activatePlacedBlock({
  type: 'click',
  target: { closest() { return nestedButton; } },
  currentTarget: { contains(candidate) { return candidate === nestedButton; } },
}, () => { selectedBlocks += 1; });
assert.equal(selectedBlocks, 2, 'nested action clicks must not bubble into block selection');

const builderTree = keyboardBuilder.render(() => keyboardBuilder.window.Builder({ go() {} }));
const paletteItem = findAll(builderTree, (node) => node.props.className === 'palette-block')[0];
assert.ok(paletteItem, 'builder palette fixture must render');
assert.equal(paletteItem.type, 'button', 'palette primary actions must be native buttons');
assert.equal(paletteItem.props.type, 'button');
assert.equal(paletteItem.props.draggable, true, 'pointer drag/drop must remain available');
assert.equal(typeof paletteItem.props.onDragStart, 'function');

const responsiveBuilder = builderHarness({ persistent: true });
let responsiveBuilderTree = responsiveBuilder.render(
  () => responsiveBuilder.window.Builder({ go() {} }),
);
function builderPanelState(tree) {
  const switcher = findAll(tree, (node) => node.props.className === 'builder-mobile-switcher')[0];
  assert.ok(switcher, 'builder must render the narrow panel switcher');
  assert.equal(switcher.props.role, 'tablist');
  assert.equal(switcher.props['aria-label'], 'Builder panels');
  const tabs = findAll(switcher, (node) => node.type === 'button');
  assert.deepEqual(tabs.map(textOf), ['Palette', 'Canvas', 'Inspector']);
  for (const tab of tabs) {
    assert.equal(tab.props.type, 'button');
    assert.equal(tab.props.role, 'tab');
    assert.ok(tab.props['aria-controls']);
    assert.equal(typeof tab.props['aria-selected'], 'boolean');
  }
  const panes = ['builder-palette', 'builder-canvas', 'builder-inspector'].map((className) => {
    const pane = findAll(tree, (node) => String(node.props.className || '').split(' ').includes(className))[0];
    assert.ok(pane, `missing mounted ${className} pane`);
    assert.equal(pane.props.role, 'tabpanel');
    assert.ok(pane.props['aria-labelledby']);
    return pane;
  });
  return { switcher, tabs, panes };
}
let responsivePanelState = builderPanelState(responsiveBuilderTree);
assert.deepEqual(responsivePanelState.tabs.map((tab) => tab.props['aria-selected']),
  [false, true, false], 'the narrow builder must start on Canvas');
assert.deepEqual(responsivePanelState.panes.map((pane) => /\bmobile-active\b/.test(pane.props.className)),
  [false, true, false]);
responsivePanelState.tabs[0].props.onClick();
responsiveBuilderTree = responsiveBuilder.render(() => responsiveBuilder.window.Builder({ go() {} }));
responsivePanelState = builderPanelState(responsiveBuilderTree);
assert.deepEqual(responsivePanelState.tabs.map((tab) => tab.props['aria-selected']),
  [true, false, false], 'the Palette tab must update the shared mobilePanel state');
assert.deepEqual(responsivePanelState.panes.map((pane) => /\bmobile-active\b/.test(pane.props.className)),
  [true, false, false], 'all panes stay mounted while only Palette becomes active');
const builderWorkspace = findAll(responsiveBuilderTree,
  (node) => node.props.className === 'builder-workspace')[0];
assert.ok(builderWorkspace, 'builder panes must share the structural workspace');
assert.ok(findAll(responsiveBuilderTree,
  (node) => node.props.className === 'builder-bar builder-header').length,
  'builder header needs its responsive structural class');
assert.ok(findAll(responsiveBuilderTree,
  (node) => node.props.className === 'row builder-actions').length,
  'builder actions need their responsive wrapping class');
assert.equal(findAll(responsiveBuilderTree, (node) => node.type === 'button'
  && /Save (template|changes)/.test(textOf(node))).length, 1,
  'builder Save must remain rendered outside the switchable panes');

const placedBlock = findAll(builderTree, (node) => String(node.props.className || '').includes('placed-block'))[0];
assert.ok(placedBlock, 'placed builder fixture must render');
assert.equal(placedBlock.type, 'div', 'placed blocks must not wrap nested actions in a native button');
assert.equal(placedBlock.props.role, 'button');
assert.equal(placedBlock.props.tabIndex, 0);
assert.equal(placedBlock.props['aria-pressed'], false);
assert.equal(typeof placedBlock.props.onClick, 'function');
assert.equal(typeof placedBlock.props.onKeyDown, 'function');
const renderedTarget = {};
let renderedSpacePrevented = 0;
const changesBeforeKeyboard = keyboardBuilder.stateChanges.length;
placedBlock.props.onKeyDown({
  type: 'keydown', key: ' ', target: renderedTarget, currentTarget: renderedTarget,
  preventDefault() { renderedSpacePrevented += 1; },
});
assert.equal(keyboardBuilder.stateChanges.length, changesBeforeKeyboard + 1,
  'the rendered placed block must wire Space to its selection state');
assert.match(keyboardBuilder.stateChanges.at(-1).value, /^u\d+$/);
assert.equal(renderedSpacePrevented, 1,
  'the rendered placed block must prevent page scrolling for Space');
const placedActions = findAll(placedBlock, (node) => node.type === 'button');
assert.deepEqual(placedActions.map((button) => button.props['aria-label']), [
  'Move Install packages down', 'Move Install packages up',
  'Duplicate Install packages', 'Remove Install packages',
]);
assert.ok(placedActions.every((button) => button.props.type === 'button'));
const moveDown = placedActions[0];
assert.equal(moveDown.props.title, 'Move down');
let moveDownPropagationStops = 0;
const changesBeforeMoveDown = keyboardBuilder.stateChanges.length;
moveDown.props.onClick({ stopPropagation() { moveDownPropagationStops += 1; } });
const moveDownChanges = keyboardBuilder.stateChanges.slice(changesBeforeMoveDown);
assert.equal(moveDownPropagationStops, 1, 'Move down must not bubble into block selection');
assert.equal(moveDownChanges.length, 1, 'Move down must dispatch exactly one state change');
assert.equal(moveDownChanges[0].index, 0,
  'Move down must update sections without dispatching parent selection state');
const placedUid = placedBlock.props.key;
const reordered = moveDownChanges[0].value([{
  id: 's-inst', blocks: [{ uid: placedUid }, { uid: 'following-block' }],
}]);
assert.deepEqual(reordered[0].blocks.map((block) => block.uid), ['following-block', placedUid],
  'Move down must dispatch onMove with direction +1');
const nestedChangesBefore = keyboardBuilder.stateChanges.length;
const nestedActionTarget = { closest() { return placedActions[0]; } };
placedBlock.props.onClick({
  type: 'click', target: nestedActionTarget,
  currentTarget: { contains(candidate) { return candidate === placedActions[0]; } },
});
assert.equal(keyboardBuilder.stateChanges.length, nestedChangesBefore,
  'the rendered placed block must ignore click bubbling from nested action buttons');
const placedGrip = findAll(placedBlock, (node) => node.props.className === 'pb-grip')[0];
assert.equal(placedGrip, moveDown, 'the existing placed-block grip must remain the Move down control');
const dropzone = findAll(builderTree, (node) => String(node.props.className || '').includes('dropzone'))[0];
assert.equal(typeof dropzone.props.onDragOver, 'function');
assert.equal(typeof dropzone.props.onDrop, 'function',
  'the canvas drop target must remain wired for pointer drag/drop');

const codeField = keyboardBuilder.render(() => keyboardBuilder.window.SchemaField({
  field: { type: 'code', label: 'Install script', name: 'script' },
  value: '', onChange() {},
}));
const codePreview = findAll(codeField, (node) => node.type === 'div'
  && node.children.some((child) => child && child.type === 'pre'))[0];
assert.ok(codePreview, 'code preview fixture must render');
assert.equal(typeof codePreview.props.onClick, 'function',
  'the existing pointer-sized code preview target must remain available');
assert.equal(textOf(findAll(codePreview, (node) => node.type === 'pre')[0]),
  '# empty — click or use Open editor', 'empty preview copy must describe both controls');
const openEditor = findAll(codePreview, (node) => node.type === 'button'
  && textOf(node) === 'Open editor')[0];
assert.ok(openEditor);
assert.equal(openEditor.props.type, 'button');

const passwordField = keyboardBuilder.render(() => keyboardBuilder.window.SchemaField({
  field: { type: 'password', label: 'Root password', name: 'password' },
  value: '', onChange() {},
}));
const showPassword = findAll(passwordField, (node) => node.type === 'button')[0];
assert.equal(showPassword.props.type, 'button');
assert.equal(showPassword.props['aria-label'], 'Show password');

const customBlockEditor = keyboardBuilder.render(() => keyboardBuilder.window.BlockEditorModal({
  initial: { name: 'Custom', schema: [{ name: 'token', type: 'secret', default: '' }] },
  onClose() {}, onSaved() {},
}));
const removeInput = findAll(customBlockEditor, (node) => node.type === 'button'
  && String(node.props.className || '').includes('danger'))[0];
assert.ok(removeInput, 'custom block input removal control must render');
assert.equal(removeInput.props.type, 'button');
assert.equal(removeInput.props['aria-label'], 'Remove token input');

assert.match(cssRule('.nav-item:focus-visible'), /outline|box-shadow/,
  'sidebar controls need a visible keyboard focus indicator');
assert.match(cssRule('.palette-block:focus-visible'), /outline|box-shadow/,
  'palette buttons need a visible keyboard focus indicator');
assert.match(cssRule('.placed-block:focus-visible'), /outline|box-shadow/,
  'placed blocks need a visible keyboard focus indicator');
assert.match(cssRule('.placed-block:focus-within .pb-actions'), /opacity\s*:\s*1\s*;/,
  'nested block actions must remain visible while focus is within the block');

function persistentManageHarness() {
  const state = [];
  let cursor = 0;
  const HarnessReact = {
    createElement: React.createElement,
    Fragment: 'fragment',
    useState(initial) {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
    useEffect() {},
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) { return HarnessReact.createElement('span', { 'data-icon': props.name }); },
    GD: {
      me: { isAdmin: true }, CONNECTIONS: [], NETWORKS: [], USERS: [],
      SECRETS: [], VARIABLES: [], PALETTE: [],
    },
    GDStore: { refresh() { return Promise.resolve(); }, toast() {} },
    API: {},
    UI: {
      Menu() {}, ConfirmModal() {},
      FormModal(props) {
        return HarnessReact.createElement('form', { 'aria-label': props.title }, props.children);
      },
      Field(props) { return HarnessReact.createElement('input', { 'aria-label': props.label }); },
      TextArea(props) { return HarnessReact.createElement('textarea', { 'aria-label': props.label }); },
      SelectField(props) { return HarnessReact.createElement('select', { 'aria-label': props.label }); },
      Toggle(props) { return HarnessReact.createElement('button', { type: 'button' }, props.label); },
      fmtBytes(value) { return String(value); },
      useFetched() { return null; },
    },
  };
  vm.runInNewContext(source, { React: HarnessReact, window: harnessWindow }, {
    filename: 'web/manage.js',
  });
  return {
    window: harnessWindow,
    render() {
      cursor = 0;
      return resolveTree(harnessWindow.Settings());
    },
  };
}

const settingsHarness = persistentManageHarness();
let settingsTree = settingsHarness.render();
let settingsSelector = findAll(settingsTree,
  (node) => node.props.className === 'seg settings-section-selector')[0];
assert.ok(settingsSelector, 'Settings needs one narrow-safe six-section selector');
let settingsTabs = findAll(settingsSelector, (node) => node.type === 'button');
assert.equal(settingsTabs.length, 6);
assert.equal(settingsTabs[0].props.className, 'active',
  'Settings must keep connections as its default section');
assert.ok(findAll(settingsTree, (node) => node.type === 'button'
  && textOf(node) === 'Add connection').length,
  'the default Settings section must retain the easy Add connection path');
settingsTabs.find((button) => textOf(button) === 'Networks').props.onClick();
settingsTree = settingsHarness.render();
settingsSelector = findAll(settingsTree,
  (node) => node.props.className === 'seg settings-section-selector')[0];
settingsTabs = findAll(settingsSelector, (node) => node.type === 'button');
assert.equal(settingsTabs.find((button) => textOf(button) === 'Networks').props.className, 'active',
  'the narrow selector must update the same Settings section state');
assert.match(textOf(settingsTree), /Per-connection networks/);

const connectionHarness = persistentManageHarness();
let connectionTree = connectionHarness.render();
findAll(connectionTree, (node) => node.type === 'button'
  && textOf(node) === 'Add connection')[0].props.onClick();
connectionTree = connectionHarness.render();
assert.ok(findAll(connectionTree,
  (node) => node.props.className === 'connection-form-grid').length,
  'the connection modal main grid needs a responsive class');
assert.ok(findAll(connectionTree,
  (node) => node.props.className === 'connection-limit-grid').length,
  'the connection modal limit grid needs a responsive class');

const tableSections = [
  ['Dashboard', sourceSection(dashboardSource, 'function TableView(', 'function CardView(')],
  ['Secrets', sourceSection(source, 'function Secrets(', 'function VarModal(')],
  ['Variables', sourceSection(source, 'function Variables(', 'function Settings(')],
  ['Networks', sourceSection(source, 'function Networks(', 'function UserModal(')],
  ['Users', sourceSection(source, 'function Users(', 'function AuditLog(')],
  ['Audit', sourceSection(source, 'function AuditLog(', 'function Backups(')],
  ['Backups', sourceSection(source, 'function Backups(', 'function Preferences(')],
];
for (const [name, section] of tableSections) {
  assert.ok(section.includes("className: 'table-scroll'"),
    `${name} table must scroll inside its card`);
  assert.ok(section.indexOf("className: 'table-scroll'") < section.indexOf("h('table'"),
    `${name} table-scroll must wrap, not follow, the table`);
}

const dashboardHarnessReact = {
  createElement: React.createElement,
  useState(initial) { return [typeof initial === 'function' ? initial() : initial, () => {}]; },
};
const dashboardWindow = {
  React: dashboardHarnessReact,
  Icon(props) { return dashboardHarnessReact.createElement('span', { 'data-icon': props.name }); },
  GD: { VMS: [], CONNECTIONS: [], me: { isAdmin: false } },
  GDStore: { vmHistory() { return []; }, refresh() { return Promise.resolve(); }, toast() {} },
  API: {},
  UI: {
    OSGlyph() {}, StatusBadge() {}, CopyField() {}, Meter() {}, Sparkline() {}, Menu() {},
    ConfirmModal() {}, FormModal() {}, Field() {}, useFetched() { return null; },
  },
};
vm.runInNewContext(dashboardSource, {
  React: dashboardHarnessReact,
  window: dashboardWindow,
  localStorage: { getItem() { return null; }, setItem() {} },
  setTimeout() {},
}, { filename: 'web/dashboard.js' });
const dashboardTree = resolveTree(dashboardWindow.Dashboard({ go() {} }));
assert.equal(findAll(dashboardTree, (node) => node.type === 'button'
  && textOf(node) === 'Deploy VM').length, 1,
  'Dashboard Deploy must remain rendered on the narrow-capable page');

function lifecycleDashboardHarness(vms) {
  const state = [];
  let cursor = 0;
  const apiCalls = [];
  const HarnessReact = {
    createElement: React.createElement,
    Fragment: 'fragment',
    useState(initial) {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) {
        state[index] = typeof initial === 'function' ? initial() : initial;
      }
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) { return HarnessReact.createElement('span', { 'data-icon': props.name }); },
    GD: { VMS: vms, CONNECTIONS: [], me: { isAdmin: false } },
    GDStore: {
      vmHistory() { return []; },
      refresh() { return Promise.resolve(); },
      toast() {},
      vmAction(depId, action) { apiCalls.push({ kind: 'store-action', depId, action }); return Promise.resolve(); },
    },
    API: {
      vmAction(depId, action) { apiCalls.push({ kind: 'action', depId, action }); return Promise.resolve(); },
      vmDestroy(depId) { apiCalls.push({ kind: 'destroy', depId }); return Promise.resolve({ jobId: depId }); },
    },
    UI: {
      isVmLifecycleLocked: window.UI.isVmLifecycleLocked,
      OSGlyph(props) { return HarnessReact.createElement('span', null, props.os); },
      StatusBadge(props) { return HarnessReact.createElement('span', { className: 'badge ' + props.status }, props.status); },
      CopyField(props) { return HarnessReact.createElement('span', null, props.value); },
      Meter() {}, Sparkline() {}, ConfirmModal() {}, FormModal() {}, Field() {},
      Menu(props) {
        return HarnessReact.createElement('div', { 'data-vm-menu': true }, props.children,
          (props.items || []).filter((item) => !item.sep).map((item) => HarnessReact.createElement(
            'button', { key: item.label, onClick: item.onClick }, item.label,
          )));
      },
      useFetched() { return null; },
    },
  };
  vm.runInNewContext(dashboardSource, {
    React: HarnessReact,
    window: harnessWindow,
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout() {},
  }, { filename: 'web/dashboard.js' });
  return {
    window: harnessWindow,
    apiCalls,
    state,
    render() {
      cursor = 0;
      return resolveTree(harnessWindow.Dashboard({ go() {} }));
    },
  };
}

function vmFixture(depId, name, status) {
  return {
    id: `vm-${depId}`, depId, name, status, owner: 'you', ownerName: 'Owner',
    ip: `10.39.0.${depId}`, image: 'Ubuntu', template: 'Base', templateId: 39,
    conn: 'pve', os: 'ubuntu', cpu: 1, ram: 2, uptime: '1h', tags: '', notes: '',
    ...(status === 'cleanup_pending' ? { err: 'cleanup failed exactly' } : {}),
  };
}

const lifecycleVms = [
  vmFixture(1, 'Running eligible', 'running'),
  vmFixture(2, 'Stopped eligible', 'stopped'),
  vmFixture(3, 'Working locked', 'working'),
  vmFixture(4, 'Cleanup locked', 'cleanup_pending'),
];
const lifecycleDashboard = lifecycleDashboardHarness(lifecycleVms);
let lifecycleTable = lifecycleDashboard.render();
function vmSurface(tree, name, type) {
  return findAll(tree, (node) => node.type === type && textOf(node).includes(name))[0];
}
for (const name of ['Working locked', 'Cleanup locked']) {
  const row = vmSurface(lifecycleTable, name, 'tr');
  assert.ok(row, `missing ${name} table row`);
  assert.equal(findAll(row, (node) => node.type === 'input' && node.props.type === 'checkbox').length, 0,
    `${name} row must not be selectable`);
  assert.equal(findAll(row, (node) => node.props['data-vm-menu'] === true).length, 0,
    `${name} row must not expose the lifecycle action menu`);
  assert.equal(findAll(row, (node) => ['Start', 'Stop', 'Restart'].includes(node.props.title)).length, 0,
    `${name} row must not expose direct lifecycle actions`);
}
for (const name of ['Running eligible', 'Stopped eligible']) {
  const row = vmSurface(lifecycleTable, name, 'tr');
  assert.equal(findAll(row, (node) => node.type === 'input' && node.props.type === 'checkbox').length, 1,
    `${name} row must remain selectable`);
  assert.equal(findAll(row, (node) => node.props['data-vm-menu'] === true).length, 1,
    `${name} row must retain its action menu`);
}

const selectAll = findAll(lifecycleTable, (node) => node.type === 'input'
  && node.props.title === 'Select all')[0];
selectAll.props.onChange({ stopPropagation() {} });
lifecycleTable = lifecycleDashboard.render();
assert.match(textOf(lifecycleTable), /2 selected/,
  'select-all and selected count must include only currently unlocked VMs');
assert.deepEqual([...lifecycleDashboard.state[8]].sort(), [1, 2],
  'select-all must never add lifecycle-locked deployment IDs');

findAll(lifecycleTable, (node) => node.type === 'button' && textOf(node) === 'Cards')[0].props.onClick();
const lifecycleCards = lifecycleDashboard.render();
for (const name of ['Working locked', 'Cleanup locked']) {
  const card = findAll(lifecycleCards, (node) => node.type === 'div'
    && String(node.props.className || '').includes('card card-pad') && textOf(node).includes(name))[0];
  assert.ok(card, `missing ${name} card`);
  assert.equal(findAll(card, (node) => node.type === 'input' && node.props.type === 'checkbox').length, 0,
    `${name} card must not be selectable`);
  assert.equal(findAll(card, (node) => node.props['data-vm-menu'] === true).length, 0,
    `${name} card must not expose the lifecycle action menu`);
  assert.equal(findAll(card, (node) => ['Start', 'Stop', 'Restart'].includes(node.props.title)).length, 0,
    `${name} card must not expose direct lifecycle actions`);
}

// Simulate state refreshing after render: the second selected VM becomes locked before
// the already-rendered bulk Start button is invoked. Submission must filter it again.
lifecycleVms[1].status = 'working';
const refreshedLifecycleCards = lifecycleDashboard.render();
assert.match(textOf(refreshedLifecycleCards), /1 selected/,
  'selected count must discard a VM that became lifecycle-locked after selection');
const staleBulkStart = findAll(refreshedLifecycleCards, (node) => node.type === 'button'
  && textOf(node) === 'Start')[0];
staleBulkStart.props.onClick();
assert.deepEqual(lifecycleDashboard.apiCalls.filter((call) => call.kind === 'action'), [
  { kind: 'action', depId: 1, action: 'start' },
], 'bulk submission must re-filter mixed/stale selection against current VM state');

function lifecycleDetailHarness(detail) {
  const state = [detail];
  let cursor = 0;
  const HarnessReact = {
    createElement: React.createElement,
    Fragment: 'fragment',
    useState(initial) {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
    useEffect() {},
    useRef(initial) { return { current: initial }; },
  };
  const harnessWindow = {
    React: HarnessReact,
    Icon(props) { return HarnessReact.createElement('span', { 'data-icon': props.name }); },
    GDStore: { nav: { depId: detail.depId }, refresh() { return Promise.resolve(); }, toast() {} },
    API: { job() { return Promise.resolve({ log: [] }); } },
    UI: {
      isVmLifecycleLocked: window.UI.isVmLifecycleLocked,
      OSGlyph() {}, ConfirmModal() {}, StatusBadge() {}, FormModal() {}, Field() {},
      Toggle() {},
      Menu(props) {
        return HarnessReact.createElement('div', { 'data-snapshot-menu': true }, props.children,
          (props.items || []).filter((item) => !item.sep).map((item) => HarnessReact.createElement(
            'button', { key: item.label, onClick: item.onClick }, item.label,
          )));
      },
      copyToClipboard() {}, readClipboard() {},
      fmtBytes(value) { return String(value); },
      useFetched() {
        return { snapshots: [{
          name: 'visible-snapshot', description: 'safe to view', created: 'now',
          vmstate: false, current: true,
        }] };
      },
    },
  };
  vm.runInNewContext(vmDetailSource, {
    React: HarnessReact,
    window: harnessWindow,
    setTimeout() {}, setInterval() {}, clearInterval() {},
  }, { filename: 'web/vmdetail.js' });
  cursor = 0;
  return resolveTree(harnessWindow.VmDetail({ go() {} }));
}

function detailFixture(status, live, err) {
  return {
    depId: 39, name: `${status} detail`, status, err, vmid: 9039, node: 'pve', ip: '10.39.0.39',
    os: 'ubuntu', tags: '', owner: 'Owner', connection: 'pve', baseImage: 'Ubuntu',
    template: 'Base', reqCpu: 2, reqRam: 4, reqDisk: 20, live: live || null,
    config: {}, agent: null, consoleReady: !!(live && live.status === 'running'),
    hasRootPassword: false, jobId: null,
  };
}

for (const [status, label, exactError] of [
  ['working', 'Working', undefined],
  ['cleanup_pending', 'Cleanup pending', 'cleanup ownership could not be confirmed exactly'],
]) {
  const tree = lifecycleDetailHarness(detailFixture(status, null, exactError));
  assert.ok(textOf(tree).includes(label), `${status} detail must render ${label}`);
  if (exactError) assert.ok(textOf(tree).includes(exactError), 'cleanup detail must render its exact error');
  const buttons = findAll(tree, (node) => node.type === 'button');
  assert.equal(buttons.some((button) => ['Start', 'Stop', 'Restart', 'Rebuild'].includes(textOf(button))), false,
    `${status} detail must not render lifecycle controls`);
  assert.equal(buttons.some((button) => button.props['aria-label'] === 'Delete VM'), false,
    `${status} detail must not render Delete VM`);
  assert.equal(buttons.some((button) => textOf(button) === 'Console'), false,
    `${status} detail must not expose console launch`);
  assert.ok(textOf(tree).includes('visible-snapshot'),
    `${status} detail must keep snapshot viewing available`);
  assert.equal(buttons.some((button) => ['Take snapshot', 'Roll back', 'Delete'].includes(textOf(button))), false,
    `${status} detail must not render snapshot mutation controls`);
}

const runningDetail = lifecycleDetailHarness(detailFixture('running', {
  status: 'running', uptime: 60, cpuPct: 5, memUsed: 1, memMax: 2, diskUsed: 1, diskMax: 2,
}));
assert.ok(findAll(runningDetail, (node) => node.type === 'button' && textOf(node) === 'Stop').length,
  'ordinary running detail must retain Stop');
assert.ok(findAll(runningDetail, (node) => node.type === 'button' && textOf(node) === 'Restart').length,
  'ordinary running detail must retain Restart');
assert.ok(findAll(runningDetail, (node) => node.type === 'button' && textOf(node) === 'Console').length,
  'ordinary running detail must retain Console');
assert.ok(findAll(runningDetail, (node) => node.type === 'button'
  && node.props['aria-label'] === 'Delete VM').length,
  'ordinary running detail must retain Delete VM');
for (const label of ['Take snapshot', 'Roll back', 'Delete']) {
  assert.ok(findAll(runningDetail, (node) => node.type === 'button' && textOf(node) === label).length,
    `ordinary running detail must retain snapshot ${label}`);
}
const stoppedDetail = lifecycleDetailHarness(detailFixture('stopped', { status: 'stopped' }));
assert.ok(findAll(stoppedDetail, (node) => node.type === 'button' && textOf(node) === 'Start').length,
  'ordinary stopped detail must retain Start');
assert.ok(findAll(stoppedDetail, (node) => node.type === 'button' && textOf(node) === 'Restart').length,
  'ordinary stopped detail must retain Restart');
assert.ok(findAll(stoppedDetail, (node) => node.type === 'button' && textOf(node) === 'Console').length,
  'ordinary stopped detail must retain Console');
assert.ok(findAll(stoppedDetail, (node) => node.type === 'button'
  && node.props['aria-label'] === 'Delete VM').length,
  'ordinary stopped detail must retain Delete VM');
for (const label of ['Take snapshot', 'Roll back', 'Delete']) {
  assert.ok(findAll(stoppedDetail, (node) => node.type === 'button' && textOf(node) === label).length,
    `ordinary stopped detail must retain snapshot ${label}`);
}

assert.equal(typeof window.UI.isVmLifecycleLocked, 'function',
  'the UI must expose one shared lifecycle-lock predicate');
assert.equal(window.UI.isVmLifecycleLocked({ status: 'working' }), true);
assert.equal(window.UI.isVmLifecycleLocked({ status: 'cleanup_pending' }), true);
assert.equal(window.UI.isVmLifecycleLocked({ status: 'running' }), false);
assert.equal(window.UI.isVmLifecycleLocked({ status: 'stopped' }), false);

assert.match(vmDetailSource, /className:\s*'row vm-detail-actions'/,
  'VM detail actions need their responsive structural class');
assert.match(vmDetailSource, /className:\s*'vm-detail-columns'/,
  'VM detail columns need their responsive structural class');
assert.match(vmDetailSource,
  /type:\s*'button',[^}]*className:\s*'btn danger sm'[^}]*'aria-label':\s*'Delete VM'/,
  'Delete VM must be a named native non-submitting control');

const mobileMediaMatches = [...stylesSource.matchAll(/@media\s*\(max-width:\s*760px\)/g)];
assert.equal(mobileMediaMatches.length, 1, 'keep one effective 760px cascade');
const mobileMediaIndex = mobileMediaMatches[0].index;
const reducedMotionIndex = stylesSource.indexOf('@media (prefers-reduced-motion: reduce)');
assert.ok(mobileMediaIndex > stylesSource.indexOf('.placed-block'),
  'the 760px cascade must follow affected builder base rules');
assert.ok(mobileMediaIndex > stylesSource.indexOf('.page-head'),
  'the 760px cascade must follow affected page-heading base rules');
assert.ok(reducedMotionIndex > mobileMediaIndex,
  'reduced-motion overrides must follow the responsive and base rules');
const mobileCss = stylesSource.slice(mobileMediaIndex, reducedMotionIndex);
assert.match(cssRule('.table-scroll'), /overflow-x\s*:\s*auto\s*;/,
  'tables must scroll horizontally inside their wrappers');
assert.match(mobileCss, /\.sidebar\s*\{[^}]*display\s*:\s*none\s*;/s,
  'closed mobile navigation must be removed from focus order with display:none');
assert.match(mobileCss, /\.sidebar\.mobile-open\s*\{[^}]*display\s*:\s*flex\s*;/s);
assert.doesNotMatch(mobileCss, /\.sidebar\s*\{[^}]*(?:transform|opacity)\s*:/s,
  'mobile navigation hiding must not depend on transform or opacity');
assert.match(mobileCss,
  /\.builder-palette\s*,\s*\.builder-canvas\s*,\s*\.builder-inspector\s*\{[^}]*display\s*:\s*none\s*;/s,
  'inactive mounted builder panes must use display:none on narrow screens');
assert.match(mobileCss, /\.bpane\.mobile-active\s*\{[^}]*display\s*:\s*flex\s*;/s);
assert.doesNotMatch(mobileCss,
  /\.builder-palette\s*,\s*\.builder-canvas\s*,\s*\.builder-inspector\s*\{[^}]*(?:transform|opacity)\s*:/s,
  'inactive builder panes must not rely on transform or opacity hiding');
assert.match(mobileCss, /\.sidebar-foot\s*\{[^}]*display\s*:\s*none\s*;/s,
  'desktop collapse footer must disappear on narrow screens');
assert.match(mobileCss, /\.vm-detail-columns\s*\{[^}]*grid-template-columns\s*:\s*1fr/s);
assert.match(mobileCss, /\.connection-form-grid\s*,\s*\.connection-limit-grid\s*\{[^}]*grid-template-columns\s*:\s*1fr/s);
const reducedMotionCss = stylesSource.slice(reducedMotionIndex);
assert.match(reducedMotionCss, /animation-duration\s*:\s*0\.01ms\s*!important\s*;/);
assert.match(reducedMotionCss, /transition-duration\s*:\s*0\.01ms\s*!important\s*;/);
assert.match(reducedMotionCss, /\[style\*=["']animation["']\][^{]*\{[^}]*animation\s*:\s*none\s*!important\s*;/s,
  'reduced motion must override inline animation declarations');

function renderedJobState(rawStatus, status, title, error) {
  const fixture = jobDetailFixture(rawStatus, status, title, error);
  const harness = jobSurfaceHarness({ stateValues: [fixture, 'checklist', true, false] });
  harness.window.GDStore.nav = { jobId: fixture.id };
  const tree = harness.render(() => harness.window.JobProgress({ go() {} }));
  return {
    tree,
    badge: findAll(tree, (node) => node.type === 'span'
      && String(node.props.className || '').startsWith('badge '))[0],
    meter: findAll(tree, (node) => node.type === 'div'
      && String(node.props.className || '').startsWith('meter'))[0],
    percentage: findAll(tree, (node) => node.type === 'span' && textOf(node) === '67%')[0],
    failureBanners: findAll(tree, (node) => node.type === 'div'
      && node.props.style && node.props.style.background === 'var(--err-ghost)'),
  };
}

const canceledJob = renderedJobState(
  'canceled', 'canceled', 'Canceled detail job', 'cancellation is not a failure banner',
);
assert.equal(textOf(canceledJob.badge), 'Canceled');
assert.equal(canceledJob.badge.props.className, 'badge canceled');
assert.ok(findAll(canceledJob.badge, (node) => node.props.className === 'dot stopped').length);
assert.equal(canceledJob.meter.props.className, 'meter');
assert.equal(canceledJob.percentage.props.style.color, 'var(--accent)');
assert.equal(canceledJob.failureBanners.length, 0,
  'a canceled detail must not render a failure banner even if legacy error text exists');

const failedJob = renderedJobState(
  'failed', 'error', 'Failed detail job', 'provisioning failed visibly',
);
assert.equal(textOf(failedJob.badge), 'Failed');
assert.equal(failedJob.badge.props.className, 'badge error');
assert.ok(findAll(failedJob.badge, (node) => node.props.className === 'dot error').length);
assert.equal(failedJob.meter.props.className, 'meter err');
assert.equal(failedJob.percentage.props.style.color, 'var(--err)');
assert.equal(failedJob.failureBanners.length, 1);
assert.match(textOf(failedJob.failureBanners[0]), /provisioning failed visibly/);

const historyFixtures = [
  {
    id: 'j-canceled-history', jobId: 3901, title: 'Canceled history job', type: 'deploy',
    rawStatus: 'canceled', status: 'canceled', pct: 41, phase: 'Canceled', elapsed: '7s',
  },
  {
    id: 'j-failed-history', jobId: 3902, title: 'Failed history job', type: 'deploy',
    rawStatus: 'failed', status: 'error', pct: 42, phase: 'Failed', elapsed: '8s',
  },
];
const historyHarness = jobSurfaceHarness({
  stateValues: [historyFixtures, null, null, null, false],
  gd: { me: { isAdmin: false } },
});
const historyTree = historyHarness.render(() => historyHarness.window.History());
function historyRow(title) {
  return findAll(historyTree, (node) => node.type === 'div'
    && node.props.style && node.props.style.borderBottom
    && textOf(node).includes(title))[0];
}
const canceledHistory = historyRow('Canceled history job');
assert.ok(canceledHistory);
assert.ok(findAll(canceledHistory, (node) => node.props.className === 'dot stopped').length);
assert.ok(findAll(canceledHistory, (node) => node.props.className === 'badge canceled'
  && textOf(node) === 'Canceled').length);
const failedHistory = historyRow('Failed history job');
assert.ok(failedHistory);
assert.ok(findAll(failedHistory, (node) => node.props.className === 'dot error').length);
assert.ok(findAll(failedHistory, (node) => node.props.className === 'badge error'
  && textOf(node) === 'Failed').length);
assert.equal(findAll(canceledHistory, (node) => node.type === 'button')[0].props['aria-label'],
  'Purge Canceled history job permanently');

const activityFixtures = [
  {
    id: 'j-canceled-activity', jobId: 3903, title: 'Canceled activity job', type: 'deploy',
    rawStatus: 'canceled', status: 'canceled', pct: 51, phase: 'Canceled', elapsed: '9s',
  },
  {
    id: 'j-failed-activity', jobId: 3904, title: 'Failed activity job', type: 'deploy',
    rawStatus: 'failed', status: 'error', pct: 52, phase: 'Failed', elapsed: '10s',
  },
];
const activityHarness = jobSurfaceHarness({ gd: { JOBS: activityFixtures } });
const activityTree = activityHarness.render(
  () => activityHarness.window.Shell.ActivityDrawer({ onClose() {}, go() {} }),
);
function activityCard(title) {
  return findAll(activityTree, (node) => String(node.props.className || '').includes('card')
    && textOf(node).includes(title))[0];
}
const canceledActivity = activityCard('Canceled activity job');
assert.ok(canceledActivity);
assert.ok(findAll(canceledActivity, (node) => node.props.className === 'dot stopped').length);
assert.ok(findAll(canceledActivity, (node) => node.props.className === 'badge canceled'
  && textOf(node) === 'Canceled').length);
assert.equal(findAll(canceledActivity, (node) => node.type === 'div'
  && String(node.props.className || '').startsWith('meter'))[0].props.className, 'meter');
const canceledDismiss = findAll(canceledActivity, (node) => node.type === 'button')[0];
assert.equal(canceledDismiss.props['aria-label'], 'Dismiss Canceled activity job');

const failedActivity = activityCard('Failed activity job');
assert.ok(failedActivity);
assert.ok(findAll(failedActivity, (node) => node.props.className === 'dot error').length);
assert.ok(findAll(failedActivity, (node) => node.props.className === 'badge error'
  && textOf(node) === 'Failed').length);
assert.equal(findAll(failedActivity, (node) => node.type === 'div'
  && String(node.props.className || '').startsWith('meter'))[0].props.className, 'meter err');

assert.equal(typeof activityHarness.window.UI.jobPresentation, 'function');
const presentation = (rawStatus) => JSON.parse(JSON.stringify(
  activityHarness.window.UI.jobPresentation(rawStatus),
));
assert.deepEqual(presentation('canceled'), {
  label: 'Canceled', badgeClass: 'canceled', dotClass: 'stopped', failure: false,
});
assert.deepEqual(presentation('failed'), {
  label: 'Failed', badgeClass: 'error', dotClass: 'error', failure: true,
});
assert.deepEqual(presentation('succeeded'), {
  label: 'Done', badgeClass: 'running', dotClass: 'running', failure: false,
});
for (const rawStatus of ['queued', 'running', 'waiting']) {
  assert.deepEqual(presentation(rawStatus), {
    label: 'Working', badgeClass: 'working', dotClass: 'working', failure: false,
  });
}
const canceledBadgeCss = cssRule('.badge.canceled');
assert.match(canceledBadgeCss, /background\s*:\s*var\(--surface-2\)\s*;/);
assert.match(canceledBadgeCss, /color\s*:\s*var\(--text-faint\)\s*;/);
assert.equal(/--err|--warn|gradient|animation/.test(canceledBadgeCss), false,
  'the canceled badge must remain subdued and static');

function statefulReact(seed = []) {
  const state = seed.slice();
  let cursor = 0;
  return {
    state,
    begin() { cursor = 0; },
    createElement: React.createElement,
    Fragment: 'fragment',
    useState(initial) {
      const index = cursor++;
      if (!Object.hasOwn(state, index)) {
        state[index] = typeof initial === 'function' ? initial() : initial;
      }
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
    useId() { return `:wave46-${cursor++}:`; },
    useRef(initial) { return { current: initial }; },
    useEffect() {},
  };
}

function wave46Icon(props) {
  return React.createElement('span', { 'data-icon': props.name });
}

async function testWave46Contracts() {
  const originalPalette = window.GD.PALETTE;
  window.GD.PALETTE = [];
  const foreignTemplate = {
    recipe: [{ blocks: [{
      ref: 'c-author-private', name: 'Author private block',
      inputs: { hostname: 'author-host', api_token: '********' },
      ask: ['hostname', 'api_token'],
      askSchema: [
        { name: 'hostname', type: 'text', label: 'Host name' },
        { name: 'api_token', type: 'secret', label: 'API token' },
      ],
    }] }],
  };
  const foreignAsks = window.UI.collectAsks(foreignTemplate);
  assert.deepEqual(JSON.parse(JSON.stringify(foreignAsks.map((ask) => ({
    addr: ask.addr, name: ask.field.name, type: ask.field.type, def: ask.def,
  })))), [
    { addr: '0.0', name: 'hostname', type: 'text', def: 'author-host' },
    { addr: '0.0', name: 'api_token', type: 'secret', def: '' },
  ], 'a public template must carry prompts for a custom block absent from the viewer palette');
  assert.deepEqual(JSON.parse(JSON.stringify(window.UI.initAskAnswers(foreignAsks))), {
    '0.0': { hostname: 'author-host', api_token: '' },
  }, 'display masks and sensitive defaults must never become deploy answers');
  window.GD.PALETTE = originalPalette;

  const editorReact = statefulReact();
  const editorCalls = [];
  function EditorFormModal() {}
  const editorWindow = {
    React: editorReact,
    Icon: wave46Icon,
    GD: { PALETTE: [], SECRETS: [] },
    GDStore: { toast() {} },
    API: {
      editBlock(key, payload) { editorCalls.push({ key, payload }); return Promise.resolve(); },
      createBlock(payload) { editorCalls.push({ key: null, payload }); return Promise.resolve(); },
    },
    UI: {
      Modal() {}, OSGlyph() {}, Field() {}, TextArea() {}, SelectField() {}, Toggle() {},
      TagInput() {}, FormModal: EditorFormModal,
    },
  };
  vm.runInNewContext(builderSource, { React: editorReact, window: editorWindow, setTimeout() {} }, {
    filename: 'web/builder.js',
  });
  const initialBlock = {
    key: 'c-edit-select', builtin: false, name: 'Selectable', cat: 'Custom', icon: 'spark',
    section: 'Scripts', phase: 'cloudinit', desc: '', cloudinit: 'echo {mode}', ansible: '',
    schema: [{ name: 'mode', type: 'select', default: 'safe', options: ['safe', 'fast'] }],
  };
  editorReact.begin();
  let editorTree = editorWindow.BlockEditorModal({ initial: initialBlock, onClose() {}, onSaved() {} });
  let optionsEditor = findAll(editorTree, (node) => node.type === 'input'
    && node.props['aria-label'] === 'Options for mode')[0];
  assert.ok(optionsEditor, 'select inputs need an options editor');
  assert.equal(optionsEditor.props.value, 'safe, fast');
  optionsEditor.props.onChange({ target: { value: 'safe, fast, debug' } });
  editorReact.begin();
  editorTree = editorWindow.BlockEditorModal({ initial: initialBlock, onClose() {}, onSaved() {} });
  optionsEditor = findAll(editorTree, (node) => node.type === 'input'
    && node.props['aria-label'] === 'Options for mode')[0];
  assert.equal(optionsEditor.props.value, 'safe, fast, debug');
  const editorForm = findAll(editorTree, (node) => node.type === EditorFormModal)[0];
  await editorForm.props.onSubmit();
  assert.deepEqual(JSON.parse(JSON.stringify(editorCalls[0].payload.input_schema)), [{
    name: 'mode', type: 'select', default: 'safe', options: ['safe', 'fast', 'debug'],
  }], 'the options editor must submit a clean select schema');

  const manageReact = statefulReact();
  const manageWindow = {
    React: manageReact,
    Icon: wave46Icon,
    GD: { PALETTE: [
      { id: 'c-ref', key: 'c-ref', name: 'Referenced block', cat: 'Custom', desc: '', builtin: false, schema: [], canDelete: false },
      { id: 'c-free', key: 'c-free', name: 'Free block', cat: 'Custom', desc: '', builtin: false, schema: [], canDelete: true },
    ] },
    GDStore: { refresh() { return Promise.resolve(); }, toast() {} }, API: {},
    UI: {
      Menu() {}, ConfirmModal() {}, FormModal() {}, Field() {}, TextArea() {}, SelectField() {},
      Toggle() {}, fmtBytes() {}, useFetched() {},
    },
  };
  vm.runInNewContext(source, { React: manageReact, window: manageWindow }, { filename: 'web/manage.js' });
  manageReact.begin();
  const blocksTree = manageWindow.BlocksLib();
  const referencedDelete = findAll(blocksTree, (node) => node.type === 'button'
    && node.props['aria-label'] === 'Delete Referenced block')[0];
  const freeDelete = findAll(blocksTree, (node) => node.type === 'button'
    && node.props['aria-label'] === 'Delete Free block')[0];
  assert.ok(referencedDelete && freeDelete, 'custom block delete controls need accessible names');
  assert.equal(referencedDelete.props.disabled, true);
  assert.match(referencedDelete.props.title, /referenced by a template/i);
  assert.equal(freeDelete.props.disabled, false);

  const imageReact = statefulReact();
  function ImageMenu(props) {
    return imageReact.createElement('div', { 'data-menu': true }, props.children,
      (props.items || []).filter((item) => !item.sep).map((item) => imageReact.createElement(
        'button', { key: item.label, disabled: !!item.disabled, title: item.title }, item.label,
      )));
  }
  const imageWindow = {
    React: imageReact,
    Icon: wave46Icon,
    GD: {
      me: { isAdmin: true }, CONNECTIONS: [], JOBS: [], TEMPLATES: [],
      BASE_IMAGES: [
        { id: 'img-ref', imgId: 461, name: 'Referenced image', os: 'ubuntu', size: '1G', canDelete: false },
        { id: 'img-free', imgId: 462, name: 'Free image', os: 'ubuntu', size: '1G', canDelete: true },
      ],
    },
    GDStore: { refresh() { return Promise.resolve(); }, toast() {} }, API: {},
    UI: {
      OSGlyph() {}, Menu: ImageMenu, ConfirmModal() {}, FormModal() {}, Field() {}, SelectField() {},
      useFetched() { return { online: false, cached: {} }; },
    },
  };
  vm.runInNewContext(imagesSource, { React: imageReact, window: imageWindow }, { filename: 'web/images.js' });
  imageReact.begin();
  const imagesTree = resolveTree(imageWindow.Isos({ go() {} }));
  function imageCard(name) {
    return findAll(imagesTree, (node) => node.props.className === 'card'
      && textOf(node).includes(name))[0];
  }
  const referencedImageDelete = findAll(imageCard('Referenced image'),
    (node) => node.type === 'button' && textOf(node) === 'Delete')[0];
  const freeImageDelete = findAll(imageCard('Free image'),
    (node) => node.type === 'button' && textOf(node) === 'Delete')[0];
  assert.equal(referencedImageDelete.props.disabled, true);
  assert.match(referencedImageDelete.props.title, /referenced by a template|deployed VM/i);
  assert.equal(freeImageDelete.props.disabled, false);

  const navigationReact = statefulReact();
  const navigationCalls = [];
  function NavigationMenu(props) { return navigationReact.createElement('div', null, props.children); }
  const navigationWindow = {
    React: navigationReact,
    Icon: wave46Icon,
    GD: { me: { name: 'Keyboard User', role: 'User', initials: 'KU' }, JOBS: [], VMS: [{
      id: 'vm-46', depId: 46, name: 'Keyboard VM', status: 'running', owner: 'you',
      ownerName: 'Keyboard User', ip: '10.46.0.46', image: 'Ubuntu', template: 'Base',
      templateId: 46, conn: 'pve', os: 'ubuntu', cpu: 1, ram: 2, uptime: '1h', tags: '', notes: '',
    }] },
    GDStore: { vmHistory() { return []; }, refresh() { return Promise.resolve(); }, toast() {}, vmAction() { return Promise.resolve(); } },
    API: {},
    UI: {
      Menu: NavigationMenu, ConfirmModal() {}, FormModal() {}, Field() {}, OSGlyph() {},
      StatusBadge() {}, CopyField() {}, Meter() {}, Sparkline() {},
      isVmLifecycleLocked(vmRow) { return ['working', 'cleanup_pending'].includes(vmRow && vmRow.status); },
    },
  };
  vm.runInNewContext(dashboardSource, {
    React: navigationReact, window: navigationWindow,
    localStorage: { getItem() { return null; }, setItem() {} }, setTimeout() {},
  }, { filename: 'web/dashboard.js' });
  navigationReact.begin();
  let dashboardTree = resolveTree(navigationWindow.Dashboard({
    go(route, nav) { navigationCalls.push({ route, nav }); },
  }));
  const vmRow = findAll(dashboardTree, (node) => node.type === 'tr'
    && textOf(node).includes('Keyboard VM'))[0];
  assert.equal(vmRow.props.role, 'link');
  assert.equal(vmRow.props.tabIndex, 0);
  vmRow.props.onKeyDown({
    type: 'keydown', key: 'Enter', target: vmRow, currentTarget: vmRow,
    preventDefault() {},
  });
  assert.equal(navigationCalls.length, 1, 'Enter must open a VM table row');
  const cardsButton = findAll(dashboardTree, (node) => node.type === 'button'
    && textOf(node) === 'Cards')[0];
  cardsButton.props.onClick();
  navigationReact.begin();
  dashboardTree = resolveTree(navigationWindow.Dashboard({
    go(route, nav) { navigationCalls.push({ route, nav }); },
  }));
  const vmCard = findAll(dashboardTree, (node) => String(node.props.className || '').includes('card card-pad')
    && textOf(node).includes('Keyboard VM'))[0];
  assert.equal(vmCard.props.role, 'link');
  assert.equal(vmCard.props.tabIndex, 0);
  let prevented = false;
  vmCard.props.onKeyDown({
    type: 'keydown', key: ' ', target: vmCard, currentTarget: vmCard,
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.equal(navigationCalls.length, 2, 'Space must open a VM card');

  const shellReact = statefulReact();
  const shellGo = [];
  const shellWindow = {
    React: shellReact, Icon: wave46Icon,
    GD: { me: { name: 'Keyboard User', role: 'User', initials: 'KU' }, JOBS: [{
      id: 'j-46', jobId: 46, title: 'Keyboard activity', status: 'working', rawStatus: 'running',
      pct: 10, phase: 'Run', elapsed: '1s', step: 1, total: 2,
    }], VMS: [] },
    GDStore: { signOut() {}, refresh() { return Promise.resolve(); }, toast() {} }, API: {},
    UI: {
      Menu: NavigationMenu,
      jobPresentation() { return { label: 'Working', badgeClass: 'working', dotClass: 'working', failure: false }; },
    },
  };
  vm.runInNewContext(shellSource, { React: shellReact, window: shellWindow }, { filename: 'web/shell.js' });
  shellReact.begin();
  const topbar = shellWindow.Shell.TopBar({
    route: 'dashboard', go() {}, theme: 'dark', setTheme() {}, openDrawer() {}, openMobileNav() {},
  });
  const accountMenu = findAll(topbar, (node) => node.type === NavigationMenu)[0];
  const accountTrigger = accountMenu.children[0];
  assert.equal(accountTrigger.type, 'button', 'the account menu trigger must be a native button');
  assert.equal(accountTrigger.props['aria-label'], 'Account menu');
  const activityTree46 = shellWindow.Shell.ActivityDrawer({
    onClose() {}, go(route, nav) { shellGo.push({ route, nav }); },
  });
  const activityCard46 = findAll(activityTree46, (node) => String(node.props.className || '').includes('card')
    && textOf(node).includes('Keyboard activity'))[0];
  assert.equal(activityCard46.props.role, 'link');
  assert.equal(activityCard46.props.tabIndex, 0);
  activityCard46.props.onKeyDown({
    type: 'keydown', key: 'Enter', target: activityCard46, currentTarget: activityCard46,
    preventDefault() {},
  });
  assert.equal(shellGo.length, 1, 'Enter must open an activity row');

  const historyReact = statefulReact([[
    { id: 'j-history-46', jobId: 460, title: 'Keyboard history', type: 'deploy',
      rawStatus: 'succeeded', status: 'done', pct: 100, phase: 'Done', elapsed: '1m' },
  ]]);
  const historyCalls = [];
  const historyWindow = {
    React: historyReact, Icon: wave46Icon,
    GD: { me: { isAdmin: false } }, GDStore: { toast() {} },
    API: {
      jobsHistory() { return Promise.resolve([]); },
      job(id) { historyCalls.push(id); return Promise.resolve({ log: [] }); },
    },
    UI: {
      ConfirmModal() {},
      jobPresentation() { return { label: 'Done', badgeClass: 'running', dotClass: 'running', failure: false }; },
    },
  };
  vm.runInNewContext(historySource, { React: historyReact, window: historyWindow }, {
    filename: 'web/history.js',
  });
  historyReact.begin();
  const historyTree46 = historyWindow.History();
  const historyHeader46 = findAll(historyTree46, (node) => String(node.props.className || '').includes('history-toggle')
    && textOf(node).includes('Keyboard history'))[0];
  assert.equal(historyHeader46.props.role, 'button');
  assert.equal(historyHeader46.props.tabIndex, 0);
  assert.equal(historyHeader46.props['aria-expanded'], false);
  historyHeader46.props.onKeyDown({
    type: 'keydown', key: 'Enter', target: historyHeader46, currentTarget: historyHeader46,
    preventDefault() {},
  });
  assert.deepEqual(historyCalls, [460], 'Enter must expand a history row');

  const menuReact = statefulReact([true, { top: 10, left: 10, right: 10 }]);
  menuReact.useRef = () => ({ current: { getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0 }; } } });
  const menuWindow = { React: menuReact, Icon: wave46Icon, UI: {} };
  vm.runInNewContext(uiSource, {
    React: menuReact,
    ReactDOM: { createPortal(node) { return node; } },
    window: menuWindow,
    navigator: {},
    document: { body: {}, addEventListener() {}, removeEventListener() {} },
  }, { filename: 'web/ui.js' });
  menuReact.begin();
  const menuTree = menuWindow.UI.Menu({
    items: [{ label: 'Profile', onClick() {} }, { label: 'Sign out', onClick() {} }],
    children: menuReact.createElement('button', { type: 'button' }, 'Account'),
  });
  const popupMenu = findAll(menuTree, (node) => node.props.role === 'menu')[0];
  assert.ok(popupMenu, 'the dropdown needs menu semantics');
  const menuItems = findAll(popupMenu, (node) => node.type === 'button');
  assert.equal(menuItems.length, 2);
  assert.ok(menuItems.every((item) => item.props.role === 'menuitem'));

  for (const selector of ['.vm-nav-surface:focus-visible', '.activity-nav-surface:focus-visible', '.history-toggle:focus-visible']) {
    assert.match(cssRule(selector), /outline\s*:/, `${selector} needs a visible keyboard focus indicator`);
  }
}

async function testIsoModalChecksumSubmission() {
  const validHarness = imageModalHarness(null);
  let tree = validHarness.render();
  const byLabel = (label) => findAll(tree, (node) => node.props['aria-label'] === label)[0];
  byLabel('Name').props.onChange({ target: { value: 'Checksum image' } });
  byLabel('Cloud image URL (.img/.qcow2)').props.onChange({
    target: { value: 'https://example.com/base.img' },
  });
  controlForLabel(tree, 'Checksum (optional)').props.onChange({
    target: { value: `  ${'A'.repeat(64)}\t` },
  });
  tree = validHarness.render();
  const checksumInput = controlForLabel(tree, 'Checksum (optional)');
  const checksumLabel = findAll(tree, (node) => node.type === 'label'
    && textOf(node) === 'Checksum (optional)')[0];
  const feedback = findAll(tree, (node) => node.props.id === checksumInput.props['aria-describedby'])[0];
  assert.ok(checksumInput.props.id, 'checksum control needs a stable id');
  assert.equal(checksumLabel.props.htmlFor, checksumInput.props.id);
  assert.equal(checksumInput.props['aria-invalid'], false);
  assert.ok(feedback, 'checksum input must reference nearby feedback');
  assert.equal(feedback.props['aria-live'], 'polite');
  assert.match(textOf(feedback), /SHA-256/);
  const validForm = findAll(tree, (node) => node.type === 'form')[0];
  await validForm.props.onSubmit();
  assert.equal(validHarness.apiCalls.length, 1);
  assert.equal(validHarness.apiCalls[0].action, 'add');
  assert.equal(validHarness.apiCalls[0].payload.checksum, 'a'.repeat(64));

  const invalidHarness = imageModalHarness({
    imgId: 39,
    name: 'Existing image',
    os: 'ubuntu',
    source_url: 'https://example.com/existing.img',
    checksum: `sha256:${'b'.repeat(64)}`,
  });
  const invalidTree = invalidHarness.render();
  const invalidInput = controlForLabel(invalidTree, 'Checksum (optional)');
  const invalidFeedback = findAll(invalidTree,
    (node) => node.props.id === invalidInput.props['aria-describedby'])[0];
  assert.equal(invalidInput.props.value, `sha256:${'b'.repeat(64)}`,
    'edit state must preserve the stored checksum for correction');
  assert.equal(invalidInput.props['aria-invalid'], true);
  assert.match(textOf(invalidFeedback), /hexadecimal/i);
  await findAll(invalidTree, (node) => node.type === 'form')[0].props.onSubmit();
  assert.equal(invalidHarness.apiCalls.length, 0,
    'known-invalid checksum must return before calling the API');
}

testWave46Contracts().then(testIsoModalChecksumSubmission).then(() => {
  console.log('ALL WAVE 39 UI TESTS PASSED');
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
