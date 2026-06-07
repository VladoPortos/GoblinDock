/* GoblinDock — Deploy VM: pick a golden image + optional template + size. */
(function () {
  const { useState, useEffect } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph } = window.UI;
  const h = React.createElement;

  function SectionLabel({ n, title, hint }) {
    return h('div', { className: 'row', style: { marginBottom: 14, gap: 10 } },
      h('span', { style: {
        width: 22, height: 22, borderRadius: 7, display: 'grid', placeItems: 'center', flexShrink: 0,
        background: 'var(--accent-ghost)', color: 'var(--accent)',
        fontFamily: 'JetBrains Mono, monospace', fontWeight: 700, fontSize: 11.5,
      } }, n),
      h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, title),
      hint && h('span', { className: 'hint', style: { marginLeft: 'auto' } }, hint));
  }

  function EmptyDeploy({ go }) {
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('button', { className: 'btn ghost sm', onClick: () => go('dashboard'), style: { marginRight: 4 } },
          h(Icon, { name: 'chevronL', size: 16 }), 'VMs'),
        h('div', null, h('h1', { className: 'page-title' }, 'Deploy a VM'))),
      h('div', { className: 'card' }, h('div', { className: 'empty' },
        h('div', { className: 'glyph' }, h(Icon, { name: 'package', size: 30 })),
        h('h3', null, 'No golden images yet'),
        h('p', null, 'Build a golden image first — you deploy VMs from golden images.'),
        h('button', { className: 'btn primary', onClick: () => go('builder') },
          h(Icon, { name: 'hammer', size: 16 }), 'Build a golden image'))));
  }

  function Deploy({ go }) {
    const goldens = (GD.GOLDEN_IMAGES || []).filter(g => g.deployable);
    if (goldens.length === 0) return h(EmptyDeploy, { go });

    const templates = GD.TEMPLATES || [];

    const navGold = (window.GDStore.nav && window.GDStore.nav.goldenImageId)
      ? goldens.find(g => g.imgId === window.GDStore.nav.goldenImageId) : null;
    const [gold, setGold] = useState(navGold || goldens[0]);

    // per-VM ceilings come from the golden's target connection (0 => global default)
    const _conn = (GD.CONNECTIONS || []).find(c => c.connId === (gold && gold.connId)) || {};
    const _gl = GD.limits || {};
    const maxCpu = _conn.maxCores || _gl.maxCores || 1;
    const maxMem = _conn.maxRamGb || _gl.maxRam || 2;
    const maxDisk = _conn.maxDiskGb || 500;

    const [templateId, setTemplateId] = useState(null);
    const [cpu, setCpu] = useState(maxCpu);
    const [mem, setMem] = useState(maxMem);
    const [disk, setDisk] = useState(Math.min(20, maxDisk));
    const [name, setName] = useState(() => {
      const used = new Set((GD.VMS || []).map((v) => v.name));
      let n = (GD.VMS || []).length + 1;
      while (used.has('gd-' + n)) n++;
      return 'gd-' + n;
    });
    const [tags, setTags] = useState('');
    const [busy, setBusy] = useState(false);
    const busyRef = React.useRef(false);

    const tpl = templates.find(r => r.templateId === templateId);
    const pickTemplate = (id) => {
      setTemplateId(id);
      const t = templates.find(x => x.templateId === id);
      if (t) { setCpu(Math.min(t.cpu, maxCpu)); setMem(Math.min(t.mem, maxMem)); setDisk(Math.min(t.disk, maxDisk)); }
    };

    // network from the golden image's connection (the template lives there)
    const nets = (GD.NETWORKS || []).filter(n => n.connId === gold.connId);
    const [netId, setNetId] = useState((nets[0] && nets[0].netId) || null);
    useEffect(() => { if (!nets.some(n => n.netId === netId)) setNetId((nets[0] && nets[0].netId) || null); }, [gold && gold.connId]);
    // clamp size to the target's limits when the golden (hence connection) changes
    useEffect(() => { setCpu(c => Math.min(c, maxCpu)); setMem(m => Math.min(m, maxMem)); setDisk(d => Math.min(d, maxDisk)); }, [gold && gold.connId]);
    const netObj = nets.find(n => n.netId === netId) || nets[0] || {};

    const doDeploy = async () => {
      // ref latch: a fast double-click fires two onClicks before the disabled-button
      // re-render commits; without this both would POST and create two VMs.
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      try {
        const r = await window.API.deploy({
          goldenImageId: gold.imgId, templateId: templateId || null,
          networkId: netObj.netId || null, name: name.trim(), cpu, ram: mem, disk,
          tags,
        });
        go('job', { jobId: r.jobId });
      } catch (e) { window.GDStore.toast(e.message || 'deploy failed', 'err'); setBusy(false); busyRef.current = false; }
    };

    return h('div', { className: 'page fadein', style: { maxWidth: 1080 } },
      h('div', { className: 'page-head' },
        h('button', { className: 'btn ghost sm', onClick: () => go('dashboard'), style: { marginRight: 4 } },
          h(Icon, { name: 'chevronL', size: 16 }), 'VMs'),
        h('div', null,
          h('h1', { className: 'page-title' }, 'Deploy a VM'),
          h('div', { className: 'page-sub' }, 'Pick a golden image — GoblinDock clones, names and tracks the rest.'))),

      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 332px', gap: 18, alignItems: 'start' } },
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 18 } },
          // 1. golden image
          h('div', { className: 'card card-pad' },
            h(SectionLabel, { n: '1', title: 'Golden image' }),
            h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 10 } },
              goldens.map(g => h('button', {
                key: g.id, onClick: () => setGold(g), className: 'tpl-card' + (gold.id === g.id ? ' sel' : ''),
              },
                h('div', { className: 'row', style: { gap: 9 } },
                  h(OSGlyph, { os: g.os, size: 22 }),
                  h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, g.name),
                  gold.id === g.id && h('span', { style: { marginLeft: 'auto', color: 'var(--accent)' } }, h(Icon, { name: 'check', size: 16 }))),
                h('div', { className: 'row mono', style: { gap: 8, fontSize: 10.5, color: 'var(--text-faint)' } },
                  h(Icon, { name: 'server', size: 12 }), g.location || '—'))))),

          // 2. template (optional)
          h('div', { className: 'card card-pad' },
            h(SectionLabel, { n: '2', title: 'Template', hint: 'optional preset' }),
            h('select', { className: 'select', value: templateId || '', onChange: (e) => pickTemplate(e.target.value ? Number(e.target.value) : null) },
              h('option', { value: '' }, 'None — plain VM from the golden image'),
              templates.map(t => h('option', { key: t.id, value: t.templateId }, t.name + ' (' + (t.blocks || []).length + ' blocks)'))),
            tpl && h('div', { className: 'hint', style: { marginTop: 8, fontSize: 12 } }, tpl.desc || 'Applied on top of the deployed VM.')),

          // 3. where + size
          h('div', { className: 'card card-pad' },
            h(SectionLabel, { n: '3', title: 'Where & size', hint: 'capped at ' + maxCpu + ' vCPU · ' + maxMem + ' GB' }),
            h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 16 } },
              h('div', null,
                h('label', { className: 'field-label' }, 'Location'),
                h('div', { className: 'input mono', style: { display: 'flex', alignItems: 'center' } }, gold.location || '—')),
              h('div', null,
                h('label', { className: 'field-label' }, 'Network'),
                nets.length
                  ? h('select', { className: 'select', value: netId || '', onChange: (e) => setNetId(Number(e.target.value)) },
                      nets.map(n => h('option', { key: n.id, value: n.netId }, n.name + ' · ' + n.mode)))
                  : h('div', { className: 'input', style: { display: 'flex', alignItems: 'center' } }, 'DHCP'))),
            h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 } },
              h(SizeField, { icon: 'cpu', label: 'vCPU', value: cpu, set: setCpu, min: 1, max: maxCpu, unit: '' }),
              h(SizeField, { icon: 'ram', label: 'Memory', value: mem, set: setMem, min: 1, max: maxMem, step: 1, unit: 'GB' }),
              h(SizeField, { icon: 'disk', label: 'Disk', value: disk, set: setDisk, min: 10, max: maxDisk, step: 5, unit: 'GB' }))),

          // 4. name
          h('div', { className: 'card card-pad' },
            h(SectionLabel, { n: '4', title: 'Name & tags' }),
            h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 14 } },
              h('div', null,
                h('label', { className: 'field-label' }, 'Name'),
                h('input', { className: 'input mono', value: name, onChange: (e) => setName(e.target.value) })),
              h('div', null,
                h('label', { className: 'field-label' }, 'Tags · optional'),
                h('input', { className: 'input', placeholder: 'e.g. project:atlas', value: tags, onChange: (e) => setTags(e.target.value) }))))),

        // summary rail
        h('div', { className: 'card', style: { position: 'sticky', top: 0, overflow: 'hidden' } },
          h('div', { style: { padding: '16px 18px', borderBottom: '1px solid var(--border-soft)' } },
            h('div', { className: 'panel-title' }, 'Summary')),
          h('div', { style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 13 } },
            h('div', { className: 'row', style: { gap: 10 } },
              h(OSGlyph, { os: gold.os, size: 30 }),
              h('div', null,
                h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 15 } }, name || 'gd-vm'),
                h('div', { className: 'hint', style: { fontSize: 11.5 } }, 'from ', gold.name))),
            h('div', { className: 'divider' }),
            h(Row, { k: 'Golden image', v: gold.name }),
            h(Row, { k: 'Template', v: tpl ? tpl.name : 'none' }),
            h(Row, { k: 'Location', v: gold.location || '—' }),
            h(Row, { k: 'Network', v: netObj.name || 'DHCP' }),
            h(Row, { k: 'Resources', v: cpu + ' vCPU · ' + mem + ' GB · ' + disk + ' GB' }),
            h('div', { className: 'divider' }),
            h('button', { className: 'btn primary', style: { width: '100%', height: 42, fontSize: 13.5 }, onClick: doDeploy, disabled: busy },
              h(Icon, { name: 'play', size: 16 }), busy ? 'Starting…' : ('Deploy ' + (name || 'VM'))),
            h('p', { className: 'hint', style: { fontSize: 11, textAlign: 'center' } }, 'Takes you straight to live progress.')))));
  }

  function SizeField({ icon, label, value, set, min, max, step, unit }) {
    const span = Math.max(1, max - min);
    const pct = ((value - min) / span) * 100;
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 10, color: 'var(--text-dim)' } },
        h(Icon, { name: icon, size: 15 }),
        h('span', { className: 'field-label', style: { margin: 0 } }, label),
        h('span', { className: 'mono', style: { marginLeft: 'auto', fontSize: 14, fontWeight: 700, color: 'var(--text)' } }, value, unit ? ' ' + unit : '')),
      h('input', { type: 'range', className: 'range', min, max, step: step || 1, value, disabled: max <= min,
        onChange: (e) => set(Number(e.target.value)), style: { '--pct': pct + '%' } }));
  }

  function Row({ k, v }) {
    return h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 12 } },
      h('span', { className: 'hint', style: { fontSize: 12 } }, k),
      h('span', { className: 'mono', style: { fontSize: 12, fontWeight: 600, textAlign: 'right' } }, v));
  }

  window.Deploy = Deploy;
})();
