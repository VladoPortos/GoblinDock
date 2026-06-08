/* GoblinDock — Login / first-run admin setup (wired to the API) */
(function () {
  const { useState, useEffect } = React;
  const Icon = window.Icon;
  const h = React.createElement;

  function Login({ go, theme, setTheme }) {
    const [needsSetup, setNeedsSetup] = useState(false);
    const [email, setEmail] = useState('');
    const [name, setName] = useState('');
    const [pw, setPw] = useState('');
    const [err, setErr] = useState('');
    const [busy, setBusy] = useState(false);

    useEffect(() => {
      window.API.authStatus().then((s) => setNeedsSetup(!!s.needsSetup)).catch(() => {});
    }, []);

    const submit = async (e) => {
      e.preventDefault();
      setErr('');
      if (!email || !pw || (needsSetup && !name)) { setErr('Fill in all fields to continue.'); return; }
      setBusy(true);
      try {
        if (needsSetup) await window.API.setup(email, name, pw);
        else await window.API.login(email, pw);
        await window.GDStore.refresh();
        go('dashboard');
      } catch (ex) {
        setErr(ex.message || 'Sign in failed.');
      } finally {
        setBusy(false);
      }
    };

    return h('div', { className: 'login-stage' },
      h('button', { className: 'icon-btn', style: { position: 'fixed', top: 18, right: 18 }, onClick: () => setTheme(theme === 'dark' ? 'light' : 'dark') },
        h(Icon, { name: theme === 'dark' ? 'sun' : 'moon', size: 18 })),
      h('div', { className: 'login-card' },
        h('img', { src: 'assets/goblindock-logo.png', alt: 'GoblinDock', className: 'login-logo' }),
        h('h1', { className: 'mono', style: { fontSize: 22, fontWeight: 800, letterSpacing: '-0.02em', textAlign: 'center' } },
          'Goblin', h('span', { style: { color: 'var(--accent)' } }, 'Dock')),
        h('p', { className: 'hint', style: { textAlign: 'center', marginTop: 4, marginBottom: 26 } },
          needsSetup ? 'Create the first admin account' : 'Self-service VM provisioning for Proxmox'),
        h('form', { onSubmit: submit, style: { display: 'flex', flexDirection: 'column', gap: 14 } },
          needsSetup && h('div', null,
            h('label', { className: 'field-label' }, 'Your name'),
            h('input', { className: 'input', value: name, onChange: (e) => setName(e.target.value), placeholder: 'Admin', autoComplete: 'name' })),
          h('div', null,
            h('label', { className: 'field-label' }, 'Email'),
            h('input', { className: 'input mono', value: email, onChange: (e) => setEmail(e.target.value), autoComplete: 'username', placeholder: 'you@example.com' })),
          h('div', null,
            h('label', { className: 'field-label' }, 'Password'),
            h('input', { className: 'input', type: 'password', placeholder: '••••••••', value: pw,
              onChange: (e) => { setPw(e.target.value); setErr(''); } })),
          err && h('div', { className: 'row', style: { gap: 7, color: 'var(--err)', fontSize: 12.5 } },
            h(Icon, { name: 'warn', size: 14 }), err),
          h('button', { className: 'btn primary', type: 'submit', style: { height: 42, marginTop: 4 }, disabled: busy },
            busy ? 'Working…' : (needsSetup ? 'Create admin' : 'Sign in'), h(Icon, { name: 'arrowRight', size: 16 }))),
        !needsSetup && h('p', { className: 'hint', style: { textAlign: 'center', fontSize: 11.5, marginTop: 18 } },
          'Ask your admin for an account if you don’t have one.')),
      h('p', { className: 'mono', style: { position: 'fixed', bottom: 16, color: 'var(--text-faint)', fontSize: 11 } }, 'GoblinDock v2.1'));
  }

  window.Login = Login;
})();
