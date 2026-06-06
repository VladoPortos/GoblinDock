/* GoblinDock — Job Progress (deploy / build / rebuild), live via SSE. */
(function () {
  const { useState, useEffect, useRef } = React;
  const Icon = window.Icon;
  const GD = window.GD;
  const h = React.createElement;

  const STEP_ICON = { done: 'check', running: 'clock', pending: 'box', failed: 'x', skipped: 'skip' };
  const STEP_TONE = { done: 'var(--ok)', running: 'var(--warn)', pending: 'var(--text-faint)', failed: 'var(--err)', skipped: 'var(--text-faint)' };

  function ChecklistTreatment({ steps }) {
    if (!steps.length) {
      return h('div', { className: 'card', style: { padding: 28, textAlign: 'center', color: 'var(--text-faint)' } },
        h('span', { className: 'mono', style: { fontSize: 12.5 } }, 'Queued — waiting for the worker…'));
    }
    return h('div', { className: 'card', style: { overflow: 'hidden' } },
      steps.map((s, i) => h('div', { key: i, style: { borderBottom: i < steps.length - 1 ? '1px solid var(--border-soft)' : 'none' } },
        h('div', { className: 'step-row', style: { background: s.state === 'running' ? 'var(--warn-ghost)' : 'transparent', cursor: 'default' } },
          h('span', { className: 'step-ico ' + s.state, style: { color: STEP_TONE[s.state] } },
            s.state === 'running' ? h('span', { className: 'spin' }) : h(Icon, { name: STEP_ICON[s.state] || 'box', size: 13, sw: 2.6 })),
          h('span', { className: 'mono', style: { fontSize: 12.5, fontWeight: s.state === 'running' ? 700 : 500, color: s.state === 'pending' ? 'var(--text-faint)' : 'var(--text)' } }, s.name),
          h('span', { className: 'mono', style: { marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' } }, s.dur || '')))));
  }

  function TimelineTreatment({ steps }) {
    return h('div', { className: 'card card-pad' },
      h('div', { className: 'timeline' },
        steps.map((s, i) => h('div', { key: i, className: 'tl-node ' + s.state },
          h('div', { className: 'tl-dot', style: { color: STEP_TONE[s.state], borderColor: STEP_TONE[s.state] } },
            s.state === 'running' ? h('span', { className: 'spin' }) : h(Icon, { name: STEP_ICON[s.state] || 'box', size: 12, sw: 2.8 })),
          i < steps.length - 1 && h('div', { className: 'tl-line', style: { background: s.state === 'done' ? 'var(--ok)' : 'var(--border)' } }),
          h('div', { className: 'tl-body' },
            h('div', { className: 'mono', style: { fontSize: 12.5, fontWeight: s.state === 'running' ? 700 : 500, color: s.state === 'pending' ? 'var(--text-faint)' : 'var(--text)' } }, s.name),
            s.state === 'running' && h('div', { className: 'hint mono', style: { fontSize: 11, marginTop: 2 } }, 'in progress…'),
            s.dur && h('div', { className: 'hint mono', style: { fontSize: 11, marginTop: 2 } }, 'took ', s.dur))))));
  }

  function LogPane({ log, wrap, setWrap, live }) {
    const ref = useRef(null);
    const [filter, setFilter] = useState('');
    // Client-side filter over the already-loaded (capped) log — no server round-trip,
    // no change to the SSE stream. Copy/Download still act on the FULL log.
    const shown = filter ? log.filter(l => (l.text || '').toLowerCase().includes(filter.toLowerCase())) : log;
    useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [shown.length]);
    const copy = () => navigator.clipboard && navigator.clipboard.writeText(log.map(l => l.text).join('\n'));
    const download = () => {
      const blob = new Blob([log.map(l => l.text).join('\n')], { type: 'text/plain' });
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'goblindock-job.log'; a.click();
    };
    return h('div', { className: 'card', style: { display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' } },
      h('div', { className: 'row', style: { padding: '11px 14px', borderBottom: '1px solid var(--border-soft)', gap: 8 } },
        h(Icon, { name: 'terminal', size: 15, style: { color: 'var(--text-faint)' } }),
        h('span', { className: 'panel-title' }, 'Live log'),
        live && h('span', { className: 'dot working', style: { marginLeft: 2 } }),
        filter && h('span', { className: 'hint mono', style: { fontSize: 11 } }, shown.length, '/', log.length),
        h('div', { className: 'search', style: { marginLeft: 'auto', flex: '0 1 190px', minWidth: 110 } },
          h(Icon, { name: 'search', size: 14 }),
          h('input', { placeholder: 'Filter log…', value: filter, onChange: (e) => setFilter(e.target.value) })),
        h('div', { className: 'row', style: { gap: 4 } },
          h('button', { className: 'btn ghost sm', onClick: () => setWrap(w => !w) }, wrap ? 'No wrap' : 'Wrap'),
          h('button', { className: 'icon-btn', title: 'Copy', onClick: copy }, h(Icon, { name: 'copy', size: 15 })),
          h('button', { className: 'icon-btn', title: 'Download', onClick: download }, h(Icon, { name: 'download', size: 15 })))),
      h('div', { ref, className: 'logpane', style: { flex: 1, border: 'none', borderRadius: 0, whiteSpace: wrap ? 'pre-wrap' : 'pre', minHeight: 0 } },
        shown.length === 0 ? h('div', { className: 'l-dim' }, filter ? ('no lines match “' + filter + '”') : 'waiting for output…')
          : shown.map((l, i) => h('div', { key: i, className: l.cls || '' }, l.text)),
        live && !filter && h('div', { className: 'l-acc', style: { display: 'inline-block', width: 8, height: 15, background: 'var(--accent)', verticalAlign: 'middle', marginTop: 2 } })));
  }

  function phaseIndex(job) {
    if (job.rawStatus === 'succeeded') return job.phases.length;
    const m = (job.phase || '').match(/Phase\s+(\d+)\s+of\s+(\d+)/i);
    if (m) return parseInt(m[1], 10) - 1;
    return Math.min(job.phases.length - 1, Math.floor((job.pct / 100) * job.phases.length));
  }

  function JobProgress({ go }) {
    const jobId = (window.GDStore.nav && window.GDStore.nav.jobId) || GD._jobId
      || ((GD.JOBS || []).find(j => j.status === 'working') || {}).jobId
      || ((GD.JOBS || [])[0] || {}).jobId;
    const [job, setJob] = useState(null);
    const [treatment, setTreatment] = useState('checklist');
    const [wrap, setWrap] = useState(true);
    const [showLog, setShowLog] = useState(true);
    const esRef = useRef(null);

    useEffect(() => {
      if (!jobId) return undefined;
      let closed = false;
      window.API.job(jobId).then(d => { if (!closed) setJob(d); }).catch(() => {});
      const es = new EventSource(`/api/jobs/${jobId}/stream`);
      esRef.current = es;
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          // The stream sends the full `log` only on the first frame; later frames carry
          // just `newLogs` deltas. Keep the accumulated log and append the delta so each
          // tick is cheap (no O(N^2) re-send of the whole growing log).
          setJob(prev => {
            let log = (d.log != null) ? d.log : ((prev && prev.log) || []);
            if (d.newLogs && d.newLogs.length) log = log.concat(d.newLogs);
            return Object.assign({}, d, { log });
          });
        } catch (x) {}
      };
      es.addEventListener('done', () => { es.close(); window.GDStore.refresh().catch(() => {}); });
      es.onerror = () => { /* auto-reconnect or closed */ };
      return () => { closed = true; es.close(); };
    }, [jobId]);

    if (!jobId) {
      return h('div', { className: 'page fadein' },
        h('div', { className: 'card' }, h('div', { className: 'empty' },
          h('div', { className: 'glyph' }, h(Icon, { name: 'activity', size: 28 })),
          h('h3', null, 'No active job'),
          h('p', null, 'Deploy a VM or build an image to see live progress here.'),
          h('button', { className: 'btn primary', onClick: () => go('dashboard') }, 'Back to VMs'))));
    }
    if (!job) {
      return h('div', { className: 'page fadein' }, h('div', { className: 'card', style: { padding: 40, textAlign: 'center', color: 'var(--text-faint)' } }, 'Loading job…'));
    }

    const steps = job.steps || [];
    const done = steps.filter(s => s.state === 'done' || s.state === 'skipped').length;
    const total = steps.length || 1;
    const pct = job.pct;
    const live = job.rawStatus === 'running' || job.rawStatus === 'queued';
    const statusBadge = job.status; // working | done | error
    const pIdx = phaseIndex(job);

    return h('div', { className: 'page fadein', style: { maxWidth: 1180 } },
      h('div', { className: 'page-head', style: { marginBottom: 18 } },
        h('button', { className: 'btn ghost sm', onClick: () => go('dashboard'), style: { marginRight: 4 } },
          h(Icon, { name: 'chevronL', size: 16 }), 'VMs'),
        h('div', null,
          h('div', { className: 'row', style: { gap: 10 } },
            h('h1', { className: 'page-title' }, job.title),
            h('span', { className: 'badge ' + statusBadge },
              h('span', { className: 'dot ' + (statusBadge === 'done' ? 'running' : statusBadge === 'error' ? 'error' : 'working') }),
              statusBadge === 'done' ? 'Done' : statusBadge === 'error' ? 'Failed' : 'Working')),
          h('div', { className: 'page-sub mono' }, job.type)),
        h('div', { className: 'spacer' }),
        h('div', { className: 'row', style: { gap: 8 } },
          h('span', { className: 'mono', style: { color: 'var(--text-dim)', fontSize: 13 } },
            h(Icon, { name: 'clock', size: 14, style: { verticalAlign: '-2px', marginRight: 5 } }), job.elapsed),
          live && h('button', { className: 'btn danger', onClick: async () => { try { await window.API.cancelJob(jobId); window.GDStore.toast('Cancel requested', 'warn'); } catch (e) { window.GDStore.toast(e.message || 'cancel failed', 'err'); } } },
            h(Icon, { name: 'cancel', size: 15 }), 'Cancel'),
          !live && h('button', { className: 'btn primary', onClick: () => go(job.type === 'image_build' ? 'golden' : 'dashboard') },
            h(Icon, { name: 'arrowRight', size: 15 }), job.type === 'image_build' ? 'View golden images' : 'Go to VMs'))),

      job.status === 'error' && job.error && h('div', { className: 'card', style: { padding: 14, marginBottom: 16, background: 'var(--err-ghost)', borderColor: 'transparent', display: 'flex', gap: 10, alignItems: 'center' } },
        h(Icon, { name: 'warn', size: 17, style: { color: 'var(--err)', flexShrink: 0 } }),
        h('span', { className: 'mono', style: { fontSize: 12.5, color: 'var(--err)' } }, job.error)),

      h('div', { className: 'card card-pad', style: { marginBottom: 16 } },
        h('div', { className: 'row', style: { marginBottom: 10 } },
          h('span', { className: 'mono', style: { fontWeight: 700, fontSize: 13 } }, job.phase),
          h('div', { style: { marginLeft: 'auto', display: 'flex', alignItems: 'baseline', gap: 8 } },
            h('span', { className: 'mono', style: { fontSize: 26, fontWeight: 800, color: statusBadge === 'error' ? 'var(--err)' : 'var(--accent)', letterSpacing: '-0.03em' } }, pct, '%'),
            h('span', { className: 'hint mono' }, done, ' / ', total, ' tasks'))),
        h('div', { className: 'meter' + (statusBadge === 'error' ? ' err' : statusBadge === 'done' ? ' ok' : ''), style: { height: 9 } },
          h('i', { style: { width: pct + '%', background: statusBadge === 'working' ? 'linear-gradient(90deg, var(--accent-lo), var(--accent-hi))' : undefined } })),
        h('div', { className: 'phase-row' },
          (job.phases || []).map((p, i) => h('div', { key: i, className: 'phase' + (i < pIdx ? ' done' : i === pIdx && live ? ' active' : '') },
            h('span', { className: 'dot ' + (i < pIdx ? 'running' : i === pIdx ? (live ? 'working' : 'running') : 'stopped') }), p)))),

      h('div', { className: 'row', style: { marginBottom: 14, gap: 10 } },
        h('span', { className: 'panel-title' }, 'Steps'),
        h('div', { className: 'seg', style: { marginLeft: 8 } },
          h('button', { className: treatment === 'checklist' ? 'active' : '', onClick: () => setTreatment('checklist') }, h(Icon, { name: 'dashboard', size: 14 }), 'Checklist'),
          h('button', { className: treatment === 'timeline' ? 'active' : '', onClick: () => setTreatment('timeline') }, h(Icon, { name: 'activity', size: 14 }), 'Timeline')),
        h('div', { style: { marginLeft: 'auto' } },
          h('button', { className: 'btn sm', onClick: () => setShowLog(s => !s) }, h(Icon, { name: 'terminal', size: 14 }), showLog ? 'Hide log' : 'Show log'))),

      h('div', { style: { display: 'grid', gridTemplateColumns: showLog ? '1fr 1fr' : '1fr', gap: 16, alignItems: 'stretch', minHeight: 420 } },
        h('div', null, treatment === 'checklist' ? h(ChecklistTreatment, { steps }) : h(TimelineTreatment, { steps })),
        showLog && h(LogPane, { log: job.log || [], wrap, setWrap, live })));
  }

  window.JobProgress = JobProgress;
})();
