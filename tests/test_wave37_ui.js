'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'job.js'), 'utf8');

function renderJob(rawStatus) {
  const job = {
    id: 37,
    title: 'Durable wait',
    type: 'deploy',
    status: rawStatus === 'succeeded' ? 'done' : 'working',
    rawStatus,
    pct: 80,
    phase: 'Waiting for guest IP',
    phases: ['Allocate', 'Prepare image', 'Create', 'Configure', 'Boot'],
    elapsed: '05:00',
    steps: [],
    log: [],
  };
  const React = {
    createElement(type, props, ...children) {
      return { type, props: props || {}, children: children.flat(Infinity).filter(Boolean) };
    },
    useState(initial) {
      return [initial === null ? job : initial, () => {}];
    },
    useEffect() {},
    useRef(initial) { return { current: initial }; },
  };
  const window = {
    React,
    Icon: function Icon() {},
    GD: { JOBS: [] },
    GDStore: { nav: { jobId: 37 }, refresh: async () => {}, toast() {} },
    API: { job: async () => job, cancelJob: async () => {} },
  };
  vm.runInNewContext(source, {
    React,
    window,
    document: {},
    EventSource: function EventSource() {},
    Blob: function Blob() {},
    URL: {},
  }, { filename: 'web/job.js' });
  return window.JobProgress({ go() {} });
}

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

function assertLiveRendering(rawStatus, expectedLive) {
  const tree = renderJob(rawStatus);
  const buttons = findAll(tree, node => node.type === 'button').map(textOf);
  const logPane = findAll(tree, node => typeof node.type === 'function' && node.type.name === 'LogPane')[0];
  assert.equal(buttons.includes('Cancel'), expectedLive, `${rawStatus} Cancel rendering`);
  assert.equal(buttons.includes('Go to VMs'), !expectedLive, `${rawStatus} terminal action rendering`);
  assert.equal(logPane.props.live, expectedLive, `${rawStatus} live-log indicator`);
}

for (const status of ['queued', 'running', 'waiting']) assertLiveRendering(status, true);
assertLiveRendering('succeeded', false);
console.log('ALL WAVE 37 UI TESTS PASSED');
