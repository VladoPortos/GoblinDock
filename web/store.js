/* GoblinDock — store: refreshes window.GD from the API and notifies React. */
window.GDStore = (function () {
  let onChange = null;
  let inflight = null;

  async function refresh() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const s = await window.API.state();
        // mutate GD in place (preserve captured references in component IIFEs)
        Object.keys(s).forEach((k) => { window.GD[k] = s[k]; });
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

  return {
    refresh,
    setOnChange: (fn) => { onChange = fn; },
    toast,
    vmAction,
    nav: {},
  };
})();
