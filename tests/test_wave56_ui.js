'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let states = [], cursor = 0, updates = [];
const h = (type, props, ...children) => ({ type, props: props || {}, children: children.flat(Infinity) });
const React = { createElement: h, useState(initial) {
  const index = cursor++;
  if (!(index in states)) states[index] = initial;
  return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
}, useId: () => 'id', useRef: () => ({current:0}), useEffect() {} };
const window = { GD: {}, Icon: 'Icon', UI: { FormModal:'FormModal', Field:'Field', SelectField:'SelectField', Menu:'Menu' },
  GDStore: {refresh:async()=>{}, toast(){}}, API:{ editImage:async(id,body)=>updates.push(body) } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/images.js'),'utf8'),{React,window,Date},{filename:'web/images.js'});
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/dashboard.js'),'utf8'),{React,window,Date},{filename:'web/dashboard.js'});
const all = tree => !tree || typeof tree !== 'object' ? [] : [tree, ...tree.children.flatMap(all)];
const text = tree => tree == null ? '' : typeof tree === 'object' ? tree.children.map(text).join(' ') : String(tree);
const render = (fn, props) => { cursor=0; return fn(props); };

(async () => {
  const {IsoCard,IsoModal} = window.ImageUI;
  assert.equal(typeof IsoCard,'function','image cards must expose refresh and pin actions');
  let refreshed = false, pinned = false;
  const img = {imgId:1,name:'Ubuntu',source_url:'https://example.test/releases/20260901/image.qcow2',checksum:'a'.repeat(64),os:'ubuntu'};
  const card = IsoCard({img,isAdmin:true,cacheState:'cached',canSync:true,onSync:()=>{refreshed=true;},onPin:()=>{pinned=true;},metadata:{downloadedAt:'2026-09-01T10:00:00Z'},go(){}});
  const refresh = all(card).find(n=>n.type==='button' && text(n).includes('Refresh'));
  assert.ok(refresh && !refresh.props.disabled,'cached image has an enabled explicit refresh action');
  refresh.props.onClick(); assert.equal(refreshed,true);
  const menu=all(card).find(n=>n.type==='Menu');
  menu.props.items.find(item=>item.label==='Pin version').onClick(); assert.equal(pinned,true);
  assert.match(text(card),/Downloaded/);
  states=[];
  let modal=render(IsoModal,{img,pinning:true,onClose(){},onDone(){}});
  await modal.props.onSubmit();
  assert.equal(updates.length,0,'pin is not submitted without immutable URL confirmation');
  const confirmation=all(modal).find(n=>n.type==='input' && n.props.type==='checkbox');
  assert.ok(confirmation,'pin dialog explains and confirms immutable source');
  confirmation.props.onChange({target:{checked:true}});
  modal=render(IsoModal,{img,pinning:true,onClose(){},onDone(){}});
  await modal.props.onSubmit();
  assert.equal(updates[0].pin,true); assert.equal(updates[0].immutable,true);
  assert.equal(updates[0].checksum,'a'.repeat(64));
  assert.equal(typeof window.InventoryNotice,'function');
  const notice=window.InventoryNotice({connections:[{name:'Primary',inventory:{stale:true,error:'Inventory unavailable',updatedAt:'2026-09-01T10:00:00Z'}},{name:'Disabled',disabled:true,inventory:{stale:true}}]});
  assert.match(text(notice),/Primary/); assert.match(text(notice),/Inventory unavailable/);
  assert.doesNotMatch(text(notice),/Disabled/);
  console.log('ALL WAVE 56 UI TESTS PASSED');
})().catch(error=>{console.error(error);process.exitCode=1;});
