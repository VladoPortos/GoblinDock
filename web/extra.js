/* GoblinDock — Profile page + Templates list (CRUD). */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu, ConfirmModal, Field } = window.UI;
  const h = React.createElement;

  /* ============ PROFILE ============ */
  function Profile({ go, theme, setTheme }) {
    const me = GD.me || {};
    const [name, setName] = useState(me.name || '');
    const [email, setEmail] = useState(me.email || '');
    const [savingId, setSavingId] = useState(false);
    const [pw, setPw] = useState({ current: '', next: '', confirm: '' });
    const [savingPw, setSavingPw] = useState(false);
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
          h('button', { className: 'btn danger sm', style: { width: 'fit-content' }, onClick: async () => { try { await window.API.logout(); } catch (e) {} window.GD._csrf = null; go('login'); } }, h(Icon, { name: 'logout', size: 14 }), 'Sign out')))));
  }

  /* ============ RECIPES LIST ============ */
  function RecipeCard({ r, go, onDelete }) {
    return h('div', { className: 'card', style: { overflow: 'hidden', display: 'flex', flexDirection: 'column' } },
      h('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12, flex: 1 } },
        h('div', { className: 'row', style: { gap: 10 } },
          h('span', { className: 'placed-ico', style: { width: 32, height: 32 } }, h(Icon, { name: 'template', size: 16 })),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, r.name),
            h('div', { className: 'hint', style: { fontSize: 11 } }, (r.blocks || []).length, ' block', (r.blocks || []).length === 1 ? '' : 's')),
          h('div', { style: { marginLeft: 'auto' } },
            r.public ? h('span', { className: 'badge accent' }, h(Icon, { name: 'globe', size: 12 }), 'Public')
              : h('span', { className: 'badge' }, h(Icon, { name: 'user', size: 12 }), 'Private'))),
        h('p', { className: 'hint', style: { fontSize: 12.5, lineHeight: 1.5, minHeight: 34 } }, r.desc || 'A reusable runtime customization applied on top of a deployed VM.'),
        (r.blocks || []).length > 0 && h('div', { className: 'row wrap', style: { gap: 5 } },
          r.blocks.slice(0, 6).map((b, i) => h('span', { key: i, className: 'chip', style: { fontSize: 10.5, padding: '3px 7px' } }, b))),
        h('div', { className: 'row mono', style: { gap: 10, fontSize: 11, color: 'var(--text-faint)', marginTop: 'auto', paddingTop: 4 } },
          h('span', null, r.used, ' deploys'))),
      h('div', { style: { display: 'flex', borderTop: '1px solid var(--border-soft)' } },
        h('button', { className: 'card-act', onClick: () => go('newrecipe', { recipeId: r.recipeId }) }, h(Icon, { name: 'edit', size: 14 }), 'Edit recipe'),
        h(Menu, { align: 'right', items: [
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => onDelete(r) },
        ] }, h('button', { className: 'card-act', style: { flex: '0 0 44px' } }, h(Icon, { name: 'more', size: 16 })))));
  }

  function RecipesList({ go }) {
    const [confirm, setConfirm] = useState(null);
    const recipes = GD.RECIPES || [];
    const del = async (r) => { await window.API.deleteRecipe(r.recipeId); window.GDStore.toast('Recipe deleted', 'ok'); window.GDStore.refresh().catch(() => {}); };
    return h('div', { className: 'page fadein' },
      h('div', { className: 'page-head' },
        h('div', null,
          h('h1', { className: 'page-title' }, 'Recipes'),
          h('div', { className: 'page-sub' }, 'Reusable runtime customizations (e.g. MySQL, k8s-node) applied to a VM at deploy time.')),
        h('div', { className: 'spacer' }),
        h('button', { className: 'btn primary', onClick: () => go('newrecipe') }, h(Icon, { name: 'plus', size: 16 }), 'New recipe')),
      recipes.length === 0
        ? h('div', { className: 'card' }, h('div', { className: 'empty' },
            h('div', { className: 'glyph' }, h(Icon, { name: 'template', size: 28 })),
            h('h3', null, 'No recipes yet'),
            h('p', null, 'Pack a set of blocks under a name — choose it (or skip it) when you deploy.'),
            h('button', { className: 'btn primary', onClick: () => go('newrecipe') }, h(Icon, { name: 'plus', size: 16 }), 'New recipe')))
        : h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 } },
            recipes.map((r) => h(RecipeCard, { key: r.id, r, go, onDelete: (x) => setConfirm(x) }))),
      confirm && h(ConfirmModal, {
        onClose: () => setConfirm(null), tone: 'danger', icon: 'trash',
        title: 'Delete ' + confirm.name + '?',
        body: 'Removes this recipe. Deployed VMs and golden images are not affected.',
        confirmLabel: 'Delete recipe', onConfirm: () => del(confirm),
      }));
  }

  window.Profile = Profile;
  window.RecipesList = RecipesList;
})();
