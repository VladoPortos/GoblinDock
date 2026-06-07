/* GoblinDock — app bootstrap + shell wiring (plain React.createElement). */
(function () {
  const { useState, useEffect } = React;
  const h = React.createElement;

  function Toast() {
    const [t, setT] = useState(window.GD._toast);
    useEffect(() => {
      const id = setInterval(() => setT(window.GD._toast), 300);
      return () => clearInterval(id);
    }, []);
    useEffect(() => {
      if (!t) return;
      const id = setTimeout(() => { window.GD._toast = null; setT(null); }, 4200);
      return () => clearTimeout(id);
    }, [t && t.ts]);
    if (!t) return null;
    const tone = t.tone === 'err' ? 'err' : t.tone === 'warn' ? 'warn' : 'ok';
    return h('div', {
      style: {
        position: 'fixed', bottom: 22, left: '50%', transform: 'translateX(-50%)', zIndex: 80,
        display: 'flex', alignItems: 'center', gap: 10, padding: '11px 16px', borderRadius: 11,
        background: 'var(--surface)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-lg)',
        color: 'var(--text)', fontFamily: 'JetBrains Mono, monospace', fontSize: 12.5, maxWidth: '90vw',
      },
    },
      h('span', { className: 'dot ' + (tone === 'err' ? 'error' : tone === 'warn' ? 'working' : 'running') }),
      h('span', null, t.msg));
  }

  function App({ initialRoute }) {
    const [theme, setThemeState] = useState(() => localStorage.getItem('gd-theme') || 'dark');
    const [route, setRoute] = useState(initialRoute);
    const [collapsed, setCollapsed] = useState(false);
    const [drawer, setDrawer] = useState(false);
    const [, setTick] = useState(0);
    // navKey changes ONLY on real navigation — it keys the screen subtree so a
    // background poll re-render preserves screen/modal state, while clicking a nav
    // item (even the same route) remounts it fresh.
    const [navKey, setNavKey] = useState(0);

    const setTheme = (t) => { setThemeState(t); localStorage.setItem('gd-theme', t); };
    useEffect(() => { document.documentElement.setAttribute('data-theme', theme); }, [theme]);
    useEffect(() => { window.GDStore.setOnChange(() => setTick((n) => n + 1)); }, []);
    useEffect(() => {
      const onUnauth = () => setRoute('login');
      window.addEventListener('gd-unauth', onUnauth);
      return () => window.removeEventListener('gd-unauth', onUnauth);
    }, []);

    const go = (r, params) => {
      window.GDStore.nav = params || {};
      if (params && params.jobId) window.GD._jobId = params.jobId;
      setNavKey((k) => k + 1);
      setRoute(r);
      localStorage.setItem('gd-route', r);
      const c = document.querySelector('.content'); if (c) c.scrollTo(0, 0);
      if (r !== 'login' && r !== 'job' && r !== 'builder') window.GDStore.refresh().catch(() => {});
    };

    // background poll for live VM/job state
    useEffect(() => {
      if (route === 'login') return undefined;
      const id = setInterval(() => {
        if (route !== 'builder' && route !== 'job') window.GDStore.refresh().catch(() => {});
      }, 5000);
      return () => clearInterval(id);
    }, [route]);

    if (route === 'login') {
      return h(React.Fragment, null, h(window.Login, { go, theme, setTheme }), h(Toast));
    }

    const { Sidebar, TopBar, ActivityDrawer } = window.Shell;
    const GD = window.GD;
    const runningJobs = (GD.JOBS || []).filter((j) => j.status === 'working').length;

    const SCREENS = {
      dashboard: () => h(window.Dashboard, { go }),
      vmdetail: () => h(window.VmDetail, { go }),
      deploy: () => h(window.Deploy, { go }),
      job: () => h(window.JobProgress, { go }),
      builder: () => h(window.Builder, { go, mode: 'golden' }),
      newtemplate: () => h(window.Builder, { go, mode: 'template' }),
      golden: () => h(window.GoldenImages, { go }),
      templates: () => h(window.TemplatesList, { go }),
      isos: () => h(window.Isos, { go }),
      blocks: () => h(window.BlocksLib, { go }),
      secrets: () => h(window.Secrets, { go }),
      variables: () => h(window.Variables, { go }),
      settings: () => h(window.Settings, { go }),
      profile: () => h(window.Profile, { go, theme, setTheme }),
    };
    const Screen = SCREENS[route] || SCREENS.dashboard;
    const fullBleed = route === 'builder' || route === 'newtemplate';

    return h('div', { className: 'app' },
      h(Sidebar, { route, go, collapsed, setCollapsed, runningJobs }),
      h('div', { className: 'main' },
        h(TopBar, { route, go, theme, setTheme, openDrawer: () => setDrawer(true), runningJobs }),
        // key by navKey: poll re-renders keep state; navigation remounts the screen.
        h('div', { className: 'content', key: navKey, style: fullBleed ? { overflow: 'hidden' } : null }, Screen())),
      drawer && h(ActivityDrawer, { onClose: () => setDrawer(false), go }),
      h(Toast));
  }

  async function boot() {
    let authed = false;
    try { await window.API.me(); authed = true; } catch (e) { /* not logged in */ }
    if (authed) { try { await window.GDStore.refresh(); } catch (e) { /* ignore */ } }
    const initial = authed ? (localStorage.getItem('gd-route') || 'dashboard') : 'login';
    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(h(App, { initialRoute: initial === 'login' && authed ? 'dashboard' : initial }));
  }

  boot();
})();
