/* GoblinDock — Dashboard (VMs) */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph, StatusBadge, CopyField, Meter, Sparkline, Menu, ConfirmModal, FormModal, Field } = window.UI;
  const h = React.createElement;

  // Transitional label for the uptime cell while an optimistic power action is in
  // flight (store sets vm._act + status='working') — gives immediate visual feedback
  // instead of the cell sitting at '—'/'offline' until the real Proxmox status lands.
  const _actLabel = (a) => ({ start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' }[a] || 'Working…');

  const VIEWS_KEY = 'gd.savedViews';
  function parseTags(s) { return (s || '').split(',').map((t) => t.trim()).filter(Boolean); }
  function loadViews() { try { return JSON.parse(localStorage.getItem(VIEWS_KEY)) || []; } catch (e) { return []; } }
  function saveViews(v) { try { localStorage.setItem(VIEWS_KEY, JSON.stringify(v)); } catch (e) { /* quota/denied */ } }

  function VmEditModal({ vm, onClose, onDone }) {
    const [name, setName] = useState(vm.name);
    const [tags, setTags] = useState(vm.tags || '');
    const [notes, setNotes] = useState(vm.notes || '');
    const [busy, setBusy] = useState(false);
    const submit = async () => {
      setBusy(true);
      try { await window.API.patchVm(vm.depId, { name: name.trim(), tags, notes }); onDone(); }
      catch (e) { window.GDStore.toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: 'Edit ' + vm.name, icon: 'edit', onClose, onSubmit: submit, busy },
      h(Field, { label: 'Name', value: name, onChange: setName, mono: true }),
      h(Field, { label: 'Tags', value: tags, onChange: setTags, placeholder: 'project:atlas, env:dev' }),
      h(Field, { label: 'Notes', value: notes, onChange: setNotes }));
  }

  // Name the current filter set as a reusable saved view; also lists/deletes existing ones.
  function SaveViewModal({ views, onClose, onSave, onDelete }) {
    const [name, setName] = useState('');
    return h(FormModal, {
      title: 'Saved views', icon: 'filter', onClose, submitLabel: 'Save view',
      onSubmit: () => { const n = name.trim(); if (n) onSave(n); },
    },
      h(Field, { label: 'Save current filters as', value: name, onChange: setName, placeholder: 'my running web VMs' }),
      views.length > 0 && h('div', null,
        h('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Existing views'),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 2 } },
          views.map((v) => h('div', { key: v.name, className: 'row', style: { justifyContent: 'space-between', padding: '3px 0' } },
            h('span', { className: 'mono', style: { fontSize: 12.5 } }, v.name),
            h('button', { className: 'icon-btn', title: 'Delete view', onClick: () => onDelete(v.name) }, h(Icon, { name: 'trash', size: 14 })))))));
  }

  function ActionCluster({ vm, onAct }) {
    const running = vm.status === 'running';
    return h('div', { className: 'row', style: { gap: 2, justifyContent: 'flex-end' } },
      running
        ? h('button', { className: 'icon-btn', title: 'Stop', onClick: (e) => { e.stopPropagation(); onAct('stop', vm); } }, h(Icon, { name: 'stop', size: 16 }))
        : h('button', { className: 'icon-btn', title: 'Start', onClick: (e) => { e.stopPropagation(); onAct('start', vm); } }, h(Icon, { name: 'play', size: 15 })),
      h('button', { className: 'icon-btn', title: 'Restart', onClick: (e) => { e.stopPropagation(); onAct('restart', vm); } }, h(Icon, { name: 'restart', size: 16 })),
      h('span', { onClick: (e) => e.stopPropagation() },
        h(Menu, { items: [
          { label: 'Rename / tags', icon: 'edit', onClick: () => onAct('edit', vm) },
          { label: 'Rebuild', icon: 'rebuild', onClick: () => onAct('rebuild', vm), disabled: !vm.templateId, title: 'legacy VM — redeploy from a template' },
          { sep: true },
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => onAct('delete', vm) },
        ]}, h('button', { className: 'icon-btn', title: 'More' }, h(Icon, { name: 'more', size: 16 }))))
    );
  }

  function JobChip({ vm, go }) {
    // live detail = the last segment of the job phase ("Phase 2 of 6 · Prepare
    // image · downloading 62%" → "downloading 62%"); falls back to step counter.
    const detail = ((vm.job.phase || '').split('·').pop() || '').trim();
    return h('button', {
      className: 'badge working', style: { cursor: 'pointer', border: 'none' },
      onClick: (e) => { e.stopPropagation(); go('job', { jobId: vm.job.jobId }); },
    }, h('span', { className: 'dot working' }), vm.job.label,
      detail ? ' · ' + detail : ('… ' + vm.job.step + '/' + vm.job.total),
      vm.job.pct != null && h('span', { className: 'mono', style: { marginLeft: 4, opacity: 0.75 } }, vm.job.pct, '%'),
      h(Icon, { name: 'chevronR', size: 12 }));
  }

  function SelBox({ checked, onToggle, title }) {
    return h('input', {
      type: 'checkbox', checked: !!checked, title: title || 'Select',
      onClick: (e) => e.stopPropagation(),
      onChange: (e) => { e.stopPropagation(); onToggle(); },
      style: { width: 15, height: 15, cursor: 'pointer', accentColor: 'var(--accent)' },
    });
  }

  function TableView({ vms, go, onAct, sel, toggleSel, allSel, toggleAll }) {
    return h('div', { className: 'card', style: { overflow: 'hidden' } },
      h('div', { style: { overflowX: 'auto' } },
        h('table', { className: 'tbl' },
          h('thead', null, h('tr', null,
            h('th', { style: { width: 34 } }, h(SelBox, { checked: allSel, onToggle: toggleAll, title: 'Select all' })),
            ['', 'Name', 'IP', 'Lineage', 'Connection', 'CPU', 'RAM', 'Uptime', ''].map((hh, i) =>
              h('th', { key: i, style: i === 0 ? { width: 36 } : null }, hh)))),
          h('tbody', null, vms.map(vm =>
            h('tr', { key: vm.id, style: { cursor: 'pointer' }, onClick: () => go('vmdetail', { depId: vm.depId }) },
              h('td', { onClick: (e) => e.stopPropagation() }, h(SelBox, { checked: sel.has(vm.depId), onToggle: () => toggleSel(vm.depId), title: 'Select ' + vm.name })),
              h('td', null, h('span', { className: 'dot ' + vm.status, title: vm.status })),
              h('td', null,
                h('div', { className: 'mono', style: { fontWeight: 600, fontSize: 13.5 } }, vm.name),
                vm.job
                  ? h('div', { style: { marginTop: 4 } }, h(JobChip, { vm, go }))
                  : vm.err
                    ? h('div', { className: 'hint mono', style: { color: 'var(--err)', fontSize: 11, marginTop: 2 } }, vm.err)
                    : h('div', { className: 'hint', style: { fontSize: 11.5 } }, vm.ownerName)
              ),
              h('td', null, h(CopyField, { value: vm.ip })),
              h('td', null,
                h('div', { className: 'row', style: { gap: 7 } },
                  h(OSGlyph, { os: vm.os || 'generic', size: 16 }),
                  h('div', null,
                    h('div', { className: 'mono', style: { fontSize: 12 } }, vm.image),
                    h('div', { className: 'hint', style: { fontSize: 10.5 } }, vm.template)))),
              h('td', null, h('span', { className: 'chip' }, vm.conn)),
              h('td', { style: { width: 96 } },
                h('div', { className: 'row', style: { gap: 7 } },
                  h('div', { style: { width: 50 } }, h(Meter, { value: vm.cpu })),
                  h('span', { className: 'mono hint', style: { fontSize: 11, minWidth: 28 } }, vm.cpu, '%'))),
              h('td', { style: { width: 96 } },
                h('div', { className: 'row', style: { gap: 7 } },
                  h('div', { style: { width: 50 } }, h(Meter, { value: vm.ram })),
                  h('span', { className: 'mono hint', style: { fontSize: 11, minWidth: 28 } }, vm.ram, '%'))),
              h('td', { className: 'mono hint', style: { fontSize: 12 } },
                vm.status === 'working'
                  ? h('span', { style: { color: 'var(--accent)' } },
                      h('span', { className: 'dot working', style: { marginRight: 5 } }), _actLabel(vm._act))
                  : vm.uptime),
              h('td', null, h(ActionCluster, { vm, onAct }))
            )))
        )
      )
    );
  }

  function CardView({ vms, go, onAct, sel, toggleSel }) {
    return h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(310px, 1fr))', gap: 14 } },
      vms.map(vm => h('div', { key: vm.id, className: 'card card-pad', style: { cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: 13, outline: sel.has(vm.depId) ? '1.5px solid var(--accent)' : 'none' }, onClick: () => go('vmdetail', { depId: vm.depId }) },
        h('div', { className: 'row' },
          h(SelBox, { checked: sel.has(vm.depId), onToggle: () => toggleSel(vm.depId), title: 'Select ' + vm.name }),
          h('span', { className: 'dot ' + vm.status }),
          h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 15 } }, vm.name),
          h('div', { style: { marginLeft: 'auto' } }, h(StatusBadge, { status: vm.status }))
        ),
        vm.job && h(JobChip, { vm, go }),
        vm.err && h('div', { style: { fontSize: 11.5, color: 'var(--err)', background: 'var(--err-ghost)', padding: '7px 9px', borderRadius: 8 }, className: 'mono' }, vm.err),
        h('div', { className: 'row', style: { gap: 8, color: 'var(--text-dim)' } },
          h(Icon, { name: 'globe', size: 14 }),
          h(CopyField, { value: vm.ip }),
          h('span', { className: 'chip', style: { marginLeft: 'auto' } }, vm.conn)
        ),
        h('div', { className: 'row', style: { gap: 8 } },
          h(OSGlyph, { os: vm.os || 'generic', size: 18 }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontSize: 12 } }, vm.image, ' → ', vm.template))),
        h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, paddingTop: 4 } },
          h(StatBlock, { label: 'CPU', value: vm.cpu, on: vm.status === 'running',
            hist: window.GDStore.vmHistory(vm.depId).map((p) => p.cpu) }),
          h(StatBlock, { label: 'RAM', value: vm.ram, on: vm.status === 'running',
            hist: window.GDStore.vmHistory(vm.depId).map((p) => p.ram) })),
        h('div', { className: 'divider' }),
        h('div', { className: 'row' },
          vm.status === 'working'
            ? h('span', { className: 'hint mono', style: { fontSize: 11, color: 'var(--accent)' } },
                h('span', { className: 'dot working', style: { marginRight: 5 } }), _actLabel(vm._act))
            : h('span', { className: 'hint mono', style: { fontSize: 11 } }, vm.status === 'running' ? '↑ ' + vm.uptime : 'offline'),
          h('div', { style: { marginLeft: 'auto' } }, h(ActionCluster, { vm, onAct }))
        )
      ))
    );
  }

  function StatBlock({ label, value, on, hist }) {
    return h('div', null,
      h('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 5 } },
        h('span', { className: 'panel-title', style: { fontSize: 10 } }, label),
        on && (hist || []).length > 1 && h(Sparkline, { data: hist, width: 56, height: 14, color: 'var(--text-faint)' }),
        h('span', { className: 'mono', style: { fontSize: 11.5, color: on ? 'var(--text)' : 'var(--text-faint)' } }, on ? value + '%' : '—')),
      h(Meter, { value: on ? value : 0 })
    );
  }

  // One row of the first-run checklist: a numbered/checked bubble + title/desc + optional CTA.
  // Module-scoped (closes over nothing) so it isn't redefined on every Dashboard render.
  function Step({ done, n, title, desc, cta, onCta, disabled }) {
    return h('div', { className: 'row', style: { gap: 12, alignItems: 'flex-start', padding: '10px 0', borderBottom: '1px solid var(--border)' } },
      h('div', { style: { width: 24, height: 24, borderRadius: '50%', flexShrink: 0, display: 'grid', placeItems: 'center', background: done ? 'var(--accent)' : 'var(--surface)', border: '1px solid var(--border)', color: done ? 'var(--accent-ink)' : 'var(--text)', fontSize: 12, fontWeight: 700 } },
        done ? h(Icon, { name: 'check', size: 14 }) : n),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('div', { style: { fontWeight: 600, fontSize: 13 } }, title),
        h('div', { className: 'hint', style: { fontSize: 12 } }, desc)),
      cta && !done && h('button', { className: 'btn sm', disabled: !!disabled, onClick: onCta }, cta));
  }

  function FirstRunChecklist({ go }) {
    const isAdmin = window.GD.me && window.GD.me.isAdmin;
    const hasConn = (window.GD.CONNECTIONS || []).length > 0;
    const first = (window.GD.CONNECTIONS || [])[0];
    const cached = window.UI.useFetched(
      () => (first ? window.API.cachedImages(first.connId) : null), [hasConn], null);
    const imgDone = !!(cached && cached.cached && Object.values(cached.cached).some(Boolean));

    // Non-admins can't add connections / sync images — just show the deploy step.
    if (!isAdmin) {
      return h('div', { className: 'card card-pad' },
        h('h3', { style: { marginTop: 0 } }, 'Get started'),
        h(Step, { done: false, n: 1, title: 'Deploy your first VM',
          desc: 'Pick a template and launch — GoblinDock names and tracks it for you.',
          cta: 'Deploy', onCta: () => go('templates') }));
    }

    return h('div', { className: 'card card-pad' },
      h('h3', { style: { marginTop: 0 } }, 'Welcome to GoblinDock — 3 steps to your first VM'),
      h(Step, { done: hasConn, n: 1, title: 'Connect a Proxmox node',
        desc: 'Add your Proxmox host so GoblinDock can build and run VMs.',
        cta: 'Add connection', onCta: () => go('settings') }),
      h(Step, { done: imgDone, n: 2, title: 'Pre-sync a base image',
        desc: 'Download a cloud image to the node so the first deploy is fast.',
        cta: 'Open ISOs', onCta: () => go('isos'), disabled: !hasConn }),
      h(Step, { done: false, n: 3, title: 'Deploy your first VM',
        desc: 'Create a template, then launch it.',
        cta: 'Templates', onCta: () => go('templates'), disabled: !hasConn }));
  }

  function Dashboard({ go }) {
    const [view, setView] = useState('table');
    const [scope, setScope] = useState('mine');
    const [status, setStatus] = useState('all');
    const [tag, setTag] = useState('all');
    const [q, setQ] = useState('');
    const [confirm, setConfirm] = useState(null);
    const [edit, setEdit] = useState(null);
    const [deploying, setDeploying] = useState(false);
    const [sel, setSel] = useState(() => new Set());
    const [bulkBusy, setBulkBusy] = useState(false);
    const [bulkDel, setBulkDel] = useState(false);
    const [views, setViews] = useState(loadViews);
    const [savingView, setSavingView] = useState(false);

    const allTags = Array.from(new Set(GD.VMS.flatMap((v) => parseTags(v.tags)))).sort();

    let vms = GD.VMS;
    if (scope === 'mine') vms = vms.filter(v => v.owner === 'you');
    if (status !== 'all') vms = vms.filter(v => v.status === status);
    if (tag !== 'all') vms = vms.filter(v => parseTags(v.tags).includes(tag));
    if (q) vms = vms.filter(v => (v.name + v.ip + v.template + (v.tags || '')).toLowerCase().includes(q.toLowerCase()));

    // ---- selection ----
    const toggleSel = (depId) => setSel((prev) => { const n = new Set(prev); if (n.has(depId)) n.delete(depId); else n.add(depId); return n; });
    const clearSel = () => setSel(new Set());
    const allSel = vms.length > 0 && vms.every(v => sel.has(v.depId));
    const toggleAll = () => setSel((prev) => {
      const n = new Set(prev);
      if (vms.every(v => n.has(v.depId))) vms.forEach(v => n.delete(v.depId));
      else vms.forEach(v => n.add(v.depId));
      return n;
    });
    const selectedVms = () => GD.VMS.filter(v => sel.has(v.depId));

    const onAct = async (action, vm) => {
      if (action === 'delete' || action === 'rebuild') { setConfirm({ action, vm }); return; }
      if (action === 'edit') { setEdit(vm); return; }
      if (action === 'start' || action === 'stop' || action === 'restart') {
        window.GDStore.vmAction(vm.depId, action).catch(() => {});
      }
    };

    // ---- bulk ---- (one runner; start/stop/restart and destroy differ only in the
    // API call, the toast wording, and the tones for success / partial failure)
    const runBulk = async (call, label, okTone, failTone) => {
      const targets = selectedVms();
      if (!targets.length) return;
      setBulkBusy(true);
      const results = await Promise.allSettled(targets.map(call));
      const ok = results.filter(r => r.status === 'fulfilled').length;
      const fail = results.length - ok;
      window.GDStore.toast(label(ok) + (fail ? (' · ' + fail + ' failed') : ''), fail ? failTone : okTone);
      setBulkBusy(false);
      clearSel();
      setTimeout(() => window.GDStore.refresh().catch(() => {}), 700);
    };
    const bulk = (action) => runBulk(v => window.API.vmAction(v.depId, action),
      (ok) => action + ': ' + ok + ' ok', 'ok', 'warn');
    const bulkDelete = () => runBulk(v => window.API.vmDestroy(v.depId),
      (ok) => 'Destroying ' + ok + ' VM' + (ok === 1 ? '' : 's'), 'warn', 'err');

    // ---- saved views ----
    const applyView = (v) => { setScope(v.scope); setStatus(v.status); setTag(v.tag || 'all'); setQ(v.q || ''); if (v.view) setView(v.view); };
    const onSaveView = (name) => {
      const nv = [...views.filter(x => x.name !== name), { name, scope, status, tag, q, view }];
      nv.sort((a, b) => a.name.localeCompare(b.name));
      setViews(nv); saveViews(nv); setSavingView(false);
      window.GDStore.toast('View “' + name + '” saved', 'ok');
    };
    const onDeleteView = (name) => { const nv = views.filter(x => x.name !== name); setViews(nv); saveViews(nv); };

    const counts = {
      running: GD.VMS.filter(v => v.status === 'running').length,
      working: GD.VMS.filter(v => v.status === 'working').length,
      error: GD.VMS.filter(v => v.status === 'error').length,
    };

    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Virtual Machines'),
          h('div', { className: 'page-sub' },
            h('span', { className: 'mono' }, counts.running), ' running · ',
            h('span', { className: 'mono' }, counts.working), ' working · ',
            h('span', { className: 'mono', style: counts.error ? { color: 'var(--err)' } : null }, counts.error), ' error',
            h('span', { style: { marginLeft: 10, color: 'var(--text-faint)' } }, '· auto-refreshing'),
            h('span', { className: 'dot running', style: { marginLeft: 6, width: 6, height: 6, display: 'inline-block', verticalAlign: 'middle' } })
          )
        ),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn primary', onClick: () => setDeploying(true) },
          h(Icon, { name: 'plus', size: 16 }), 'Deploy VM')
      ),

      h('div', { className: 'row wrap', style: { marginBottom: 16, gap: 10 } },
        h('div', { className: 'seg' },
          h('button', { className: scope === 'mine' ? 'active' : '', onClick: () => setScope('mine') }, 'My VMs'),
          h('button', { className: scope === 'all' ? 'active' : '', onClick: () => setScope('all') }, 'All VMs')
        ),
        h('select', { className: 'select', style: { width: 'auto', minWidth: 140 }, value: status, onChange: (e) => setStatus(e.target.value) },
          h('option', { value: 'all' }, 'All statuses'),
          h('option', { value: 'running' }, 'Running'),
          h('option', { value: 'stopped' }, 'Stopped'),
          h('option', { value: 'working' }, 'Working'),
          h('option', { value: 'error' }, 'Error')
        ),
        allTags.length > 0 && h('select', { className: 'select', style: { width: 'auto', minWidth: 120 }, value: tag, onChange: (e) => setTag(e.target.value), title: 'Filter by tag' },
          h('option', { value: 'all' }, 'All tags'),
          allTags.map((t) => h('option', { key: t, value: t }, t))
        ),
        h('div', { className: 'search', style: { flex: 1, maxWidth: 260 } },
          h(Icon, { name: 'search', size: 15 }),
          h('input', { placeholder: 'Search VMs…', value: q, onChange: (e) => setQ(e.target.value) })
        ),
        h('div', { className: 'row', style: { gap: 6 } },
          h('select', {
            className: 'select', style: { width: 'auto', minWidth: 128 }, value: '',
            onChange: (e) => { const v = views.find(x => x.name === e.target.value); if (v) applyView(v); },
            title: 'Apply a saved view',
          },
            h('option', { value: '' }, views.length ? 'Saved views…' : 'No saved views'),
            views.map((v) => h('option', { key: v.name, value: v.name }, v.name))),
          h('button', { className: 'btn sm', title: 'Save current filters as a view', onClick: () => setSavingView(true) },
            h(Icon, { name: 'save', size: 14 }), 'Save')),
        h('div', { className: 'seg', style: { marginLeft: 'auto' } },
          h('button', { className: view === 'table' ? 'active' : '', onClick: () => setView('table'), title: 'Table' },
            h(Icon, { name: 'dashboard', size: 15 }), 'Table'),
          h('button', { className: view === 'cards' ? 'active' : '', onClick: () => setView('cards'), title: 'Cards' },
            h(Icon, { name: 'blocks', size: 15 }), 'Cards')
        )
      ),

      // ---- bulk action bar (appears when something is selected) ----
      sel.size > 0 && h('div', { className: 'card card-pad', style: { marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', borderColor: 'var(--accent)' } },
        h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, sel.size, ' selected'),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('start') }, h(Icon, { name: 'play', size: 14 }), 'Start'),
        h('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('stop') }, h(Icon, { name: 'stop', size: 14 }), 'Stop'),
        h('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('restart') }, h(Icon, { name: 'restart', size: 14 }), 'Restart'),
        h('button', { className: 'btn sm danger', disabled: bulkBusy, onClick: () => setBulkDel(true) }, h(Icon, { name: 'trash', size: 14 }), 'Delete'),
        h('button', { className: 'btn ghost sm', disabled: bulkBusy, onClick: clearSel }, 'Clear')
      ),

      vms.length === 0
        ? ((q || tag !== 'all' || status !== 'all')
            ? h('div', { className: 'card' }, h('div', { className: 'empty' },
                h('div', { className: 'glyph' }, h(Icon, { name: 'server', size: 30 })),
                h('h3', null, 'No VMs match'),
                h('p', null, 'Try clearing the filters above.')))
            : h(FirstRunChecklist, { go }))
        : view === 'table'
          ? h(TableView, { vms, go, onAct, sel, toggleSel, allSel, toggleAll })
          : h(CardView, { vms, go, onAct, sel, toggleSel }),

      confirm && h(ConfirmModal, {
        onClose: () => setConfirm(null),
        tone: confirm.action === 'delete' ? 'danger' : 'accent',
        icon: confirm.action === 'delete' ? 'trash' : 'rebuild',
        title: confirm.action === 'delete' ? 'Delete ' + confirm.vm.name + '?' : 'Rebuild ' + confirm.vm.name + '?',
        body: confirm.action === 'delete'
          ? 'This destroys the VM and its disk on ' + confirm.vm.conn + '. The IP ' + confirm.vm.ip + ' returns to the pool. This cannot be undone.'
          : 'Re-clones from image ' + confirm.vm.image + ', keeping the name and IP (' + confirm.vm.ip + '). Anything written on disk is lost.',
        confirmLabel: confirm.action === 'delete' ? 'Delete VM' : 'Rebuild',
        onConfirm: async () => {
          // toast + rethrow: ConfirmModal stays open for retry when the call fails
          try {
            if (confirm.action === 'rebuild') {
              const r = await window.API.vmRebuild(confirm.vm.depId);
              go('job', { jobId: r.jobId });
            } else {
              const r = await window.API.vmDestroy(confirm.vm.depId);
              window.GDStore.toast('Destroying ' + confirm.vm.name, 'warn');
              go('job', { jobId: r.jobId });
            }
          } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); throw e; }
        },
      }),

      bulkDel && h(ConfirmModal, {
        onClose: () => setBulkDel(false), tone: 'danger', icon: 'trash',
        title: 'Destroy ' + sel.size + ' VM' + (sel.size === 1 ? '' : 's') + '?',
        body: 'This destroys ' + sel.size + ' VM' + (sel.size === 1 ? '' : 's') + ' and their disks, returning their IPs to the pool. This cannot be undone.',
        confirmLabel: 'Destroy ' + sel.size,
        onConfirm: bulkDelete,
      }),

      savingView && h(SaveViewModal, { views, onClose: () => setSavingView(false), onSave: onSaveView, onDelete: onDeleteView }),

      edit && h(VmEditModal, { vm: edit, onClose: () => setEdit(null), onDone: () => { setEdit(null); window.GDStore.toast('VM updated', 'ok'); window.GDStore.refresh().catch(() => {}); } }),

      deploying && h(window.DeployModal, { go, onClose: () => setDeploying(false) })
    );
  }

  window.Dashboard = Dashboard;
})();
