/* GoblinDock — store: refreshes window.GD from the API and notifies React. */
window.GDStore = (function () {
  let onChange = null;
  let inflight = null;

  // Client-side tombstones for rows removed by an immediate action (local-only
  // VM cleanup, connection/network delete): a /state response that was already
  // in flight when the row was deleted predates the deletion and would
  // resurrect it — filter such ghosts for a short window. (SQLite may reuse a
  // freed id, so the window stays small.)
  const TOMBSTONE_MS = 10000;
  const ROW_ID = { VMS: 'depId', CONNECTIONS: 'connId', NETWORKS: 'netId' };
  const removedRows = new Map();   // `${listKey}:${id}` -> Date.now() at removal

  function _removeRow(listKey, id) {
    removedRows.set(listKey + ':' + id, Date.now());
    const rows = window.GD[listKey] || [];
    const idx = rows.findIndex((row) => row[ROW_ID[listKey]] === id);
    if (idx >= 0) rows.splice(idx, 1);
  }

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

  async function refresh(opts) {
    if (inflight) {
      // A response already in flight may predate a change the caller just made
      // (e.g. local-only cleanup) — with {fresh: true}, wait it out and fetch
      // again so the result is guaranteed to reflect the change. Any request
      // that starts after that point also post-dates the change, so the
      // ordinary collapse is safe again on the recursive call.
      if (!(opts && opts.fresh)) return inflight;
      try { await inflight; } catch (e) { /* the stale fetch's failure is not ours */ }
      return refresh();
    }
    inflight = (async () => {
      try {
        const s = await window.API.state();
        // mutate GD in place (preserve captured references in component IIFEs)
        Object.keys(s).forEach((k) => { window.GD[k] = s[k]; });
        if (removedRows.size) {
          const now = Date.now();
          removedRows.forEach((ts, key) => { if (now - ts > TOMBSTONE_MS) removedRows.delete(key); });
          if (removedRows.size) {
            Object.keys(ROW_ID).forEach((listKey) => {
              window.GD[listKey] = (window.GD[listKey] || []).filter(
                (row) => !removedRows.has(listKey + ':' + row[ROW_ID[listKey]]));
            });
          }
        }
        recordHistory();
        if (onChange) onChange();
        return s;
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  // Immediate client-side removal after a successful delete-like action, so the
  // row disappears without waiting for the (possibly slow — offline Proxmox
  // probes) next /state fetch. The tombstone stops an in-flight stale response
  // from resurrecting it; refresh({fresh: true}) reconciles with the server.
  function removeVm(depId) {
    _removeRow('VMS', depId);
    if (onChange) onChange();
  }

  // Deleting a connection cascades to its networks server-side — mirror that.
  function removeConnection(connId) {
    _removeRow('CONNECTIONS', connId);
    (window.GD.NETWORKS || []).filter((n) => n.connId === connId)
      .forEach((n) => _removeRow('NETWORKS', n.netId));
    if (onChange) onChange();
  }

  function removeNetwork(netId) {
    _removeRow('NETWORKS', netId);
    if (onChange) onChange();
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
    // Flip to "working" AND record which action so the UI can show a live
    // "Starting…/Stopping…/Restarting…" label in the uptime cell until the real
    // Proxmox status arrives (the next /state refetch replaces VMS and clears _act).
    if (vm) { vm.status = 'working'; vm._act = action; if (onChange) onChange(); }
    try {
      await window.API.vmAction(id, action);
      // success: statebus ping → refresh() reconciles to the real status (~1s)
    } catch (e) {
      if (vm) { vm.status = prev; vm._act = null; }
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
    removeVm,
    removeConnection,
    removeNetwork,
    setOnChange: (fn) => { onChange = fn; },
    toast,
    vmAction,
    signOut,
    vmHistory: (depId) => history[depId] || [],
    nav: {},
  };
})();
