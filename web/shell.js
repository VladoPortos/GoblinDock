/* GoblinDock — app shell: sidebar, topbar, activity drawer */
(function () {
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu } = window.UI;
  const h = React.createElement;

  const NAV = [
    { group: 'Operate', items: [
      { id: 'dashboard', label: 'Dashboard', icon: 'server' },
    ]},
    { group: 'Build', items: [
      { id: 'templates', label: 'Templates',   icon: 'template' },
      { id: 'blocks',    label: 'Blocks',     icon: 'blocks' },
    ]},
    { group: 'Manage', items: [
      { id: 'isos',      label: 'ISOs',       icon: 'disk' },
      { id: 'secrets',   label: 'Secrets',    icon: 'lock' },
      { id: 'variables', label: 'Variables',  icon: 'tag' },
      { id: 'history',   label: 'History',    icon: 'history' },
      { id: 'settings',  label: 'Settings',   icon: 'settings', admin: true },
    ]},
  ];

  function Sidebar({ route, go, collapsed, setCollapsed }) {
    return h('aside', { className: 'sidebar' + (collapsed ? ' collapsed' : '') },
      h('div', { className: 'brand' },
        h('img', { src: 'assets/goblindock-logo.png', alt: 'GoblinDock',
          style: { width: 34, height: 34, objectFit: 'contain', flexShrink: 0, filter: 'drop-shadow(0 1px 3px rgba(0,0,0,.4))' } }),
        !collapsed && h('span', { className: 'brand-word' }, 'Goblin', h('b', null, 'Dock'))
      ),
      h('nav', { className: 'nav' },
        NAV.map(sec => h(React.Fragment, { key: sec.group },
          !collapsed && h('div', { className: 'nav-label' }, sec.group),
          collapsed && h('div', { style: { height: 10 } }),
          sec.items.filter(it => !it.admin || (GD.me && GD.me.isAdmin)).map(it => h('div', {
            key: it.id,
            className: 'nav-item' + (route === it.id ? ' active' : ''),
            onClick: () => go(it.id),
            title: collapsed ? it.label : null,
          },
            h(Icon, { name: it.icon, size: 18 }),
            !collapsed && h('span', null, it.label),
            !collapsed && it.id === 'dashboard' && (GD.VMS || []).length > 0 && h('span', { className: 'badge count', style: { fontSize: 11, fontWeight: 700, fontFamily: 'system-ui, -apple-system, sans-serif' } }, GD.VMS.length)
          ))
        ))
      ),
      h('div', { className: 'sidebar-foot' },
        h('div', { className: 'nav-item', onClick: () => setCollapsed(c => !c), title: 'Collapse' },
          h(Icon, { name: 'collapse', size: 18 }),
          !collapsed && h('span', null, 'Collapse')
        )
      )
    );
  }

  const TITLES = {
    dashboard: ['Operate', 'Virtual Machines'], vmdetail: ['Operate', 'Virtual Machine'],
    job: ['Operate', 'Job Progress'],
    templates: ['Build', 'Templates'],
    newtemplate: ['Build', 'Template'], blocks: ['Build', 'Blocks'],
    isos: ['Manage', 'ISOs / Base Images'], secrets: ['Manage', 'Secrets'],
    variables: ['Manage', 'Variables'],
    history: ['Manage', 'History'],
    settings: ['Manage', 'Settings'], profile: ['Account', 'Profile'],
  };

  function TopBar({ route, go, theme, setTheme, openDrawer }) {
    const t = TITLES[route] || ['', route];
    return h('header', { className: 'topbar' },
      h('div', { className: 'crumb' },
        h('span', null, t[0]),
        h(Icon, { name: 'chevronR', size: 14 }),
        h('span', { className: 'mono' }, t[1])
      ),
      h('div', { className: 'topbar-right' },
        (function () {
          const jobs = GD.JOBS || [];
          const total = jobs.length;
          const anyRunning = jobs.some(j => j.status === 'working');
          return h('button', { className: 'icon-btn', onClick: openDrawer, title: 'Activity', style: { position: 'relative' } },
            h(Icon, { name: 'bell', size: 17 }),
            total > 0 && h('span', {
              style: { position: 'absolute', top: 1, right: 1, minWidth: 15, height: 15, padding: '0 3px',
                borderRadius: 99, background: anyRunning ? 'var(--warn)' : 'var(--accent)', color: 'var(--bg)', fontFamily: 'JetBrains Mono, monospace',
                fontSize: 9.5, fontWeight: 700, display: 'grid', placeItems: 'center', border: '2px solid var(--surface)',
                animation: anyRunning ? 'pulse 1.4s ease-in-out infinite' : 'none' } }, total));
        })(),
        h('button', { className: 'icon-btn', onClick: () => setTheme(theme === 'dark' ? 'light' : 'dark'), title: 'Theme' },
          h(Icon, { name: theme === 'dark' ? 'sun' : 'moon', size: 17 })),
        h('div', { style: { width: 1, height: 22, background: 'var(--border)' } }),
        h(Menu, {
          items: [
            { label: ((GD.me && GD.me.name) || 'Account') + ' · ' + ((GD.me && GD.me.role) || ''), icon: 'user' },
            { sep: true },
            { label: 'Profile', icon: 'user', onClick: () => go('profile') },
            { label: 'Sign out', icon: 'logout', onClick: () => window.GDStore.signOut(go) },
          ]
        }, h('div', { className: 'avatar', title: (GD.me && GD.me.name) || '' }, (GD.me && GD.me.initials) || '··'))
      )
    );
  }

  function ActivityDrawer({ onClose, go }) {
    const jobs = GD.JOBS || [];
    const finished = jobs.filter(j => j.status !== 'working').length;
    const dismiss = async (e, j) => {
      e.stopPropagation();
      try { await window.API.deleteJob(j.jobId); window.GDStore.refresh().catch(() => {}); }
      catch (err) { window.GDStore.toast(err.message, 'err'); }
    };
    const clearAll = async () => {
      try { await window.API.clearJobs(); window.GDStore.toast('Cleared finished activity', 'ok'); window.GDStore.refresh().catch(() => {}); }
      catch (err) { window.GDStore.toast(err.message, 'err'); }
    };
    return h(React.Fragment, null,
      // overlay must sit BELOW the drawer (z 55) so the drawer stays clickable;
      // it only catches clicks OUTSIDE the drawer to close it.
      h('div', { className: 'overlay', style: { background: 'transparent', backdropFilter: 'none', zIndex: 54 }, onClick: onClose }),
      h('div', { className: 'drawer' },
        h('div', { className: 'drawer-head' },
          h(Icon, { name: 'activity', size: 17 }),
          h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, 'Activity'),
          h('span', { className: 'badge accent', style: { marginLeft: 4 } }, jobs.filter(j => j.status === 'working').length, ' running'),
          h('div', { style: { marginLeft: 'auto', display: 'flex', gap: 4 } },
            finished > 0 && h('button', { className: 'btn ghost sm', onClick: clearAll, title: 'Clear finished' },
              h(Icon, { name: 'trash', size: 14 }), 'Clear'),
            h('button', { className: 'icon-btn', onClick: onClose, 'aria-label': 'Close' },
              h(Icon, { name: 'x', size: 16 })))
        ),
        h('div', { style: { padding: 12, overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: 8 } },
          jobs.length === 0
            ? h('div', { className: 'hint', style: { textAlign: 'center', padding: 30, fontSize: 12.5 } }, 'No recent activity.')
            : jobs.map(j => h('div', {
                key: j.id, className: 'card', style: { padding: 13, cursor: 'pointer', position: 'relative' },
                onClick: () => { onClose(); go('job', { jobId: j.jobId }); },
              },
                h('div', { className: 'row', style: { marginBottom: 9 } },
                  h('span', { className: 'dot ' + j.status }),
                  h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, j.title),
                  h('span', { className: 'mono', style: { marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' } }, j.elapsed),
                  j.status !== 'working' && h('button', { className: 'icon-btn sm', title: 'Dismiss', onClick: (e) => dismiss(e, j) }, h(Icon, { name: 'x', size: 13 }))
                ),
                h('div', { className: 'meter ' + (j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : '') },
                  h('i', { style: { width: j.pct + '%' } })),
                h('div', { className: 'row', style: { marginTop: 8, justifyContent: 'space-between' } },
                  h('span', { className: 'hint mono', style: { fontSize: 11 } }, j.phase),
                  j.status === 'working' && h('span', { className: 'hint mono', style: { fontSize: 11 } }, j.step, '/', j.total)
                )
              ))
        )
      )
    );
  }

  window.Shell = { Sidebar, TopBar, ActivityDrawer };
})();
