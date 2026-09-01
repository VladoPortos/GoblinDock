/* GoblinDock — Blocks library, Secrets, Settings (full CRUD). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu, ConfirmModal, FormModal, Field, TextArea, SelectField, Toggle, fmtBytes, useFetched } = window.UI;
  const h = React.createElement;

  const refresh = () => window.GDStore.refresh().catch(() => {});
  const toast = (m, t) => window.GDStore.toast(m, t);

  // Secrets and Variables share the UPPER_SNAKE name rule and the Global/Personal
  // scope selector — one copy each instead of a clone per modal.
  const normName = (v) => v.toUpperCase().replace(/[^A-Z0-9_]/g, '_');
  function ScopeField({ scope, setScope }) {
    return h('div', null, h('label', { className: 'field-label' }, 'Scope'),
      h('div', { className: 'seg', style: { width: '100%' } },
        h('button', { className: scope === 'global' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('global') }, 'Global'),
        h('button', { className: scope === 'user' ? 'active' : '', style: { flex: 1, justifyContent: 'center' }, onClick: () => setScope('user') }, 'Personal')));
  }

  /* ============ BLOCKS LIBRARY ============ */
  function BlocksLib() {
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
          h('div', { className: 'page-sub' }, 'The Lego pieces of every template. Fork a built-in to customise it.')),
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
                  h('button', {
                    type: 'button', className: 'icon-btn danger',
                    disabled: b.canDelete !== true,
                    title: b.canDelete === true ? 'Delete block' : 'This block is referenced by a template',
                    'aria-label': 'Delete ' + b.name,
                    onClick: () => setConfirm(b),
                  }, h(Icon, { name: 'trash', size: 15 }))))))),
      editor && h(window.BlockEditorModal, { initial: editor.initial, onClose: () => setEditor(null), onSaved: () => { setEditor(null); toast('Block saved', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Delete ' + confirm.name + '?', body: 'This permanently removes your custom block. It is not referenced by any template.', confirmLabel: 'Delete block', onConfirm: () => del(confirm) }));
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
      h(Field, { label: 'Name', value: name, onChange: (v) => setName(normName(v)), mono: true, placeholder: 'TEAM_SSH_PUBKEY' }),
      h(TextArea, { label: editing ? 'New value' : 'Value', value, onChange: setValue, rows: 4, mono: true }),
      !editing && isAdmin && h(ScopeField, { scope, setScope }),
      h('p', { className: 'hint', style: { fontSize: 11.5 } }, 'Reference in blocks as ', h('code', { className: 'kbd' }, '{{ secrets.' + (name || 'NAME') + ' }}'), '.'));
  }

  function Secrets() {
    const [modal, setModal] = useState(null);   // 'add' | {secret}
    const [confirm, setConfirm] = useState(null);
    const del = async (s) => { try { await window.API.deleteSecret(s.secId); toast('Secret deleted', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); } };
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
            h('div', { className: 'table-scroll' },
            h('table', { className: 'tbl' },
              h('thead', null, h('tr', null, ['Name', 'Scope', 'Value', 'Created by', 'Last used', ''].map((c, i) => h('th', { key: i }, c)))),
              h('tbody', null, (GD.SECRETS || []).map((s) => h(SecretRow, { key: s.id, s, onDelete: (x) => setConfirm(x), onEdit: (x) => setModal({ secret: x }) })))))),
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
      h(Field, { label: 'Name', value: name, onChange: (v) => setName(normName(v)), mono: true, placeholder: 'APP_PORT' }),
      h(TextArea, { label: 'Value', value, onChange: setValue, rows: 3, mono: true }),
      !editing && isAdmin && h(ScopeField, { scope, setScope }),
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
          h('div', { className: 'page-sub' }, 'Plain (non-secret) values — visible, reusable across scripts and templates.')),
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
            h('div', { className: 'table-scroll' },
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
                ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 })))))))))),
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
      h('div', { className: 'seg settings-section-selector', style: { marginBottom: 18 } },
        h('button', { className: tab === 'connections' ? 'active' : '', onClick: () => setTab('connections') }, h(Icon, { name: 'server', size: 14 }), 'Proxmox'),
        h('button', { className: tab === 'networks' ? 'active' : '', onClick: () => setTab('networks') }, h(Icon, { name: 'network', size: 14 }), 'Networks'),
        h('button', { className: tab === 'users' ? 'active' : '', onClick: () => setTab('users') }, h(Icon, { name: 'users', size: 14 }), 'Users'),
        h('button', { className: tab === 'backups' ? 'active' : '', onClick: () => setTab('backups') }, h(Icon, { name: 'download', size: 14 }), 'Backups'),
        h('button', { className: tab === 'audit' ? 'active' : '', onClick: () => setTab('audit') }, h(Icon, { name: 'history', size: 14 }), 'Audit'),
        h('button', { className: tab === 'prefs' ? 'active' : '', onClick: () => setTab('prefs') }, h(Icon, { name: 'sliders', size: 14 }), 'Preferences'),
        h('button', { className: tab === 'health' ? 'active' : '', onClick: () => setTab('health') }, h(Icon, { name: 'activity', size: 14 }), 'Health')),
      tab === 'connections' ? h(Connections) : tab === 'networks' ? h(Networks)
        : tab === 'users' ? h(Users) : tab === 'backups' ? h(Backups)
        : tab === 'prefs' ? h(Preferences) : tab === 'health' ? h(Health) : h(AuditLog));
  }

  /* ---- Connections ---- */
  function connectionDraft(conn) {
    const c = conn || {};
    const hostFromUrl = (c.url || '').replace(/^https?:\/\//, '').split(':')[0];
    return {
      name: c.name ?? '',
      host: c.host ?? hostFromUrl,
      port: c.port ?? 8006,
      token_id: c.tokenId ?? '',
      token_secret: '',
      node: c.node ?? '',
      storage: c.storage === '—' ? '' : (c.storage ?? (conn ? '' : 'local-zfs')),
      iso_storage: c.isoStorage ?? 'local',
      snippet_storage: c.snippetStorage ?? 'local',
      bridge: c.bridge ?? 'vmbr0',
      verify_tls: c.verifyTls == null ? true : !!c.verifyTls,
      ssh_host: c.sshHost ?? '',
      ssh_user: c.sshUser ?? 'root',
      ssh_key_path: c.sshKeyPath ?? '',
      max_cores: c.maxCores ?? 0,
      max_ram_gb: c.maxRamGb ?? 0,
      max_disk_gb: c.maxDiskGb ?? 0,
    };
  }

  function finiteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function nonnegativeNumber(value) {
    return Math.max(0, finiteNumber(value, 0));
  }

  function connectionPayload(draft, editing) {
    const portText = String(draft.port == null ? '' : draft.port).trim();
    const payload = {
      name: draft.name,
      host: draft.host,
      port: portText ? finiteNumber(draft.port, 8006) : 8006,
      token_id: draft.token_id,
      verify_tls: draft.verify_tls,
      node: draft.node,
      storage: draft.storage,
      iso_storage: draft.iso_storage,
      snippet_storage: draft.snippet_storage,
      bridge: draft.bridge,
      ssh_host: draft.ssh_host,
      ssh_user: draft.ssh_user,
      ssh_key_path: draft.ssh_key_path,
      max_cores: nonnegativeNumber(draft.max_cores),
      max_ram_gb: nonnegativeNumber(draft.max_ram_gb),
      max_disk_gb: nonnegativeNumber(draft.max_disk_gb),
    };
    if (!editing || String(draft.token_secret ?? '').trim()) {
      payload.token_secret = draft.token_secret ?? '';
    }
    return payload;
  }

  function ConnModal({ conn, onClose, onDone }) {
    const editing = !!conn;
    const [f, setF] = useState(() => connectionDraft(conn));
    const [busy, setBusy] = useState(false);
    const [probe, setProbe] = useState(null);      // null until a successful "Load from Proxmox"
    const [probing, setProbing] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const canProbe = !!(f.host && f.token_id && (editing || f.token_secret));
    const loadFromProxmox = async () => {
      setProbing(true);
      try {
        const r = await window.API.probeConnection({
          host: f.host, port: Number(f.port), token_id: f.token_id,
          token_secret: f.token_secret, verify_tls: f.verify_tls,
          conn_id: editing ? conn.connId : null,
        });
        if (r && r.ok) { setProbe(r); toast('Loaded from Proxmox', 'ok'); }
        else { toast((r && r.error) || 'Could not reach Proxmox', 'err'); }
      } catch (e) { toast(e.message, 'err'); }
      setProbing(false);
    };
    // Build <select> options for a discovered field, ALWAYS including the current
    // value (so editing never silently drops a stored value not on the host).
    const optsWith = (names, current) => {
      const list = (names || []).slice();
      if (current && !list.includes(current)) list.unshift(current);
      return list;
    };
    const storeNames = probe ? probe.storages.map((s) => s.name) : [];
    const vmStores = probe ? probe.storages.filter((s) => (s.content || []).includes('images')).map((s) => s.name) : [];
    const isoStores = probe ? probe.storages.filter((s) => (s.content || []).includes('import') || (s.content || []).includes('iso')).map((s) => s.name) : [];
    // Empty filter → fall back to ALL storages so the field is never un-pickable.
    const vmStoreOpts = optsWith(vmStores.length ? vmStores : storeNames, f.storage);
    const isoStoreOpts = optsWith(isoStores.length ? isoStores : storeNames, f.iso_storage);
    const nodeOpts = optsWith(probe ? probe.nodes : [], f.node);
    const bridgeOpts = optsWith(probe ? probe.bridges : [], f.bridge);
    const submit = async () => {
      if (!f.name || !f.host || (!editing && (!f.token_id || !f.token_secret))) { toast('Name, host and token are required', 'err'); return; }
      setBusy(true);
      try {
        const payload = connectionPayload(f, editing);
        if (editing) {
          await window.API.editConnection(conn.connId, payload);
        } else {
          await window.API.addConnection(payload);
        }
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit connection' : 'Add Proxmox connection', icon: 'server', onClose, onSubmit: submit, busy, submitLabel: editing ? 'Save' : 'Add', width: 'min(560px, 94vw)' },
      h('div', { className: 'connection-form-grid', style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
        h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v) }),
        h(Field, { label: 'Host / IP', value: f.host, onChange: (v) => set('host', v), mono: true }),
        h(Field, { label: 'API port', value: f.port, onChange: (v) => set('port', v.replace(/[^0-9]/g, '')), mono: true }),
        h(Field, { label: 'Token ID', value: f.token_id, onChange: (v) => set('token_id', v), mono: true, placeholder: 'goblindock@pve!app' }),
        h(Field, { label: 'Token secret' + (editing ? ' (leave blank to keep)' : ''), value: f.token_secret, onChange: (v) => set('token_secret', v), mono: true, type: 'password', placeholder: editing ? '••••••••' : '' }),
        h('div', { style: { gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 10 } },
          h('button', { type: 'button', className: 'btn sm', onClick: loadFromProxmox, disabled: !canProbe || probing },
            h(Icon, { name: 'download', size: 14 }), probing ? 'Loading…' : 'Load from Proxmox'),
          probe && h('span', { className: 'hint mono', style: { fontSize: 11, color: 'var(--ok)' } },
            '✓ PVE ' + (probe.version || '—') + ' · ' + (probe.nodes || []).length + ' node' + ((probe.nodes || []).length === 1 ? '' : 's')),
          !probe && !canProbe && h('span', { className: 'hint', style: { fontSize: 11 } },
            'Enter host, token id' + (editing ? '' : ' and secret') + ' to load')),
        h('div', { style: { gridColumn: '1 / -1' } },
          h(Toggle, { label: 'Verify TLS certificate (uncheck for a self-signed homelab node)',
                      on: f.verify_tls, onChange: (v) => set('verify_tls', v) })),
        probe
          ? h(SelectField, { label: 'Default node', value: f.node, onChange: (v) => set('node', v), options: nodeOpts })
          : h(Field, { label: 'Default node', value: f.node, onChange: (v) => set('node', v), mono: true }),
        probe
          ? h(SelectField, { label: 'VM storage', value: f.storage, onChange: (v) => set('storage', v), options: vmStoreOpts })
          : h(Field, { label: 'VM storage', value: f.storage, onChange: (v) => set('storage', v), mono: true }),
        probe
          ? h(SelectField, { label: 'ISO storage', value: f.iso_storage, onChange: (v) => set('iso_storage', v), options: isoStoreOpts })
          : h(Field, { label: 'ISO storage', value: f.iso_storage, onChange: (v) => set('iso_storage', v), mono: true }),
        h(Field, { label: 'Snippet storage', value: f.snippet_storage, onChange: (v) => set('snippet_storage', v), mono: true }),
        probe
          ? h(SelectField, { label: 'Bridge', value: f.bridge, onChange: (v) => set('bridge', v), options: bridgeOpts })
          : h(Field, { label: 'Bridge', value: f.bridge, onChange: (v) => set('bridge', v), mono: true }),
        h(Field, { label: 'SSH host', value: f.ssh_host, onChange: (v) => set('ssh_host', v), mono: true }),
        h(Field, { label: 'SSH user', value: f.ssh_user, onChange: (v) => set('ssh_user', v), mono: true }),
        h(Field, { label: 'SSH key path', value: f.ssh_key_path, onChange: (v) => set('ssh_key_path', v), mono: true, placeholder: '/run/secrets/pve_key' }),
        h('div', { style: { gridColumn: '1 / -1' } },
          h('label', { className: 'field-label' }, 'Per-VM limits for this target ',
            h('span', { className: 'hint', style: { fontWeight: 400, fontSize: 11 } }, '· 0 = unlimited')),
          h('div', { className: 'connection-limit-grid', style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 } },
            h(Field, { label: 'Max vCPU', value: f.max_cores, onChange: (v) => set('max_cores', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' }),
            h(Field, { label: 'Max RAM (GB)', value: f.max_ram_gb, onChange: (v) => set('max_ram_gb', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' }),
            h(Field, { label: 'Max disk (GB)', value: f.max_disk_gb, onChange: (v) => set('max_disk_gb', v.replace(/[^0-9]/g, '')), mono: true, placeholder: '0' })))));
  }

  function NodeGauge({ connId, disabled }) {
    // a disabled source is never probed — don't even ask the capacity endpoint
    const cap = useFetched(() => disabled
      ? Promise.resolve({ online: false, disabled: true })
      : window.API.connectionCapacity(connId), [connId, disabled], { online: false });
    if (disabled) return h('div', { className: 'hint mono', style: { fontSize: 11, opacity: 0.6 } }, 'source disabled — not polled');
    if (!cap) return h('div', { className: 'hint mono', style: { fontSize: 11 } }, 'checking capacity…');
    if (!cap.online) return h('div', { className: 'hint mono', style: { fontSize: 11, opacity: 0.6 } }, 'node offline');
    const bar = (label, used, total) => h('div', { style: { marginTop: 4 } },
      h('div', { className: 'hint mono', style: { fontSize: 10.5, display: 'flex', justifyContent: 'space-between' } },
        h('span', null, label), h('span', null, used + ' / ' + total + ' GB')),
      h('div', { style: { height: 5, background: 'var(--border)', borderRadius: 3, overflow: 'hidden', marginTop: 2 } },
        h('div', { style: { height: '100%', width: Math.min(100, total ? (used / total * 100) : 0) + '%', background: 'var(--accent)' } })));
    return h('div', { style: { marginTop: 6 } },
      bar('RAM', cap.mem.usedGb, cap.mem.totalGb),
      bar(cap.storage.name, cap.storage.usedGb, cap.storage.totalGb));
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
    const del = async (c) => {
      try {
        await window.API.deleteConnection(c.connId);
        // drop the card (and its networks) NOW — the in-flight /state may still be
        // waiting on this very connection's dead probe and predates the delete
        window.GDStore.removeConnection(c.connId);
        toast('Connection removed', 'ok');
        window.GDStore.refresh({ fresh: true }).catch(() => {});
      } catch (e) { toast(e.message, 'err'); }
    };
    const toggleDisabled = async (c) => {
      try {
        await window.API.editConnection(c.connId, { disabled: !c.disabled });
        toast(c.disabled
          ? c.name + ' enabled — inventory refreshes on the next poll'
          : c.name + ' disabled — its VMs are hidden and it will not be polled or targeted', c.disabled ? 'ok' : 'warn');
        refresh();
      } catch (e) { toast(e.message, 'err'); }
    };
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
              c.disabled ? h('span', { className: 'badge', title: 'Disabled by an admin — not polled, VMs hidden, no new operations' }, h('span', { className: 'dot' }), 'Disabled')
                : c.status === 'online' ? h('span', { className: 'badge running' }, h('span', { className: 'dot running' }), 'v', c.version)
                : c.status === 'offline' ? h('span', { className: 'badge error' }, h('span', { className: 'dot error' }), 'Offline')
                : h('span', { className: 'badge' }, 'Unknown'))),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 } },
            h(Stat, { k: 'Node', v: c.node || '—' }), h(Stat, { k: 'Storage', v: c.storage }), h(Stat, { k: 'VMs', v: c.vms })),
          (function () {
            // 0 = unlimited for that dimension (the connection limit is authoritative).
            const cpu = c.maxCores || '∞';
            const ram = c.maxRamGb || '∞';
            const disk = c.maxDiskGb || '∞';
            return h('div', { className: 'row', style: { gap: 7, color: 'var(--text-faint)' } },
              h(Icon, { name: 'sliders', size: 12 }),
              h('span', { className: 'hint mono', style: { fontSize: 11 } }, 'Per-VM max: ' + cpu + ' vCPU · ' + ram + ' GB · ' + disk + ' GB'));
          })(),
          h(NodeGauge, { connId: c.connId, disabled: c.disabled }),
          h('div', { className: 'divider' }),
          h('div', { className: 'row', style: { gap: 8 } },
            h('button', { className: 'btn sm', style: { flex: 1 }, onClick: () => test(c), disabled: testing[c.connId] }, h(Icon, { name: 'refresh', size: 14 }), testing[c.connId] ? 'Testing…' : 'Test'),
            h('button', {
              className: 'btn sm' + (c.disabled ? ' primary' : ''), style: { flex: 1 },
              title: c.disabled ? 'Re-enable: VMs reappear and inventory refreshes — nothing was lost'
                : 'Disable: keep the config and VM records, but stop polling and hide its VMs (for a source that is offline / in maintenance / retired)',
              onClick: () => toggleDisabled(c),
            }, h(Icon, { name: c.disabled ? 'play' : 'stop', size: 14 }), c.disabled ? 'Enable' : 'Disable'),
            h('button', { className: 'btn ghost sm icon', onClick: () => setModal({ conn: c }) }, h(Icon, { name: 'edit', size: 15 })),
            h('button', { className: 'icon-btn danger', onClick: () => setConfirm(c) }, h(Icon, { name: 'trash', size: 16 })))))),
      modal === 'add' && h(ConnModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Connection added', 'ok'); refresh(); } }),
      modal && modal.conn && h(ConnModal, { conn: modal.conn, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Connection updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Remove ' + confirm.name + '?', body: 'Only allowed if it has no VMs. (Legacy image rows that still reference it block deletion too.) This does not touch the Proxmox node.', confirmLabel: 'Remove', onConfirm: () => del(confirm) }));
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
    const del = async (n) => {
      try {
        await window.API.deleteNetwork(n.netId);
        window.GDStore.removeNetwork(n.netId);
        toast('Network deleted', 'ok');
        window.GDStore.refresh({ fresh: true }).catch(() => {});
      } catch (e) { toast(e.message, 'err'); }
    };
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 14 } },
        h('span', { className: 'panel-title' }, 'Per-connection networks'),
        h('button', { className: 'btn primary sm', style: { marginLeft: 'auto' }, onClick: () => setModal('add') }, h(Icon, { name: 'plus', size: 15 }), 'Add network')),
      h('div', { className: 'card', style: { overflow: 'hidden' } },
        h('div', { className: 'table-scroll' },
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
              ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 })))))))))),
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
        h('div', { className: 'table-scroll' },
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
              ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 16 })))))))))),
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
            h('div', { className: 'table-scroll' },
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
            )),
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
        h('div', { className: 'table-scroll' },
        h('table', { className: 'tbl' },
          h('thead', null, h('tr', null, ['Backup file', 'Size', 'Created'].map((c, i) => h('th', { key: i }, c)))),
          h('tbody', null, list.length === 0
            ? h('tr', null, h('td', { colSpan: 3, className: 'hint', style: { textAlign: 'center', padding: 24 } }, 'No backups yet — they appear here on schedule, or use “Back up now”.'))
            : list.map((b) => h('tr', { key: b.name },
                h('td', { className: 'mono', style: { fontSize: 12 } }, b.name),
                h('td', { className: 'mono hint', style: { fontSize: 12 } }, fmtBytes(b.bytes)),
                h('td', { className: 'hint', style: { fontSize: 12 } }, fmtTs(b.modified)))))))));
  }

  /* ---- Health ---- */
  function fmtUptime(sec) {
    if (sec == null) return '—';
    const d = Math.floor(sec / 86400), hrs = Math.floor((sec % 86400) / 3600), min = Math.floor((sec % 3600) / 60);
    if (d) return d + 'd ' + hrs + 'h';
    if (hrs) return hrs + 'h ' + min + 'm';
    return Math.max(1, min) + 'm';
  }

  function HealthRow({ ok, label, detail }) {
    // details may be long (build ids, backup names) — wrap, never truncate
    return h('div', { className: 'row', style: { gap: 9, padding: '6px 0', alignItems: 'baseline' } },
      h('span', { className: 'dot ' + (ok ? 'running' : 'error'), style: { flexShrink: 0 } }),
      h('span', { className: 'mono', style: { fontSize: 12.5, fontWeight: 600, minWidth: 110, flexShrink: 0 } }, label),
      h('span', { className: 'hint mono', style: { fontSize: 11.5, flex: 1, minWidth: 0, overflowWrap: 'anywhere' } },
        detail || (ok ? 'ok' : 'not running')));
  }

  function Health() {
    const [tick, setTick] = useState(0);
    const hlt = useFetched(() => window.API.systemHealth(), [tick], { error: true });
    if (!hlt) return h('div', { className: 'card card-pad hint' }, 'Checking system health…');
    if (hlt.error) return h('div', { className: 'card card-pad' },
      h('div', { className: 'row', style: { gap: 9 } }, h('span', { className: 'dot error' }),
        h('span', { className: 'hint' }, 'Could not read system health')),
      h('button', { className: 'btn sm', style: { marginTop: 10 }, onClick: () => setTick((t) => t + 1) },
        h(Icon, { name: 'refresh', size: 14 }), 'Retry'));
    const c = hlt.components || {};
    const worker = c.worker || {}, sched = c.scheduler || {}, db = c.database || {}, bk = c.backups || {};
    const inv = hlt.inventory || {};
    const vms = inv.vms || {}, conns = inv.connections || {}, jobs = inv.jobs || {};
    const disk = hlt.disk || {};
    const usedBytes = (disk.totalBytes || 0) - (disk.freeBytes || 0);
    const usedPct = disk.totalBytes ? Math.round(usedBytes / disk.totalBytes * 100) : 0;
    const statusOrder = ['running', 'stopped', 'working', 'error', 'cleanup_pending'];
    const byStatus = vms.byStatus || {};
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 14 } },
        h('span', { className: 'panel-title' }, 'System health'),
        h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: () => setTick((t) => t + 1) },
          h(Icon, { name: 'refresh', size: 14 }), 'Refresh')),
      // two wide cards per row (one on narrow screens) — the details are long
      // mono strings (build ids, backup names) and must never truncate
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(480px, 100%), 1fr))', gap: 14 } },
        h('div', { className: 'card card-pad' },
          h('div', { className: 'panel-title', style: { marginBottom: 10 } }, 'Version'),
          h('div', { className: 'mono', style: { fontSize: 24, fontWeight: 700 } }, 'v', hlt.version),
          h('div', { className: 'copy mono', style: { fontSize: 11.5, marginTop: 3, overflowWrap: 'anywhere' } },
            hlt.build || 'local build (no CI build id)'),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, marginTop: 14 } },
            h(Stat, { k: 'Python', v: hlt.python || '—' }),
            h(Stat, { k: 'Uptime', v: fmtUptime(hlt.uptimeSeconds) }),
            h(Stat, { k: 'Started', v: hlt.startedAt ? new Date(hlt.startedAt).toLocaleString() : '—' }))),
        h('div', { className: 'card card-pad' },
          h('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Components'),
          h(HealthRow, { ok: true, label: 'API', detail: 'serving requests' }),
          h(HealthRow, {
            ok: !!worker.ok, label: 'Job worker',
            detail: worker.jobWorkerAlive
              ? ('running' + (worker.waitingWorkerAlive ? ' · waiting-poller running' : ' · waiting-poller DOWN'))
              : 'not running',
          }),
          h(HealthRow, {
            ok: !!sched.ok, label: 'Scheduler',
            detail: sched.running
              ? ((sched.jobs || []).map((j) => j.id
                  + (j.nextRun ? ' (next ' + new Date(j.nextRun).toLocaleString() + ')' : ''))
                  .join(' · ') || 'running — no tasks registered')
              : 'not running',
          }),
          h(HealthRow, {
            ok: !!db.ok, label: 'Database',
            detail: db.ok
              ? (String(db.journalMode || '').toUpperCase() + ' mode · db ' + fmtBytes(db.sizeBytes || 0)
                 + (db.walBytes ? ' · wal ' + fmtBytes(db.walBytes) : ''))
              : 'unreadable',
          }),
          // enabled-with-none-yet is healthy on a fresh instance (the first
          // scheduled run may be hours away) — the scheduler row covers firing
          h(HealthRow, {
            ok: true, label: 'Backups',
            detail: bk.enabled
              ? (bk.count
                ? (bk.count + ' kept · latest ' + ((bk.newest || {}).name || '—')
                   + ((bk.newest || {}).modified
                     ? ' (' + new Date(bk.newest.modified).toLocaleString() + ')' : ''))
                : 'enabled — first run pending')
              : 'disabled',
          })),
        h('div', { className: 'card card-pad' },
          h('div', { className: 'panel-title', style: { marginBottom: 10 } }, 'Inventory'),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 } },
            h(Stat, { k: 'VMs', v: vms.total || 0 }),
            h(Stat, { k: 'Connections', v: (conns.total || 0) + (conns.disabled ? ' (' + conns.disabled + ' off)' : '') }),
            h(Stat, { k: 'Users', v: inv.users || 0 }),
            h(Stat, { k: 'Templates', v: inv.templates || 0 }),
            h(Stat, { k: 'Base images', v: inv.baseImages || 0 }),
            h(Stat, { k: 'Jobs in queue', v: (jobs.queued || 0) + (jobs.running || 0) + (jobs.waiting || 0) })),
          h('div', { className: 'row', style: { gap: 6, marginTop: 12, flexWrap: 'wrap' } },
            statusOrder.filter((s) => byStatus[s]).map((s) => h('span', {
              key: s, className: 'badge ' + (s === 'running' ? 'running' : (s === 'error' || s === 'cleanup_pending') ? 'error' : s === 'working' ? 'working' : ''),
            }, byStatus[s], ' ', s.replace('_', ' '))))),
        h('div', { className: 'card card-pad' },
          h('div', { className: 'panel-title', style: { marginBottom: 10 } }, 'Storage'),
          h('div', { className: 'hint mono', style: { fontSize: 10.5, display: 'flex', justifyContent: 'space-between' } },
            h('span', null, 'Data volume'), h('span', null, fmtBytes(usedBytes) + ' / ' + fmtBytes(disk.totalBytes || 0))),
          h('div', { style: { height: 5, background: 'var(--border)', borderRadius: 3, overflow: 'hidden', marginTop: 3 } },
            h('div', { style: { height: '100%', width: usedPct + '%', background: usedPct > 90 ? 'var(--err)' : 'var(--accent)' } })),
          h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 14 } },
            h(Stat, { k: 'Free', v: fmtBytes(disk.freeBytes || 0) }),
            h(Stat, { k: 'Database', v: fmtBytes((db.sizeBytes || 0) + (db.walBytes || 0)) })))));
  }

  function Preferences() {
    const [on, setOn] = useState(null);
    React.useEffect(() => { window.API.autoRootPwGet().then((r) => setOn(!!r.enabled)).catch((e) => { setOn(true); toast(e.message || 'Could not load setting', 'err'); }); }, []);
    const toggle = async () => {
      const next = !on;
      try { await window.API.autoRootPwSet(next); setOn(next); toast('Saved', 'ok'); }
      catch (e) { toast(e.message || 'failed', 'err'); }
    };
    return h('div', { className: 'card card-pad' },
      h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 16 } },
        h('div', null,
          h('div', { className: 'panel-title' }, 'Auto-generate VM root password'),
          h('p', { className: 'hint', style: { fontSize: 12, marginTop: 4, maxWidth: 520 } },
            'On every deploy, set a random root password and store it encrypted so you can read it on the VM page (usable at the Proxmox console). Turn off to leave root locked.')),
        h('button', { className: 'btn ' + (on ? 'primary' : ''), onClick: toggle, disabled: on === null },
          h(Icon, { name: on ? 'check' : 'x', size: 14 }), on === null ? '…' : on ? 'On' : 'Off')));
  }

  window.BlocksLib = BlocksLib;
  window.Secrets = Secrets;
  window.Variables = Variables;
  window.Settings = Settings;
  window.ConnectionUI = { connectionDraft, connectionPayload };
})();
