/* GoblinDock — live data store.
   Static maps stay here; the dynamic lists are filled from /api/state by store.js.
   window.GD is created ONCE and mutated in place so component IIFEs that captured
   `const GD = window.GD` keep seeing fresh data. */
(function () {
  const OS_COLORS = {
    ubuntu: '#E95420', debian: '#A80030', alpine: '#0D597F',
    rocky: '#10B981', windows: '#0078D4', generic: '#888',
  };
  const OS_LABEL = { ubuntu: 'Ubuntu', debian: 'Debian', alpine: 'Alpine', rocky: 'Rocky', windows: 'Windows', generic: 'Linux' };

  window.GD = {
    // static
    OS_COLORS, OS_LABEL,
    // identity + limits (filled on bootstrap)
    me: null,
    limits: { maxCores: 1, maxRam: 2, vmidMin: 8000, vmidMax: 8099 },
    // dynamic collections (filled from /api/state)
    VMS: [],
    BASE_IMAGES: [],
    TEMPLATES: [],
    PALETTE: [],
    SECRETS: [],
    VARIABLES: [],
    CONNECTIONS: [],
    NETWORKS: [],
    USERS: [],
    JOBS: [],
    // transient nav (e.g. selected job id)
    _jobId: null,
    _toast: null,
    _csrf: null,
  };
})();
