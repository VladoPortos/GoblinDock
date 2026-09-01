/* Wave 54 — store: immediate VM removal must survive a stale in-flight /state.

   Local-only cleanup deletes the row server-side, but a /state response that was
   already in flight predates the deletion (and can be seconds away when the VM's
   Proxmox is unreachable). removeVm() drops the row instantly and tombstones it
   so the stale response cannot resurrect it; refresh({fresh: true}) waits the
   stale fetch out and fetches again so the store reconciles with the server.

   Run:   node tests/test_wave54_ui.js
*/
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const storeSource = fs.readFileSync(path.join(__dirname, '..', 'web', 'store.js'), 'utf8');

function makeStore() {
  const calls = [];
  const windowObj = {
    GD: { VMS: [] },
    API: {
      state: () => new Promise((resolve, reject) => { calls.push({ resolve, reject }); }),
    },
  };
  vm.runInNewContext(storeSource, { window: windowObj }, { filename: 'web/store.js' });
  return { windowObj, calls, store: windowObj.GDStore };
}

async function testRemoveVmDropsTheRowImmediately() {
  const { windowObj, store } = makeStore();
  let changes = 0;
  store.setOnChange(() => { changes += 1; });
  windowObj.GD.VMS = [{ depId: 1, name: 'a' }, { depId: 2, name: 'b' }];
  store.removeVm(1);
  assert.deepEqual(windowObj.GD.VMS.map((v) => v.depId), [2],
    'removeVm must drop the row without waiting for any fetch');
  assert.equal(changes, 1, 'removeVm must notify React');
}

async function testStaleInflightResponseCannotResurrectARemovedVm() {
  const { windowObj, calls, store } = makeStore();
  windowObj.GD.VMS = [{ depId: 5, name: 'ghost' }];
  const stale = store.refresh();                 // fetch #1 starts BEFORE the delete
  store.removeVm(5);                             // delete lands while #1 is in flight
  const fresh = store.refresh({ fresh: true });  // must wait #1 out, then fetch again
  assert.equal(calls.length, 1, 'fresh refresh must not race a second fetch ahead of the stale one');
  calls[0].resolve({ VMS: [{ depId: 5, name: 'ghost' }] });  // stale body still has the VM
  await stale;
  assert.deepEqual(windowObj.GD.VMS, [],
    'a response that predates the deletion must not resurrect the row');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 2, 'fresh refresh must refetch after the stale one settles');
  calls[1].resolve({ VMS: [] });
  await fresh;
  assert.deepEqual(windowObj.GD.VMS, []);
}

async function testFreshRefreshSurvivesAFailingStaleFetch() {
  const { windowObj, calls, store } = makeStore();
  const stale = store.refresh();
  stale.catch(() => {});                         // the stale failure is not the caller's
  const fresh = store.refresh({ fresh: true });
  calls[0].reject(new Error('state 502'));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 2, 'a failed stale fetch must still be followed by the fresh one');
  calls[1].resolve({ VMS: [{ depId: 7 }] });
  await fresh;
  assert.deepEqual(windowObj.GD.VMS.map((v) => v.depId), [7]);
}

async function testPlainRefreshStillCollapsesIntoTheInflightFetch() {
  const { windowObj, calls, store } = makeStore();
  const first = store.refresh();
  const second = store.refresh();
  assert.equal(calls.length, 1, 'plain refreshes must keep collapsing (SSE burst behaviour)');
  calls[0].resolve({ VMS: [{ depId: 9 }] });
  await first;
  await second;
  assert.equal(windowObj.GD.VMS.length, 1);
}

(async () => {
  await testRemoveVmDropsTheRowImmediately();
  await testStaleInflightResponseCannotResurrectARemovedVm();
  await testFreshRefreshSurvivesAFailingStaleFetch();
  await testPlainRefreshStillCollapsesIntoTheInflightFetch();
  console.log('\nALL WAVE 54 UI TESTS PASSED');
})().catch((e) => { console.error(e); process.exit(1); });
