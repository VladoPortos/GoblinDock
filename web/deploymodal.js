/* GoblinDock — Deploy modal: pick a template, answer its inputs, deploy. */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const h = React.createElement;

  function DeployModal({ tpl: fixedTpl, go, onClose }) {
    const { Field, FormModal, AskInputs, collectAsks, initAskAnswers, asksMissing, OSGlyph, SizeField } = window.UI;
    const deployables = (GD.TEMPLATES || []).filter((t) => t.deployable);
    const [tplId, setTplId] = useState(fixedTpl ? fixedTpl.templateId : (deployables[0] ? deployables[0].templateId : null));
    const tpl = fixedTpl || deployables.find((t) => t.templateId === tplId) || null;

    const [name, setName] = useState(() => {
      const used = new Set((GD.VMS || []).map((v) => v.name));
      let n = (GD.VMS || []).length + 1;
      while (used.has('gd-' + n)) n++;
      return 'gd-' + n;
    });
    const [answers, setAnswers] = useState(() => (tpl ? initAskAnswers(collectAsks(tpl)) : {}));
    const [adv, setAdv] = useState(false);
    const [cpu, setCpu] = useState(tpl ? tpl.cpu : 1);
    const [mem, setMem] = useState(tpl ? tpl.mem : 2);
    const [disk, setDisk] = useState(tpl ? tpl.disk : 20);
    const [busy, setBusy] = useState(false);
    const busyRef = React.useRef(false);
    const capData = window.UI.useFetched(
      () => ((tpl && tpl.connectionId) ? window.API.connectionCapacity(tpl.connectionId) : null),
      [tpl && tpl.connectionId], null);
    const cap = capData && capData.online ? capData : null;

    // won't-fit: RAM/disk over free → amber warning; CPU over cores → soft note. Never blocks.
    const capWarn = (() => {
      if (!cap) return null;
      const msgs = [];
      if (cap.mem && mem > cap.mem.freeGb)
        msgs.push(`needs ${mem} GB RAM, node has ${cap.mem.freeGb} GB free`);
      if (cap.storage && disk > cap.storage.freeGb)
        msgs.push(`needs ${disk} GB disk, ${cap.storage.name} has ${cap.storage.freeGb} GB free`);
      const cpuNote = cap.cpu && cpu > cap.cpu.cores
        ? `${cpu} vCPU on a ${cap.cpu.cores}-core node (overcommit)` : null;
      return { msgs, cpuNote };
    })();

    const conn = tpl ? (GD.CONNECTIONS || []).find((c) => c.connId === tpl.connectionId) || {} : {};
    const gl = GD.limits || {};
    const maxCpu = conn.maxCores || gl.maxCores || 1;
    const maxMem = conn.maxRamGb || gl.maxRam || 2;
    // connection cap → global cap → 500 GB slider default when neither is set
    const maxDisk = conn.maxDiskGb || gl.maxDisk || 500;

    const pick = (id) => {
      setTplId(id);
      const t = deployables.find((x) => x.templateId === id);
      setAnswers(t ? initAskAnswers(collectAsks(t)) : {});
      if (t) { setCpu(t.cpu); setMem(t.mem); setDisk(t.disk); setAdv(false); }
    };

    const asks = tpl ? collectAsks(tpl) : [];
    const missing = (name.trim() ? [] : ['VM name']).concat(asksMissing(asks, answers));

    const submit = async () => {
      if (busyRef.current || !tpl) return;
      if (missing.length) { window.GDStore.toast('Required: ' + missing.join(', '), 'warn'); return; }
      busyRef.current = true; setBusy(true);
      try {
        const r = await window.API.deploy({
          templateId: tpl.templateId, name: name.trim(), deployInputs: answers,
          cpu: Math.min(cpu, maxCpu), ram: Math.min(mem, maxMem), disk: Math.min(disk, maxDisk),
          tags: '',
        });
        onClose(); go('job', { jobId: r.jobId });
      } catch (e) { window.GDStore.toast(e.message || 'deploy failed', 'err'); setBusy(false); busyRef.current = false; }
    };

    if (!deployables.length && !fixedTpl) {
      return h(FormModal, { title: 'Deploy a VM', icon: 'play', onClose, onSubmit: () => { onClose(); go('templates'); }, submitLabel: 'Open Templates' },
        h('p', { className: 'hint', style: { fontSize: 12.5, lineHeight: 1.6 } },
          'No deployable templates yet. Create one first — pick a base image, a location, add blocks, save.'));
    }

    return h(FormModal, { title: 'Deploy a VM', icon: 'play', onClose, onSubmit: submit, busy, submitLabel: 'Deploy ' + (name.trim() || 'VM') },
      !fixedTpl && h('div', null,
        h('label', { className: 'field-label' }, 'Template'),
        h('select', { className: 'select', value: tplId || '', onChange: (e) => pick(Number(e.target.value) || null) },
          deployables.map((t) => h('option', { key: t.id, value: t.templateId }, t.name + ' (' + (t.blocks || []).length + ' blocks)')))),
      h(Field, { label: 'VM name', value: name, onChange: setName, mono: true }),
      asks.length > 0 && h('div', null,
        h('div', { className: 'panel-title', style: { marginBottom: 10 } }, 'Required inputs'),
        h(AskInputs, { asks, answers, setAnswers })),
      tpl && h('div', null,
        h('button', { className: 'btn ghost sm', onClick: (e) => { e.preventDefault(); setAdv(!adv); } },
          h(Icon, { name: 'sliders', size: 13 }), adv ? 'Hide resources' : 'Adjust resources (optional)'),
        adv && h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14, marginTop: 12 } },
          h(SizeField, { icon: 'cpu', label: 'vCPU', value: cpu, set: setCpu, min: 1, max: maxCpu, unit: '' }),
          h(SizeField, { icon: 'ram', label: 'Memory', value: mem, set: setMem, min: 1, max: maxMem, step: 1, unit: 'GB' }),
          h(SizeField, { icon: 'disk', label: 'Disk', value: disk, set: setDisk, min: 10, max: maxDisk, step: 5, unit: 'GB' }))),
      cap && h('p', { className: 'hint mono', style: { fontSize: 11, marginTop: 4 } },
        `Node ${cap.node}: ${cap.mem.freeGb} GB RAM free · ${cap.cpu.cores} cores · ` +
        `${cap.storage.freeGb} GB free on ${cap.storage.name}`),
      capWarn && capWarn.msgs.length > 0 && h('p', {
        className: 'hint', style: { fontSize: 11, color: 'var(--warn, #d89b1c)', marginTop: 2 },
      }, '⚠ This VM ' + capWarn.msgs.join('; ') + ' — you can still deploy.'),
      capWarn && capWarn.cpuNote && h('p', {
        className: 'hint', style: { fontSize: 11, marginTop: 2 },
      }, capWarn.cpuNote),
      tpl && h('div', { className: 'divider' }),
      tpl && h('div', { className: 'row', style: { gap: 10 } },
        h(OSGlyph, { os: tpl.os, size: 26 }),
        h('div', { style: { minWidth: 0 } },
          h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, tpl.base || '—'),
          h('div', { className: 'hint mono', style: { fontSize: 11 } },
            cpu + ' vCPU · ' + mem + ' GB · ' + disk + ' GB · ' + (tpl.location || '—') + ' · ' + (tpl.blocks || []).length + ' blocks'))),
      tpl && h('p', { className: 'hint', style: { fontSize: 11 } },
        'Builds the VM fresh from ' + (tpl.base || 'the base image') + ' and applies the blocks — takes a few minutes; you watch it live.'));
  }

  window.DeployModal = DeployModal;
})();
