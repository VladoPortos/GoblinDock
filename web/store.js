/* GoblinDock — store: refreshes window.GD from the API and notifies React. */
window.GDStore = (function () {
  let onChange = null;
  let inflight = null;

  // Per-VM CPU/RAM ring buffer fed by every state refresh — powers the dashboard
  // sparklines. Client-side and best-effort by design: it shows the trend since
  // this tab opened, no backend storage. Samples are throttled so a burst of
  // statebus pings doesn't compress the time axis.
  const HIST_LEN = 40;
  const HIST_MIN_GAP_MS = 4000;
  const history = {};   // depId -> [{t, cpu, ram}]
  function recordHistory() {
    const now = Date.now();
    const seen = new Set();
    (window.GD.VMS || []).forEach((v) => {
      seen.add(v.depId);
      const h = history[v.depId] || (history[v.depId] = []);
      const last = h[h.length - 1];
      if (last && now - last.t < HIST_MIN_GAP_MS) return;
      h.push({ t: now, cpu: v.status === 'running' ? (v.cpu || 0) : 0, ram: v.status === 'running' ? (v.ram || 0) : 0 });
      if (h.length > HIST_LEN) h.shift();
    });
    Object.keys(history).forEach((k) => { if (!seen.has(Number(k))) delete history[k]; });
  }

  async function refresh() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const s = await window.API.state();
        // mutate GD in place (preserve captured references in component IIFEs)
        Object.keys(s).forEach((k) => { window.GD[k] = s[k]; });
        recordHistory();
        if (onChange) onChange();
        return s;
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  function toast(msg, tone) {
    window.GD._toast = { msg, tone: tone || 'ok', ts: Date.now() };
    if (onChange) onChange();
  }

  // Optimistic VM power action: flip the card to "working" immediately, fire the
  // request, and let the live-state refetch reconcile to the real Proxmox status.
  // On failure, restore the previous status and toast the error.
  async function vmAction(id, action) {
    const vms = window.GD.VMS || [];
    const vm = vms.find((v) => v.depId === id || v.id === id);
    const prev = vm ? vm.status : null;
    if (vm) { vm.status = 'working'; if (onChange) onChange(); }
    try {
      await window.API.vmAction(id, action);
      // success: statebus ping → refresh() reconciles to the real status (~1s)
    } catch (e) {
      if (vm) { vm.status = prev; }
      toast(e.message || (action + ' failed'), 'err');
      if (onChange) onChange();
      throw e;
    }
  }

  // Shared sign-out: best-effort server logout, drop the CSRF token, then route to
  // the login screen. Used by the topbar menu and the Profile page.
  async function signOut(go) {
    try { await window.API.logout(); } catch (e) { /* cookie may already be gone */ }
    window.GD._csrf = null;
    if (go) go('login');
  }

  return {
    refresh,
    setOnChange: (fn) => { onChange = fn; },
    toast,
    vmAction,
    signOut,
    vmHistory: (depId) => history[depId] || [],
    nav: {},
  };
})();
