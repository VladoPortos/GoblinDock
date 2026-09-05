/* GoblinDock — ISOs / base images (Manage). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph, Menu, ConfirmModal, FormModal, Field, SelectField } = window.UI;
  const h = React.createElement;
  const refresh = () => window.GDStore.refresh().catch(() => {});
  const toast = (m, t) => window.GDStore.toast(m, t);

  const CHECKSUM_ALGORITHMS = {
    32: 'MD5', 40: 'SHA-1', 64: 'SHA-256', 96: 'SHA-384', 128: 'SHA-512',
  };

  function checksumMeta(value) {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized) {
      return { normalized: '', valid: true, algorithm: 'Optional', message: 'Optional' };
    }
    const algorithm = CHECKSUM_ALGORITHMS[normalized.length];
    if (algorithm && /^[0-9a-f]+$/.test(normalized)) {
      return { normalized, valid: true, algorithm, message: algorithm + ' checksum' };
    }
    return {
      normalized,
      valid: false,
      algorithm: '',
      message: 'Enter a bare hexadecimal checksum with 32, 40, 64, 96, or 128 characters.',
    };
  }

  /* ============ ISOs / BASE IMAGES (Manage) ============ */
  function IsoCard({ img, go, onEdit, onDelete, onPin, isAdmin, cacheState, syncing, canSync, onSync, metadata = {} }) {
    return h('div', { className: 'card', style: { overflow: 'hidden', display: 'flex', flexDirection: 'column' } },
      h('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, flex: 1 } },
        h('div', { className: 'row', style: { gap: 10 } },
          h(OSGlyph, { os: img.os, size: 32 }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, img.name),
            h('div', { className: 'hint mono', style: { fontSize: 11 } }, img.size)),
          h('div', { style: { marginLeft: 'auto', textAlign: 'right' } },
            h('span', { className: 'badge running' }, h('span', { className: 'dot running' }), img.pinned ? 'Pinned' : 'Ready'),
            h('div', { className: 'hint mono', style: { fontSize: 10.5, marginTop: 5 } },
              cacheState === 'cached' ? h('span', { style: { color: 'var(--ok)' } }, '✓ cached on node')
                : cacheState === 'missing' ? '○ not downloaded'
                : '— cache unknown'))),
        h('div', null,
          h('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Cloud image URL'),
          h('div', { className: 'copy mono', style: { fontSize: 10.5, wordBreak: 'break-all' } }, img.source_url || img.checksum || 'Not provided')),
        h('div', { className: 'hint', style: { fontSize: 11 } },
          metadata.downloadedAt ? 'Downloaded ' + new Date(metadata.downloadedAt).toLocaleString() : 'Download date unknown',
          metadata.validatedAt ? ' · Checksum verified ' + new Date(metadata.validatedAt).toLocaleString() : ' · No recorded checksum validation'),
        metadata.sourceIdentity && h('div', { className: 'hint mono', style: { fontSize: 10 }, title: metadata.sourceIdentity },
          'Source identity ' + metadata.sourceIdentity.slice(0, 16))),
      h('div', { style: { display: 'flex', borderTop: '1px solid var(--border-soft)' } },
        h('button', { className: 'card-act', onClick: () => go('newtemplate', { baseImageId: img.imgId }) }, h(Icon, { name: 'template', size: 14 }), 'New template'),
        isAdmin && h('button', { className: 'card-act', disabled: !canSync,
          title: syncing ? 'Sync in progress…'
            : cacheState === 'cached' ? 'Download and validate a new copy; retain the previous cache'
            : !canSync && cacheState !== 'missing' ? 'Cache state unknown (target offline or no URL)'
            : 'Download to the target node now',
          onClick: onSync },
          h(Icon, { name: 'download', size: 14 }), syncing ? 'Syncing…' : cacheState === 'cached' ? 'Refresh cache' : 'Sync'),
        isAdmin && h(Menu, { align: 'right', items: [
          { label: 'Edit', icon: 'edit', onClick: () => onEdit(img) },
          { label: 'Pin version', icon: 'lock', onClick: () => onPin(img) },
          { sep: true },
          { label: 'Delete', icon: 'trash', danger: true,
            disabled: img.canDelete !== true,
            title: img.canDelete === true ? 'Delete base image' : 'This image is referenced by a template or deployed VM',
            onClick: () => onDelete(img) },
        ] }, h('button', { className: 'card-act', style: { flex: '0 0 44px' } }, h(Icon, { name: 'more', size: 16 })))));
  }

  function IsoModal({ img, pinning = false, onClose, onDone }) {
    const editing = !!img;
    const [f, setF] = useState({
      name: img ? img.name : '',
      os_family: img ? img.os : 'ubuntu',
      source_url: img ? (img.source_url || '') : '',
      checksum: img ? (img.checksum || '') : '',
    });
    const [busy, setBusy] = useState(false);
    const [immutable, setImmutable] = useState(false);
    const checksumId = React.useId();
    const checksumFeedbackId = checksumId + '-feedback';
    const checksum = checksumMeta(f.checksum);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const submit = async () => {
      if (!f.name.trim() || !f.source_url.trim()) { toast('Name and URL required', 'err'); return; }
      if (!checksum.valid) { toast(checksum.message, 'err'); return; }
      if (pinning && (!immutable || !checksum.normalized)) {
        toast('Pinning requires a version-specific URL, checksum, and immutable URL confirmation', 'err'); return;
      }
      setBusy(true);
      try {
        const payload = { ...f, checksum: checksum.normalized, ...(pinning ? { pin: true, immutable } : {}) };
        if (editing) await window.API.editImage(img.imgId, payload);
        else await window.API.addBaseImage(payload);
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: pinning ? 'Pin image version' : editing ? 'Edit base image' : 'Add base image (ISO)', icon: 'disk', onClose, onSubmit: submit, busy },
      h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v), placeholder: 'Ubuntu 24.04 LTS' }),
      h(SelectField, { label: 'OS family', value: f.os_family, onChange: (v) => set('os_family', v), options: ['ubuntu', 'debian', 'alpine', 'rocky', 'generic'] }),
      h(Field, { label: 'Cloud image URL (.img/.qcow2)', value: f.source_url, onChange: (v) => set('source_url', v), mono: true, placeholder: 'https://…/noble-server-cloudimg-amd64.img' }),
      h('div', null,
        h('label', { className: 'field-label', htmlFor: checksumId }, pinning ? 'Checksum (required)' : 'Checksum (optional)'),
        h('input', {
          id: checksumId,
          className: 'input mono',
          value: f.checksum,
          placeholder: '64-character SHA-256 digest',
          spellCheck: false,
          autoCapitalize: 'none',
          'aria-invalid': !checksum.valid,
          'aria-describedby': checksumFeedbackId,
          onChange: (e) => set('checksum', e.target.value),
        }),
        h('div', {
          id: checksumFeedbackId,
          className: 'hint',
          'aria-live': 'polite',
          style: { fontSize: 11, marginTop: 4, color: checksum.valid ? null : 'var(--err)' },
        }, checksum.message)),
      pinning && h('label', { className: 'hint', style: { display: 'flex', gap: 8 } },
        h('input', { type: 'checkbox', checked: immutable, onChange: e => setImmutable(e.target.checked) }),
        'This URL identifies one immutable release, not a latest/current alias. Future downloads must match this checksum.'),
      pinning && h('div', { className: 'hint' }, 'Existing VMs retain their disks. Deploys and refreshes use this pinned source after saving.'));
  }

  function Isos({ go }) {
    const isAdmin = GD.me && GD.me.isAdmin;
    const [modal, setModal] = useState(null);   // 'add' | {img}
    const [confirm, setConfirm] = useState(null);
    const bases = GD.BASE_IMAGES || [];
    // toast + rethrow: ConfirmModal keeps itself open for retry when the handler throws
    const del = async (img) => {
      try { await window.API.deleteImage(img.imgId); toast('Base image removed', 'ok'); refresh(); }
      catch (e) { toast(e.message || 'delete failed', 'err'); throw e; }
    };

    const conns = (GD.CONNECTIONS || []).filter(c => !c.disabled);
    const [targetId, setTargetId] = useState((conns[0] && conns[0].connId) || null);
    const [bump, setBump] = useState(0);                                // forces a cache refetch
    const syncCountRef = React.useRef(0);
    React.useEffect(() => {
      if (!conns.some(c => c.connId === targetId)) setTargetId((conns[0] && conns[0].connId) || null);
    }, [targetId, conns.map(c => c.connId).join(',')]);
    React.useEffect(() => {
      const timer = setInterval(() => setBump(b => b + 1), 6000);
      return () => clearInterval(timer);
    }, []);

    // online null = unknown/loading
    const cache = window.UI.useFetched(
      () => (targetId ? window.API.cachedImages(targetId) : { online: null, cached: {} }),
      [targetId, bump], { online: false, cached: {} }) || { online: null, cached: {} };

    // when a running sync job finishes (count drops), the cache may have changed
    const workingSyncs = (GD.JOBS || []).filter((j) => j.imageId != null && j.status === 'working').length;
    React.useEffect(() => {
      if (workingSyncs < syncCountRef.current) setBump((b) => b + 1);
      syncCountRef.current = workingSyncs;
    }, [workingSyncs]);

    const doSync = async (img, forceRefresh = false) => {
      try {
        await window.API.syncImage(img.imgId, { connectionId: targetId, force_refresh: forceRefresh });
        toast('Sync started — watch the activity bell', 'ok');
        window.GDStore.refresh().catch(() => {});
      } catch (e) { toast(e.message || 'sync failed', 'err'); if (forceRefresh) throw e; }
    };

    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'ISOs / Base Images'),
          h('div', { className: 'page-sub' }, 'Public cloud images — the raw material templates deploy from.')),
        h('div', { className: 'spacer' }),
        conns.length > 0 && h('div', { className: 'row', style: { gap: 8 } },
          h('span', { className: 'hint', style: { fontSize: 12 } }, 'Target:'),
          h('select', { className: 'select', style: { width: 'auto', minWidth: 140 }, value: targetId || '',
            onChange: (e) => setTargetId(Number(e.target.value) || null) },
            conns.map((c) => h('option', { key: c.connId, value: c.connId }, c.name))),
          cache.online === false && h('span', { className: 'badge', style: { background: 'var(--warn-ghost)', color: 'var(--warn)', border: 'none' } }, cache.inventory && cache.inventory.completedAt ? 'target unavailable' : 'waiting for inventory')),
        isAdmin && h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'download', size: 16 }), 'Add base image')),
      cache.inventory && h('div', { className: 'hint', role: 'status', style: { marginBottom: 14 } },
        cache.inventory.error || (cache.inventory.stale ? 'Inventory is stale' : 'Inventory is current'),
        cache.inventory.updatedAt ? ' · Last successful check ' + new Date(cache.inventory.updatedAt).toLocaleString() : ' · First check pending'),
      bases.length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty', style: { padding: '44px 20px' } },
            h('div', { className: 'glyph' }, h(Icon, { name: 'disk', size: 26 })),
            h('h3', null, 'No base images'),
            isAdmin && h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'download', size: 16 }), 'Add base image')))
        : h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 } },
            bases.map((img) => {
              const known = Object.prototype.hasOwnProperty.call(cache.cached, String(img.imgId));
              const cacheState = cache.online && known
                ? (cache.cached[String(img.imgId)] ? 'cached' : 'missing') : 'unknown';
              const syncing = (GD.JOBS || []).some((j) => j.imageId === img.imgId && j.status === 'working');
              return h(IsoCard, { key: img.id, img, go, isAdmin, cacheState, syncing,
                metadata: (cache.metadata || {})[String(img.imgId)] || {},
                canSync: !!targetId && !!img.source_url && !syncing,
                onSync: () => cacheState === 'cached' ? setConfirm({ refresh: true, img }) : doSync(img),
                onPin: (x) => setModal({ img: x, pinning: true }),
                onEdit: (x) => setModal({ img: x }), onDelete: (x) => setConfirm(x) });
            })),
      modal === 'add' && h(IsoModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Base image added', 'ok'); refresh(); } }),
      modal && modal.img && h(IsoModal, { img: modal.img, pinning: !!modal.pinning, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Base image updated', 'ok'); refresh(); } }),
      confirm && confirm.refresh && h(ConfirmModal, { onClose: () => setConfirm(null), icon: 'download', title: 'Refresh ' + confirm.img.name + '?',
        body: 'Download a new copy and validate its checksum when configured. The previous cached file remains available and existing VMs keep their disks. Extra storage is required.',
        confirmLabel: 'Refresh cache', onConfirm: () => doSync(confirm.img, true) }),
      confirm && !confirm.refresh && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Remove ' + confirm.name + '?',
        body: 'Removes the base image entry. No template or deployed VM references it. Downloaded files on the node are not deleted.',
        confirmLabel: 'Remove', onConfirm: () => del(confirm) }));
  }

  window.ImageUI = { checksumMeta, IsoModal, IsoCard };
  window.Isos = Isos;
})();
