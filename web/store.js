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

  return {
    refresh,
    setOnChange: (fn) => { onChange = fn; },
    toast,
    nav: {},
  };
})();
