/* GoblinDock — app shell: sidebar, topbar, activity drawer */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const { Menu } = window.UI;

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

  function Sidebar({ route, go, collapsed, setCollapsed, runningJobs }) {
    return React.createElement('aside', { className: 'sidebar' + (collapsed ? ' collapsed' : '') },
      React.createElement('div', { className: 'brand' },
        React.createElement('img', { src: 'assets/goblindock-logo.png', alt: 'GoblinDock',
          style: { width: 34, height: 34, objectFit: 'contain', flexShrink: 0, filter: 'drop-shadow(0 1px 3px rgba(0,0,0,.4))' } }),
        !collapsed && React.createElement('span', { className: 'brand-word' }, 'Goblin', React.createElement('b', null, 'Dock'))
      ),
      React.createElement('nav', { className: 'nav' },
        NAV.map(sec => React.createElement(React.Fragment, { key: sec.group },
          !collapsed && React.createElement('div', { className: 'nav-label' }, sec.group),
          collapsed && React.createElement('div', { style: { height: 10 } }),
          sec.items.filter(it => !it.admin || (GD.me && GD.me.isAdmin)).map(it => React.createElement('div', {
            key: it.id,
            className: 'nav-item' + (route === it.id ? ' active' : '') + (it.primary ? ' primary-nav' : ''),
            onClick: () => go(it.id),
            title: collapsed ? it.label : null,
            style: it.primary && route !== it.id ? { color: 'var(--accent)' } : null,
          },
            React.createElement(Icon, { name: it.icon, size: 18 }),
            !collapsed && React.createElement('span', null, it.label),
            !collapsed && it.id === 'dashboard' && (GD.VMS || []).length > 0 && React.createElement('span', { className: 'badge count', style: { fontSize: 11, fontWeight: 700, fontFamily: 'system-ui, -apple-system, sans-serif' } }, GD.VMS.length)
          ))
        ))
      ),
      React.createElement('div', { className: 'sidebar-foot' },
        React.createElement('div', { className: 'nav-item', onClick: () => setCollapsed(c => !c), title: 'Collapse' },
          React.createElement(Icon, { name: 'collapse', size: 18 }),
          !collapsed && React.createElement('span', null, 'Collapse')
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

  function TopBar({ route, go, theme, setTheme, openDrawer, runningJobs }) {
    const t = TITLES[route] || ['', route];
    return React.createElement('header', { className: 'topbar' },
      React.createElement('div', { className: 'crumb' },
        React.createElement('span', null, t[0]),
        React.createElement(Icon, { name: 'chevronR', size: 14 }),
        React.createElement('span', { className: 'mono' }, t[1])
      ),
      React.createElement('div', { className: 'topbar-right' },
        (function () {
          const jobs = GD.JOBS || [];
          const total = jobs.length;
          const anyRunning = jobs.some(j => j.status === 'working');
          return React.createElement('button', { className: 'icon-btn', onClick: openDrawer, title: 'Activity', style: { position: 'relative' } },
            React.createElement(Icon, { name: 'bell', size: 17 }),
            total > 0 && React.createElement('span', {
              style: { position: 'absolute', top: 1, right: 1, minWidth: 15, height: 15, padding: '0 3px',
                borderRadius: 99, background: anyRunning ? 'var(--warn)' : 'var(--accent)', color: 'var(--bg)', fontFamily: 'JetBrains Mono, monospace',
                fontSize: 9.5, fontWeight: 700, display: 'grid', placeItems: 'center', border: '2px solid var(--surface)',
                animation: anyRunning ? 'pulse 1.4s ease-in-out infinite' : 'none' } }, total));
        })(),
        React.createElement('button', { className: 'icon-btn', onClick: () => setTheme(theme === 'dark' ? 'light' : 'dark'), title: 'Theme' },
          React.createElement(Icon, { name: theme === 'dark' ? 'sun' : 'moon', size: 17 })),
        React.createElement('div', { style: { width: 1, height: 22, background: 'var(--border)' } }),
        React.createElement(Menu, {
          items: [
            { label: ((GD.me && GD.me.name) || 'Account') + ' · ' + ((GD.me && GD.me.role) || ''), icon: 'user' },
            { sep: true },
            { label: 'Profile', icon: 'user', onClick: () => go('profile') },
            { label: 'Sign out', icon: 'logout', onClick: async () => { try { await window.API.logout(); } catch (e) {} window.GD._csrf = null; go('login'); } },
          ]
        }, React.createElement('div', { className: 'avatar', title: (GD.me && GD.me.name) || '' }, (GD.me && GD.me.initials) || '··'))
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
    return React.createElement(React.Fragment, null,
      // overlay must sit BELOW the drawer (z 55) so the drawer stays clickable;
      // it only catches clicks OUTSIDE the drawer to close it.
      React.createElement('div', { className: 'overlay', style: { background: 'transparent', backdropFilter: 'none', zIndex: 54 }, onClick: onClose }),
      React.createElement('div', { className: 'drawer' },
        React.createElement('div', { className: 'drawer-head' },
          React.createElement(Icon, { name: 'activity', size: 17 }),
          React.createElement('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, 'Activity'),
          React.createElement('span', { className: 'badge accent', style: { marginLeft: 4 } }, jobs.filter(j => j.status === 'working').length, ' running'),
          React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', gap: 4 } },
            finished > 0 && React.createElement('button', { className: 'btn ghost sm', onClick: clearAll, title: 'Clear finished' },
              React.createElement(Icon, { name: 'trash', size: 14 }), 'Clear'),
            React.createElement('button', { className: 'icon-btn', onClick: onClose, 'aria-label': 'Close' },
              React.createElement(Icon, { name: 'x', size: 16 })))
        ),
        React.createElement('div', { style: { padding: 12, overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: 8 } },
          jobs.length === 0
            ? React.createElement('div', { className: 'hint', style: { textAlign: 'center', padding: 30, fontSize: 12.5 } }, 'No recent activity.')
            : jobs.map(j => React.createElement('div', {
                key: j.id, className: 'card', style: { padding: 13, cursor: 'pointer', position: 'relative' },
                onClick: () => { onClose(); go('job', { jobId: j.jobId }); },
              },
                React.createElement('div', { className: 'row', style: { marginBottom: 9 } },
                  React.createElement('span', { className: 'dot ' + j.status }),
                  React.createElement('span', { className: 'mono', style: { fontWeight: 600, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, j.title),
                  React.createElement('span', { className: 'mono', style: { marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' } }, j.elapsed),
                  j.status !== 'working' && React.createElement('button', { className: 'icon-btn sm', title: 'Dismiss', onClick: (e) => dismiss(e, j) }, React.createElement(Icon, { name: 'x', size: 13 }))
                ),
                React.createElement('div', { className: 'meter ' + (j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : '') },
                  React.createElement('i', { style: { width: j.pct + '%' } })),
                React.createElement('div', { className: 'row', style: { marginTop: 8, justifyContent: 'space-between' } },
                  React.createElement('span', { className: 'hint mono', style: { fontSize: 11 } }, j.phase),
                  j.status === 'working' && React.createElement('span', { className: 'hint mono', style: { fontSize: 11 } }, j.step, '/', j.total)
                )
              ))
        )
      )
    );
  }

  window.Shell = { Sidebar, TopBar, ActivityDrawer };
})();
