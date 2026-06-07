/* GoblinDock — ISOs / base images (Manage). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { OSGlyph, Menu, ConfirmModal, FormModal, Field, SelectField } = window.UI;
  const h = React.createElement;
  const refresh = () => window.GDStore.refresh().catch(() => {});
  const toast = (m, t) => window.GDStore.toast(m, t);

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
        h('button', { className: 'card-act', onClick: () => go('newtemplate', { baseImageId: img.imgId }) }, h(Icon, { name: 'template', size: 14 }), 'New template'),
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
          h('div', { className: 'page-sub' }, 'Public cloud images — the raw material templates deploy from.')),
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
      confirm && h(ConfirmModal, { onClose: () => setConfirm(null), tone: 'danger', icon: 'trash', title: 'Remove ' + confirm.name + '?',
        body: 'Removes the base image entry. Downloaded files on the node are not deleted.'
          + (function () {
            const refs = ((window.GD.TEMPLATES) || []).filter((t) => t.baseImageId === confirm.imgId).length;
            return refs ? ' ' + refs + ' template' + (refs === 1 ? ' references' : 's reference') + ' this image — '
              + (refs === 1 ? 'it keeps' : 'they keep') + ' working but ' + (refs === 1 ? 'loses' : 'lose')
              + ' deploy until re-pointed.' : '';
          })(),
        confirmLabel: 'Remove', onConfirm: () => del(confirm) }));
  }

  window.Isos = Isos;
})();
