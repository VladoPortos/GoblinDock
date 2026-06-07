/* GoblinDock — Image Builder / Templates block canvas (working inputs). */
(function () {
  const { useState, useEffect } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Modal, OSGlyph, Field, TextArea, SelectField, Toggle, TagInput, FormModal, ConfirmModal } = window.UI;
  const h = React.createElement;

  let UID = 100;
  const SECTION_BY_NAME = { 'OS Setup': 's-os', 'Install': 's-inst', 'Configure': 's-conf', 'Scripts': 's-scr', 'Cleanup': 's-clean' };
  const blankSections = () => ([
    { id: 's-os', name: 'OS Setup', blocks: [] },
    { id: 's-inst', name: 'Install', blocks: [] },
    { id: 's-conf', name: 'Configure', blocks: [] },
    { id: 's-scr', name: 'Scripts', blocks: [] },
    { id: 's-clean', name: 'Cleanup', blocks: [] },
  ]);
  const paletteByKey = (ref) => (GD.PALETTE || []).find((p) => p.id === ref) || {};
  const refIcon = (ref) => paletteByKey(ref).icon || 'box';
  const refCat = (ref) => paletteByKey(ref).cat || 'Custom';

  function summaryOf(b) {
    const sc = paletteByKey(b.ref).schema || [];
    const parts = [];
    sc.forEach((f) => {
      const v = (b.inputs || {})[f.name];
      if (v == null || v === '') return;
      if (f.type === 'bool') { if (v) parts.push(f.label || f.name); }
      else if (Array.isArray(v)) { if (v.length) parts.push(v.slice(0, 4).join(', ')); }
      else parts.push(String(v).length > 28 ? String(v).slice(0, 28) + '…' : String(v));
    });
    return parts.join(' · ') || paletteByKey(b.ref).desc || '';
  }
  function warnOf(b) {
    const sc = paletteByKey(b.ref).schema || [];
    return sc.some((f) => (f.type === 'text' || f.type === 'secret') && !(b.inputs || {})[f.name]);
  }

  // ---------- Palette ----------
  function Palette({ onAdd, dragRef, onNewBlock }) {
    const [tab, setTab] = useState('builtin');
    const [q, setQ] = useState('');
    let blocks = (GD.PALETTE || []).filter((b) => (tab === 'builtin' ? b.builtin : !b.builtin));
    if (q) blocks = blocks.filter((b) => (b.name + b.cat + b.desc).toLowerCase().includes(q.toLowerCase()));
    const cats = {};
    blocks.forEach((b) => { (cats[b.cat] = cats[b.cat] || []).push(b); });
    return h('div', { className: 'bpane', style: { width: 256, borderRight: '1px solid var(--border-soft)' } },
      h('div', { className: 'bpane-head' },
        h('span', { className: 'panel-title' }, 'Block Palette'),
        h('button', { className: 'btn ghost sm', title: 'New custom block', style: { marginLeft: 'auto' }, onClick: onNewBlock }, h(Icon, { name: 'plus', size: 14 }))),
      h('div', { style: { padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 9 } },
        h('div', { className: 'seg', style: { width: '100%' } },
          h('button', { className: tab === 'builtin' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setTab('builtin') }, 'Built-in'),
          h('button', { className: tab === 'mine' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setTab('mine') }, 'My blocks')),
        h('div', { className: 'search', style: { minWidth: 0, height: 32 } },
          h(Icon, { name: 'search', size: 14 }),
          h('input', { placeholder: 'Search blocks…', value: q, onChange: (e) => setQ(e.target.value) }))),
      h('div', { style: { overflowY: 'auto', flex: 1, padding: '4px 12px 16px' } },
        Object.keys(cats).map((cat) => h('div', { key: cat, style: { marginBottom: 14 } },
          h('div', { className: 'nav-label', style: { padding: '6px 2px 8px' } }, cat),
          h('div', { style: { display: 'flex', flexDirection: 'column', gap: 7 } },
            cats[cat].map((b) => h('div', {
              key: b.id, className: 'palette-block', draggable: true,
              onDragStart: () => { dragRef.current = b; }, onClick: () => onAdd(b), title: 'Click or drag to add',
            },
              h('span', { className: 'pb-ico' }, h(Icon, { name: b.icon, size: 15 })),
              h('div', { style: { minWidth: 0 } },
                h('div', { className: 'mono', style: { fontSize: 12, fontWeight: 600 } }, b.name),
                h('div', { className: 'hint', style: { fontSize: 10.5 } }, b.desc)),
              h('span', { className: 'pb-grip' }, h(Icon, { name: 'grip', size: 14 }))))))),
        tab === 'mine' && Object.keys(cats).length === 0 && h('div', { className: 'hint', style: { padding: 12, textAlign: 'center' } }, 'No custom blocks yet. Click ＋ to create one.')));
  }

  // ---------- Canvas ----------
  function Canvas({ sections, sel, setSel, accepts, onDrop, onRemove, onDup, onMove }) {
    return h('div', { className: 'bpane', style: { flex: 1, background: 'var(--bg)' } },
      h('div', { style: { overflowY: 'auto', flex: 1, padding: '20px 24px 60px' } },
        h('div', { style: { maxWidth: 620, margin: '0 auto', display: 'flex', flexDirection: 'column' } },
          sections.map((sec, si) => h('div', { key: sec.id, className: 'canvas-section' },
            h('div', { className: 'cs-head' },
              h('span', { className: 'cs-num mono' }, si + 1),
              h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 12.5 } }, sec.name),
              h('span', { className: 'hint mono', style: { marginLeft: 'auto', fontSize: 10.5 } }, sec.blocks.length, sec.blocks.length === 1 ? ' block' : ' blocks')),
            h('div', {
              // category-locked: a block only drops into its own section. We skip
              // preventDefault() on a non-matching section so the browser shows the
              // native "no-drop" cursor and the drop never fires there.
              className: 'dropzone' + (sec.blocks.length === 0 ? ' empty' : ''),
              onDragOver: (e) => { if (!accepts(sec.id)) return; e.preventDefault(); e.currentTarget.classList.add('over'); },
              onDragLeave: (e) => e.currentTarget.classList.remove('over'),
              onDrop: (e) => { if (!accepts(sec.id)) return; e.preventDefault(); e.currentTarget.classList.remove('over'); onDrop(sec.id); },
            },
              sec.blocks.length === 0
                ? h('div', { className: 'dz-hint mono' }, h(Icon, { name: 'plus', size: 15 }), 'drop blocks here')
                : sec.blocks.map((b, bi) => h('div', {
                    key: b.uid, className: 'placed-block' + (sel === b.uid ? ' sel' : '') + (warnOf(b) ? ' warn' : ''),
                    onClick: () => setSel(b.uid),
                  },
                    h('span', { className: 'pb-grip', style: { cursor: 'grab' }, title: 'Reorder',
                      onClick: (e) => { e.stopPropagation(); onMove(sec.id, b.uid, 1); } }, h(Icon, { name: 'grip', size: 15 })),
                    h('span', { className: 'placed-ico' }, h(Icon, { name: refIcon(b.ref), size: 15 })),
                    h('div', { style: { minWidth: 0, flex: 1 } },
                      h('div', { className: 'row', style: { gap: 7 } },
                        h('span', { className: 'mono', style: { fontSize: 12.5, fontWeight: 600 } }, b.name),
                        (function () {
                          const ph = paletteByKey(b.ref).phase;
                          return h('span', { className: 'badge', style: { fontSize: 9, padding: '1px 5px', border: 'none', background: ph === 'cloudinit' ? 'var(--info-ghost)' : 'var(--accent-ghost)', color: ph === 'cloudinit' ? 'var(--info)' : 'var(--accent)' } }, ph === 'cloudinit' ? 'cloud-init' : 'ansible');
                        })(),
                        warnOf(b) && h('span', { className: 'badge', style: { background: 'var(--warn-ghost)', color: 'var(--warn)', border: 'none', padding: '1px 6px', fontSize: 10 } },
                          h(Icon, { name: 'warn', size: 11 }), 'needs input')),
                      h('div', { className: 'hint mono', style: { fontSize: 11, marginTop: 2 } }, summaryOf(b))),
                    h('div', { className: 'pb-actions' },
                      h('button', { className: 'icon-btn', title: 'Move up', onClick: (e) => { e.stopPropagation(); onMove(sec.id, b.uid, -1); } }, h(Icon, { name: 'chevronR', size: 14, style: { transform: 'rotate(-90deg)' } })),
                      h('button', { className: 'icon-btn', title: 'Duplicate', onClick: (e) => { e.stopPropagation(); onDup(sec.id, b); } }, h(Icon, { name: 'duplicate', size: 14 })),
                      h('button', { className: 'icon-btn danger', title: 'Remove', onClick: (e) => { e.stopPropagation(); onRemove(sec.id, b.uid); } }, h(Icon, { name: 'trash', size: 14 })))))))))));
  }

  // ---------- schema-driven field ----------
  function SecretPicker({ value, onChange }) {
    const secrets = GD.SECRETS || [];
    return h('div', { className: 'row', style: { gap: 6 } },
      h('div', { style: { flex: 1 } }, h(Field, { value, onChange, mono: true })),
      secrets.length > 0 && h('select', {
        className: 'select', style: { width: 38, padding: 0, textAlign: 'center' }, value: '',
        title: 'Insert a secret', onChange: (e) => { if (e.target.value) onChange('{{ secrets.' + e.target.value + ' }}'); },
      }, [h('option', { key: '', value: '' }, '🔒'), ...secrets.map((s) => h('option', { key: s.id, value: s.name }, s.name))]));
  }

  // ---------- CodeMirror-backed code editor ----------
  function CMEditor({ value, mode, onChange }) {
    const host = React.useRef(null);
    const cmRef = React.useRef(null);
    const onChangeRef = React.useRef(onChange);
    onChangeRef.current = onChange;
    React.useEffect(() => {
      if (!window.CodeMirror || !host.current) return undefined;
      const cm = window.CodeMirror(host.current, {
        value: value || '', mode: mode === 'python' ? 'python' : 'shell',
        theme: 'material-darker', lineNumbers: true, lineWrapping: true,
        indentUnit: 2, tabSize: 2, autofocus: true,
      });
      cm.setSize('100%', '60vh');
      cm.on('change', (inst) => onChangeRef.current(inst.getValue()));
      cmRef.current = cm;
      setTimeout(() => cm.refresh(), 30);
      return () => { const el = host.current; if (el) while (el.firstChild) el.removeChild(el.firstChild); cmRef.current = null; };
    }, []);
    React.useEffect(() => { if (cmRef.current) cmRef.current.setOption('mode', mode === 'python' ? 'python' : 'shell'); }, [mode]);
    if (!window.CodeMirror) {   // graceful fallback if the vendored editor failed to load
      return h('textarea', { className: 'input mono', defaultValue: value, onChange: (e) => onChange(e.target.value),
        style: { width: '100%', height: '60vh', resize: 'vertical', border: 'none', borderRadius: 0 } });
    }
    return h('div', { ref: host, style: { fontSize: 13.5 } });
  }

  function CodeField({ label, value, onChange }) {
    const [open, setOpen] = useState(false);
    const looksPy = /(^|\n)\s*(import |from \S+ import |def |print\()/.test(value || '');
    const [mode, setMode] = useState(looksPy ? 'python' : 'bash');
    const lines = (value || '').split('\n');
    return h('div', null,
      h('label', { className: 'field-label' }, label),
      h('div', { onClick: () => setOpen(true), title: 'Open editor',
        style: { cursor: 'pointer', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--inset)', padding: '9px 11px', overflow: 'hidden' } },
        h('pre', { className: 'mono', style: { margin: 0, fontSize: 11.5, color: value ? 'var(--text-dim)' : 'var(--text-faint)', whiteSpace: 'pre', overflow: 'hidden', maxHeight: 78 } },
          (lines.slice(0, 4).join('\n')) || '# empty — click to edit'),
        h('div', { className: 'row', style: { marginTop: 8, gap: 8 } },
          h('button', { className: 'btn sm', onClick: (e) => { e.stopPropagation(); setOpen(true); } }, h(Icon, { name: 'code', size: 13 }), 'Open editor'),
          h('span', { className: 'hint mono', style: { fontSize: 10.5, marginLeft: 'auto' } }, lines.length, lines.length === 1 ? ' line' : ' lines'))),
      open && h(Modal, { onClose: () => setOpen(false), width: 'min(940px, 96vw)' },
        h('div', { className: 'modal-head' },
          h(Icon, { name: 'code', size: 17, style: { color: 'var(--accent)' } }),
          h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, label || 'Script'),
          h('div', { className: 'seg', style: { marginLeft: 12 } },
            h('button', { className: mode === 'bash' ? 'active' : '', onClick: () => setMode('bash') }, 'Bash'),
            h('button', { className: mode === 'python' ? 'active' : '', onClick: () => setMode('python') }, 'Python')),
          h('button', { className: 'btn primary sm', style: { marginLeft: 'auto' }, onClick: () => setOpen(false) }, 'Done')),
        h('div', { style: { padding: 0 } }, h(CMEditor, { value, mode, onChange }))));
  }

  function SchemaField({ field, value, onChange }) {
    const label = field.label || field.name;
    if (field.type === 'bool') return h(Toggle, { label, on: !!value, onChange });
    if (field.type === 'tags') return h(TagInput, { label, tags: Array.isArray(value) ? value : [], onChange });
    if (field.type === 'code') return h(CodeField, { label, value, onChange });
    if (field.type === 'select') return h(SelectField, { label, value, onChange, options: field.options || [] });
    if (field.type === 'secret') return h('div', null, h('label', { className: 'field-label' }, label), h(SecretPicker, { value, onChange }));
    return h(Field, { label, value, onChange, mono: field.type === 'text' });
  }

  // labelled field with a leading icon — used by the right-hand "spec" panel
  function SpecField({ icon, label, children }) {
    return h('div', null,
      h('div', { className: 'row', style: { gap: 6, marginBottom: 6 } },
        h(Icon, { name: icon, size: 13, style: { color: 'var(--accent)' } }),
        h('label', { className: 'field-label', style: { margin: 0 } }, label)),
      children);
  }

  // ---------- Inspector ----------
  function Inspector({ sections, sel, mode, meta, setInput }) {
    let block = null, secId = null;
    sections.forEach((s) => s.blocks.forEach((b) => { if (b.uid === sel) { block = b; secId = s.id; } }));
    if (!block) {
      return h('div', { className: 'bpane', style: { width: 308, borderLeft: '1px solid var(--border-soft)', background: 'var(--surface-2)', borderTop: '2px solid var(--accent)' } },
        h('div', { className: 'bpane-head' }, h('span', { className: 'panel-title' }, mode === 'golden' ? 'Golden image' : 'Template')),
        h('div', { style: { padding: 16, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 14 } },
          h(SpecField, { icon: 'tag', label: mode === 'golden' ? 'Image name' : 'Template name' },
            h(Field, { value: meta.name, onChange: meta.setName, mono: true })),
          meta.imageSelect && h(SpecField, { icon: meta.imageSelect.icon || 'disk', label: meta.imageSelect.label },
            h(SelectField, { value: meta.imageSelect.value || '', onChange: (v) => meta.imageSelect.set(Number(v) || null), options: meta.imageSelect.options })),
          meta.locationSelect && h(SpecField, { icon: meta.locationSelect.icon || 'server', label: meta.locationSelect.label },
            h(SelectField, { value: meta.locationSelect.value || '', onChange: (v) => meta.locationSelect.set(Number(v) || null), options: meta.locationSelect.options })),
          mode === 'golden' && !meta.imageSelect && h(SpecField, { icon: 'disk', label: 'Base image' },
            h('div', { className: 'row', style: { gap: 8, padding: '8px 10px', background: 'var(--inset)', borderRadius: 9, border: '1px solid var(--border)' } },
              h(OSGlyph, { os: meta.os, size: 18 }), h('span', { className: 'mono', style: { fontSize: 12.5 } }, meta.base))),
          h('div', { className: 'divider' }),
          mode === 'golden'
            ? h(SpecField, { icon: 'cpu', label: 'Disk (GB)' },
                h(Field, { value: meta.disk, onChange: meta.setDisk, mono: true }))
            : h(SpecField, { icon: 'cpu', label: 'Default resources' },
                h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 } },
                  h(Field, { label: 'vCPU', value: meta.cpu, onChange: meta.setCpu, mono: true }),
                  h(Field, { label: 'RAM', value: meta.mem, onChange: meta.setMem, mono: true }),
                  h(Field, { label: 'Disk', value: meta.disk, onChange: meta.setDisk, mono: true }))),
          h('div', { className: 'card', style: { padding: 13, background: 'var(--surface)', display: 'flex', gap: 10 } },
            h(Icon, { name: 'info', size: 15, style: { color: 'var(--text-faint)', flexShrink: 0, marginTop: 1 } }),
            h('p', { className: 'hint', style: { fontSize: 11.5 } }, mode === 'golden'
              ? 'Blocks here are baked INTO the image. Cloud-init blocks run at first boot; ansible blocks run post-boot.'
              : 'These blocks run on every VM you deploy with this template.'))));
    }
    const schema = paletteByKey(block.ref).schema || [];
    return h('div', { className: 'bpane', style: { width: 308, borderLeft: '1px solid var(--border-soft)', background: 'var(--surface-2)', borderTop: '2px solid var(--accent)' } },
      h('div', { className: 'bpane-head' },
        h('span', { className: 'placed-ico', style: { width: 22, height: 22 } }, h(Icon, { name: refIcon(block.ref), size: 14 })),
        h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 12.5 } }, block.name)),
      h('div', { style: { padding: 16, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 14 } },
        h('div', { className: 'chip', style: { width: 'fit-content' } }, refCat(block.ref)),
        schema.length === 0
          ? h('p', { className: 'hint', style: { fontSize: 12 } }, 'This block has no inputs.')
          : schema.map((f) => h(SchemaField, { key: f.name, field: f, value: (block.inputs || {})[f.name], onChange: (v) => setInput(block.uid, f.name, v) }))));
  }

  // ---------- Custom block editor ----------
  function BlockEditorModal({ initial, onClose, onSaved }) {
    const editing = initial && !initial.builtin && initial.key;
    const [f, setF] = useState(() => ({
      name: initial ? (editing ? initial.name : initial.name + ' (copy)') : 'My block',
      category: (initial && initial.cat) || 'Custom', icon: (initial && initial.icon) || 'spark',
      section: (initial && initial.section) || 'Scripts', phase: (initial && initial.phase) || 'ansible',
      description: (initial && initial.desc) || '',
      cloudinit: (initial && initial.cloudinit) || 'echo hello', ansible: (initial && initial.ansible) || '- name: my task\n  ansible.builtin.debug: { msg: hi }',
      schema: (initial && initial.schema) ? JSON.parse(JSON.stringify(initial.schema)) : [],
    }));
    const [busy, setBusy] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const setField = (i, k, v) => setF((p) => ({ ...p, schema: p.schema.map((x, j) => j === i ? { ...x, [k]: v } : x) }));
    const addField = () => set('schema', [...f.schema, { name: 'var' + (f.schema.length + 1), type: 'text', default: '' }]);
    const submit = async () => {
      setBusy(true);
      try {
        const payload = { name: f.name, category: f.category, icon: f.icon, section: f.section, phase: f.phase, description: f.description, cloudinit_template: f.cloudinit, ansible_template: f.ansible, input_schema: f.schema };
        if (editing) await window.API.editBlock(initial.key, payload);
        else await window.API.createBlock(payload);
        onSaved();
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit block' : 'New custom block', icon: 'blocks', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save block' : 'Create block', width: 'min(600px, 95vw)' },
      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
        h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v) }),
        h(SelectField, { label: 'Section', value: f.section, onChange: (v) => set('section', v), options: ['OS Setup', 'Install', 'Configure', 'Scripts', 'Cleanup'] }),
        h(SelectField, { label: 'Phase', value: f.phase, onChange: (v) => set('phase', v), options: [{ value: 'cloudinit', label: 'cloud-init (first boot)' }, { value: 'ansible', label: 'ansible (post-boot)' }] }),
        h(SelectField, { label: 'Icon', value: f.icon, onChange: (v) => set('icon', v), options: ['spark', 'code', 'package', 'settings', 'sliders', 'docker', 'key', 'file', 'trash'] })),
      h(Field, { label: 'Description', value: f.description, onChange: (v) => set('description', v) }),
      f.phase === 'cloudinit'
        ? h(TextArea, { label: 'Cloud-init (shell run at first boot)', value: f.cloudinit, onChange: (v) => set('cloudinit', v), rows: 4, mono: true })
        : h(TextArea, { label: 'Ansible task(s) (YAML, run post-boot)', value: f.ansible, onChange: (v) => set('ansible', v), rows: 5, mono: true }),
      h('div', null,
        h('div', { className: 'row', style: { marginBottom: 6 } },
          h('label', { className: 'field-label', style: { margin: 0 } }, 'Inputs'),
          h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: addField }, h(Icon, { name: 'plus', size: 13 }), 'Add input')),
        f.schema.map((fld, i) => h('div', { key: i, className: 'row', style: { gap: 6, marginBottom: 6 } },
          h('input', { className: 'input mono', style: { flex: 1 }, value: fld.name, placeholder: 'name', onChange: (e) => setField(i, 'name', e.target.value) }),
          h('select', { className: 'select', style: { width: 100 }, value: fld.type, onChange: (e) => setField(i, 'type', e.target.value) },
            ['text', 'tags', 'bool', 'select', 'code', 'secret'].map((t) => h('option', { key: t, value: t }, t))),
          h('input', { className: 'input mono', style: { flex: 1 }, value: fld.default == null ? '' : fld.default, placeholder: 'default', onChange: (e) => setField(i, 'default', e.target.value) }),
          h('button', { className: 'icon-btn danger', onClick: () => set('schema', f.schema.filter((_, j) => j !== i)) }, h(Icon, { name: 'trash', size: 14 })))),
        f.schema.length === 0 && h('div', { className: 'hint', style: { fontSize: 11.5 } }, 'No inputs — the block runs as-is.')));
  }

  // ---------- Shell ----------
  function Builder({ go, mode }) {
    // mode: 'golden' (base + location + bake-time blocks) | 'template' (runtime blocks)
    const nav = window.GDStore.nav || {};
    const loadedImg = mode === 'golden' && nav.imageId ? (GD.GOLDEN_IMAGES || []).find((g) => g.imgId === nav.imageId) : null;
    const loadedTpl = mode === 'template' && nav.templateId ? (GD.TEMPLATES || []).find((r) => r.templateId === nav.templateId) : null;
    const firstBase = (nav.baseImageId ? (GD.BASE_IMAGES || []).find((b) => b.imgId === nav.baseImageId) : null) || (GD.BASE_IMAGES || [])[0] || {};

    const initSections = () => {
      const src = (loadedImg && loadedImg.recipe) || (loadedTpl && loadedTpl.recipe) || null;
      if (!src || !src.length) return blankSections();
      const out = blankSections();
      const byId = Object.fromEntries(out.map((s) => [s.id, s]));
      src.forEach((sec) => {
        const target = byId[sec.id] || byId[SECTION_BY_NAME[sec.name]] || out[1];
        (sec.blocks || []).forEach((b) => target.blocks.push({ uid: 'u' + (UID++), ref: b.ref, name: b.name || paletteByKey(b.ref).name || b.ref, inputs: b.inputs || {} }));
      });
      return out;
    };

    const [sections, setSections] = useState(initSections);
    const [sel, setSel] = useState(null);
    const [yaml, setYaml] = useState(false);
    const [yamlText, setYamlText] = useState('# compiling…');
    const [busy, setBusy] = useState(false);
    const busyRef = React.useRef(false);
    const [blockModal, setBlockModal] = useState(null);
    const [rebuildAsk, setRebuildAsk] = useState(false);
    const [buildAsk, setBuildAsk] = useState(false);
    const dragRef = React.useRef(null);

    const bases = GD.BASE_IMAGES || [];
    const conns = (GD.CONNECTIONS || []);
    const [recipeName, setRecipeName] = useState(loadedImg ? loadedImg.name : loadedTpl ? loadedTpl.name : (mode === 'golden' ? 'gd-custom' : 'Custom template'));
    const [cpu, setCpu] = useState(loadedTpl ? loadedTpl.cpu : 1);
    const [mem, setMem] = useState(loadedTpl ? loadedTpl.mem : 2);
    const [disk, setDisk] = useState(loadedImg ? 20 : (loadedTpl ? loadedTpl.disk : 20));
    // golden-mode: base + location (which Proxmox + node)
    const [selBaseId, setSelBaseId] = useState(loadedImg ? null : (firstBase.imgId || null));
    const [selConnId, setSelConnId] = useState(loadedImg ? loadedImg.connId : ((conns[0] && conns[0].connId) || null));
    const selBase = bases.find((b) => b.imgId === selBaseId) || firstBase;
    const selConn = conns.find((c) => c.connId === selConnId) || conns[0] || {};

    let metaBase, metaOs, imageSelect = null, locationSelect = null;
    if (mode === 'golden') {
      if (loadedImg) { metaBase = loadedImg.base; metaOs = loadedImg.osFamily; }
      else {
        metaBase = selBase.name || 'Ubuntu 24.04 LTS'; metaOs = selBase.os || 'ubuntu';
        imageSelect = { label: 'Base image', value: selBaseId, set: setSelBaseId,
          options: bases.map((b) => ({ value: b.imgId, label: b.name })) };
        locationSelect = { label: 'Location (Proxmox)', value: selConnId, set: setSelConnId,
          options: conns.map((c) => ({ value: c.connId, label: c.name + ' · ' + (c.node || 'auto') })) };
      }
    } else {
      metaBase = ''; metaOs = loadedTpl ? loadedTpl.os : 'ubuntu';
    }
    const meta = {
      name: recipeName, setName: setRecipeName, base: metaBase, os: metaOs,
      cpu, setCpu, mem, setMem, disk, setDisk, imageSelect, locationSelect,
    };

    const recipePayload = () => sections.map((s) => ({
      id: s.id, name: s.name,
      blocks: s.blocks.map((b) => ({ ref: b.ref, name: b.name, inputs: b.inputs || {} })),
    }));
    const sectionFor = (block) => SECTION_BY_NAME[block.section] || SECTION_BY_NAME[block.cat] || 's-inst';
    const addBlock = (block, secId) => {
      const sid = secId || sectionFor(block);
      const uid = 'u' + (UID++);
      const inputs = {};
      (block.schema || []).forEach((f) => { inputs[f.name] = f.default; });
      setSections((prev) => prev.map((s) => s.id === sid ? { ...s, blocks: [...s.blocks, { uid, ref: block.id, name: block.name, inputs }] } : s));
      setSel(uid);
    };
    // a block accepts only its own category section (drives the drop lock + cursor)
    const accepts = (secId) => { const b = dragRef.current; return !b || sectionFor(b) === secId; };
    const onDrop = (secId) => {
      const b = dragRef.current; dragRef.current = null;
      if (b && sectionFor(b) === secId) addBlock(b, secId);
    };
    const onRemove = (secId, uid) => setSections((prev) => prev.map((s) => s.id === secId ? { ...s, blocks: s.blocks.filter((b) => b.uid !== uid) } : s));
    const onDup = (secId, b) => { const uid = 'u' + (UID++); setSections((prev) => prev.map((s) => s.id === secId ? { ...s, blocks: [...s.blocks, { ...b, uid, inputs: { ...(b.inputs || {}) } }] } : s)); };
    const onMove = (secId, uid, dir) => setSections((prev) => prev.map((s) => {
      if (s.id !== secId) return s;
      const idx = s.blocks.findIndex((b) => b.uid === uid);
      const ni = idx + dir;
      if (idx < 0 || ni < 0 || ni >= s.blocks.length) return s;
      const arr = s.blocks.slice(); const [m] = arr.splice(idx, 1); arr.splice(ni, 0, m);
      return { ...s, blocks: arr };
    }));
    const setInput = (uid, name, value) => setSections((prev) => prev.map((s) => ({ ...s, blocks: s.blocks.map((b) => b.uid === uid ? { ...b, inputs: { ...(b.inputs || {}), [name]: value } } : b) })));

    const openYaml = async () => {
      setYaml(true); setYamlText('# compiling…');
      try { const r = await window.API.compile(recipePayload(), recipeName); setYamlText(r.yaml); }
      catch (e) { setYamlText('# compile failed: ' + (e.message || '')); }
    };
    const doBuildOrSave = async () => {
      if (busyRef.current) return;       // ref latch — block a double-click double-submit
      busyRef.current = true;
      setBusy(true);
      try {
        if (mode === 'golden' && loadedImg) {
          // editing an existing golden image: update its baked recipe, then rebuild IT
          await window.API.editImage(loadedImg.imgId, { name: recipeName.trim(), recipe: recipePayload() });
          const r = await window.API.rebuildGolden(loadedImg.imgId);
          go('job', { jobId: r.jobId });
        } else if (mode === 'golden') {
          if (!selBase.imgId) { window.GDStore.toast('Add a base image (ISO) first', 'err'); setBusy(false); busyRef.current = false; return; }
          const r = await window.API.buildGolden({
            name: recipeName.trim() || 'gd-custom', os_family: selBase.os || 'ubuntu',
            baseImageId: selBase.imgId, connectionId: selConn.connId, node: selConn.node || null,
            recipe: recipePayload(), disk: Number(disk) || 20,
          });
          go('job', { jobId: r.jobId });
        } else if (loadedTpl) {
          await window.API.editTemplate(loadedTpl.templateId, { name: recipeName.trim(), recipe: recipePayload(), cpu: Number(cpu), ram: Number(mem), disk: Number(disk), public: loadedTpl.public });
          window.GDStore.toast('Template updated', 'ok'); await window.GDStore.refresh().catch(() => {}); go('templates');
        } else {
          await window.API.saveTemplate({ name: recipeName.trim() || 'Custom template', recipe: recipePayload(), cpu: Number(cpu), ram: Number(mem), disk: Number(disk) });
          window.GDStore.toast('Template saved', 'ok'); await window.GDStore.refresh().catch(() => {}); go('templates');
        }
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); setBusy(false); busyRef.current = false; }
    };

    const blockCount = sections.reduce((a, s) => a + s.blocks.length, 0);
    const warnCount = sections.reduce((a, s) => a + s.blocks.filter(warnOf).length, 0);

    return h('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
      h('div', { className: 'builder-bar' },
        h('div', { className: 'row', style: { gap: 10, minWidth: 0 } },
          h('button', { className: 'btn ghost sm', title: 'Back', onClick: () => go(mode === 'golden' ? 'golden' : 'templates') },
            h(Icon, { name: 'chevronL', size: 16 }), mode === 'golden' ? 'Golden Images' : 'Templates'),
          h(Icon, { name: mode === 'golden' ? 'hammer' : 'template', size: 17, style: { color: 'var(--accent)' } }),
          h('input', { className: 'recipe-name mono', value: recipeName, onChange: (e) => setRecipeName(e.target.value) }),
          h('span', { className: 'chip', style: { gap: 6 } }, h(OSGlyph, { os: meta.os, size: 14 }), meta.base),
          h('span', { className: 'hint mono', style: { fontSize: 11 } }, blockCount, ' blocks'),
          warnCount > 0 && h('span', { className: 'badge', style: { background: 'var(--warn-ghost)', color: 'var(--warn)', border: 'none' } }, h(Icon, { name: 'warn', size: 12 }), warnCount, ' need input')),
        h('div', { className: 'row', style: { marginLeft: 'auto', gap: 8 } },
          h('button', { className: 'btn sm', onClick: openYaml }, h(Icon, { name: 'code', size: 15 }), 'View YAML'),
          h('button', { className: 'btn primary sm', onClick: () => { if (mode === 'golden' && loadedImg) setRebuildAsk(true); else if (mode === 'golden') setBuildAsk(true); else doBuildOrSave(); }, disabled: busy }, h(Icon, { name: mode === 'golden' ? 'hammer' : 'check', size: 15 }), busy ? 'Working…' : (mode === 'golden' ? (loadedImg ? 'Rebuild' : 'Build image') : (loadedTpl ? 'Save changes' : 'Save template'))))),
      h('div', { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(Palette, { onAdd: (b) => addBlock(b), dragRef, onNewBlock: () => setBlockModal({ new: true }) }),
        h(Canvas, { sections, sel, setSel, accepts, onDrop, onRemove, onDup, onMove }),
        h(Inspector, { sections, sel, mode, meta, setInput })),
      yaml && h(Modal, { onClose: () => setYaml(false), width: 'min(680px, 94vw)' },
        h('div', { className: 'modal-head' },
          h(Icon, { name: 'code', size: 17, style: { color: 'var(--accent)' } }),
          h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, 'Generated Ansible playbook'),
          h('span', { className: 'badge', style: { marginLeft: 6 } }, 'read-only'),
          h('button', { className: 'icon-btn', style: { marginLeft: 'auto' }, onClick: () => setYaml(false) }, h(Icon, { name: 'x', size: 16 }))),
        h('div', { style: { padding: 0 } },
          h('pre', { className: 'logpane', style: { margin: 0, borderRadius: 0, border: 'none', maxHeight: '60vh', fontSize: 12 } }, yamlText))),
      blockModal && h(BlockEditorModal, { initial: blockModal.initial, onClose: () => setBlockModal(null), onSaved: () => { setBlockModal(null); window.GDStore.toast('Block saved', 'ok'); window.GDStore.refresh().catch(() => {}); } }),
      buildAsk && h(ConfirmModal, {
        onClose: () => setBuildAsk(false), tone: 'accent', icon: 'hammer',
        title: 'Build ' + recipeName + '?',
        body: 'This immediately creates a temporary build VM on ' + (selConn.name || 'your Proxmox target')
          + (selConn.node ? ' · ' + selConn.node : '')
          + ', runs the blocks, and converts it into a reusable template. You can watch progress live.',
        confirmLabel: 'Build image', onConfirm: () => { setBuildAsk(false); doBuildOrSave(); },
      }),
      rebuildAsk && h(ConfirmModal, {
        onClose: () => setRebuildAsk(false), tone: 'danger', icon: 'rebuild',
        title: 'Rebuild ' + recipeName + '?',
        body: 'This deletes the current template (vmid ' + ((loadedImg && loadedImg.vmid) || '—') + ') and builds a fresh one with these blocks. Already-deployed VMs keep running and are unaffected.',
        confirmLabel: 'Delete & rebuild', onConfirm: () => { setRebuildAsk(false); doBuildOrSave(); },
      }));
  }

  window.Builder = Builder;
  window.BlockEditorModal = BlockEditorModal;
})();
