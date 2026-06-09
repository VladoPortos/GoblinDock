/* GoblinDock — History: an auto-populated log of ALL jobs (deploy / start / stop /
   destroy / rebuild / sync), newest first — like Audit. The activity bell is separate
   (transient, dismissible). Per-row + bulk purge; admin-configurable auto-prune. */
(function () {
  const { useState, useEffect } = React;
  const Icon = window.Icon;
  const { ConfirmModal } = window.UI;
  const h = React.createElement;

  const RETENTION_OPTS = [
    { v: 0, label: 'Keep forever' },
    { v: 14, label: '14 days' },
    { v: 30, label: '30 days' },
    { v: 90, label: '90 days' },
  ];

  function History() {
    const isAdmin = window.GD.me && window.GD.me.isAdmin;
    const [rows, setRows] = useState(null);
    const [open, setOpen] = useState(null);   // jobId whose log is expanded
    const [log, setLog] = useState(null);     // null = loading; else array of {cls,text}
    const [retention, setRetention] = useState(null);  // days; null until loaded
    const [confirming, setConfirming] = useState(false);

    const load = () => window.API.jobsHistory().then(setRows).catch(() => setRows([]));
    useEffect(() => {
      load();
      if (isAdmin) {
        window.API.jobRetentionGet()
          .then((r) => setRetention(r && typeof r.days === 'number' ? r.days : 0))
          .catch(() => setRetention(0));
      }
    }, []);

    const setRet = (days) => {
      const prev = retention;
      setRetention(days);   // optimistic
      window.API.jobRetentionSet(days).catch((e) => {
        setRetention(prev);
        window.GDStore.toast(e.message || 'could not save retention', 'err');
      });
    };

    const purgeAll = () => window.API.purgeAllJobs()
      .then((r) => { load(); window.GDStore.toast('Purged ' + (r.purged || 0) + ' job(s)', 'ok'); })
      // toast + rethrow: ConfirmModal stays open for retry when the purge fails
      .catch((e) => { window.GDStore.toast(e.message || 'purge failed', 'err'); throw e; });

    const expand = (id) => {
      if (open === id) { setOpen(null); setLog(null); return; }
      setOpen(id); setLog(null);
      // /api/jobs/{id} returns log as an array of {cls, text} (see serialize.job_detail)
      window.API.job(id)
        .then((d) => setLog(Array.isArray(d.log) ? d.log : []))
        .catch(() => setLog([{ cls: 'l-err', text: '(log unavailable)' }]));
    };
    const purge = async (id) => {
      try { await window.API.purgeJob(id); load(); }
      catch (e) { window.GDStore.toast(e.message || 'purge failed', 'err'); }
    };

    const logBody = () => {
      if (log === null) return 'Loading log…';
      if (!log.length) return '(no output)';
      return log.map((l, i) => h('div', { key: i, className: l.cls || '' }, l.text));
    };
    const tone = (s) => (s === 'done' ? 'running' : s === 'error' ? 'error' : s === 'working' ? 'working' : 'stopped');

    const renderRow = (j) => {
      const header = h('div', {
        className: 'row',
        style: { justifyContent: 'space-between', padding: '10px 14px', cursor: 'pointer' },
        onClick: () => expand(j.jobId),
      },
        h('div', { className: 'row', style: { gap: 10, minWidth: 0 } },
          h('span', { className: 'dot ' + tone(j.status) }),
          h('span', { className: 'mono', style: { fontSize: 12.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }, j.title || j.type),
          h('span', { className: 'badge ' + (j.status === 'done' ? 'running' : j.status === 'error' ? 'error' : '') }, j.status)),
        h('span', { onClick: (e) => e.stopPropagation() },
          h('button', { className: 'icon-btn', title: 'Purge permanently', onClick: () => purge(j.jobId) },
            h(Icon, { name: 'trash', size: 15 }))));
      const body = open === j.jobId
        ? h('pre', { className: 'mono', style: { fontSize: 11, padding: '8px 14px', margin: 0, maxHeight: 280, overflow: 'auto', background: 'var(--surface)' } }, logBody())
        : null;
      return h('div', { key: j.jobId, style: { borderBottom: '1px solid var(--border)' } }, header, body);
    };

    if (rows === null) return h('div', { className: 'page fadein' }, h('p', { className: 'hint' }, 'Loading…'));

    const controls = h('div', { className: 'row', style: { gap: 10, alignItems: 'center', flexShrink: 0 } },
      isAdmin && retention !== null && h('label', { className: 'row', style: { gap: 6, alignItems: 'center' } },
        h('span', { className: 'hint', style: { fontSize: 12 } }, 'Auto-prune'),
        h('select', { className: 'select', style: { width: 'auto' }, value: retention, onChange: (e) => setRet(Number(e.target.value)) },
          RETENTION_OPTS.map((o) => h('option', { key: o.v, value: o.v }, o.label)))),
      rows.length > 0 && h('button', { className: 'btn sm danger', onClick: () => setConfirming(true) },
        h(Icon, { name: 'trash', size: 14 }), 'Purge all'));

    const head = h('div', { className: 'page-head', style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 } },
      h('div', { style: { minWidth: 0 } },
        h('h1', { className: 'page-title' }, 'History'),
        h('p', { className: 'hint' }, 'Every deploy, start, stop, destroy and image sync — newest first. The activity bell is just recent notifications; dismissing there never removes anything here.')),
      controls);

    const list = rows.length === 0
      ? h('div', { className: 'card' }, h('div', { className: 'empty' },
          h('h3', null, 'No jobs yet'),
          h('p', null, 'Deploy a VM or sync an image and it shows up here automatically.')))
      : h('div', { className: 'card', style: { overflow: 'hidden' } }, rows.map(renderRow));

    return h('div', { className: 'page fadein' }, head, list,
      confirming && h(ConfirmModal, {
        title: 'Purge all history?',
        body: 'Permanently delete every finished job and its logs' + (isAdmin ? ' (all users)' : ' you own') +
              '. Running jobs are kept. This cannot be undone.',
        confirmLabel: 'Purge all',
        onConfirm: purgeAll,
        onClose: () => setConfirming(false),
      }));
  }

  window.History = History;
})();
