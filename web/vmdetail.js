/* GoblinDock — single-VM detail: live metrics, config, guest agent, serial console, log. */
(function () {
  const { useState, useEffect, useRef } = React;
  const Icon = window.Icon;
  const { OSGlyph, ConfirmModal, StatusBadge, FormModal, Field, Toggle, Menu,
          copyToClipboard, readClipboard, fmtBytes, useFetched } = window.UI;
  const h = React.createElement;
  const toast = (m, t) => window.GDStore.toast(m, t);
  // Transitional uptime-box label while a power action is in flight, until the live
  // Proxmox status catches up (so the box doesn't sit at '—' and feel dead).
  const _actLabel = (a) => ({ start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' }[a] || 'Working…');

  // Small toolbar shown above each console.
  function ConsoleBar(children) {
    return h('div', { className: 'row', style: { gap: 6, marginBottom: 8, flexWrap: 'wrap' } }, children);
  }

  const fmtUptime = (s) => {
    s = Number(s) || 0; if (!s) return '—';
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + 'd ' : '') + (d || hh ? hh + 'h ' : '') + m + 'm';
  };

  // ---------- xterm serial console ----------
  // Shared status pill for both consoles — maps connection status to a badge tone.
  function ConsoleStatusBadge({ status, label }) {
    const tone = status === 'open' ? 'running' : status === 'connecting' ? 'working' : 'error';
    return h(StatusBadge, { status: tone, label });
  }

  function VmConsole({ depId, tall }) {
    const host = useRef(null);
    const termRef = useRef(null);
    const wsRef = useRef(null);
    const [status, setStatus] = useState('connecting');
    useEffect(() => {
      if (!window.Terminal || !host.current) { setStatus('noterm'); return undefined; }
      const term = new window.Terminal({
        cursorBlink: true, fontSize: 13, fontFamily: '"JetBrains Mono", monospace', scrollback: 2000,
        theme: { background: '#0b0e14', foreground: '#cbd5e1', cursor: '#f59e0b' },
      });
      termRef.current = term;
      let fit = null;
      try { fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); } catch (e) { /* no fit */ }
      term.open(host.current);
      try { fit && fit.fit(); } catch (e) { /* */ }
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(proto + '://' + location.host + '/api/vms/' + depId + '/console', 'binary');
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;
      ws.onopen = () => { setStatus('open'); try { ws.send('1:' + term.cols + ':' + term.rows + ':'); } catch (e) {} term.focus(); };
      ws.onmessage = (e) => term.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data));
      ws.onclose = () => setStatus('closed');
      ws.onerror = () => setStatus('closed');
      const dDisp = term.onData((data) => { if (ws.readyState === 1) ws.send('0:' + new TextEncoder().encode(data).length + ':' + data); });
      const rDisp = term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send('1:' + cols + ':' + rows + ':'); });
      const refit = () => { try { fit && fit.fit(); } catch (e) {} };
      window.addEventListener('resize', refit);
      // Re-fit when the panel itself changes size (e.g. the Expand toggle), not just window.
      let ro = null;
      try { ro = new ResizeObserver(refit); ro.observe(host.current); } catch (e) { /* old browser */ }
      return () => {
        window.removeEventListener('resize', refit);
        try { ro && ro.disconnect(); } catch (e) {}
        try { dDisp.dispose(); rDisp.dispose(); } catch (e) {}
        try { ws.close(); } catch (e) {}
        try { term.dispose(); } catch (e) {}
        termRef.current = null; wsRef.current = null;
      };
    }, [depId]);
    const paste = async () => {
      const text = await readClipboard();
      if (text == null) return;
      const ws = wsRef.current;
      if (ws && ws.readyState === 1) ws.send('0:' + new TextEncoder().encode(text).length + ':' + text);
      if (termRef.current) termRef.current.focus();
    };
    const copy = () => {
      const text = termRef.current ? termRef.current.getSelection() : '';
      if (!text) { toast('Select text in the console first', 'warn'); return; }
      copyToClipboard(text);
    };
    const label = status === 'open' ? 'Connected' : status === 'connecting' ? 'Connecting…' : status === 'noterm' ? 'unavailable' : 'Disconnected';
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 9, gap: 8 } },
        h('span', { className: 'panel-title' }, 'Serial console'),
        h(ConsoleStatusBadge, { status, label }),
        h('span', { className: 'hint', style: { marginLeft: 'auto', fontSize: 11 } }, 'Press Enter if the prompt looks blank')),
      ConsoleBar([
        h('button', { key: 'c', className: 'btn ghost sm', onClick: copy, title: 'Copy selection' }, h(Icon, { name: 'copy', size: 13 }), 'Copy'),
        h('button', { key: 'p', className: 'btn ghost sm', onClick: paste, title: 'Paste clipboard into the console' }, h(Icon, { name: 'file', size: 13 }), 'Paste'),
      ]),
      h('div', { ref: host, style: { height: tall ? '72vh' : 400, background: '#0b0e14', borderRadius: 10, padding: 8, overflow: 'hidden', border: '1px solid var(--border)' } }));
  }

  // ---------- noVNC graphical console (the same console Proxmox uses) ----------
  function VncConsole({ depId, tall }) {
    const host = useRef(null);
    const rfbRef = useRef(null);
    const [status, setStatus] = useState('connecting');
    const [fitScreen, setFitScreen] = useState(true);
    useEffect(() => {
      if (!window.RFB) { setStatus('norfb'); return undefined; }
      if (!host.current) return undefined;
      let cancelled = false;
      window.API.vncProxy(depId).then((r) => {
        if (cancelled || !host.current) return;
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const url = proto + '://' + location.host + '/api/vms/' + depId + '/vnc?t=' + encodeURIComponent(r.wsToken);
        const rfb = new window.RFB(host.current, url, { credentials: { password: r.ticket } });
        rfb.scaleViewport = true;
        rfb.background = '#0b0e14';
        rfb.addEventListener('connect', () => setStatus('open'));
        rfb.addEventListener('disconnect', () => setStatus('closed'));
        rfb.addEventListener('securityfailure', () => setStatus('authfail'));
        // Remote → local copy: when the guest puts text on its clipboard, mirror it locally.
        rfb.addEventListener('clipboard', (e) => {
          try { if (e.detail && e.detail.text && navigator.clipboard) navigator.clipboard.writeText(e.detail.text); } catch (x) { /* ignore */ }
        });
        rfbRef.current = rfb;
      }).catch(() => { if (!cancelled) setStatus('err'); });
      return () => { cancelled = true; try { rfbRef.current && rfbRef.current.disconnect(); } catch (e) {} };
    }, [depId]);
    // Keep the scaling mode in sync with the Fit/Actual toggle.
    useEffect(() => { if (rfbRef.current) { try { rfbRef.current.scaleViewport = fitScreen; } catch (e) {} } }, [fitScreen]);
    const paste = async () => {
      const text = await readClipboard();
      if (text == null) return;
      const rfb = rfbRef.current;
      if (!rfb || !rfb.clipboardPasteFrom) { toast('Paste not supported by this console', 'warn'); return; }
      try { rfb.clipboardPasteFrom(text); toast('Pasted to console', 'ok'); rfb.focus && rfb.focus(); }
      catch (e) { toast('Paste failed', 'err'); }
    };
    const label = status === 'open' ? 'Connected' : status === 'connecting' ? 'Connecting…'
      : status === 'norfb' ? 'unavailable' : status === 'authfail' ? 'Auth failed' : 'Disconnected';
    return h('div', null,
      h('div', { className: 'row', style: { marginBottom: 9, gap: 8 } },
        h('span', { className: 'panel-title' }, 'Graphical console'),
        h(ConsoleStatusBadge, { status, label }),
        h('span', { className: 'hint', style: { marginLeft: 'auto', fontSize: 11 } }, 'Click the screen, then type')),
      ConsoleBar([
        h('button', { key: 'p', className: 'btn ghost sm', onClick: paste, title: 'Paste clipboard into the console' }, h(Icon, { name: 'file', size: 13 }), 'Paste'),
        h('button', { key: 'f', className: 'btn ghost sm', onClick: () => setFitScreen((v) => !v), title: 'Toggle scale-to-fit vs actual size' }, h(Icon, { name: 'width', size: 13 }), fitScreen ? 'Actual size' : 'Fit screen'),
      ]),
      h('div', { ref: host, style: { height: tall ? '78vh' : 460, background: '#0b0e14', borderRadius: 10, overflow: fitScreen ? 'hidden' : 'auto', border: '1px solid var(--border)', display: 'grid', placeItems: 'center' } }));
  }

  // ---------- deployment log (reuses the job's stored log) ----------
  function DeployLog({ jobId }) {
    const [log, setLog] = useState(null);
    useEffect(() => {
      if (!jobId) { setLog([]); return; }
      window.API.job(jobId).then((j) => setLog(j.log || [])).catch(() => setLog([]));
    }, [jobId]);
    if (log === null) return h('div', { className: 'hint', style: { padding: 16 } }, 'Loading log…');
    if (!log.length) return h('div', { className: 'hint', style: { padding: 16 } }, 'No deployment log.');
    return h('div', { className: 'logpane', style: { maxHeight: 300, fontSize: 12, borderRadius: 10, border: '1px solid var(--border-soft)' } },
      log.map((l, i) => h('div', { key: i, className: l.cls || '' }, l.text)));
  }

  // Stat card with an optional bar — distinct from UI.Meter (the bare percentage bar).
  function StatCard({ icon, label, value, pct, tone }) {
    return h('div', { className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 9 } },
      h('div', { className: 'row', style: { color: 'var(--text-dim)' } },
        h(Icon, { name: icon, size: 15 }),
        h('span', { className: 'field-label', style: { margin: 0 } }, label),
        h('span', { className: 'mono', style: { marginLeft: 'auto', fontWeight: 700, fontSize: 14, color: 'var(--text)' } }, value)),
      pct != null && h('div', { className: 'meter', style: { height: 7 } },
        h('i', { style: { width: Math.min(100, Math.max(2, pct)) + '%', background: tone || 'var(--accent)' } })));
  }

  // ---------- snapshots (Proxmox-native, per VM) ----------
  function SnapModal({ depId, running, onClose, onDone }) {
    const [name, setName] = useState('');
    const [desc, setDesc] = useState('');
    const [ram, setRam] = useState(false);
    const [busy, setBusy] = useState(false);
    const submit = async () => {
      setBusy(true);
      try {
        const r = await window.API.createSnapshot(depId, { name: name.trim(), description: desc, includeRam: ram });
        toast('Snapshot ' + r.name + ' created', 'ok');
        onDone();
      } catch (e) { toast(e.message || 'snapshot failed', 'err'); setBusy(false); }
    };
    return h(FormModal, { title: 'Take snapshot', icon: 'save', onClose, onSubmit: submit, busy, submitLabel: 'Snapshot' },
      h(Field, { label: 'Name', value: name, onChange: setName, mono: true, placeholder: 'auto (snap-YYYYMMDD-HHMMSS)',
        hint: 'Starts with a letter; letters, digits, - and _ only.' }),
      h(Field, { label: 'Description', value: desc, onChange: setDesc, placeholder: 'before upgrading…' }),
      running && h(Toggle, { label: 'Include RAM (resume exactly here after rollback)', on: ram, onChange: setRam }),
      h('p', { className: 'hint', style: { fontSize: 11.5 } },
        'Snapshots live on the Proxmox node next to the VM disk. Rolling back discards everything written since.'));
  }

  function Snapshots({ depId, running }) {
    const [bump, setBump] = useState(0);
    const [taking, setTaking] = useState(false);
    const [confirm, setConfirm] = useState(null);   // { kind: 'rollback' | 'delete', snap }
    const [startAfter, setStartAfter] = useState(true);   // "start VM after rollback" toggle
    const data = useFetched(() => window.API.vmSnapshots(depId), [depId, bump], { error: true });
    const reload = () => setBump((b) => b + 1);

    const run = (kind, snap) => async () => {
      // toast + rethrow: ConfirmModal stays open for retry on failure
      try {
        if (kind === 'rollback') {
          const r = await window.API.rollbackSnapshot(depId, snap.name, { start: startAfter });
          toast('Rolled back to ' + snap.name + (
            (r && r.started) || snap.vmstate ? ' — VM is running'
            : startAfter ? ' — VM is stopped (couldn’t auto-start)'
            : ' — VM is stopped'), 'ok');
        } else {
          await window.API.deleteSnapshot(depId, snap.name);
          toast('Snapshot ' + snap.name + ' deleted', 'ok');
        }
        reload();
        window.GDStore.refresh().catch(() => {});
      } catch (e) { toast(e.message || (kind + ' failed'), 'err'); throw e; }
    };

    let body;
    if (data === null) body = h('div', { className: 'hint', style: { fontSize: 12.5, padding: '4px 0' } }, 'Loading snapshots…');
    else if (data.error) body = h('div', { className: 'hint', style: { fontSize: 12.5, padding: '4px 0' } }, 'Snapshots unavailable (VM not provisioned or node offline).');
    else if (!(data.snapshots || []).length) body = h('div', { className: 'hint', style: { fontSize: 12.5, padding: '4px 0' } },
      'No snapshots yet. Take one before risky changes — rollback is instant.');
    else body = h('div', null, (data.snapshots || []).map((s) => h('div', {
      key: s.name, className: 'row', style: { gap: 9, padding: '7px 0', borderBottom: '1px solid var(--border-soft)' } },
        h(Icon, { name: 'save', size: 14, style: { color: 'var(--accent)', flexShrink: 0 } }),
        h('div', { style: { minWidth: 0, flex: 1 } },
          h('div', { className: 'row', style: { gap: 7 } },
            h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 12.5 } }, s.name),
            s.current && h('span', { className: 'badge accent', style: { fontSize: 10 } }, 'current'),
            s.vmstate && h('span', { className: 'badge', style: { fontSize: 10 } }, 'RAM')),
          h('div', { className: 'hint', style: { fontSize: 11 } }, s.created, s.description ? ' · ' + s.description : '')),
        h(Menu, { items: [
          { label: 'Roll back', icon: 'history', onClick: () => { setStartAfter(true); setConfirm({ kind: 'rollback', snap: s }); } },
          { sep: true },
          { label: 'Delete', icon: 'trash', danger: true, onClick: () => setConfirm({ kind: 'delete', snap: s }) },
        ] }, h('button', { className: 'icon-btn' }, h(Icon, { name: 'more', size: 15 }))))));

    return h(React.Fragment, null,
      Card('Snapshots', body,
        h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: () => setTaking(true),
          disabled: !!(data && data.error) }, h(Icon, { name: 'plus', size: 13 }), 'Take snapshot')),
      taking && h(SnapModal, { depId, running, onClose: () => setTaking(false), onDone: () => { setTaking(false); reload(); } }),
      confirm && h(ConfirmModal, {
        onClose: () => setConfirm(null),
        tone: 'danger',
        icon: confirm.kind === 'rollback' ? 'history' : 'trash',
        title: (confirm.kind === 'rollback' ? 'Roll back to ' : 'Delete ') + confirm.snap.name + '?',
        body: confirm.kind === 'rollback'
          ? h('div', null,
              h('p', { style: { color: 'var(--text-dim)', lineHeight: 1.6, marginBottom: confirm.snap.vmstate ? 0 : 12 } },
                'The VM disk reverts to this snapshot — everything written since is lost.'
                + (confirm.snap.vmstate ? ' The VM resumes from the saved RAM state.' : '')),
              !confirm.snap.vmstate && h(Toggle, {
                label: 'Start the VM after rollback',
                on: startAfter, onChange: setStartAfter }))
          : 'Removes the snapshot from the node. The VM itself is not affected.',
        confirmLabel: confirm.kind === 'rollback' ? 'Roll back' : 'Delete snapshot',
        onConfirm: run(confirm.kind, confirm.snap),
      }));
  }

  function Row({ k, v, mono, copy }) {
    return h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 12, padding: '5px 0' } },
      h('span', { className: 'hint', style: { fontSize: 12.5 } }, k),
      h('span', { className: (mono ? 'mono ' : '') + (copy ? 'copy' : ''), style: { fontSize: 12.5, fontWeight: 600, textAlign: 'right', wordBreak: 'break-all' } }, v || '—'));
  }

  function Card(title, children, extra) {
    return h('div', { className: 'card card-pad', style: { display: 'flex', flexDirection: 'column', gap: 6 } },
      h('div', { className: 'row', style: { marginBottom: 4 } }, h('span', { className: 'panel-title' }, title), extra), children);
  }

  function VmDetail({ go }) {
    const depId = window.GDStore.nav && window.GDStore.nav.depId;
    const [d, setD] = useState(null);
    const [err, setErr] = useState(null);
    const [showConsole, setShowConsole] = useState(false);
    const [conMode, setConMode] = useState('vnc');
    const [tall, setTall] = useState(false);
    const [confirm, setConfirm] = useState(false);
    const [busy, setBusy] = useState('');
    const [cred, setCred] = useState(null);
    const revealCred = async () => {
      try { setCred(await window.API.revealVmCredentials(depId)); }
      catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
    };

    const [pending, setPending] = useState('');   // power action awaiting the live status to catch up
    const load = () => window.API.vmDetail(depId).then((x) => { setD(x); setErr(null); }).catch((e) => setErr(e.message || 'failed'));
    useEffect(() => {
      if (!depId) { go('dashboard'); return undefined; }
      setCred(null);
      load();
      const id = setInterval(load, 5000);
      return () => clearInterval(id);
    }, [depId]);
    // Clear the transitional label once the live status reaches the action's target.
    useEffect(() => {
      if (!pending) return;
      const st = (d && d.live && d.live.status) || (d && d.status);
      if (pending === 'stop' ? st !== 'running' : st === 'running') setPending('');
    }, [d, pending]);

    const act = async (action) => {
      setBusy(action);
      setPending(action);
      // safety net: never let the label stick forever if the VM never reaches the target
      setTimeout(() => setPending((p) => (p === action ? '' : p)), 90000);
      try {
        await window.GDStore.vmAction(depId, action);
        setTimeout(load, 800); // detail view also re-pulls its own richer data
      } catch (e) { setPending(''); /* store already toasted + reverted */ }
      finally { setBusy(''); }
    };
    const destroy = async () => {
      try { const r = await window.API.vmDestroy(depId); go('job', { jobId: r.jobId }); }
      catch (e) { window.GDStore.toast(e.message || 'failed', 'err'); }
    };

    if (err && !d) return h('div', { className: 'page fadein' },
      h('div', { className: 'card' }, h('div', { className: 'empty' },
        h('div', { className: 'glyph' }, h(Icon, { name: 'warn', size: 28 })),
        h('h3', null, 'Could not load VM'), h('p', null, err),
        h('button', { className: 'btn primary', onClick: () => go('dashboard') }, 'Back to VMs'))));
    if (!d) return h('div', { className: 'page fadein' }, h('div', { className: 'card', style: { padding: 40, textAlign: 'center', color: 'var(--text-faint)' } }, 'Loading VM…'));

    const live = d.live || {};
    const cfg = d.config || {};
    const running = live.status === 'running';
    const statusTone = running ? 'running' : (d.status === 'working') ? 'working' : (d.status === 'error') ? 'error' : 'stopped';
    const memPct = live.memMax ? Math.round(live.memUsed / live.memMax * 100) : null;
    const diskPct = live.diskMax ? Math.round(live.diskUsed / live.diskMax * 100) : null;

    return h('div', { className: 'page fadein', style: { maxWidth: 1180 } },
      // header
      h('div', { className: 'page-head', style: { marginBottom: 18 } },
        h('button', { className: 'btn ghost sm', onClick: () => go('dashboard'), style: { marginRight: 4 } }, h(Icon, { name: 'chevronL', size: 16 }), 'VMs'),
        h('div', null,
          h('div', { className: 'row', style: { gap: 11 } },
            h(OSGlyph, { os: d.os, size: 26 }),
            h('h1', { className: 'page-title' }, d.name),
            h('span', { className: 'badge ' + statusTone }, h('span', { className: 'dot ' + statusTone }),
              d.status === 'working' ? 'Working' : d.status === 'error' ? 'Error' : running ? 'Running' : 'Stopped')),
          h('div', { className: 'page-sub mono' }, 'vmid ', d.vmid || '—', ' · ', d.node, d.ip ? ' · ' + d.ip : '')),
        h('div', { className: 'spacer' }),
        h('div', { className: 'row', style: { gap: 8 } },
          running
            ? h('button', { className: 'btn sm', onClick: () => act('stop'), disabled: busy }, h(Icon, { name: 'stop', size: 14 }), 'Stop')
            : h('button', { className: 'btn sm', onClick: () => act('start'), disabled: busy || d.status === 'working' }, h(Icon, { name: 'play', size: 14 }), 'Start'),
          h('button', { className: 'btn sm', onClick: () => act('restart'), disabled: busy || !running }, h(Icon, { name: 'restart', size: 14 }), 'Restart'),
          h('button', { className: 'btn primary sm', onClick: () => setShowConsole((s) => !s), disabled: !d.consoleReady, title: d.consoleReady ? '' : 'Start the VM to use the console' }, h(Icon, { name: 'terminal', size: 14 }), showConsole ? 'Hide console' : 'Console'),
          h('button', { className: 'btn danger sm', onClick: () => setConfirm(true) }, h(Icon, { name: 'trash', size: 14 }))),
      ),

      // live metrics
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 14, marginBottom: 16 } },
        h(StatCard, { icon: 'cpu', label: 'CPU', value: running ? (live.cpuPct + ' %') : '—', pct: running ? live.cpuPct : null }),
        h(StatCard, { icon: 'ram', label: 'Memory', value: running ? (fmtBytes(live.memUsed) + ' / ' + fmtBytes(live.memMax)) : '—', pct: memPct, tone: 'var(--info)' }),
        h(StatCard, { icon: 'disk', label: 'Disk', value: live.diskMax ? (fmtBytes(live.diskUsed) + ' / ' + fmtBytes(live.diskMax)) : '—', pct: diskPct, tone: 'var(--ok)' }),
        h(StatCard, { icon: 'clock', label: 'Uptime',
          value: running ? fmtUptime(live.uptime)
            : pending ? h('span', { style: { color: 'var(--accent)' } },
                h('span', { className: 'dot working', style: { marginRight: 5 } }), _actLabel(pending))
            : '—' })),

      // console (toggle between the Proxmox graphical console and the serial console)
      showConsole && h('div', { className: 'card card-pad', style: { marginBottom: 16 } },
        h('div', { className: 'row', style: { marginBottom: 12 } },
          h('div', { className: 'seg' },
            h('button', { className: conMode === 'vnc' ? 'active' : '', onClick: () => setConMode('vnc') }, h(Icon, { name: 'server', size: 14 }), 'Graphical'),
            h('button', { className: conMode === 'serial' ? 'active' : '', onClick: () => setConMode('serial') }, h(Icon, { name: 'terminal', size: 14 }), 'Serial')),
          h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: () => setTall((t) => !t), title: 'Toggle console size' },
            h(Icon, { name: tall ? 'collapse' : 'width', size: 14 }), tall ? 'Compact' : 'Expand')),
        conMode === 'vnc' ? h(VncConsole, { key: 'vnc', depId, tall }) : h(VmConsole, { key: 'serial', depId, tall })),

      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, alignItems: 'start' } },
        // left column
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 16 } },
          Card('Overview', h('div', null,
            h(Row, { k: 'Base image', v: d.baseImage || '—' }),
            h(Row, { k: 'Template', v: d.template || 'none' }),
            h(Row, { k: 'Target', v: d.connection + ' · ' + d.node }),
            h(Row, { k: 'IP address', v: d.ip, mono: true, copy: true }),
            h(Row, { k: 'MAC', v: d.mac || (cfg.net0 || '').match(/[0-9A-Fa-f:]{17}/)?.[0], mono: true }),
            h(Row, { k: 'Owner', v: d.owner }),
            h(Row, { k: 'Created', v: d.created }),
            h(Row, { k: 'Tags', v: d.tags || '—' }))),
          Card('Configuration', h('div', null,
            h(Row, { k: 'vCPU (cores)', v: cfg.cores || d.reqCpu, mono: true }),
            h(Row, { k: 'Memory', v: (cfg.memoryMb ? (Math.round(cfg.memoryMb / 1024 * 10) / 10 + ' GB') : d.reqRam + ' GB'), mono: true }),
            h(Row, { k: 'Disk', v: (cfg.scsi0 || '').match(/size=(\S+)/)?.[1] || (d.reqDisk + 'G'), mono: true }),
            h(Row, { k: 'OS type', v: cfg.ostype, mono: true }),
            h(Row, { k: 'Network', v: (cfg.net0 || '—').split(',')[0], mono: true }),
            h(Row, { k: 'Guest agent', v: live.agentRunning ? 'running' : (cfg.agent ? 'enabled' : 'off'), mono: true }),
            h(Row, { k: 'Serial console', v: cfg.serial0 ? 'enabled' : 'not set', mono: true }))),
          d.hasRootPassword && Card('Access', h('div', null,
            h(Row, { k: 'Console user', v: d.credUser || 'root', mono: true }),
            h('div', { className: 'row', style: { justifyContent: 'space-between', gap: 12, padding: '5px 0' } },
              h('span', { className: 'hint', style: { fontSize: 12.5 } }, 'Password'),
              h('span', { className: 'row', style: { gap: 8, alignItems: 'center' } },
                cred
                  ? h('span', { className: 'mono copy', style: { fontSize: 12.5, fontWeight: 600 }, onClick: () => copyToClipboard(cred.password, 'Password copied') }, cred.password)
                  : h('span', { className: 'mono', style: { fontSize: 13, letterSpacing: 2 } }, '••••••••'),
                h('button', { className: 'btn ghost sm', onClick: cred ? () => setCred(null) : revealCred },
                  h(Icon, { name: cred ? 'eyeOff' : 'eye', size: 14 }), cred ? 'Hide' : 'Show'))),
            cred && h('p', { className: 'hint', style: { fontSize: 11, marginTop: 2 } },
              'Console login (noVNC / serial). Click the value to copy.')))),
        // right column
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 16 } },
          d.agent
            ? Card('Guest agent', h('div', null,
                h(Row, { k: 'Hostname', v: (d.agent.os && (d.agent.os['pretty-name'] || d.agent.os.name)) || '—' }),
                h(Row, { k: 'Kernel', v: (d.agent.os && d.agent.os['kernel-release']) || '—', mono: true }),
                h('div', { className: 'divider' }),
                h('div', { className: 'panel-title', style: { marginBottom: 6 } }, 'Network interfaces'),
                (d.agent.interfaces || []).length === 0
                  ? h('div', { className: 'hint', style: { fontSize: 12 } }, 'none reported')
                  : (d.agent.interfaces || []).map((ifc, i) => h('div', { key: i, style: { marginBottom: 8 } },
                      h('div', { className: 'row', style: { gap: 7 } }, h('span', { className: 'mono', style: { fontWeight: 600, fontSize: 12.5 } }, ifc.name), h('span', { className: 'hint mono', style: { fontSize: 10.5 } }, ifc.mac)),
                      h('div', { className: 'copy mono', style: { fontSize: 11.5 } }, (ifc.ips || []).join(', ') || '—')))))
            : Card('Guest agent', h('div', { className: 'hint', style: { fontSize: 12.5, padding: '4px 0' } },
                running ? 'Waiting for qemu-guest-agent… (installed by GoblinDock on first boot)' : 'Start the VM to read guest info.')),
          h(Snapshots, { depId, running }),
          Card('Deployment log', h(DeployLog, { jobId: d.jobId }),
            d.jobId && h('button', { className: 'btn ghost sm', style: { marginLeft: 'auto' }, onClick: () => go('job', { jobId: d.jobId }) }, 'Open full log')))),

      confirm && h(ConfirmModal, {
        onClose: () => setConfirm(false), tone: 'danger', icon: 'trash',
        title: 'Delete ' + d.name + '?',
        body: 'This destroys the VM and its disk on ' + d.node + '. The IP returns to the pool. This cannot be undone.',
        confirmLabel: 'Delete VM', onConfirm: destroy,
      }));
  }

  window.VmDetail = VmDetail;
})();
