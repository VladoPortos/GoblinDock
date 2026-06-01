/* GoblinDock — Blocks library, Secrets, Settings (full CRUD). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu, Modal, ConfirmModal, FormModal, Field, TextArea, SelectField, Toggle } = window.UI;
  const h = React.createElement;

  const refresh = () => window.GDStore.refresh().catch(() => {});
  const toast = (m, t) => window.GDStore.toast(m, t);

  /* ============ BLOCKS LIBRARY ============ */
  function BlocksLib({ go }) {
    const [q, setQ] = useState('');
    const [editor, setEditor] = useState(null);     // {initial?}
    const [confirm, setConfirm] = useState(null);
    let blocks = GD.PALETTE || [];
    if (q) blocks = blocks.filter((b) => (b.name + b.cat + b.desc).toLowerCase().includes(q.toLowerCase()));

    const fork = async (b) => {
      try { await window.API.forkBlock(b.key || b.id); toast('Forked to a custom copy', 'ok'); refresh(); }
      catch (e) { toast(e.message, 'err'); }
    };
    const del = async (b) => {
      try { await window.API.deleteBlock(b.key || b.id); toast('Block deleted', 'ok'); refresh(); }
      catch (e) { toast(e.message, 'err'); }
    };

    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Blocks'),
          h('div', { className: 'page-sub' }, 'The Lego pieces of every recipe. Fork a built-in to customise it.')),
        h('div', { className: 'spacer' }),
        h('div', { className: 'search', style: { maxWidth: 240 } },
          h(Icon, { name: 'search', size: 15 }), h('input', { placeholder: 'Search blocks…', value: q, onChange: (e) => setQ(e.target.value) })),
        h('button', { className: 'btn primary', onClick: () => setEditor({}) }, h(Icon, { name: 'plus', size: 16 }), 'New block')),
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14 } },
        blocks.map((b) => h('div', { key: b.id, className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 11 } },
          h('div', { className: 'row', style: { gap: 10 } },
            h('span', { className: 'placed-ico', style: { width: 32, height: 32 } }, h(Icon, { name: b.icon, size: 16 })),
            h('div', { style: { minWidth: 0 } },
              h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, b.name),
              h('div', { className: 'chip', style: { fontSize: 10, padding: '2px 6px', marginTop: 3 } }, b.cat)),
            h('div', { style: { marginLeft: 'auto' } },
              b.builtin ? h('span', { className: 'badge', style: { fontSize: 10 } }, 'Built-in')
                : h('span', { className: 'badge accent', style: { fontSize: 10 } }, 'Custom'))),
          h('p', { className: 'hint', style: { fontSize: 12.5, lineHeight: 1.5, minHeight: 34 } }, b.desc),
          h('div', { className: 'divider' }),
          h('div', { className: 'row' },
            h('span', { className: 'hint mono', style: { fontSize: 10.5 } }, (b.schema || []).length, ' inputs'),
            b.builtin
              ? h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: () => fork(b) }, h(Icon, { name: 'duplicate', size: 14 }), 'Fork')
              : h('div', { className: 'row', style: { marginLeft: 'auto', gap: 4 } },
                  h('button', { className: 'btn ghost sm', onClick: () => setEditor({ initial: b }) }, h(Icon, { name: 'edit', size: 14 }), 'Edit'),
                  h('button', { className: 'icon-btn danger', onClick: () => setConfirm(b) }, h(Icon, { name: 'trash', size: 15 }))))))),
      editor && h(window.BlockEditorModal, { initial: editor.initial, onClose: () => setEditor(null), onSaved: () => { setEditor(null); toast('Block saved', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'This removes your custom block. Recipes already using it keep their copy of the inputs.', confirmLabel: 'Delete block', onConfirm: () => del(confirm) }));
  }

  /* ============ SECRETS ============ */
  function SecretRow({ s, onDelete, onEdit }) {
    const [show, setShow] = useState(false);
    const [val, setVal] = useState(null);
    const toggle = async () => {
      if (!show && val === null) {
        try { const r = await window.API.revealSecret(s.secId); setVal(r.val); } catch (e) { toast(e.message, 'err'); return; }
      }
      setShow((v) => !v);
    };
    return h('tr', null,
      h('td', null, h('div', { className: 'row', style: { gap: 9 } },
        h(Icon, { name: 'key', size: 15, style: { color: 'var(--accent)' } }),
        h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 13 } }, s.name))),
      h('td', null, s.scope === 'Global'
        ? h('span', { className: 'badge accent' }, h(Icon, { name: 'globe', size: 12 }), 'Global')
        : h('span', { className: 'badge info' }, h(Icon, { name: 'user', size: 12 }), 'Personal')),
      h('td', null, h('div', { className: 'row', style: { gap: 8, maxWidth: 320 } },
        h('span', { className: 'mono', style: { fontSize: 12, color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
          show ? (val || '') : '••••••••••••••••••••'),
        h('button', { className: 'icon-btn sm', onClick: toggle, title: show ? 'Hide' : 'Reveal' }, h(Icon, { name: show ? 'eyeOff' : 'eye', size: 14 })))),
      h('td', { className: 'hint', style: { fontSize: 12 } }, s.by),
      h('td', { className: 'mono hint', style: { fontSize: 12 } }, s.used),
      h('td', null, h(Menu, { items: [
        { label: 'Edit value', icon: 'edit', onClick: () => onEdit(s) },
        { sep: true },
        { label: 'Delete', icon: 'trash', danger: true, onClick: () => onDelete(s) },
      ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 })))));
  }

  function SecretModal({ secret, onClose, onDone }) {
    const isAdmin = GD.me && GD.me.isAdmin;
    const editing = !!secret;
    const [name, setName] = useState(secret ? secret.name : '');
    const [value, setValue] = useState('');
    const [scope, setScope] = useState(secret ? (secret.scope === 'Global' ? 'global' : 'user') : (isAdmin ? 'global' : 'user'));
    const [busy, setBusy] = useState(false);
    const submit = async () => {
      if (!editing && (!name.trim() || !value)) { toast('Name and value required', 'err'); return; }
      setBusy(true);
      try {
        if (editing) await window.API.editSecret(secret.secId, { name: name.trim(), value });
        else await window.API.addSecret({ name: name.trim(), value, scope });
        onDone();
      } catch (e) { toast(e.message || 'failed', 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit secret' : 'Add secret', icon: 'lock', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add secret' },
      h(Field, { label: 'Name', value: name, onChange: (v) => setName(v.toUpperCase().replace(/[^A-Z0-9_]/g, '_')), mono: true, placeholder: 'TEAM_SSH_PUBKEY' }),
      h(TextArea, { label: editing ? 'New value' : 'Value', value, onChange: setValue, rows: 4, mono: true }),
      !editing && isAdmin && h('div', null, h('label', { className: 'field-label' }, 'Scope'),
        h('div', { className: 'seg', style: { width: '100%' } },
          h('button', { className: scope === 'global' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('global') }, 'Global'),
          h('button', { className: scope === 'user' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('user') }, 'Personal'))),
      h('p', { className: 'hint', style: { fontSize: 11.5 } }, 'Reference in blocks as ', h('code', { className: 'kbd' }, '{{ secrets.' + (name || 'NAME') + ' }}'), '.'));
  }

  function Secrets() {
    const [modal, setModal] = useState(null);   // 'add' | {secret}
    const [confirm, setConfirm] = useState(null);
    const del = async (s) => { try { await window.API.delSecret(s.secId); toast('Secret deleted', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Secrets'),
          h('div', { className: 'page-sub' }, 'Encrypted values for blocks. Never printed in logs.')),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 16 }), 'Add secret')),
      h('div', { className: 'card', style: { padding: 13, marginBottom: 16, display: 'flex', gap: 10, alignItems: 'center', background: 'var(--surface-2)' } },
        h(Icon, { name: 'info', size: 16, style: { color: 'var(--text-faint)', flexShrink: 0 } }),
        h('span', { className: 'hint', style: { fontSize: 12.5 } }, 'Reference any secret inside a block as ',
          h('code', { className: 'kbd' }, '{{ secrets.NAME }}'), '. Add ', h('code', { className: 'kbd' }, 'DEPLOY_SSH_PUBKEY'), ' so your VMs are reachable.')),
      (GD.SECRETS || []).length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty', style: { padding: '40px 20px' } },
            h('div', { className: 'glyph' }, h(Icon, { name: 'lock', size: 24 })),
            h('h3', null, 'No secrets yet'),
            h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 16 }), 'Add secret')))
        : h('div', { className: 'card', style: { overflow: 'hidden' } },
            h('table', { className: 'tbl' },
              h('thead', null, h('tr', null, ['Name', 'Scope', 'Value', 'Created by', 'Last used', ''].map((c, i) => h('th', { key: i }, c)))),
              h('tbody', null, (GD.SECRETS || []).map((s) => h(SecretRow, { key: s.id, s, onDelete: (x) => setConfirm(x), onEdit: (x) => setModal({ secret: x }) }))))),
      modal === 'add' && h(SecretModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Secret added', 'ok'); refresh(); } }),
      modal && modal.secret && h(SecretModal, { secret: modal.secret, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Secret updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'Blocks referencing this secret will fail until you add it again.', confirmLabel: 'Delete', onConfirm: () => del(confirm) }));
  }

  /* ============ VARIABLES ============ */
  function VarModal({ variable, onClose, onDone }) {
    const isAdmin = GD.me && GD.me.isAdmin;
    const editing = !!variable;
    const [name, setName] = useState(variable ? variable.name : '');
    const [value, setValue] = useState(variable ? variable.value : '');
    const [scope, setScope] = useState(variable ? variable.rawScope : (isAdmin ? 'global' : 'user'));
    const [busy, setBusy] = useState(false);
    const submit = async () => {
      if (!name.trim()) { toast('Name required', 'err'); return; }
      setBusy(true);
      try {
        if (editing) await window.API.editVariable(variable.varId, { name: name.trim(), value });
        else await window.API.addVariable({ name: name.trim(), value, scope });
        onDone();
      } catch (e) { toast(e.message || 'failed', 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit variable' : 'Add variable', icon: 'tag', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add variable' },
      h(Field, { label: 'Name', value: name, onChange: (v) => setName(v.toUpperCase().replace(/[^A-Z0-9_]/g, '_')), mono: true, placeholder: 'APP_PORT' }),
      h(TextArea, { label: 'Value', value, onChange: setValue, rows: 3, mono: true }),
      !editing && isAdmin && h('div', null, h('label', { className: 'field-label' }, 'Scope'),
        h('div', { className: 'seg', style: { width: '100%' } },
          h('button', { className: scope === 'global' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('global') }, 'Global'),
          h('button', { className: scope === 'user' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('user') }, 'Personal'))),
      h('p', { className: 'hint', style: { fontSize: 11.5 } }, 'Reference in blocks as ', h('code', { className: 'kbd' }, '{{ variable.' + (name || 'NAME') + ' }}'), '.'));
  }

  function Variables() {
    const [modal, setModal] = useState(null);   // 'add' | {variable}
    const [confirm, setConfirm] = useState(null);
    const del = async (v) => { try { await window.API.deleteVariable(v.varId); toast('Variable deleted', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Variables'),
          h('div', { className: 'page-sub' }, 'Plain (non-secret) values — visible, reusable across scripts and recipes.')),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 16 }), 'Add variable')),
      h('div', { className: 'card', style: { padding: 13, marginBottom: 16, display: 'flex', gap: 10, alignItems: 'center', background: 'var(--surface-2)' } },
        h(Icon, { name: 'info', size: 16, style: { color: 'var(--text-faint)', flexShrink: 0 } }),
        h('span', { className: 'hint', style: { fontSize: 12.5 } }, 'Reference any variable inside a block as ',
          h('code', { className: 'kbd' }, '{{ variable.NAME }}'), '. Use Secrets for anything sensitive.')),
      (GD.VARIABLES || []).length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty', style: { padding: '40px 20px' } },
            h('div', { className: 'glyph' }, h(Icon, { name: 'tag', size: 24 })),
            h('h3', null, 'No variables yet'),
            h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 16 }), 'Add variable')))
        : h('div', { className: 'card', style: { overflow: 'hidden' } },
            h('table', { className: 'tbl' },
              h('thead', null, h('tr', null, ['Name', 'Scope', 'Value', 'Created by', ''].map((c, i) => h('th', { key: i }, c)))),
              h('tbody', null, (GD.VARIABLES || []).map((v) => h('tr', { key: v.id },
                h('td', null, h('div', { className: 'row', style: { gap: 9 } },
                  h(Icon, { name: 'tag', size: 15, style: { color: 'var(--accent)' } }),
                  h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 13 } }, v.name))),
                h('td', null, v.scope === 'Global'
                  ? h('span', { className: 'badge accent' }, h(Icon, { name: 'globe', size: 12 }), 'Global')
                  : h('span', { className: 'badge info' }, h(Icon, { name: 'user', size: 12 }), 'Personal')),
                h('td', null, h('span', { className: 'mono', style: { fontSize: 12, color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block', maxWidth: 340 } }, v.value || '—')),
                h('td', { className: 'hint', style: { fontSize: 12 } }, v.by),
                h('td', null, h(Menu, { items: [
                  { label: 'Edit', icon: 'edit', onClick: () => setModal({ variable: v }) },
                  { sep: true },
                  { label: 'Delete', icon: 'trash', danger: true, onClick: () => setConfirm(v) },
                ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 }))))))))),
      modal === 'add' && h(VarModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Variable added', 'ok'); refresh(); } }),
      modal && modal.variable && h(VarModal, { variable: modal.variable, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Variable updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'Blocks referencing this variable fall back to an empty value.', confirmLabel: 'Delete', onConfirm: () => del(confirm) }));
  }

  /* ============ SETTINGS ============ */
  function Settings() {
    const [tab, setTab] = useState('connections');
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head', style: { marginBottom: 16 } },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Settings'),
          h('div', { className: 'page-sub' }, 'Admin · infrastructure that everything else builds on.'))),
      h('div', { className: 'seg', style: { marginBottom: 18 } },
        h('button', { className: tab === 'connections' ? 'active' : '', onClick: () => setTab('connections') }, h(Icon, { name: 'server', size: 14 }), 'Proxmox'),
        h('button', { className: tab === 'networks' ? 'active' : '', onClick: () => setTab('networks') }, h(Icon, { name: 'network', size: 14 }), 'Networks'),
        h('button', { className: tab === 'users' ? 'active' : '', onClick: () => setTab('users') }, h(Icon, { name: 'users', size: 14 }), 'Users'),
        h('button', { className: tab === 'backups' ? 'active' : '', onClick: () => setTab('backups') }, h(Icon, { name: 'download', size: 14 }), 'Backups'),
        h('button', { className: tab === 'audit' ? 'active' : '', onClick: () => setTab('audit') }, h(Icon, { name: 'history', size: 14 }), 'Audit')),
      tab === 'connections' ? h(Connections) : tab === 'networks' ? h(Networks)
        : tab === 'users' ? h(Users) : tab === 'backups' ? h(Backups) : h(AuditLog));
  }

  /* ---- Connections ---- */
  function ConnModal({ conn, onClose, onDone }) {
    const editing = !!conn;
    const [f, setF] = useState(() => ({
      name: conn ? conn.name : '',
      host: conn ? (conn.host || (conn.url || '').replace(/^https?:\/\//, '').split(':')[0]) : '',
      port: conn ? (conn.port || 8006) : 8006,
      token_id: conn ? (conn.tokenId || '') : '', token_secret: '',
      node: conn ? conn.node : '',
      storage: conn ? (conn.storage === '—' ? '' : conn.storage) : 'local-zfs',
      iso_storage: conn ? (conn.isoStorage || 'local') : 'local',
      snippet_storage: conn ? (conn.snippetStorage || 'local') : 'local',
      bridge: conn ? conn.bridge : 'vmbr0',
      max_cores: conn ? (conn.maxCores || 0) : 0,
      max_ram_gb: conn ? (conn.maxRamGb || 0) : 0,
      max_disk_gb: conn ? (conn.maxDiskGb || 0) : 0,
    }));
    const [busy, setBusy] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const submit = async () => {
      if (!f.name || !f.host || (!editing && (!f.token_id || !f.token_secret))) { toast('Name, host and token are required', 'err'); return; }
      setBusy(true);
      try {
        const limits = { max_cores: Number(f.max_cores) || 0, max_ram_gb: Number(f.max_ram_gb) || 0, max_disk_gb: Number(f.max_disk_gb) || 0 };
        if (editing) {
          const payload = { name: f.name, host: f.host, port: Number(f.port), node: f.node, storage: f.storage, iso_storage: f.iso_storage, snippet_storage: f.snippet_storage, bridge: f.bridge, ...limits };
          if (f.token_id) payload.token_id = f.token_id;
          if (f.token_secret) payload.token_secret = f.token_secret;
          await window.API.editConnection(conn.connId, payload);
        } else {
          await window.API.addConnection({ ...f, port: Number(f.port), ...limits });
        }
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit connection' : 'Add Proxmox connection', icon: 'server', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add', width: 'min(560px, 94vw)' },
      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
        h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v) }),
        h(Field, { label: 'Host / IP', value: f.host, onChange: (v) => set('host', v), mono: true }),
        h(Field, { label: 'Token ID', value: f.token_id, onChange: (v) => set('token_id', v), mono: true, placeholder: 'goblindock@pve!app' }),
        h(Field, { label: 'Token secret' + (editing ? ' (leave blank to keep)' : ''), value: f.token_secret, onChange: (v) => set('token_secret', v), mono: true, type: 'password', placeholder: editing ? '••••••••' : '' }),
        h(Field, { label: 'Default node', value: f.node, onChange: (v) => set('node', v), mono: true }),
        h(Field, { label: 'VM storage', value: f.storage, onChange: (v) => set('storage', v), mono: true }),
        h(Field, { label: 'ISO storage', value: f.iso_storage, onChange: (v) => set('iso_storage', v), mono: true }),
        h(Field, { label: 'Bridge', value: f.bridge, onChange: (v) => set('bridge', v), mono: true }),
        h('div', { style: { gridColumn: '1 / -1' } },
          h('label', { className: 'field-label' }, 'Per-VM limits for this target ',
            h('span', { className: 'hint', style: { fontWeight: 400, fontSize: 11 } }, '· 0 = inherit global')),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 } },
            h(Field, { label: 'Max vCPU', value: f.max_cores, onChange: (v) => set('max_cores', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' }),
            h(Field, { label: 'Max RAM (GB)', value: f.max_ram_gb, onChange: (v) => set('max_ram_gb', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' }),
            h(Field, { label: 'Max disk (GB)', value: f.max_disk_gb, onChange: (v) => set('max_disk_gb', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' })))));
  }

  function Connections() {
    const [modal, setModal] = useState(null);    // 'add' | {conn}
    const [confirm, setConfirm] = useState(null);
    const [testing, setTesting] = useState({});
    const test = async (c) => {
      setTesting((t) => ({ ...t, [c.connId]: true }));
      try { const r = await window.API.testConnection(c.connId); toast(r.ok ? (c.name + ' online · v' + r.version) : (c.name + ' offline: ' + (r.error || '')), r.ok ? 'ok' : 'err'); }
      catch (e) { toast(e.message, 'err'); }
      setTesting((t) => ({ ...t, [c.connId]: false })); refresh();
    };
    const del = async (c) => { try { await window.API.deleteConnection(c.connId); toast('Connection removed', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 14 } },
        h('span', { className: 'panel-title' }, (GD.CONNECTIONS || []).length, ' connections'),
        h('button', { className: 'btn primary sm', style: { marginLeft: 'auto' }, onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 15 }), 'Add connection')),
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 14 } },
        (GD.CONNECTIONS || []).map((c) => h('div', { key: c.id, className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 13 } },
          h('div', { className: 'row', style: { gap: 10 } },
            h('span', { className: 'placed-ico', style: { width: 34, height: 34, background: c.status === 'online' ? 'var(--ok-ghost)' : 'var(--surface-3)', color: c.status === 'online' ? 'var(--ok)' : 'var(--text-faint)' } }, h(Icon, { name: 'server', size: 17 })),
            h('div', { style: { minWidth: 0 } },
              h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, c.name),
              h('div', { className: 'copy mono', style: { fontSize: 11 } }, c.url)),
            h('div', { style: { marginLeft: 'auto' } },
              c.status === 'online' ? h('span', { className: 'badge running' }, h('span', { className: 'dot running' }), 'v', c.version)
                : c.status === 'offline' ? h('span', { className: 'badge error' }, h('span', { className: 'dot error' }), 'Offline')
                : h('span', { className: 'badge' }, 'Unknown'))),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 } },
            h(Stat, { k: 'Node', v: c.node || '—' }), h(Stat, { k: 'Storage', v: c.storage }), h(Stat, { k: 'VMs', v: c.vms })),
          (function () {
            const g = GD.limits || {};
            const cpu = c.maxCores || g.maxCores || '∞';
            const ram = c.maxRamGb || g.maxRam || '∞';
            const disk = c.maxDiskGb || '∞';
            return h('div', { className: 'row', style: { gap: 7, color: 'var(--text-faint)' } },
              h(Icon, { name: 'sliders', size: 12 }),
              h('span', { className: 'hint mono', style: { fontSize: 11 } }, 'Per-VM max: ' + cpu + ' vCPU · ' + ram + ' GB · ' + disk + ' GB'));
          })(),
          h('div', { className: 'divider' }),
          h('div', { className: 'row', style: { gap: 8 } },
            h('button', { className: 'btn sm', style: { flex: 1 }, onClick: () => test(c), disabled: testing[c.connId] }, h(Icon, { name: 'refresh', size: 14 }), testing[c.connId] ? 'Testing…' : 'Test'),
            h('button', { className: 'btn ghost sm icon', onClick: () => setModal({ conn: c }) }, h(Icon, { name: 'edit', size: 15 })),
            h('button', { className: 'icon-btn danger', onClick: () => setConfirm(c) }, h(Icon, { name: 'trash', size: 16 })))))),
      modal === 'add' && h(ConnModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Connection added', 'ok'); refresh(); } }),
      modal && modal.conn && h(ConnModal, { conn: modal.conn, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Connection updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Remove ' + confirm.name + '?', body: 'Only allowed if it has no VMs or golden images. This does not touch the Proxmox node.', confirmLabel: 'Remove', onConfirm: () => del(confirm) }));
  }

  function Stat({ k, v }) {
    return h('div', null,
      h('div', { className: 'panel-title', style: { fontSize: 10, marginBottom: 3 } }, k),
      h('div', { className: 'mono', style: { fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' } }, v));
  }

  /* ---- Networks ---- */
  function NetworkModal({ net, onClose, onDone }) {
    const editing = !!net;
    const conns = GD.CONNECTIONS || [];
    const [f, setF] = useState(() => ({
      connectionId: net ? net.connId : (conns[0] && conns[0].connId) || null,
      name: net ? net.name : '', mode: net ? net.rawMode : 'dhcp', bridge: net ? net.bridge : 'vmbr0',
      vlan: net && net.vlan !== '—' ? net.vlan : '', subnet_cidr: net ? net.subnet === '(DHCP)' ? '' : net.subnet : '',
      gateway: net ? net.gateway : '', range_start: net ? net.rangeStart : '', range_end: net ? net.rangeEnd : '', dns: net ? net.dns : '',
    }));
    const [busy, setBusy] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const submit = async () => {
      if (!f.name.trim()) { toast('Name required', 'err'); return; }
      setBusy(true);
      try {
        const payload = { ...f, connectionId: Number(f.connectionId), vlan: f.vlan ? Number(f.vlan) : null };
        if (editing) await window.API.editNetwork(net.netId, payload);
        else await window.API.addNetwork(payload);
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit network' : 'Add network', icon: 'network', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add' },
      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
        h(SelectField, { label: 'Connection', value: f.connectionId, onChange: (v) => set('connectionId', v), options: conns.map((c) => ({ value: c.connId, label: c.name })) }),
        h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v), mono: true }),
        h(SelectField, { label: 'Mode', value: f.mode, onChange: (v) => set('mode', v), options: [{ value: 'dhcp', label: 'DHCP' }, { value: 'static', label: 'Static pool' }] }),
        h(Field, { label: 'Bridge', value: f.bridge, onChange: (v) => set('bridge', v), mono: true }),
        h(Field, { label: 'VLAN (optional)', value: f.vlan, onChange: (v) => set('vlan', v), mono: true })),
      f.mode === 'static' && h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
        h(Field, { label: 'Subnet CIDR', value: f.subnet_cidr, onChange: (v) => set('subnet_cidr', v), mono: true, placeholder: '10.0.50.0/24' }),
        h(Field, { label: 'Gateway', value: f.gateway, onChange: (v) => set('gateway', v), mono: true }),
        h(Field, { label: 'Range start', value: f.range_start, onChange: (v) => set('range_start', v), mono: true }),
        h(Field, { label: 'Range end', value: f.range_end, onChange: (v) => set('range_end', v), mono: true }),
        h(Field, { label: 'DNS', value: f.dns, onChange: (v) => set('dns', v), mono: true })));
  }

  function Networks() {
    const [modal, setModal] = useState(null);
    const [confirm, setConfirm] = useState(null);
    const del = async (n) => { try { await window.API.deleteNetwork(n.netId); toast('Network deleted', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 14 } },
        h('span', { className: 'panel-title' }, 'Per-connection networks'),
        h('button', { className: 'btn primary sm', style: { marginLeft: 'auto' }, onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 15 }), 'Add network')),
      h('div', { className: 'card', style: { overflow: 'hidden' } },
        h('table', { className: 'tbl' },
          h('thead', null, h('tr', null, ['Name', 'Connection', 'Mode', 'Bridge', 'Subnet', 'IP allocation', ''].map((c, i) => h('th', { key: i }, c)))),
          h('tbody', null, (GD.NETWORKS || []).map((n) => h('tr', { key: n.id },
            h('td', null, h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 13 } }, n.name)),
            h('td', null, h('span', { className: 'chip' }, n.conn)),
            h('td', null, n.mode === 'DHCP' ? h('span', { className: 'badge info' }, 'DHCP') : h('span', { className: 'badge accent' }, 'Static')),
            h('td', { className: 'mono', style: { fontSize: 12 } }, n.bridge, n.vlan !== '—' ? (' · vlan ' + n.vlan) : ''),
            h('td', { className: 'mono', style: { fontSize: 12 } }, n.subnet),
            h('td', { style: { width: 150 } }, h('div', { className: 'row', style: { gap: 8 } },
              h('div', { style: { width: 64 } }, h('div', { className: 'meter' }, h('i', { style: { width: (n.used / Math.max(1, n.total) * 100) + '%' } }))),
              h('span', { className: 'mono hint', style: { fontSize: 11 } }, n.used, ' / ', n.total))),
            h('td', null, h(Menu, { items: [
              { label: 'Edit', icon: 'edit', onClick: () => setModal({ net: n }) },
              { sep: true },
              { label: 'Delete', icon: 'trash', danger: true, onClick: () => setConfirm(n) },
            ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 }))))))))),
      modal === 'add' && h(NetworkModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Network added', 'ok'); refresh(); } }),
      modal && modal.net && h(NetworkModal, { net: modal.net, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Network updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'Only allowed if no VM uses it.', confirmLabel: 'Delete', onConfirm: () => del(confirm) }));
  }

  /* ---- Users ---- */
  function UserModal({ user, onClose, onDone }) {
    const editing = !!user;
    const [f, setF] = useState(() => ({
      name: user ? user.name : '', email: user ? user.email : '', password: '',
      role: user ? user.rawRole : 'user', disabled: user ? !!user.disabled : false,
    }));
    const [busy, setBusy] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const submit = async () => {
      setBusy(true);
      try {
        if (editing) await window.API.editUser(user.userId, { name: f.name, role: f.role, disabled: f.disabled });
        else {
          if (!f.name || !f.email || !f.password) { toast('All fields required', 'err'); setBusy(false); return; }
          await window.API.addUser(f);
        }
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit user' : 'Add user', icon: 'users', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add user' },
      h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v) }),
      !editing && h(Field, { label: 'Email', value: f.email, onChange: (v) => set('email', v), mono: true }),
      !editing && h(Field, { label: 'Password', value: f.password, onChange: (v) => set('password', v), type: 'password' }),
      h('div', null, h('label', { className: 'field-label' }, 'Role'),
        h('div', { className: 'seg', style: { width: '100%' } },
          h('button', { className: f.role === 'user' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => set('role', 'user') }, 'User'),
          h('button', { className: f.role === 'admin' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => set('role', 'admin') }, 'Admin'))),
      editing && h(Toggle, { label: 'Account disabled', on: f.disabled, onChange: (v) => set('disabled', v) }));
  }

  function ResetPwModal({ user, onClose, onDone }) {
    const [pw, setPw] = useState('');
    const [busy, setBusy] = useState(false);
    const submit = async () => { setBusy(true); try { await window.API.resetUserPassword(user.userId, pw); onDone(); } catch (e) { toast(e.message, 'err'); setBusy(false); } };
    return h(FormModal, { title: 'Reset password · ' + user.name, icon: 'key', onClose, onSubmit: submit, busy, submitLabel: 'Set password' },
      h(Field, { label: 'New password', value: pw, onChange: setPw, type: 'password', hint: 'At least 10 chars, 3 character classes.' }));
  }

  function Users() {
    const [modal, setModal] = useState(null);     // 'add' | {user} | {reset}
    const [confirm, setConfirm] = useState(null);
    const del = async (u) => { try { await window.API.deleteUser(u.userId); toast('User deleted', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 14 } },
        h('span', { className: 'panel-title' }, (GD.USERS || []).length, ' users'),
        h('button', { className: 'btn primary sm', style: { marginLeft: 'auto' }, onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 15 }), 'Add user')),
      h('div', { className: 'card', style: { overflow: 'hidden' } },
        h('table', { className: 'tbl' },
          h('thead', null, h('tr', null, ['User', 'Email', 'Role', 'Last login', 'VMs', ''].map((c, i) => h('th', { key: i }, c)))),
          h('tbody', null, (GD.USERS || []).map((u) => h('tr', { key: u.id, style: u.disabled ? { opacity: 0.5 } : null },
            h('td', null, h('div', { className: 'row', style: { gap: 9 } },
              h('span', { className: 'avatar', style: { width: 28, height: 28, cursor: 'default' } }, u.name.split(' ').map((x) => x[0]).join('').slice(0, 2)),
              h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 13 } }, u.name, u.disabled ? ' (disabled)' : ''))),
            h('td', { className: 'mono', style: { fontSize: 12, color: 'var(--text-dim)' } }, u.email),
            h('td', null, u.role === 'Admin' ? h('span', { className: 'badge accent' }, h(Icon, { name: 'shield', size: 12 }), 'Admin') : h('span', { className: 'badge' }, 'User')),
            h('td', { className: 'mono hint', style: { fontSize: 12 } }, u.last),
            h('td', { className: 'mono', style: { fontSize: 13 } }, u.vms),
            h('td', null, h(Menu, { items: [
              { label: 'Edit', icon: 'edit', onClick: () => setModal({ user: u }) },
              { label: 'Reset password', icon: 'key', onClick: () => setModal({ reset: u }) },
              { sep: true },
              { label: 'Delete', icon: 'trash', danger: true, onClick: () => setConfirm(u) },
            ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 }))))))))),
      modal === 'add' && h(UserModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('User added', 'ok'); refresh(); } }),
      modal && modal.user && h(UserModal, { user: modal.user, onClose: () => setModal(null), onDone: () => { setModal(null); toast('User updated', 'ok'); refresh(); } }),
      modal && modal.reset && h(ResetPwModal, { user: modal.reset, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Password reset', 'ok'); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'Only allowed if the user owns no VMs.', confirmLabel: 'Delete', onConfirm: () => del(confirm) }));
  }

  /* ---- Audit ---- */
  function AuditLog() {
    const LIMIT = 50;
    const [data, setData] = useState(null);   // { rows, total, limit, offset }
    const [q, setQ] = useState('');
    const [offset, setOffset] = useState(0);
    // Debounce the search; reload whenever the (debounced) query or page changes.
    React.useEffect(() => {
      let live = true;
      const t = setTimeout(() => {
        window.API.audit({ q, limit: LIMIT, offset })
          .then((d) => { if (live) setData(d); })
          .catch(() => { if (live) setData({ rows: [], total: 0, limit: LIMIT, offset }); });
      }, 220);
      return () => { live = false; clearTimeout(t); };
    }, [q, offset]);
    const onSearch = (v) => { setQ(v); setOffset(0); };
    const rows = (data && data.rows) || [];
    const total = (data && data.total) || 0;
    const page = Math.floor(offset / LIMIT) + 1;
    const pages = Math.max(1, Math.ceil(total / LIMIT));
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 12, gap: 10 } },
        h('div', { className: 'search', style: { flex: 1, maxWidth: 320 } },
          h(Icon, { name: 'search', size: 15 }),
          h('input', { placeholder: 'Search user, action, target, detail, IP…', value: q, onChange: (e) => onSearch(e.target.value) })),
        h('span', { className: 'hint mono', style: { fontSize: 12 } }, total, ' event', total === 1 ? '' : 's')),
      data === null
        ? h('div', { className: 'card', style: { padding: 30, textAlign: 'center', color: 'var(--text-faint)' } }, 'Loading…')
        : h('div', { className: 'card', style: { overflow: 'hidden' } },
            h('table', { className: 'tbl' },
              h('thead', null, h('tr', null, ['When', 'User', 'Action', 'Target', 'IP', 'Detail'].map((c, i) => h('th', { key: i }, c)))),
              h('tbody', null, rows.length === 0
                ? h('tr', null, h('td', { colSpan: 6, className: 'hint', style: { textAlign: 'center', padding: 24 } }, q ? 'No matching activity.' : 'No activity yet.'))
                : rows.map((a) => h('tr', { key: a.id },
                    h('td', { className: 'mono hint', style: { fontSize: 12 } }, a.ts),
                    h('td', { style: { fontSize: 12.5 } }, a.user),
                    h('td', null, h('span', { className: 'chip', style: { fontSize: 10.5 } }, a.action)),
                    h('td', { className: 'mono hint', style: { fontSize: 11.5 } }, a.target),
                    h('td', { className: 'mono hint', style: { fontSize: 11.5 } }, a.ip || '—'),
                    h('td', { className: 'hint', style: { fontSize: 12 } }, a.detail))))),
            ),
      pages > 1 && h('div', { className: 'row', style: { marginTop: 12, justifyContent: 'center', gap: 12 } },
        h('button', { className: 'btn sm', disabled: offset <= 0, onClick: () => setOffset(Math.max(0, offset - LIMIT)) }, h(Icon, { name: 'chevronL', size: 14 }), 'Prev'),
        h('span', { className: 'hint mono', style: { fontSize: 12 } }, 'Page ', page, ' / ', pages),
        h('button', { className: 'btn sm', disabled: page >= pages, onClick: () => setOffset(offset + LIMIT) }, 'Next', h(Icon, { name: 'chevronR', size: 14 }))));
  }

  /* ---- Backups (admin) ---- */
  function Backups() {
    const [data, setData] = useState(null);
    const [busy, setBusy] = useState(false);
    const load = () => window.API.adminBackups().then(setData).catch(() => setData({ backups: [] }));
    React.useEffect(() => { load(); }, []);
    const backupNow = async () => {
      setBusy(true);
      try { const r = await window.API.runBackup(); toast('Backup created · ' + r.name, 'ok'); load(); }
      catch (e) { toast(e.message || 'backup failed', 'err'); }
      setBusy(false);
    };
    const fmtBytes = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB');
    const fmtTs = (iso) => { try { return new Date(iso).toLocaleString(); } catch (e) { return iso; } };
    if (data === null) return h('div', { className: 'card', style: { padding: 30, textAlign: 'center', color: 'var(--text-faint)' } }, 'Loading…');
    const list = data.backups || [];
    return h('div', null,
      h('div', { className: 'card card-pad', style: { marginBottom: 14 } },
        h('div', { className: 'row', style: { gap: 12 } },
          h(Icon, { name: 'history', size: 18, style: { color: 'var(--accent)', flexShrink: 0 } }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 13.5 } },
              data.enabled ? ('Automatic backup every ' + data.intervalHours + 'h') : 'Automatic backups disabled'),
            h('div', { className: 'hint', style: { fontSize: 11.5, wordBreak: 'break-all' } }, 'Keeps newest ' + data.keep + ' · ' + (data.dir || ''))),
          h('div', { style: { marginLeft: 'auto' } },
            h('button', { className: 'btn primary', onClick: backupNow, disabled: busy },
              h(Icon, { name: 'download', size: 15 }), busy ? 'Backing up…' : 'Back up now')))),
      h('div', { className: 'card', style: { overflow: 'hidden' } },
        h('table', { className: 'tbl' },
          h('thead', null, h('tr', null, ['Backup file', 'Size', 'Created'].map((c, i) => h('th', { key: i }, c)))),
          h('tbody', null, list.length === 0
            ? h('tr', null, h('td', { colSpan: 3, className: 'hint', style: { textAlign: 'center', padding: 24 } }, 'No backups yet — they appear here on schedule, or use “Back up now”.'))
            : list.map((b) => h('tr', { key: b.name },
                h('td', { className: 'mono', style: { fontSize: 12 } }, b.name),
                h('td', { className: 'mono hint', style: { fontSize: 12 } }, fmtBytes(b.bytes)),
                h('td', { className: 'hint', style: { fontSize: 12 } }, fmtTs(b.modified))))))));
  }

  window.BlocksLib = BlocksLib;
  window.Secrets = Secrets;
  window.Variables = Variables;
  window.Settings = Settings;
})();
