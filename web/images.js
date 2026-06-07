/* GoblinDock — Golden Images (Build) + ISOs / base images (Manage). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph, Menu, ConfirmModal, FormModal, Field, SelectField } = window.UI;
  const h = React.createElement;
  const refresh = () => window.GDStore.refresh().catch(() => {});
  const toast = (m, t) => window.GDStore.toast(m, t);

  /* ============ GOLDEN IMAGES (Build) ============ */
  function GoldenCard({ img, go, onRebuild, onDelete }) {
    return h('div', { className: 'card', style: { overflow: 'hidden', display: 'flex', flexDirection: 'column' } },
      h('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, flex: 1 } },
        h('div', { className: 'row', style: { gap: 10 } },
          h(OSGlyph, { os: img.os, size: 32 }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, img.name),
            h('div', { className: 'hint', style: { fontSize: 11.5 } }, 'from ', img.base)),
          h('div', { style: { marginLeft: 'auto' } },
            img.state === 'building'
              ? h('span', { className: 'badge working' }, h('span', { className: 'dot working' }), img.progress, '%')
              : img.state === 'failed'
                ? h('span', { className: 'badge error' }, h('span', { className: 'dot error' }), 'Failed')
                : h('span', { className: 'badge running' }, h('span', { className: 'dot running' }), 'Built ', img.built))),
        img.state === 'building' && h('div', { className: 'meter', style: { height: 6 } }, h('i', { style: { width: (img.progress || 5) + '%', background: 'var(--warn)' } })),
        h('div', { className: 'row', style: { gap: 7 } },
          h(Icon, { name: 'server', size: 13, style: { color: 'var(--text-faint)' } }),
          h('span', { className: 'hint mono', style: { fontSize: 11 } }, img.location || '—')),
        h('div', null,
          h('div', { className: 'panel-title', style: { marginBottom: 8 } }, 'Baked blocks'),
          h('div', { className: 'row wrap', style: { gap: 5 } },
            (img.blocks.length ? img.blocks : ['clean cloud-init base']).map((b, i) => h('span', { key: i, className: 'chip', style: { fontSize: 10.5, padding: '3px 7px' } }, b)))),
        h('div', { className: 'row', style: { marginTop: 'auto', paddingTop: 4 } },
          h('span', { className: 'copy', style: { fontSize: 11.5 } },
            h(Icon, { name: 'history', size: 13 }), img.vmid ? ('template vmid ' + img.vmid) : 'not built yet'))),
      h('div', { style: { display: 'flex', borderTop: '1px solid var(--border-soft)' } },
        h('button', { className: 'card-act', disabled: !img.deployable, onClick: () => go('deploy', { goldenImageId: img.imgId }) }, h(Icon, { name: 'play', size: 14 }), 'Deploy'),
        h('button', { className: 'card-act', onClick: () => go('builder', { imageId: img.imgId }) }, h(Icon, { name: 'edit', size: 14 }), 'Edit'),
        h('button', { className: 'card-act', onClick: () => onRebuild(img), disabled: img.state === 'building' }, h(Icon, { name: 'rebuild', size: 14 }), 'Rebuild'),
        h(Menu, { align: 'right', items: [
          { label: 'New template from this image', icon: 'template', onClick: () => go('newtemplate', { goldenImageId: img.imgId }) },
          { label: 'Delete image', icon: 'trash', danger: true, onClick: () => onDelete(img) },
        ] }, h('button', { className: 'card-act', style: { flex: '0 0 44px' } }, h(Icon, { name: 'more', size: 16 })))));
  }

  // Read-only "what's stale" report + opt-in bulk delete. Deletion reuses the existing
  // DELETE /api/images/{id} (409-on-deployments / RBAC / shared-vmid guards intact).
  function StaleCleanupModal({ onClose, onDone }) {
    const [data, setData] = useState(null);
    const [sel, setSel] = useState(() => new Set());
    const [busy, setBusy] = useState(false);
    React.useEffect(() => {
      window.API.staleImages().then((d) => {
        setData(d);
        // pre-select the clearly-dead ones (failed / never-built), not merely-unused templates
        setSel(new Set((d.candidates || [])
          .filter((c) => c.canDelete && c.reason !== 'no deployments use it')
          .map((c) => c.imgId)));
      }).catch(() => setData({ candidates: [] }));
    }, []);
    const cands = (data && data.candidates) || [];
    const toggle = (id) => setSel((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
    const selCount = cands.filter((c) => sel.has(c.imgId)).length;
    const del = async () => {
      const ids = cands.filter((c) => sel.has(c.imgId)).map((c) => c.imgId);
      if (!ids.length) { onClose(); return; }
      setBusy(true);
      const res = await Promise.allSettled(ids.map((id) => window.API.deleteImage(id)));
      const ok = res.filter((r) => r.status === 'fulfilled').length;
      const fail = res.length - ok;
      toast('Deleted ' + ok + ' image' + (ok === 1 ? '' : 's') + (fail ? (' · ' + fail + ' failed') : ''), fail ? 'warn' : 'ok');
      setBusy(false);
      onDone();
    };
    return h(FormModal, {
      title: 'Stale image cleanup', icon: 'trash', danger: true, onClose, onSubmit: del,
      busy, submitLabel: selCount ? ('Delete ' + selCount) : 'Delete', width: 'min(620px, 95vw)',
    },
      data === null
        ? h('div', { className: 'hint', style: { padding: 12 } }, 'Scanning golden images…')
        : cands.length === 0
          ? h('div', { className: 'hint', style: { padding: 12 } }, 'Nothing stale — every golden image is built and in use.')
          : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
              h('div', { className: 'hint', style: { fontSize: 11.5 } }, 'Selected images are destroyed (Proxmox template + DB row). Any image with deployed VMs is refused server-side.'),
              cands.map((c) => h('label', { key: c.imgId, className: 'row', style: { gap: 10, padding: '8px 10px', borderRadius: 9, border: '1px solid var(--border-soft)', cursor: c.canDelete ? 'pointer' : 'not-allowed', opacity: c.canDelete ? 1 : 0.55 } },
                h('input', { type: 'checkbox', checked: sel.has(c.imgId), disabled: !c.canDelete, onChange: () => toggle(c.imgId), style: { width: 15, height: 15, accentColor: 'var(--accent)' } }),
                h('div', { style: { minWidth: 0, flex: 1 } },
                  h('div', { className: 'mono', style: { fontWeight: 600, fontSize: 13 } }, c.name),
                  h('div', { className: 'hint', style: { fontSize: 11 } }, 'vmid ' + (c.vmid || '—') + ' · built ' + c.built + (c.owner && c.owner !== '—' ? (' · ' + c.owner) : ''))),
                h('span', { className: 'badge ' + (c.reason === 'no deployments use it' ? 'info' : 'error') }, c.reason)))));
  }

  function GoldenImages({ go }) {
    const [confirm, setConfirm] = useState(null);
    const [rebuildC, setRebuildC] = useState(null);
    const [cleanup, setCleanup] = useState(false);
    const golden = GD.GOLDEN_IMAGES || [];
    const doRebuild = async (img) => {
      try { const r = await window.API.rebuildGolden(img.imgId); go('job', { jobId: r.jobId }); }
      catch (e) { toast(e.message || 'failed', 'err'); }
    };
    const del = async (img) => {
      await window.API.deleteImage(img.imgId); toast('Golden image deleted', 'ok'); refresh();
    };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Golden Images'),
          h('div', { className: 'page-sub' }, 'A base image + baked customization = a deployable Proxmox template.')),
        h('div', { className: 'spacer' }),
        golden.length > 0 && h('button', { className: 'btn', onClick: () => setCleanup(true), title: 'Find and remove stale / failed / unused golden images' }, h(Icon, { name: 'trash', size: 15 }), 'Clean up'),
        h('button', { className: 'btn primary', onClick: () => go('builder') }, h(Icon, { name: 'hammer', size: 16 }), 'New golden image')),
      golden.length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty', style: { padding: '44px 20px' } },
            h('div', { className: 'glyph' }, h(Icon, { name: 'package', size: 28 })),
            h('h3', null, 'No golden images yet'),
            h('p', null, 'Bake one from a base ISO — pick a base, a location, and customise it with blocks.'),
            h('button', { className: 'btn primary', onClick: () => go('builder') }, h(Icon, { name: 'hammer', size: 16 }), 'New golden image')))
        : h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 } },
            golden.map((img) => h(GoldenCard, { key: img.id, img, go, onRebuild: (x) => setRebuildC(x), onDelete: (x) => setConfirm(x) }))),
      confirm && (function () {
        const refs = ((window.GD.TEMPLATES) || []).filter((t) => t.goldenImageId === confirm.imgId).length;
        return h(ConfirmModal, {
          onClose: () => setConfirm(null), tone: 'danger', icon: 'trash',
          title: 'Delete ' + confirm.name + '?',
          body: 'Destroys the Proxmox template (vmid ' + (confirm.vmid || '—') + ') and removes it. Already-deployed VMs are unaffected. Cannot be undone.'
            + (refs ? ' ' + refs + ' template' + (refs === 1 ? ' references' : 's reference') + ' this image — ' + (refs === 1 ? 'it keeps' : 'they keep') + ' working but ' + (refs === 1 ? 'loses' : 'lose') + ' one-click deploy until re-pointed.' : ''),
          confirmLabel: 'Delete image', onConfirm: () => del(confirm),
        });
      })(),
      rebuildC && h(ConfirmModal, {
        onClose: () => setRebuildC(null), tone: 'danger', icon: 'rebuild',
        title: 'Rebuild ' + rebuildC.name + '?',
        body: 'This deletes the current template (vmid ' + (rebuildC.vmid || '—') + ') and builds a fresh one in its place. Already-deployed VMs keep running and are unaffected.',
        confirmLabel: 'Delete & rebuild', onConfirm: () => doRebuild(rebuildC),
      }),
      cleanup && h(StaleCleanupModal, { onClose: () => setCleanup(false), onDone: () => { setCleanup(false); refresh(); } }));
  }

  /* ============ ISOs / BASE IMAGES (Manage) ============ */
  function IsoCard({ img, go, onEdit, onDelete, isAdmin }) {
    return h('div', { className: 'card', style: { overflow: 'hidden', display: 'flex', flexDirection: 'column' } },
      h('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, flex: 1 } },
        h('div', { className: 'row', style: { gap: 10 } },
          h(OSGlyph, { os: img.os, size: 32 }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, img.name),
            h('div', { className: 'hint mono', style: { fontSize: 11 } }, img.size)),
          h('div', { style: { marginLeft: 'auto' } },
            h('span', { className: 'badge running' }, h('span', { className: 'dot running' }), 'Ready'))),
        h('div', null,
          h('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Cloud image URL'),
          h('div', { className: 'copy mono', style: { fontSize: 10.5, wordBreak: 'break-all' } }, img.source_url || img.checksum))),
      h('div', { style: { display: 'flex', borderTop: '1px solid var(--border-soft)' } },
        h('button', { className: 'card-act', onClick: () => go('builder', { baseImageId: img.imgId }) }, h(Icon, { name: 'hammer', size: 14 }), 'Bake golden'),
        isAdmin && h(Menu, { align: 'right', items: [
          { label: 'Edit', icon: 'edit', onClick: () => onEdit(img) },
          { sep: true },
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => onDelete(img) },
        ] }, h('button', { className: 'card-act', style: { flex: '0 0 44px' } }, h(Icon, { name: 'more', size: 16 })))));
  }

  function IsoModal({ img, onClose, onDone }) {
    const editing = !!img;
    const [f, setF] = useState({ name: img ? img.name : '', os_family: img ? img.os : 'ubuntu', source_url: img ? (img.source_url || '') : '' });
    const [busy, setBusy] = useState(false);
    const set = (k, v) => setF((p) => ({ ...p, [k]: v }));
    const submit = async () => {
      if (!f.name.trim() || !f.source_url.trim()) { toast('Name and URL required', 'err'); return; }
      setBusy(true);
      try {
        if (editing) await window.API.editImage(img.imgId, f);
        else await window.API.addBaseImage(f);
        onDone();
      } catch (e) { toast(e.message, 'err'); setBusy(false); }
    };
    return h(FormModal, { title: editing ? 'Edit base image' : 'Add base image (ISO)', icon: 'disk', onClose, onSubmit: submit, busy },
      h(Field, { label: 'Name', value: f.name, onChange: (v) => set('name', v), placeholder: 'Ubuntu 24.04 LTS' }),
      h(SelectField, { label: 'OS family', value: f.os_family, onChange: (v) => set('os_family', v), options: ['ubuntu', 'debian', 'alpine', 'rocky', 'generic'] }),
      h(Field, { label: 'Cloud image URL (.img/.qcow2)', value: f.source_url, onChange: (v) => set('source_url', v), mono: true, placeholder: 'https://…/noble-server-cloudimg-amd64.img' }));
  }

  function Isos({ go }) {
    const isAdmin = GD.me && GD.me.isAdmin;
    const [modal, setModal] = useState(null);   // 'add' | {img}
    const [confirm, setConfirm] = useState(null);
    const bases = GD.BASE_IMAGES || [];
    const del = async (img) => { await window.API.deleteImage(img.imgId); toast('Base image removed', 'ok'); refresh(); };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'ISOs / Base Images'),
          h('div', { className: 'page-sub' }, 'Public cloud images — the raw material you bake Golden Images from.')),
        h('div', { className: 'spacer' }),
        isAdmin && h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'download', size: 16 }), 'Add base image')),
      bases.length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty', style: { padding: '44px 20px' } },
            h('div', { className: 'glyph' }, h(Icon, { name: 'disk', size: 26 })),
            h('h3', null, 'No base images'),
            isAdmin && h('button', { className: 'btn primary', onClick: () => setModal('add') }, h(Icon, { name: 'download', size: 16 }), 'Add base image')))
        : h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 } },
            bases.map((img) => h(IsoCard, { key: img.id, img, go, isAdmin, onEdit: (x) => setModal({ img: x }), onDelete: (x) => setConfirm(x) }))),
      modal === 'add' && h(IsoModal, { onClose: () => setModal(null), onDone: () => { setModal(null); toast('Base image added', 'ok'); refresh(); } }),
      modal && modal.img && h(IsoModal, { img: modal.img, onClose: () => setModal(null), onDone: () => { setModal(null); toast('Base image updated', 'ok'); refresh(); } }),
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Remove ' + confirm.name + '?', body: 'Removes the base image entry. Downloaded files on the node are not deleted.', confirmLabel: 'Remove', onConfirm: () => del(confirm) }));
  }

  window.GoldenImages = GoldenImages;
  window.Isos = Isos;
})();
