/* GoblinDock — fetch wrapper over the REST API (with CSRF). */
window.API = (function () {
  function setCsrf(data) {
    if (data && typeof data === 'object' && data.csrf) window.GD._csrf = data.csrf;
    return data;
  }

  // Build a ?a=b query string, skipping empty/null params.
  function qs(params) {
    if (!params) return '';
    const parts = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v));
    return parts.length ? '?' + parts.join('&') : '';
  }

  // Under concurrency a background poll can momentarily 401 while the session cookie
  // is still perfectly valid. Confirm the session is REALLY gone before evicting the
  // user to the login screen. /api/auth/status always returns 200 (never 401), so this
  // check can't recurse into the eviction path. On a network/parse error we stay put
  // rather than spuriously logging out.
  async function stillAuthenticated() {
    try {
      const r = await fetch('/api/auth/status', { credentials: 'same-origin' });
      if (!r.ok) return false;
      const d = await r.json();
      return !!d.authenticated;
    } catch (e) {
      return true;
    }
  }

  async function req(method, url, body) {
    const opts = { method, headers: {}, credentials: 'same-origin' };
    const mutating = !['GET', 'HEAD'].includes(method);
    if (mutating && window.GD._csrf) opts.headers['X-CSRF-Token'] = window.GD._csrf;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let r = await fetch(url, opts);
    // A mutating call can get an opaque 403 if the CSRF token went stale (e.g. it was
    // nulled on sign-out and a request fired before the next bootstrap). Refresh the
    // token from /api/auth/me and retry ONCE before surfacing the error.
    if (r.status === 403 && mutating) {
      let d403 = null;
      try { d403 = await r.clone().json(); } catch (e) { /* no body */ }
      if (d403 && typeof d403.detail === 'string' && /csrf/i.test(d403.detail)) {
        try {
          const me = await fetch('/api/auth/me', { credentials: 'same-origin' });
          if (me.ok) setCsrf(await me.json());
        } catch (e) { /* leave token as-is */ }
        if (window.GD._csrf) {
          opts.headers['X-CSRF-Token'] = window.GD._csrf;
          r = await fetch(url, opts);
        }
      }
    }
    if (r.status === 401 && url !== '/api/auth/status' && !(await stillAuthenticated())) {
      window.dispatchEvent(new CustomEvent('gd-unauth'));
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) {
      const msg = (data && (data.detail || data.error)) || r.statusText;
      const err = new Error(typeof msg === 'string' ? msg : 'request failed');
      err.status = r.status; err.data = data;
      throw err;
    }
    return setCsrf(data);
  }

  return {
    // auth + bootstrap
    authStatus: () => req('GET', '/api/auth/status'),
    me: () => req('GET', '/api/auth/me'),
    login: (email, password) => req('POST', '/api/auth/login', { email, password }),
    setup: (email, name, password) => req('POST', '/api/auth/setup', { email, name, password }),
    logout: () => req('POST', '/api/auth/logout'),
    state: () => req('GET', '/api/state'),

    // profile
    profile: () => req('GET', '/api/profile'),
    updateProfile: (p) => req('PUT', '/api/profile', p),
    changePassword: (current, neu) => req('POST', '/api/profile/password', { current, new: neu }),
    generateWidgetKey: () => req('POST', '/api/profile/widget-key'),
    revokeWidgetKey: () => req('DELETE', '/api/profile/widget-key'),

    // deployments
    deploy: (p) => req('POST', '/api/deployments', p),
    vmAction: (id, action) => req('POST', `/api/deployments/${id}/action`, { action }),
    vmRebuild: (id) => req('POST', `/api/deployments/${id}/rebuild`),
    vmDestroy: (id) => req('DELETE', `/api/deployments/${id}`),
    patchVm: (id, p) => req('PATCH', `/api/deployments/${id}`, p),
    vmDetail: (id) => req('GET', `/api/vms/${id}/detail`),
    vncProxy: (id) => req('POST', `/api/vms/${id}/vncproxy`),

    // images
    buildGolden: (p) => req('POST', '/api/images/golden', p),
    rebuildGolden: (id) => req('POST', `/api/images/${id}/rebuild`),
    addBaseImage: (p) => req('POST', '/api/images/base', p),
    editImage: (id, p) => req('PUT', `/api/images/${id}`, p),
    deleteImage: (id) => req('DELETE', `/api/images/${id}`),
    staleImages: () => req('GET', '/api/images/stale'),

    // recipes (runtime customisation, decoupled from images)
    saveRecipe: (p) => req('POST', '/api/recipes', p),
    editRecipe: (id, p) => req('PUT', `/api/recipes/${id}`, p),
    deleteRecipe: (id) => req('DELETE', `/api/recipes/${id}`),
    compile: (recipe, name) => req('POST', '/api/recipes/compile', { recipe, name }),

    // blocks
    createBlock: (p) => req('POST', '/api/blocks', p),
    forkBlock: (key) => req('POST', `/api/blocks/${key}/fork`),
    editBlock: (key, p) => req('PUT', `/api/blocks/${key}`, p),
    deleteBlock: (key) => req('DELETE', `/api/blocks/${key}`),

    // secrets
    addSecret: (p) => req('POST', '/api/secrets', p),
    editSecret: (id, p) => req('PUT', `/api/secrets/${id}`, p),
    delSecret: (id) => req('DELETE', `/api/secrets/${id}`),
    revealSecret: (id) => req('POST', `/api/secrets/${id}/reveal`),

    // variables (plaintext, visible)
    addVariable: (p) => req('POST', '/api/variables', p),
    editVariable: (id, p) => req('PUT', `/api/variables/${id}`, p),
    deleteVariable: (id) => req('DELETE', `/api/variables/${id}`),

    // connections
    addConnection: (p) => req('POST', '/api/connections', p),
    editConnection: (id, p) => req('PUT', `/api/connections/${id}`, p),
    deleteConnection: (id) => req('DELETE', `/api/connections/${id}`),
    testConnection: (id) => req('POST', `/api/connections/${id}/test`),

    // networks
    addNetwork: (p) => req('POST', '/api/networks', p),
    editNetwork: (id, p) => req('PUT', `/api/networks/${id}`, p),
    deleteNetwork: (id) => req('DELETE', `/api/networks/${id}`),

    // users
    addUser: (p) => req('POST', '/api/users', p),
    editUser: (id, p) => req('PUT', `/api/users/${id}`, p),
    deleteUser: (id) => req('DELETE', `/api/users/${id}`),
    resetUserPassword: (id, value) => req('POST', `/api/users/${id}/password`, { name: '', value }),

    // jobs + audit
    job: (id) => req('GET', `/api/jobs/${id}`),
    cancelJob: (id) => req('POST', `/api/jobs/${id}/cancel`),
    deleteJob: (id) => req('DELETE', `/api/jobs/${id}`),
    clearJobs: () => req('POST', '/api/jobs/clear'),
    audit: (params) => req('GET', '/api/audit' + qs(params)),

    // admin: scheduled DB backups
    adminBackups: () => req('GET', '/api/admin/backups'),
    runBackup: () => req('POST', '/api/admin/backup'),
  };
})();
