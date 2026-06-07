/* GoblinDock — shared UI primitives */
(function () {
  const { useState } = React;
  const Icon = window.Icon;
  const GD = window.GD;

  function OSGlyph({ os, size = 18 }) {
    const c = GD.OS_COLORS[os] || GD.OS_COLORS.generic;
    const letter = (GD.OS_LABEL[os] || 'L')[0];
    return React.createElement('span', {
      style: {
        width: size, height: size, borderRadius: 6, flexShrink: 0,
        display: 'grid', placeItems: 'center',
        fontFamily: 'JetBrains Mono, monospace', fontWeight: 700,
        fontSize: size * 0.5, color: '#fff',
        background: c, boxShadow: `0 1px 4px ${c}55`,
      }
    }, letter);
  }

  const STATUS_LABEL = { running:'Running', stopped:'Stopped', working:'Working', error:'Error', unknown:'Unknown' };
  function StatusBadge({ status, label }) {
    return React.createElement('span', { className: 'badge ' + status },
      React.createElement('span', { className: 'dot ' + status }),
      label || STATUS_LABEL[status] || status
    );
  }

  function CopyField({ value, mono = true }) {
    const [copied, setCopied] = useState(false);
    const doCopy = (e) => {
      e.stopPropagation();
      const text = String(value == null ? '' : value);
      const ok = () => { setCopied(true); setTimeout(() => setCopied(false), 1100); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok).catch(() => {});
      } else {
        try {
          const ta = document.createElement('textarea');
          ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select(); document.execCommand('copy');
          document.body.removeChild(ta); ok();
        } catch (x) { /* ignore */ }
      }
    };
    return React.createElement('span', {
      className: 'copy' + (mono ? ' mono' : ''), onClick: doCopy, title: 'Copy',
    }, value, React.createElement(Icon, { name: copied ? 'check' : 'copy' }));
  }

  function Meter({ value, tone }) {
    const t = tone || (value > 80 ? 'err' : value > 60 ? 'warn' : 'ok');
    return React.createElement('div', { className: 'meter ' + t },
      React.createElement('i', { style: { width: Math.max(2, value) + '%' } }));
  }

  function Modal({ children, onClose, width }) {
    const ref = React.useRef(null);
    React.useEffect(() => {
      const onKey = (e) => { if (e.key === 'Escape' && onClose) onClose(); };
      document.addEventListener('keydown', onKey);
      const node = ref.current && ref.current.querySelector('input,select,textarea,button');
      if (node) setTimeout(() => { try { node.focus(); } catch (x) {} }, 30);
      return () => document.removeEventListener('keydown', onKey);
    }, []);
    return React.createElement('div', { className: 'overlay', onMouseDown: onClose },
      React.createElement('div', {
        ref, className: 'modal', role: 'dialog', 'aria-modal': 'true',
        style: width ? { width } : null, onMouseDown: (e) => e.stopPropagation(),
      }, children));
  }

  // ---- reusable form fields ----
  function Field({ label, value, onChange, mono, type, placeholder, hint }) {
    return React.createElement('div', null,
      label && React.createElement('label', { className: 'field-label' }, label),
      React.createElement('input', {
        className: 'input' + (mono ? ' mono' : ''), type: type || 'text', value: value == null ? '' : value,
        placeholder: placeholder || '', onChange: (e) => onChange && onChange(e.target.value),
      }),
      hint && React.createElement('div', { className: 'hint', style: { fontSize: 11, marginTop: 4 } }, hint));
  }
  function TextArea({ label, value, onChange, rows, mono }) {
    return React.createElement('div', null,
      label && React.createElement('label', { className: 'field-label' }, label),
      React.createElement('textarea', {
        className: 'input' + (mono ? ' mono' : ''), value: value == null ? '' : value,
        style: { height: (rows || 4) * 22, padding: 10, resize: 'vertical', lineHeight: 1.5 },
        onChange: (e) => onChange && onChange(e.target.value),
      }));
  }
  function SelectField({ label, value, onChange, options }) {
    return React.createElement('div', null,
      label && React.createElement('label', { className: 'field-label' }, label),
      React.createElement('select', { className: 'select', value: value, onChange: (e) => onChange && onChange(e.target.value) },
        options.map((o) => {
          const val = typeof o === 'object' ? o.value : o;
          const lbl = typeof o === 'object' ? o.label : o;
          return React.createElement('option', { key: String(val), value: val }, lbl);
        })));
  }
  function Toggle({ label, on, onChange }) {
    return React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', cursor: 'pointer' }, onClick: () => onChange && onChange(!on) },
      React.createElement('span', { style: { fontSize: 13, color: 'var(--text-dim)' } }, label),
      React.createElement('button', { type: 'button', className: 'toggle' + (on ? ' on' : ''), 'aria-pressed': !!on }));
  }
  function TagInput({ label, tags, onChange }) {
    const [draft, setDraft] = useState('');
    const list = tags || [];
    const add = () => { const v = draft.trim(); if (v && !list.includes(v)) onChange([...list, v]); setDraft(''); };
    return React.createElement('div', null,
      label && React.createElement('label', { className: 'field-label' }, label),
      React.createElement('div', { className: 'tag-input' },
        list.map((t) => React.createElement('span', { key: t, className: 'tag mono' }, t,
          React.createElement(Icon, { name: 'x', size: 11, onClick: () => onChange(list.filter((x) => x !== t)) }))),
        React.createElement('input', {
          placeholder: '+ add', className: 'mono', value: draft,
          onChange: (e) => setDraft(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); } },
          onBlur: add,
        })));
  }
  function FormModal({ title, icon, onClose, onSubmit, submitLabel, busy, children, width, danger }) {
    return React.createElement(Modal, { onClose, width: width || 'min(520px, 94vw)' },
      React.createElement('div', { className: 'modal-head' },
        icon && React.createElement(Icon, { name: icon, size: 16, style: { color: danger ? 'var(--err)' : 'var(--accent)' } }),
        React.createElement('span', { className: 'mono', style: { fontWeight: 700, fontSize: 14 } }, title),
        React.createElement('button', { className: 'icon-btn', style: { marginLeft: 'auto' }, onClick: onClose, 'aria-label': 'Close' }, React.createElement(Icon, { name: 'x', size: 16 }))),
      React.createElement('div', { className: 'modal-body', style: { display: 'flex', flexDirection: 'column', gap: 14 } }, children),
      React.createElement('div', { className: 'modal-foot' },
        React.createElement('button', { className: 'btn', onClick: onClose }, 'Cancel'),
        React.createElement('button', { className: 'btn ' + (danger ? 'danger' : 'primary'), onClick: onSubmit, disabled: busy }, busy ? 'Working…' : (submitLabel || 'Save'))));
  }

  function ConfirmModal({ icon = 'warn', tone = 'danger', title, body, confirmLabel = 'Confirm', onConfirm, onClose }) {
    const [busy, setBusy] = useState(false);
    const confirm = async () => {
      setBusy(true);
      try { await Promise.resolve(onConfirm && onConfirm()); onClose(); }
      catch (e) { setBusy(false); }  // handler shows its own toast; keep modal for retry
    };
    return React.createElement(Modal, { onClose: busy ? () => {} : onClose },
      React.createElement('div', { className: 'modal-head' },
        React.createElement('span', { style: {
          width: 34, height: 34, borderRadius: 9, display: 'grid', placeItems: 'center', flexShrink: 0,
          background: tone === 'danger' ? 'var(--err-ghost)' : 'var(--accent-ghost)',
          color: tone === 'danger' ? 'var(--err)' : 'var(--accent)',
        } }, React.createElement(Icon, { name: icon, size: 18 })),
        React.createElement('div', { className: 'mono', style: { fontWeight: 700, fontSize: 15 } }, title)
      ),
      React.createElement('div', { className: 'modal-body' },
        React.createElement('p', { style: { color: 'var(--text-dim)', lineHeight: 1.6 } }, body)),
      React.createElement('div', { className: 'modal-foot' },
        React.createElement('button', { className: 'btn', onClick: onClose, disabled: busy }, 'Cancel'),
        React.createElement('button', { className: 'btn ' + (tone === 'danger' ? 'danger' : 'primary'),
          onClick: confirm, disabled: busy }, busy ? 'Working…' : confirmLabel))
    );
  }

  // small dropdown menu — rendered in a portal so it is NEVER clipped by a card's
  // overflow:hidden, and positioned with fixed coords from the trigger's rect.
  function Menu({ items, children, align = 'right' }) {
    const [open, setOpen] = useState(false);
    const [pos, setPos] = useState(null);
    const ref = React.useRef(null);
    const openMenu = (e) => {
      e.stopPropagation();
      const r = ref.current.getBoundingClientRect();
      setPos({ top: Math.round(r.bottom + 6), left: Math.round(r.left), right: Math.round(window.innerWidth - r.right) });
      setOpen(true);
    };
    React.useEffect(() => {
      if (!open) return undefined;
      const close = () => setOpen(false);
      const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
      window.addEventListener('scroll', close, true);
      window.addEventListener('resize', close);
      document.addEventListener('keydown', onKey);
      return () => {
        window.removeEventListener('scroll', close, true);
        window.removeEventListener('resize', close);
        document.removeEventListener('keydown', onKey);
      };
    }, [open]);

    const dropdown = open && pos && ReactDOM.createPortal(
      React.createElement(React.Fragment, null,
        React.createElement('div', { style: { position: 'fixed', inset: 0, zIndex: 200 }, onMouseDown: (e) => { e.stopPropagation(); setOpen(false); } }),
        React.createElement('div', {
          onMouseDown: (e) => e.stopPropagation(),
          style: {
            position: 'fixed', top: pos.top, zIndex: 201,
            ...(align === 'right' ? { right: pos.right } : { left: pos.left }),
            minWidth: 172, padding: 6, borderRadius: 11,
            background: 'var(--surface)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-lg)',
          },
        }, items.map((it, i) => it.sep
          ? React.createElement('div', { key: i, className: 'divider', style: { margin: '5px 4px' } })
          : React.createElement('button', {
              key: i, className: 'menu-item' + (it.danger ? ' danger' : ''),
              onClick: (e) => { e.stopPropagation(); setOpen(false); it.onClick && it.onClick(); },
              style: {
                display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left',
                padding: '8px 9px', borderRadius: 7, border: 'none', cursor: 'pointer',
                background: 'transparent', color: it.danger ? 'var(--err)' : 'var(--text-dim)',
                fontFamily: 'JetBrains Mono, monospace', fontSize: 12.5, fontWeight: 500,
              },
              onMouseEnter: (e) => e.currentTarget.style.background = 'var(--surface-2)',
              onMouseLeave: (e) => e.currentTarget.style.background = 'transparent',
            }, it.icon && React.createElement(Icon, { name: it.icon, size: 15 }), it.label)
        )),
      ), document.body);

    return React.createElement('div', { ref, style: { display: 'inline-flex' }, onClick: openMenu },
      children, dropdown);
  }

  // ---- ask-on-deploy helpers (shared by the quick-deploy modal + deploy page) ----
  function collectAsks(tpl) {
    const GDx = window.GD;
    const asks = [];
    (tpl.recipe || []).forEach((sec, si) => (sec.blocks || []).forEach((b, bi) => {
      (Array.isArray(b.ask) ? b.ask : []).forEach((n) => {
        const pal = (GDx.PALETTE || []).find((p) => p.id === b.ref) || {};
        const field = (pal.schema || []).find((x) => x.name === n);
        if (!field) return;  // ask references an input the block no longer has — no prompt
        asks.push({ addr: si + '.' + bi, blockName: b.name || pal.name || b.ref, field, def: (b.inputs || {})[n] });
      });
    }));
    return asks;
  }
  function initAskAnswers(asks) {
    const out = {};
    asks.forEach((a) => {
      out[a.addr] = out[a.addr] || {};
      out[a.addr][a.field.name] = a.def != null && a.def !== '' ? a.def
        : a.field.type === 'bool' ? false : (a.field.type === 'tags' || a.field.type === 'list') ? [] : '';
    });
    return out;
  }
  function asksMissing(asks, answers) {
    return asks.filter((a) => {
      const t = a.field.type || 'text';
      if (t === 'bool' || t === 'tags' || t === 'list' || t === 'select') return false;
      const v = (answers[a.addr] || {})[a.field.name];
      return !(v && String(v).trim());
    }).map((a) => a.field.label || a.field.name);
  }
  function AskInputs({ asks, answers, setAnswers }) {
    const SchemaField = window.SchemaField;  // exported by builder.js (load order safe: render-time)
    if (!asks.length || !SchemaField) return null;
    return React.createElement('div', { style: { display: 'flex', flexDirection: 'column', gap: 12 } },
      asks.map((a) => React.createElement('div', { key: a.addr + ':' + a.field.name },
        React.createElement('div', { className: 'hint mono', style: { fontSize: 10.5, marginBottom: 3 } }, a.blockName),
        React.createElement(SchemaField, {
          field: a.field, value: (answers[a.addr] || {})[a.field.name],
          onChange: (v) => setAnswers((prev) => ({ ...prev, [a.addr]: { ...(prev[a.addr] || {}), [a.field.name]: v } })),
        }))));
  }

  window.UI = {
    OSGlyph, StatusBadge, CopyField, Meter, Modal, ConfirmModal, Menu,
    Field, TextArea, SelectField, Toggle, TagInput, FormModal,
    collectAsks, initAskAnswers, asksMissing, AskInputs,
  };
})();
