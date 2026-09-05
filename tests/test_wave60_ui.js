'use strict';
// Cache polling must retain the rendered inventory until a replacement arrives,
// but must never show another target's cache or accept an obsolete response.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const states = [], deps = [], cleanups = [], effects = [], requests = [], timers = new Map();
  let cursor = 0, nextTimer = 0;
  const React = {
    createElement: (type, props, ...children) => ({type, props: props || {}, children}),
    useState(initial) {
      const i = cursor++;
      if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial;
      return [states[i], value => { states[i] = typeof value === 'function' ? value(states[i]) : value; }];
    },
    useRef(initial) {
      const i = cursor++;
      if (!(i in states)) states[i] = {current: initial};
      return states[i];
    },
    useEffect(fn, values) {
      const i = cursor++;
      if (!deps[i] || values.some((value, n) => !Object.is(value, deps[i][n]))) {
        deps[i] = values;
        effects.push(() => { if (cleanups[i]) cleanups[i](); cleanups[i] = fn(); });
      }
    },
  };
  const window = {
    GD: {me: {isAdmin: true}, CONNECTIONS: [{connId: 1, name: 'A'}, {connId: 2, name: 'B'}],
      BASE_IMAGES: [{id: 'img-1', imgId: 1, name: 'Ubuntu', source_url: 'https://example.test/image'}], JOBS: []},
    GDStore: {refresh: async () => {}, toast() {}},
    API: {cachedImages: targetId => new Promise((resolve, reject) => requests.push({targetId, resolve, reject}))},
  };
  const context = vm.createContext({window, React,
    setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearInterval: id => timers.delete(id),
  });
  for (const file of ['ui.js', 'images.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../web', file), 'utf8'), context);
  }
  const render = (component = () => window.Isos({go() {}})) => {
    cursor = 0;
    const tree = component();
    for (const effect of effects.splice(0)) effect();
    return tree;
  };
  return {window, requests, render, poll: () => { for (const fn of timers.values()) fn(); },
    unmount: () => { for (const cleanup of cleanups) if (cleanup) cleanup(); }};
}
const settle = () => new Promise(setImmediate);
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.children)];
}
function card(t, tree) { return nodes(tree).find(n => n.type === t.window.ImageUI.IsoCard).props; }
const inventory = {online: true, cached: {'1': true}, metadata: {'1': {downloadedAt: '2026-09-05T18:00:00Z'}},
  inventory: {updatedAt: '2026-09-05T18:00:00Z', completedAt: '2026-09-05T18:00:00Z', stale: false}};

(async () => {
  const t = harness();
  t.render(); await settle(); t.requests[0].resolve(inventory); await settle();
  assert.equal(card(t, t.render()).cacheState, 'cached');
  t.poll(); t.render(); await settle();
  let tree = t.render();
  assert.equal(card(t, tree).cacheState, 'cached', 'background fetch must not flash cache unknown');
  assert.equal(card(t, tree).metadata.downloadedAt, '2026-09-05T18:00:00Z');
  assert.ok(nodes(tree).some(n => n.props.role === 'status'), 'inventory row must stay mounted during polling');
  t.requests[1].resolve({...inventory, cached: {'1': false}}); await settle();
  tree = t.render(); assert.equal(card(t, tree).cacheState, 'missing', 'completed refresh updates status');

  // A response from A must not leak into B, even if A finishes after selection.
  t.poll(); t.render(); await settle();
  nodes(tree).find(n => n.type === 'select').props.onChange({target: {value: '2'}});
  tree = t.render();
  assert.equal(card(t, tree).cacheState, 'unknown', 'target switch clears old inventory immediately');
  await settle();
  assert.equal(t.requests[3].targetId, 2);
  t.requests[2].resolve(inventory); await settle();
  assert.equal(card(t, t.render()).cacheState, 'unknown', 'obsolete response is ignored');
  t.requests[3].resolve({...inventory, online: false, cached: {}}); await settle();
  assert.equal(card(t, t.render()).cacheState, 'unknown', 'offline target never appears cached');

  t.poll(); t.render(); await settle(); t.requests[4].reject(new Error('network unavailable')); await settle();
  tree = t.render();
  assert.equal(card(t, tree).cacheState, 'unknown', 'failed fetch must not claim verified cache availability');
  t.poll(); t.render(); await settle(); t.requests[5].resolve(inventory); await settle();
  assert.equal(card(t, t.render()).cacheState, 'cached', 'polling recovers after request failure');
  t.unmount();

  // Existing non-polling consumers keep their loading and cancellation behavior.
  const u = harness(); let revision = 0;
  const fetch = () => u.window.UI.useFetched(() => u.window.API.cachedImages(revision), [revision], {error: true});
  u.render(fetch); await settle(); u.requests[0].resolve('first'); await settle();
  assert.equal(u.render(fetch), 'first');
  revision++; u.render(fetch);
  assert.equal(u.render(fetch), null, 'default hook still clears while loading');
  await settle(); u.unmount(); u.requests[1].resolve('late'); await settle();
  assert.equal(u.render(fetch), null, 'unmounted fetch cannot update state');
  console.log('PASS wave60 stable ISO inventory refresh and request isolation');
})().catch(error => { console.error(error); process.exitCode = 1; });
