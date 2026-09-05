/* Preflight, revision-aware rebuild, and explicit recovery actions. */
(function () {
  const h = React.createElement;
  const {useState, useEffect} = React;
  const toast = (e) => window.GDStore.toast(e.message || 'Operation failed', 'err');

  function PreflightReport({report}) {
    if (!report) return null;
    return h('div', {className: 'card card-pad', role: 'status'},
      h('strong', null, report.ok ? 'Preflight passed' : 'Preflight needs attention'),
      h('ul', {style: {paddingLeft: 20}}, (report.checks || []).map((c) =>
        h('li', {key: c.name, style: {marginTop: 6}},
          h('strong', null, c.name + ': '), c.status === 'pass' ? 'Passed. ' : 'Failed. ', c.detail))),
      h('p', {className: 'hint'}, report.note));
  }

  function RebuildModal({depId, name, go, onClose}) {
    const {FormModal, AskInputs, collectAsks, initAskAnswers, asksMissing} = window.UI;
    const [info, setInfo] = useState(null);
    const [mode, setMode] = useState('original');
    const [answers, setAnswers] = useState({});
    const [busy, setBusy] = useState(false);
    const busyRef = React.useRef(false);
    const [report, setReport] = useState(null);
    const [preview, setPreview] = useState(null);
    const [error, setError] = useState('');
    const tpl = info && (window.GD.TEMPLATES || []).find(t => t.templateId === info.currentTemplateId);
    const asks = mode === 'current' && tpl ? collectAsks(tpl) : [];
    useEffect(() => { window.API.rebuildPlan(depId).then(setInfo).catch(e => setError(e.message)); }, [depId]);
    const selectMode = (m) => {
      setMode(m); setReport(null); setPreview(null);
      setAnswers(m === 'current' && tpl ? initAskAnswers(collectAsks(tpl)) : {});
    };
    const body = {mode, deployInputs: mode === 'current' ? answers : {}};
    const check = async () => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true); setError('');
      try {
        const p = await window.API.rebuildPreview(depId, body); setPreview(p);
        const r = await window.API.rebuildPreflight(depId, body); setReport(r);
      } catch (e) { setError(e.message); setReport(null); }
      finally { setBusy(false); busyRef.current = false; }
    };
    const submit = async () => {
      if (busyRef.current) return;
      if (!report || !report.ok) { await check(); return; }
      setBusy(true);
      busyRef.current = true;
      try { const r = await window.API.vmRebuild(depId, body); onClose(); go('job', {jobId:r.jobId}); }
      catch (e) { setError(e.message); setBusy(false); busyRef.current = false; }
    };
    return h(FormModal, {title: 'Rebuild ' + name, icon:'rebuild', onClose, onSubmit:submit, busy,
      submitLabel: report && report.ok ? 'Rebuild VM' : 'Run preflight'},
      h('p', {className:'hint'}, 'Rebuild replaces the VM disk. Choose which captured recipe and answers to use.'),
      h('label', {className:'field-label', htmlFor:'rebuild-revision'}, 'Recipe version'),
      h('select', {id:'rebuild-revision', className:'select', value:mode, disabled:busy,
        onChange:e=>selectMode(e.target.value)},
        h('option', {value:'original', disabled: !info || !info.originalAvailable}, 'Original deployment recipe and saved answers'),
        h('option', {value:'current', disabled: !tpl}, 'Current template with fresh answers')),
      info && !info.originalAvailable && h('p', {className:'hint'}, 'This older deployment has no recoverable original snapshot. Select the current template explicitly.'),
      asks.length > 0 && h(AskInputs, {asks, answers, setAnswers:a=>{setAnswers(a);setReport(null);setPreview(null);}}),
      asksMissing(asks,answers).length > 0 && h('p', {className:'hint'}, 'Required: ' + asksMissing(asks,answers).join(', ')),
      preview && h('p', {className:'hint'}, 'Changes: ' + ((preview.changes || []).join(', ') || 'No changes from the original recipe.')),
      error && h('p', {role:'alert', style:{color:'var(--err)'}}, error),
      h(PreflightReport, {report}));
  }

  function Recovery({go}) {
    const [items,setItems] = useState([]);
    const [error,setError] = useState('');
    const [busy,setBusy] = useState(null);
    const [result,setResult] = useState(null);
    const [confirm,setConfirm] = useState(null);
    const [rebuild,setRebuild] = useState(null);
    const load = () => window.API.recovery().then(r=>setItems(r.items)).catch(e=>setError(e.message));
    useEffect(()=>{load();},[]);
    const reconcile = async (item, confirmed=false) => {
      setBusy(item.depId);
      try {
        const r = await window.API.reconcileVm(item.depId, {confirmIdentity:confirmed});
        setResult({id:item.depId,...r}); await load();
      } catch(e){toast(e);} finally {setBusy(null);}
    };
    const execute = async () => {
      const {item,action} = confirm;
      try {
        if(action==='identity'){await reconcile(item,true);setConfirm(null);return;}
        const r = action==='configure' ? await window.API.retryConfiguration(item.depId) : await window.API.retryCleanup(item.depId);
        setConfirm(null);go('job',{jobId:r.jobId});
      } catch(e){toast(e);throw e;}
    };
    return h('div',{className:'page fadein',style:{maxWidth:1100}},
      h('div',{className:'page-head'},h('h1',{className:'page-title'},'Recovery'),
        h('button',{className:'btn sm',onClick:load},'Refresh')),
      h('p',{className:'page-sub'},'Inspect interrupted work and choose how to recover each VM.'),
      error && h('p',{role:'alert'},error),
      items.length===0 && h('div',{className:'card card-pad'},'No VMs need recovery.'),
      items.map(item=>h('section',{key:item.depId,className:'card card-pad',style:{marginBottom:14}},
        h('h2',null,item.name),h('p',{className:'mono'},'VM '+(item.vmid||'unassigned')+' · '+(item.node||'unknown node')),
        h('p',null,item.error || item.phase),
        item.phase && h('p',{className:'hint'},'Last phase: '+item.phase),
        item.remoteTask && h('p',{className:'hint mono',style:{overflowWrap:'anywhere'}},'Proxmox task ('+item.remoteNode+'): '+item.remoteTask),
        h('div',{className:'row',style:{gap:8,flexWrap:'wrap'}},
          h('button',{className:'btn sm',disabled:busy===item.depId,onClick:()=>reconcile(item)},'Reconcile'),
          item.jobId && h('button',{className:'btn ghost sm',onClick:()=>go('job',{jobId:item.jobId})},'Job log'),
          h('button',{className:'btn ghost sm',onClick:()=>go('vmdetail',{depId:item.depId})},'VM details'),
          !item.uncertainIdentity && h('button',{className:'btn sm',onClick:()=>setConfirm({item,action:'configure'})},'Retry configuration'),
          !item.uncertainIdentity && h('button',{className:'btn sm',onClick:()=>setRebuild(item)},'Rebuild'),
          !item.uncertainIdentity && h('button',{className:'btn danger sm',onClick:()=>setConfirm({item,action:'delete'})},'Retry cleanup')),
        result && result.id===item.depId && h('div',{role:'status',style:{marginTop:10}},
          h('p',null,result.detail),result.requiresIdentityConfirmation && h('button',{className:'btn sm',onClick:()=>setConfirm({item,action:'identity'})},'Confirm VM ownership')))),
      rebuild && h(RebuildModal,{depId:rebuild.depId,name:rebuild.name,go,onClose:()=>setRebuild(null)}),
      confirm && h(window.UI.ConfirmModal,{title:confirm.action==='configure'?'Run configuration again?':confirm.action==='identity'?'Confirm this VM belongs to this deployment?':'Delete the VM?',
        body:confirm.action==='configure'?'Captured post-boot scripts will run again on the existing VM. Scripts can repeat changes or overwrite data. First-boot cloud-init is not replayed.':confirm.action==='identity'?'Proceed only after verifying the VM identifier and task history in Proxmox. Recovery actions will be allowed to modify this VM.':'This stops the VM and deletes its disks. Local allocations are released after absence is verified.',
        confirmLabel:confirm.action==='configure'?'Retry configuration':confirm.action==='identity'?'Confirm ownership':'Delete VM',
        onConfirm:execute,onClose:()=>setConfirm(null)}));
  }
  window.PreflightReport=PreflightReport;
  window.RebuildModal=RebuildModal;
  window.Recovery=Recovery;
})();
