'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../web/operations.js'),'utf8');

function harness() {
  const states=[], deps=[], effects=[];
  let cursor=0, calls={preview:0,preflight:0,rebuild:0,closed:0};
  const React={
    createElement:(type,props,...children)=>({type,props:props||{},children}),
    useState(initial){const i=cursor++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;
      return [states[i],v=>states[i]=typeof v==='function'?v(states[i]):v];},
    useRef(initial){const i=cursor++;if(!(i in states))states[i]={current:initial};return states[i];},
    useEffect(fn,d){const i=cursor++;if(!deps[i]||d.some((v,n)=>v!==deps[i][n])){deps[i]=d;effects.push(fn);}},
  };
  const windowObj={GD:{TEMPLATES:[{templateId:1}]},GDStore:{toast(){}},
    UI:{FormModal:'FormModal',collectAsks:()=>[],initAskAnswers:()=>({}),asksMissing:()=>[]},
    API:{rebuildPlan:async()=>({originalAvailable:true,currentTemplateId:1}),
      rebuildPreview:async()=>{calls.preview++;return {changes:[]};},
      rebuildPreflight:async()=>{calls.preflight++;return {ok:true,checks:[]};},
      vmRebuild:async()=>{calls.rebuild++;return {jobId:9};}}
  };
  vm.runInNewContext(source,{window:windowObj,React});
  const render=()=>{cursor=0;return windowObj.RebuildModal({depId:1,name:'vm',go(){},onClose(){calls.closed++;}});};
  const settle=async()=>{for(const fn of effects.splice(0))fn();await new Promise(setImmediate);};
  return {render,settle,calls,windowObj};
}

(async()=>{
  const t=harness(); t.render();await t.settle();
  let tree=t.render();await tree.props.onSubmit();
  assert.equal(t.calls.rebuild,0,'first submit checks readiness without destroying a VM');
  assert.equal(t.calls.preflight,1);
  tree=t.render();assert.equal(tree.props.submitLabel,'Rebuild VM');
  await Promise.all([tree.props.onSubmit(),tree.props.onSubmit()]);
  assert.equal(t.calls.rebuild,1,'double click cannot submit two rebuild jobs');
  assert.equal(t.calls.closed,1);

  const u=harness();u.render();await u.settle();
  tree=u.render();await tree.props.onSubmit();tree=u.render();
  tree.children.find(x=>x&&x.type==='select').props.onChange({target:{value:'current'}});
  tree=u.render();assert.equal(tree.props.submitLabel,'Run preflight','changing revision invalidates prior readiness');
  await tree.props.onSubmit();assert.equal(u.calls.rebuild,0);assert.equal(u.calls.preflight,2);

  const f=harness();f.windowObj.API.rebuildPreflight=async()=>({ok:false,checks:[]});
  f.render();await f.settle();tree=f.render();await tree.props.onSubmit();tree=f.render();await tree.props.onSubmit();
  assert.equal(f.calls.rebuild,0,'failed readiness never unlocks rebuild');
  console.log('PASS wave58 preflight and rebuild UI behavior');
})().catch(e=>{console.error(e);process.exitCode=1;});
