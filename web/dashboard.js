/* GoblinDock — Dashboard (VMs) */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph, StatusBadge, CopyField, Meter, Menu, ConfirmModal, FormModal, Field } = window.UI;

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
    return React.createElement(FormModal, { title: 'Edit ' + vm.name, icon: 'edit', onClose, onSubmit: submit, busy },
      React.createElement(Field, { label: 'Name', value: name, onChange: setName, mono: true }),
      React.createElement(Field, { label: 'Tags', value: tags, onChange: setTags, placeholder: 'project:atlas, env:dev' }),
      React.createElement(Field, { label: 'Notes', value: notes, onChange: setNotes }));
  }

  // Name the current filter set as a reusable saved view; also lists/deletes existing ones.
  function SaveViewModal({ views, onClose, onSave, onDelete }) {
    const [name, setName] = useState('');
    return React.createElement(FormModal, {
      title: 'Saved views', icon: 'filter', onClose, submitLabel: 'Save view',
      onSubmit: () => { const n = name.trim(); if (n) onSave(n); },
    },
      React.createElement(Field, { label: 'Save current filters as', value: name, onChange: setName, placeholder: 'my running web VMs' }),
      views.length > 0 && React.createElement('div', null,
        React.createElement('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Existing views'),
        React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 2 } },
          views.map((v) => React.createElement('div', { key: v.name, className: 'row', style: { justifyContent: 'space-between', padding: '3px 0' } },
            React.createElement('span', { className: 'mono', style: { fontSize: 12.5 } }, v.name),
            React.createElement('button', { className: 'icon-btn', title: 'Delete view', onClick: () => onDelete(v.name) }, React.createElement(Icon, { name: 'trash', size: 14 })))))));
  }

  function ActionCluster({ vm, onAct }) {
    const running = vm.status === 'running';
    return React.createElement('div', { className: 'row', style: { gap: 2, justifyContent: 'flex-end' } },
      running
        ? React.createElement('button', { className: 'icon-btn', title: 'Stop', onClick: (e) => { e.stopPropagation(); onAct('stop', vm); } }, React.createElement(Icon, { name: 'stop', size: 16 }))
        : React.createElement('button', { className: 'icon-btn', title: 'Start', onClick: (e) => { e.stopPropagation(); onAct('start', vm); } }, React.createElement(Icon, { name: 'play', size: 15 })),
      React.createElement('button', { className: 'icon-btn', title: 'Restart', onClick: (e) => { e.stopPropagation(); onAct('restart', vm); } }, React.createElement(Icon, { name: 'restart', size: 16 })),
      React.createElement('span', { onClick: (e) => e.stopPropagation() },
        React.createElement(Menu, { items: [
          { label: 'Rename / tags', icon: 'edit', onClick: () => onAct('edit', vm) },
          { label: 'Rebuild', icon: 'rebuild', onClick: () => onAct('rebuild', vm), disabled: !vm.templateId, title: 'legacy VM — redeploy from a template' },
          { sep: true },
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => onAct('delete', vm) },
        ]}, React.createElement('button', { className: 'icon-btn', title: 'More' }, React.createElement(Icon, { name: 'more', size: 16 }))))
    );
  }

  function JobChip({ vm, go }) {
    return React.createElement('button', {
      className: 'badge working', style: { cursor: 'pointer', border: 'none' },
      onClick: (e) => { e.stopPropagation(); go('job', { jobId: vm.job.jobId }); },
    }, React.createElement('span', { className: 'dot working' }), vm.job.label, '… ', vm.job.step, '/', vm.job.total, React.createElement(Icon, { name: 'chevronR', size: 12 }));
  }

  function SelBox({ checked, onToggle, title }) {
    return React.createElement('input', {
      type: 'checkbox', checked: !!checked, title: title || 'Select',
      onClick: (e) => e.stopPropagation(),
      onChange: (e) => { e.stopPropagation(); onToggle(); },
      style: { width: 15, height: 15, cursor: 'pointer', accentColor: 'var(--accent)' },
    });
  }

  function TableView({ vms, go, onAct, sel, toggleSel, allSel, toggleAll }) {
    return React.createElement('div', { className: 'card', style: { overflow: 'hidden' } },
      React.createElement('div', { style: { overflowX: 'auto' } },
        React.createElement('table', { className: 'tbl' },
          React.createElement('thead', null, React.createElement('tr', null,
            React.createElement('th', { style: { width: 34 } }, React.createElement(SelBox, { checked: allSel, onToggle: toggleAll, title: 'Select all' })),
            ['', 'Name', 'IP', 'Lineage', 'Connection', 'CPU', 'RAM', 'Uptime', ''].map((hh, i) =>
              React.createElement('th', { key: i, style: i === 0 ? { width: 36 } : null }, hh)))),
          React.createElement('tbody', null, vms.map(vm =>
            React.createElement('tr', { key: vm.id, style: { cursor: 'pointer' }, onClick: () => go('vmdetail', { depId: vm.depId }) },
              React.createElement('td', { onClick: (e) => e.stopPropagation() }, React.createElement(SelBox, { checked: sel.has(vm.depId), onToggle: () => toggleSel(vm.depId), title: 'Select ' + vm.name })),
              React.createElement('td', null, React.createElement('span', { className: 'dot ' + vm.status, title: vm.status })),
              React.createElement('td', null,
                React.createElement('div', { className: 'mono', style: { fontWeight: 600, fontSize: 13.5 } }, vm.name),
                vm.job
                  ? React.createElement('div', { style: { marginTop: 4 } }, React.createElement(JobChip, { vm, go }))
                  : vm.err
                    ? React.createElement('div', { className: 'hint mono', style: { color: 'var(--err)', fontSize: 11, marginTop: 2 } }, vm.err)
                    : React.createElement('div', { className: 'hint', style: { fontSize: 11.5 } }, vm.ownerName)
              ),
              React.createElement('td', null, React.createElement(CopyField, { value: vm.ip })),
              React.createElement('td', null,
                React.createElement('div', { className: 'row', style: { gap: 7 } },
                  React.createElement(OSGlyph, { os: vm.os || 'generic', size: 16 }),
                  React.createElement('div', null,
                    React.createElement('div', { className: 'mono', style: { fontSize: 12 } }, vm.image),
                    React.createElement('div', { className: 'hint', style: { fontSize: 10.5 } }, vm.template)))),
              React.createElement('td', null, React.createElement('span', { className: 'chip' }, vm.conn)),
              React.createElement('td', { style: { width: 96 } },
                React.createElement('div', { className: 'row', style: { gap: 7 } },
                  React.createElement('div', { style: { width: 50 } }, React.createElement(Meter, { value: vm.cpu })),
                  React.createElement('span', { className: 'mono hint', style: { fontSize: 11, minWidth: 28 } }, vm.cpu, '%'))),
              React.createElement('td', { style: { width: 96 } },
                React.createElement('div', { className: 'row', style: { gap: 7 } },
                  React.createElement('div', { style: { width: 50 } }, React.createElement(Meter, { value: vm.ram })),
                  React.createElement('span', { className: 'mono hint', style: { fontSize: 11, minWidth: 28 } }, vm.ram, '%'))),
              React.createElement('td', { className: 'mono hint', style: { fontSize: 12 } }, vm.uptime),
              React.createElement('td', null, React.createElement(ActionCluster, { vm, onAct }))
            )))
        )
      )
    );
  }

  function CardView({ vms, go, onAct, sel, toggleSel }) {
    return React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(310px, 1fr))', gap: 14 } },
      vms.map(vm => React.createElement('div', { key: vm.id, className: 'card card-pad', style: { cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: 13, outline: sel.has(vm.depId) ? '1.5px solid var(--accent)' : 'none' }, onClick: () => go('vmdetail', { depId: vm.depId }) },
        React.createElement('div', { className: 'row' },
          React.createElement(SelBox, { checked: sel.has(vm.depId), onToggle: () => toggleSel(vm.depId), title: 'Select ' + vm.name }),
          React.createElement('span', { className: 'dot ' + vm.status }),
          React.createElement('span', { className: 'mono', style: { fontWeight: 700, fontSize: 15 } }, vm.name),
          React.createElement('div', { style: { marginLeft: 'auto' } }, React.createElement(StatusBadge, { status: vm.status }))
        ),
        vm.job && React.createElement(JobChip, { vm, go }),
        vm.err && React.createElement('div', { style: { fontSize: 11.5, color: 'var(--err)', background: 'var(--err-ghost)', padding: '7px 9px', borderRadius: 8 }, className: 'mono' }, vm.err),
        React.createElement('div', { className: 'row', style: { gap: 8, color: 'var(--text-dim)' } },
          React.createElement(Icon, { name: 'globe', size: 14 }),
          React.createElement(CopyField, { value: vm.ip }),
          React.createElement('span', { className: 'chip', style: { marginLeft: 'auto' } }, vm.conn)
        ),
        React.createElement('div', { className: 'row', style: { gap: 8 } },
          React.createElement(OSGlyph, { os: vm.os || 'generic', size: 18 }),
          React.createElement('div', { style: { minWidth: 0 } },
            React.createElement('div', { className: 'mono', style: { fontSize: 12 } }, vm.image, ' → ', vm.template))),
        React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, paddingTop: 4 } },
          React.createElement(StatBlock, { label: 'CPU', value: vm.cpu, on: vm.status === 'running' }),
          React.createElement(StatBlock, { label: 'RAM', value: vm.ram, on: vm.status === 'running' })),
        React.createElement('div', { className: 'divider' }),
        React.createElement('div', { className: 'row' },
          React.createElement('span', { className: 'hint mono', style: { fontSize: 11 } }, vm.status === 'running' ? '↑ ' + vm.uptime : 'offline'),
          React.createElement('div', { style: { marginLeft: 'auto' } }, React.createElement(ActionCluster, { vm, onAct }))
        )
      ))
    );
  }

  function StatBlock({ label, value, on }) {
    return React.createElement('div', null,
      React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 5 } },
        React.createElement('span', { className: 'panel-title', style: { fontSize: 10 } }, label),
        React.createElement('span', { className: 'mono', style: { fontSize: 11.5, color: on ? 'var(--text)' : 'var(--text-faint)' } }, on ? value + '%' : '—')),
      React.createElement(Meter, { value: on ? value : 0 })
    );
  }

  function Dashboard({ go }) {
    const [view, setView] = useState('table');
    const [scope, setScope] = useState('mine');
    const [status, setStatus] = useState('all');
    const [tag, setTag] = useState('all');
    const [q, setQ] = useState('');
    const [confirm, setConfirm] = useState(null);
    const [edit, setEdit] = useState(null);
    const [deploying, setDeploying] = React.useState(false);
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
      try {
        await window.API.vmAction(vm.depId, action);
        window.GDStore.toast(action + ' → ' + vm.name, 'ok');
        setTimeout(() => window.GDStore.refresh().catch(() => {}), 700);
      } catch (e) {
        window.GDStore.toast(e.message || 'action failed', 'err');
      }
    };

    // ---- bulk ----
    const bulk = async (action) => {
      const targets = selectedVms();
      if (!targets.length) return;
      setBulkBusy(true);
      const results = await Promise.allSettled(targets.map(v => window.API.vmAction(v.depId, action)));
      const ok = results.filter(r => r.status === 'fulfilled').length;
      const fail = results.length - ok;
      window.GDStore.toast(action + ': ' + ok + ' ok' + (fail ? (' · ' + fail + ' failed') : ''), fail ? 'warn' : 'ok');
      setBulkBusy(false);
      clearSel();
      setTimeout(() => window.GDStore.refresh().catch(() => {}), 700);
    };
    const bulkDelete = async () => {
      const targets = selectedVms();
      setBulkBusy(true);
      const results = await Promise.allSettled(targets.map(v => window.API.vmDestroy(v.depId)));
      const ok = results.filter(r => r.status === 'fulfilled').length;
      const fail = results.length - ok;
      window.GDStore.toast('Destroying ' + ok + ' VM' + (ok === 1 ? '' : 's') + (fail ? (' · ' + fail + ' failed') : ''), fail ? 'err' : 'warn');
      setBulkBusy(false);
      clearSel();
      setTimeout(() => window.GDStore.refresh().catch(() => {}), 700);
    };

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

    return React.createElement('div', { className: 'page fadein' },
      React.createElement('div', { className: 'page-head' },
        React.createElement('div', null,
          React.createElement('h1', { className: 'page-title' }, 'Virtual Machines'),
          React.createElement('div', { className: 'page-sub' },
            React.createElement('span', { className: 'mono' }, counts.running), ' running · ',
            React.createElement('span', { className: 'mono' }, counts.working), ' working · ',
            React.createElement('span', { className: 'mono', style: counts.error ? { color: 'var(--err)' } : null }, counts.error), ' error',
            React.createElement('span', { style: { marginLeft: 10, color: 'var(--text-faint)' } }, '· auto-refreshing'),
            React.createElement('span', { className: 'dot running', style: { marginLeft: 6, width: 6, height: 6, display: 'inline-block', verticalAlign: 'middle' } })
          )
        ),
        React.createElement('div', { className: 'spacer' }),
        React.createElement('button', { className: 'btn primary', onClick: () => setDeploying(true) },
          React.createElement(Icon, { name: 'plus', size: 16 }), 'Deploy VM')
      ),

      React.createElement('div', { className: 'row wrap', style: { marginBottom: 16, gap: 10 } },
        React.createElement('div', { className: 'seg' },
          React.createElement('button', { className: scope === 'mine' ? 'active' : '', onClick: () => setScope('mine') }, 'My VMs'),
          React.createElement('button', { className: scope === 'all' ? 'active' : '', onClick: () => setScope('all') }, 'All VMs')
        ),
        React.createElement('select', { className: 'select', style: { width: 'auto', minWidth: 140 }, value: status, onChange: (e) => setStatus(e.target.value) },
          React.createElement('option', { value: 'all' }, 'All statuses'),
          React.createElement('option', { value: 'running' }, 'Running'),
          React.createElement('option', { value: 'stopped' }, 'Stopped'),
          React.createElement('option', { value: 'working' }, 'Working'),
          React.createElement('option', { value: 'error' }, 'Error')
        ),
        allTags.length > 0 && React.createElement('select', { className: 'select', style: { width: 'auto', minWidth: 120 }, value: tag, onChange: (e) => setTag(e.target.value), title: 'Filter by tag' },
          React.createElement('option', { value: 'all' }, 'All tags'),
          allTags.map((t) => React.createElement('option', { key: t, value: t }, t))
        ),
        React.createElement('div', { className: 'search', style: { flex: 1, maxWidth: 260 } },
          React.createElement(Icon, { name: 'search', size: 15 }),
          React.createElement('input', { placeholder: 'Search VMs…', value: q, onChange: (e) => setQ(e.target.value) })
        ),
        React.createElement('div', { className: 'row', style: { gap: 6 } },
          React.createElement('select', {
            className: 'select', style: { width: 'auto', minWidth: 128 }, value: '',
            onChange: (e) => { const v = views.find(x => x.name === e.target.value); if (v) applyView(v); },
            title: 'Apply a saved view',
          },
            React.createElement('option', { value: '' }, views.length ? 'Saved views…' : 'No saved views'),
            views.map((v) => React.createElement('option', { key: v.name, value: v.name }, v.name))),
          React.createElement('button', { className: 'btn sm', title: 'Save current filters as a view', onClick: () => setSavingView(true) },
            React.createElement(Icon, { name: 'save', size: 14 }), 'Save')),
        React.createElement('div', { className: 'seg', style: { marginLeft: 'auto' } },
          React.createElement('button', { className: view === 'table' ? 'active' : '', onClick: () => setView('table'), title: 'Table' },
            React.createElement(Icon, { name: 'dashboard', size: 15 }), 'Table'),
          React.createElement('button', { className: view === 'cards' ? 'active' : '', onClick: () => setView('cards'), title: 'Cards' },
            React.createElement(Icon, { name: 'blocks', size: 15 }), 'Cards')
        )
      ),

      // ---- bulk action bar (appears when something is selected) ----
      sel.size > 0 && React.createElement('div', { className: 'card card-pad', style: { marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', borderColor: 'var(--accent)' } },
        React.createElement('span', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, sel.size, ' selected'),
        React.createElement('div', { className: 'spacer' }),
        React.createElement('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('start') }, React.createElement(Icon, { name: 'play', size: 14 }), 'Start'),
        React.createElement('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('stop') }, React.createElement(Icon, { name: 'stop', size: 14 }), 'Stop'),
        React.createElement('button', { className: 'btn sm', disabled: bulkBusy, onClick: () => bulk('restart') }, React.createElement(Icon, { name: 'restart', size: 14 }), 'Restart'),
        React.createElement('button', { className: 'btn sm danger', disabled: bulkBusy, onClick: () => setBulkDel(true) }, React.createElement(Icon, { name: 'trash', size: 14 }), 'Delete'),
        React.createElement('button', { className: 'btn ghost sm', disabled: bulkBusy, onClick: clearSel }, 'Clear')
      ),

      vms.length === 0
        ? React.createElement('div', { className: 'card' }, React.createElement('div', { className: 'empty' },
            React.createElement('div', { className: 'glyph' }, React.createElement(Icon, { name: 'server', size: 30 })),
            React.createElement('h3', null, (q || tag !== 'all' || status !== 'all') ? 'No VMs match' : 'No VMs yet'),
            React.createElement('p', null, (q || tag !== 'all' || status !== 'all') ? 'Try clearing the filters above.' : 'Deploy your first one — GoblinDock names and tracks it for you.'),
            React.createElement('button', { className: 'btn primary', onClick: () => setDeploying(true) },
              React.createElement(Icon, { name: 'plus', size: 16 }), 'Deploy your first VM')))
        : view === 'table'
          ? React.createElement(TableView, { vms, go, onAct, sel, toggleSel, allSel, toggleAll })
          : React.createElement(CardView, { vms, go, onAct, sel, toggleSel }),

      confirm && React.createElement(ConfirmModal, {
        onClose: () => setConfirm(null),
        tone: confirm.action === 'delete' ? 'danger' : 'accent',
        icon: confirm.action === 'delete' ? 'trash' : 'rebuild',
        title: confirm.action === 'delete' ? 'Delete ' + confirm.vm.name + '?' : 'Rebuild ' + confirm.vm.name + '?',
        body: confirm.action === 'delete'
          ? 'This destroys the VM and its disk on ' + confirm.vm.conn + '. The IP ' + confirm.vm.ip + ' returns to the pool. This cannot be undone.'
          : 'Re-clones from image ' + confirm.vm.image + ', keeping the name and IP (' + confirm.vm.ip + '). Anything written on disk is lost.',
        confirmLabel: confirm.action === 'delete' ? 'Delete VM' : 'Rebuild',
        onConfirm: async () => {
          try {
            if (confirm.action === 'rebuild') {
              const r = await window.API.vmRebuild(confirm.vm.depId);
              go('job', { jobId: r.jobId });
            } else {
              const r = await window.API.vmDestroy(confirm.vm.depId);
              window.GDStore.toast('Destroying ' + confirm.vm.name, 'warn');
              go('job', { jobId: r.jobId });
            }
          } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
        },
      }),

      bulkDel && React.createElement(ConfirmModal, {
        onClose: () => setBulkDel(false), tone: 'danger', icon: 'trash',
        title: 'Destroy ' + sel.size + ' VM' + (sel.size === 1 ? '' : 's') + '?',
        body: 'This destroys ' + sel.size + ' VM' + (sel.size === 1 ? '' : 's') + ' and their disks, returning their IPs to the pool. This cannot be undone.',
        confirmLabel: 'Destroy ' + sel.size,
        onConfirm: bulkDelete,
      }),

      savingView && React.createElement(SaveViewModal, { views, onClose: () => setSavingView(false), onSave: onSaveView, onDelete: onDeleteView }),

      edit && React.createElement(VmEditModal, { vm: edit, onClose: () => setEdit(null), onDone: () => { setEdit(null); window.GDStore.toast('VM updated', 'ok'); window.GDStore.refresh().catch(() => {}); } }),

      deploying && React.createElement(window.DeployModal, { go, onClose: () => setDeploying(false) })
    );
  }

  window.Dashboard = Dashboard;
})();
