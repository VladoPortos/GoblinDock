/* GoblinDock — Profile page + Templates list (CRUD). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu, ConfirmModal, FormModal, SelectField, Field, CopyField, OSGlyph, copyToClipboard } = window.UI;
  const h = React.createElement;

  /* ============ PROFILE ============ */
  function Profile({ go, theme, setTheme }) {
    const me = GD.me || {};
    const [name, setName] = useState(me.name || '');
    const [email, setEmail] = useState(me.email || '');
    const [savingId, setSavingId] = useState(false);
    const [pw, setPw] = useState({ current: '', next: '', confirm: '' });
    const [savingPw, setSavingPw] = useState(false);
    const [wkBusy, setWkBusy] = useState(false);
    const [freshKey, setFreshKey] = useState(null);
    const [showSnippet, setShowSnippet] = useState(false);
    const [wkConfirm, setWkConfirm] = useState(null);
    const myVms = (GD.VMS || []).filter((v) => v.owner === 'you').length;

    const saveIdentity = async () => {
      setSavingId(true);
      try {
        await window.API.updateProfile({ name, email });
        await window.GDStore.refresh().catch(() => {});
        window.GDStore.toast('Profile updated', 'ok');
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
      setSavingId(false);
    };
    const savePassword = async () => {
      if (pw.next !== pw.confirm) { window.GDStore.toast('New passwords do not match', 'err'); return; }
      setSavingPw(true);
      try {
        await window.API.changePassword(pw.current, pw.next);
        setPw({ current: '', next: '', confirm: '' });
        window.GDStore.toast('Password changed', 'ok');
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
      setSavingPw(false);
    };

    const Card = (title, children) => h('div', { className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 14 } },
      h('div', { className: 'panel-title' }, title), children);

    const wk = me.widgetKey || { present: false };
    const origin = window.location.origin;
    const wkSnippet = (key) => [
      '- GoblinDock:',
      '    href: ' + origin,
      '    widget:',
      '      type: customapi',
      '      url: ' + origin + '/api/widget/summary',
      '      refreshInterval: 15000',
      '      headers:',
      '        X-API-Key: ' + (key || 'YOUR_KEY_HERE'),
      '      mappings:',
      '        - { field: vms_running,   label: Running, format: number }',
      '        - { field: vms_total,     label: VMs,     format: number }',
      '        - { field: jobs_active,   label: Jobs,    format: number }',
      '        - { field: templates,     label: Templates, format: number }',
    ].join('\n');
    const genKey = async () => {
      setWkBusy(true);
      try {
        const r = await window.API.generateWidgetKey();
        setFreshKey(r.key); setShowSnippet(true);
        await window.GDStore.refresh().catch(() => {});
        window.GDStore.toast('Widget key generated', 'ok');
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
      setWkBusy(false);
    };
    const revokeKey = async () => {
      setWkBusy(true);
      try {
        await window.API.revokeWidgetKey();
        setFreshKey(null);
        await window.GDStore.refresh().catch(() => {});
        window.GDStore.toast('Widget key revoked', 'ok');
      } catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
      setWkBusy(false);
    };
    const widgetCard = h('div', { className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, marginTop: 16 } },
      h('div', { className: 'panel-title' }, 'Homepage widget'),
      h('div', { className: 'hint', style: { fontSize: 12.5, marginTop: -2 } },
        'A read-only API key for a ',
        h('a', { href: 'https://gethomepage.dev', target: '_blank', rel: 'noreferrer', style: { color: 'var(--accent)', textDecoration: 'none' } }, 'Homepage'),
        ' ', h('span', { className: 'mono' }, 'customapi'),
        ' widget — it exposes only VM, job and image counts, nothing else.'),
      freshKey && h('div', { className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 8 } },
        h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 10 } },
          h('span', { className: 'badge accent' }, h(Icon, { name: 'key', size: 12 }), 'New key'),
          h('span', { className: 'hint', style: { fontSize: 11 } }, 'Copy it now — you will not see it again')),
        h(CopyField, { value: freshKey })),
      (wk.present && !freshKey) && h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 10 } },
        h('div', null,
          h('span', { className: 'mono', style: { fontWeight: 700 } }, wk.prefix, '••••'),
          h('div', { className: 'hint mono', style: { fontSize: 11, marginTop: 4 } }, 'created ', wk.createdAt || '—', ' · last used ', wk.lastUsed || '—')),
        h('div', { className: 'row', style: { gap: 8 } },
          h('button', { className: 'btn sm', disabled: wkBusy, onClick: () => setWkConfirm('regen') }, h(Icon, { name: 'refresh', size: 13 }), 'Regenerate'),
          h('button', { className: 'btn danger sm', disabled: wkBusy, onClick: () => setWkConfirm('revoke') }, h(Icon, { name: 'trash', size: 13 }), 'Revoke'))),
      (!wk.present && !freshKey) && h('button', { className: 'btn primary sm', style: { width: 'fit-content' }, disabled: wkBusy, onClick: genKey }, h(Icon, { name: 'key', size: 14 }), wkBusy ? 'Generating…' : 'Generate key'),
      (freshKey || wk.present) && h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
        h('button', { className: 'btn sm', style: { width: 'fit-content' }, onClick: () => setShowSnippet((s) => !s) }, (showSnippet ? '▾ ' : '▸ '), 'services.yaml snippet'),
        showSnippet && h('pre', { className: 'mono', style: { whiteSpace: 'pre', overflowX: 'auto', background: 'rgba(127,127,127,0.10)', border: '1px solid var(--border, rgba(127,127,127,0.22))', padding: 12, borderRadius: 8, fontSize: 11.5, margin: 0, lineHeight: 1.5 } }, wkSnippet(freshKey)),
        showSnippet && h('button', { className: 'btn sm', style: { width: 'fit-content' }, onClick: () => copyToClipboard(wkSnippet(freshKey), 'Snippet copied') }, h(Icon, { name: 'copy', size: 13 }), 'Copy snippet')),
      wkConfirm === 'regen' && h(ConfirmModal, { onClose: () => setWkConfirm(null), tone: 'danger', icon: 'refresh', title: 'Regenerate widget key?', body: 'The current key stops working immediately. Update your Homepage config with the new key.', confirmLabel: 'Regenerate', onConfirm: genKey }),
      wkConfirm === 'revoke' && h(ConfirmModal, { onClose: () => setWkConfirm(null), tone: 'danger', icon: 'trash', title: 'Revoke widget key?', body: 'The key stops working immediately; the Homepage widget will stop updating until you generate a new one.', confirmLabel: 'Revoke', onConfirm: revokeKey }));

    return h('div', { className: 'page fadein', style: { maxWidth: 760 } },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Profile'),
          h('div', { className: 'page-sub' }, 'Your account, security and preferences.'))),

      h('div', { className: 'card card-pad', style: { display: 'flex', gap: 18, alignItems: 'center', marginBottom: 18 } },
        h('span', { className: 'avatar', style: { width: 64, height: 64, fontSize: 24, borderRadius: 18, cursor: 'default' } }, me.initials || '··'),
        h('div', { style: { flex: 1 } },
          h('div', { className: 'mono', style: { fontWeight: 800, fontSize: 20 } }, me.name),
          h('div', { className: 'hint mono', style: { fontSize: 12.5 } }, me.email)),
        h('div', { style: { textAlign: 'right' } },
          me.isAdmin
            ? h('span', { className: 'badge accent' }, h(Icon, { name: 'shield', size: 12 }), 'Admin')
            : h('span', { className: 'badge' }, 'User'),
          h('div', { className: 'hint mono', style: { fontSize: 11, marginTop: 8 } }, myVms, ' VMs · joined ', me.createdAt || '—'))),

      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 } },
        Card('Identity', h(React.Fragment, null,
          h(Field, { label: 'Display name', value: name, onChange: setName }),
          h(Field, { label: 'Email', value: email, onChange: setEmail, mono: true }),
          h('button', { className: 'btn primary sm', style: { width: 'fit-content' }, onClick: saveIdentity, disabled: savingId }, savingId ? 'Saving…' : 'Save identity'))),

        Card('Security', h(React.Fragment, null,
          h(Field, { label: 'Current password', value: pw.current, onChange: (v) => setPw((p) => ({ ...p, current: v })), type: 'password' }),
          h(Field, { label: 'New password', value: pw.next, onChange: (v) => setPw((p) => ({ ...p, next: v })), type: 'password', hint: 'At least 10 chars, 3 character classes.' }),
          h(Field, { label: 'Confirm new password', value: pw.confirm, onChange: (v) => setPw((p) => ({ ...p, confirm: v })), type: 'password' }),
          h('button', { className: 'btn primary sm', style: { width: 'fit-content' }, onClick: savePassword, disabled: savingPw }, savingPw ? 'Saving…' : 'Change password')))),

      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 } },
        Card('Preferences', h('div', { className: 'row', style: { justifyContent: 'space-between' } },
          h('span', { style: { fontSize: 13, color: 'var(--text-dim)' } }, 'Theme'),
          h('div', { className: 'seg' },
            h('button', { className: theme === 'dark' ? 'active' : '', onClick: () => setTheme('dark') }, h(Icon, { name: 'moon', size: 14 }), 'Dark'),
            h('button', { className: theme === 'light' ? 'active' : '', onClick: () => setTheme('light') }, h(Icon, { name: 'sun', size: 14 }), 'Light')))),
        Card('Account', h(React.Fragment, null,
          h('div', { className: 'row', style: { justifyContent: 'space-between' } }, h('span', { className: 'hint' }, 'Last login'), h('span', { className: 'mono', style: { fontSize: 12 } }, me.lastLogin || '—')),
          h('div', { className: 'row', style: { justifyContent: 'space-between' } }, h('span', { className: 'hint' }, 'Role'), h('span', { className: 'mono', style: { fontSize: 12 } }, me.role)),
          h('div', { className: 'divider' }),
          h('button', { className: 'btn danger sm', style: { width: 'fit-content' }, onClick: () => window.GDStore.signOut(go) }, h(Icon, { name: 'logout', size: 14 }), 'Sign out')))),
      widgetCard);
  }

  /* ============ TEMPLATES LIST ============ */
  function parseTemplateBundle(text) {
    if (text.length > 2000000) throw new Error('Template bundle exceeds 2 MB.');
    let bundle;
    try { bundle = JSON.parse(text); } catch (_) { throw new Error('Choose a valid JSON template bundle.'); }
    if (!bundle || bundle.format !== 'goblindock-template' || bundle.version !== 1 || !bundle.template)
      throw new Error('Unsupported template bundle format or version.');
    return bundle;
  }

  function templateImportPayload(bundle, baseImageId, connectionId, networkId, name) {
    const ids = [baseImageId, connectionId, networkId].map(Number);
    if (ids.some((id) => !Number.isSafeInteger(id) || id <= 0))
      throw new Error('Choose a local base image, connection and network.');
    return { bundle, baseImageId: ids[0], connectionId: ids[1], networkId: ids[2], name };
  }

  async function downloadTemplate(t) {
    try {
      const bundle = await window.API.exportTemplate(t.templateId);
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url; link.download = (t.name || 'template').replace(/[^a-zA-Z0-9_-]/g, '-') + '.goblindock.json';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { window.GDStore.toast(e.message || 'Export failed', 'err'); }
  }

  function ImportTemplateModal({ onClose }) {
    const [bundle, setBundle] = useState(null);
    const [name, setName] = useState('');
    const [baseId, setBaseId] = useState('');
    const [connId, setConnId] = useState('');
    const [netId, setNetId] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const busyRef = React.useRef(false);
    const chooseFile = async (event) => {
      const file = event.target.files?.[0]; setError(''); setBundle(null);
      if (!file) return;
      try {
        if (file.size > 2000000) throw new Error('Template bundle exceeds 2 MB.');
        const parsed = parseTemplateBundle(await file.text());
        setBundle(parsed); setName(parsed.template.name || 'Imported template');
      } catch (e) { setError(e.message); }
    };
    const submit = async () => {
      if (busyRef.current) return;
      setError('');
      try {
        if (!bundle) throw new Error('Choose a template bundle first.');
        const payload = templateImportPayload(bundle, baseId, connId, netId, name);
        busyRef.current = true; setBusy(true);
        await window.API.importTemplate(payload);
        await window.GDStore.refresh();
        window.GDStore.toast('Template imported', 'ok'); onClose();
      } catch (e) { setError(e.message || 'Import failed'); }
      finally { busyRef.current = false; setBusy(false); }
    };
    const options = (rows, id) => [{ value: '', label: 'Choose…' }, ...rows.map((r) => ({ value: r[id], label: r.name }))];
    return h(FormModal, { title: 'Import template', icon: 'upload', onClose: busy ? () => {} : onClose,
      onSubmit: submit, submitLabel: 'Import template', busy },
      h('label', { className: 'field-label' }, 'Template bundle JSON', h('input', {
        className: 'input', type: 'file', accept: '.json,application/json', disabled: busy, onChange: chooseFile })),
      bundle && h('p', { className: 'hint' }, (bundle.customBlocks || []).length + ' custom blocks. Source image: ' +
        (bundle.baseImage?.name || 'unspecified') + '. Select its matching local image below.'),
      h(Field, { label: 'Template name', value: name, onChange: setName }),
      h(SelectField, { label: 'Local base image', value: baseId, onChange: setBaseId, options: options(GD.BASE_IMAGES || [], 'imgId') }),
      h(SelectField, { label: 'Connection', value: connId, onChange: (v) => { setConnId(v); setNetId(''); },
        options: options((GD.CONNECTIONS || []).filter((c) => !c.disabled), 'connId') }),
      h(SelectField, { label: 'Network', value: netId, onChange: setNetId,
        options: options((GD.NETWORKS || []).filter((n) => n.connId === Number(connId)), 'netId') }),
      bundle?.secretReferences?.length > 0 && h('p', { className: 'hint' }, 'Required secret references: ' + bundle.secretReferences.join(', ')),
      h('p', { className: 'hint' }, 'The imported template is private. Review its scripts before deploying.'),
      error && h('p', { role: 'alert', style: { color: 'var(--err)' } }, error));
  }

  function templateActionFlags(template) {
    const canEdit = template?.canEdit === true;
    return {
      canEdit,
      canDelete: canEdit && template?.canDelete === true,
    };
  }

  function TemplateCard({ r, go, onDelete, onDeploy }) {
    const deployable = !!r.deployable;
    const actions = templateActionFlags(r);
    return h('div', { className: 'card', style: { overflow: 'hidden', display: 'flex', flexDirection: 'column' } },
      h('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, flex: 1 } },
        h('div', { className: 'row', style: { gap: 10 } },
          h('span', { className: 'placed-ico', style: { width: 32, height: 32 } }, h(Icon, { name: 'template', size: 16 })),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, r.name),
            h('div', { className: 'hint', style: { fontSize: 11 } }, (r.blocks || []).length, ' block', (r.blocks || []).length === 1 ? '' : 's',
              ' · ', r.cpu, ' vCPU · ', r.mem, ' GB · ', r.disk, ' GB')),
          h('div', { style: { marginLeft: 'auto' } },
            r.public ? h('span', { className: 'badge accent' }, h(Icon, { name: 'globe', size: 12 }), 'Public')
              : h('span', { className: 'badge' }, h(Icon, { name: 'user', size: 12 }), 'Private'))),
        r.base
          ? h('div', { className: 'row mono', style: { gap: 8, fontSize: 11.5 } },
              h(OSGlyph, { os: r.os, size: 15 }),
              h('span', { style: { fontWeight: 600 } }, r.base),
              h('span', { className: 'hint' }, r.location || ''),
              !deployable && h('span', { className: 'badge', style: { background: 'var(--warn-ghost)', color: 'var(--warn)', border: 'none' } }, 'pick a location — edit'))
          : h('div', { className: 'row', style: { gap: 6 } },
              h('span', { className: 'badge', style: { background: 'var(--warn-ghost)', color: 'var(--warn)', border: 'none' } },
                h(Icon, { name: 'warn', size: 11 }), 'no base image — edit to enable deploy')),
        h('p', { className: 'hint', style: { fontSize: 12.5, lineHeight: 1.5, minHeight: 20 } }, r.desc || 'A reusable deployment preset.'),
        (r.blocks || []).length > 0 && h('div', { className: 'row wrap', style: { gap: 5 } },
          r.blocks.slice(0, 6).map((b, i) => h('span', { key: i, className: 'chip', style: { fontSize: 10.5, padding: '3px 7px' } }, b))),
        h('div', { className: 'row mono', style: { gap: 10, fontSize: 11, color: 'var(--text-faint)', marginTop: 'auto', paddingTop: 4 } },
          h('span', null, r.used, ' deploys'))),
      h('div', { style: { display: 'flex', borderTop: '1px solid var(--border-soft)' } },
        h('button', { className: 'card-act', disabled: !deployable, title: deployable ? 'One-click deploy from this template' : 'Pick a base image + location first (Edit)', onClick: () => onDeploy(r) }, h(Icon, { name: 'play', size: 14 }), 'Deploy'),
        actions.canEdit && h('button', { className: 'card-act', onClick: () => go('newtemplate', { templateId: r.templateId }) }, h(Icon, { name: 'edit', size: 14 }), 'Edit'),
        actions.canEdit && h('button', { className: 'card-act', onClick: () => downloadTemplate(r) }, h(Icon, { name: 'download', size: 14 }), 'Export'),
        actions.canDelete && h(Menu, { align: 'right', items: [
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => onDelete(r) },
        ] }, h('button', { className: 'card-act', style: { flex: '0 0 44px' }, 'aria-label': 'Template actions' }, h(Icon, { name: 'more', size: 16 })))));
  }

  function TemplatesList({ go }) {
    const [confirm, setConfirm] = useState(null);
    const [deploying, setDeploying] = useState(null);
    const [importing, setImporting] = useState(false);
    const templates = GD.TEMPLATES || [];
    // toast + rethrow: ConfirmModal keeps itself open for retry when the handler throws
    const del = async (t) => {
      try { await window.API.deleteTemplate(t.templateId); window.GDStore.toast('Template deleted', 'ok'); window.GDStore.refresh().catch(() => {}); }
      catch (e) { window.GDStore.toast(e.message || 'delete failed', 'err'); throw e; }
    };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Templates'),
          h('div', { className: 'page-sub' }, 'Deployment presets — a base image + blocks + defaults. Deploy in one click.')),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn', onClick: () => setImporting(true) }, h(Icon, { name: 'upload', size: 16 }), 'Import'),
        h('button', { className: 'btn primary', onClick: () => go('newtemplate') }, h(Icon, { name: 'plus', size: 16 }), 'New template')),
      templates.length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty' },
            h('div', { className: 'glyph' }, h(Icon, { name: 'template', size: 28 })),
            h('h3', null, 'No templates yet'),
            h('p', null, 'Save a base image + blocks + sizing under a name — then deploy it again and again.'),
            h('button', { className: 'btn primary', onClick: () => go('newtemplate') }, h(Icon, { name: 'plus', size: 16 }), 'New template')))
        : h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 } },
            templates.map((t) => h(TemplateCard, { key: t.id, r: t, go, onDelete: (x) => setConfirm(x), onDeploy: (x) => setDeploying(x) }))),
      deploying && h(window.DeployModal, { tpl: deploying, go, onClose: () => setDeploying(null) }),
      importing && h(ImportTemplateModal, { onClose: () => setImporting(false) }),
      confirm && h(ConfirmModal, {
        onClose: () => setConfirm(null), tone: 'danger', icon: 'trash',
        title: 'Delete ' + confirm.name + '?',
        body: 'Removes this template. Deployed VMs are not affected.',
        confirmLabel: 'Delete template', onConfirm: () => del(confirm),
      }));
  }

  window.Profile = Profile;
  window.TemplatesList = TemplatesList;
  window.TemplateUI = { templateActionFlags, parseTemplateBundle, templateImportPayload };
})();
