// Shared helpers: API client, DOM builder, dialogs, formatting and a small Markdown renderer.

export const state = {
  pid: null,
  info: null,
  dirty: new Set(),        // editors with unsaved changes
  expectRestart: false,
};

// ---------------------------------------------------------------- API

export async function api(method, path, body, { quiet = false } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    const err = new Error('The server is unreachable.');
    if (!quiet) toast(err.message, 'error');
    throw err;
  }
  if (res.status === 401 && path !== '/api/login') {
    await loginDialog();
    return api(method, path, body, { quiet });
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : j.detail.map?.(d => d.msg).join('; ') || JSON.stringify(j.detail);
    } catch { /* not JSON */ }
    const err = new Error(msg);
    err.status = res.status;
    if (!quiet) toast(msg, 'error');
    throw err;
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : res.text();
}

export const enc = encodeURIComponent;

/** Navigate to a hash route; re-renders even if the hash is unchanged. */
export function go(hash) {
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = hash;
}

export function field(label, input, hint) {
  return h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'hint' }, hint) : null);
}

let loginPromise = null;
function loginDialog() {
  loginPromise ??= new Promise(resolve => {
    const input = h('input', { type: 'password', placeholder: 'Access token' });
    const m = modal({
      title: 'Access token required',
      body: [h('p', { class: 'muted' }, 'This server was started with AGENT_TOKEN. Enter it to continue.'), input],
      dismissable: false,
      actions: [{
        label: 'Sign in', kind: 'primary', onClick: async () => {
          try { await api('POST', '/api/login', { token: input.value }); } catch { return false; }
          loginPromise = null;
          resolve();
        },
      }],
    });
    setTimeout(() => input.focus(), 50);
    input.addEventListener('keydown', e => e.key === 'Enter' && m.el.querySelector('.modal-foot .primary').click());
  });
  return loginPromise;
}

// ---------------------------------------------------------------- DOM

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'value') el.value = v;
    else if (typeof v === 'boolean') el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

/** Replace an element's children, skipping null/false (DOM replaceChildren would print "null"). */
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter(c => c != null && c !== false));
  return el;
}

/**
 * Make a grid column resizable. Returns the gutter element: a zero-width grid item to place next
 * to `target` (before it when side is 'left'). The width lives in the CSS variable --w on target and
 * in localStorage. Dragging below `collapseAt` snaps the column to `collapsed` px and adds the class
 * "collapsed" (icon-only view); double-click the gutter or click a `.pico` icon to expand it again.
 * `flex` is the column that gives up the space; it keeps at least 360px.
 */
export function resizer(target, { key, width, min = 180, max = 520, collapseAt = 130, collapsed = 56, side = 'right', flex }) {
  const storeKey = `col-w:${key}`;
  let last = width;  // last expanded width
  const set = w => {
    const shut = w < collapseAt;
    if (!shut) last = w;
    target.style.setProperty('--w', `${shut ? collapsed : w}px`);
    target.classList.toggle('collapsed', shut);
  };
  const save = () => localStorage.setItem(storeKey, target.classList.contains('collapsed') ? `c${last}` : String(last));
  const saved = localStorage.getItem(storeKey) || '';
  const n = parseInt(saved.replace(/^c/, ''), 10);
  if (n >= min && n <= max) last = n;
  set(saved.startsWith('c') ? 0 : last);

  const toggle = () => { set(target.classList.contains('collapsed') ? last : 0); save(); };
  target.addEventListener('click', e => {
    if (target.classList.contains('collapsed') && e.target.closest('.pico')) toggle();
  });

  const gutter = h('div', {
    class: `gutter ${side}`, role: 'separator', tabindex: 0, 'aria-orientation': 'vertical',
    title: 'Drag to resize · double-click to collapse or expand',
    ondblclick: toggle,
    onkeydown: e => {
      const step = { ArrowLeft: -16, ArrowRight: 16 }[e.key];
      if (e.key === 'Enter') toggle();
      else if (step) {
        const cur = target.classList.contains('collapsed') ? 0 : last;
        set(Math.min(Math.max(cur + (side === 'left' ? -step : step), min), max));
        save();
      } else return;
      e.preventDefault();
    },
  });
  gutter.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    gutter.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startW = target.getBoundingClientRect().width;
    const room = flex ? Math.max(0, flex.getBoundingClientRect().width - 360) : Infinity;
    const limit = Math.max(min, Math.min(max, startW + room));
    document.body.classList.add('resizing');
    gutter.classList.add('active');
    const move = ev => {
      const raw = startW + (side === 'left' ? startX - ev.clientX : ev.clientX - startX);
      set(raw < collapseAt ? 0 : Math.round(Math.min(Math.max(raw, min), limit)));
    };
    const end = () => {
      gutter.removeEventListener('pointermove', move);
      gutter.removeEventListener('pointerup', end);
      gutter.removeEventListener('pointercancel', end);
      document.body.classList.remove('resizing');
      gutter.classList.remove('active');
      save();
    };
    gutter.addEventListener('pointermove', move);
    gutter.addEventListener('pointerup', end);
    gutter.addEventListener('pointercancel', end);
  });
  return gutter;
}

export function toast(message, kind = 'info', ms = 4500) {
  const el = h('div', { class: `toast ${kind}` }, message);
  const host = document.getElementById('toasts');
  host.append(el);
  while (host.children.length > 4) host.firstChild.remove();
  setTimeout(() => el.remove(), kind === 'error' ? Math.max(ms, 7000) : ms);
}

export function banner(content, actions = []) {
  const el = document.getElementById('banner');
  if (content == null) { el.hidden = true; return; }
  fill(el, h('span', {}, content), ...actions);
  el.hidden = false;
}

/** Modal dialog. actions: [{label, kind, onClick}] - onClick may return false (or a promise of false) to keep it open. */
export function modal({ title, body, actions = [], wide = false, dismissable = true, onClose }) {
  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); onClose?.(); };
  const onKey = e => { if (e.key === 'Escape' && dismissable) close(); };
  const buttons = actions.map(a => {
    const b = h('button', { class: a.kind || '' }, a.label);
    b.onclick = async () => {
      b.disabled = true;
      try {
        if ((await a.onClick?.()) !== false) close();
      } finally { b.disabled = false; }
    };
    return b;
  });
  if (dismissable) buttons.unshift(h('button', { onclick: close }, 'Cancel'));
  const box = h('div', { class: `modal${wide ? ' wide' : ''}` },
    h('div', { class: 'modal-head' }, h('h2', {}, title)),
    h('div', { class: 'modal-body' }, body),
    h('div', { class: 'modal-foot' }, buttons));
  const backdrop = h('div', { class: 'modal-backdrop' }, box);
  backdrop.addEventListener('mousedown', e => { if (e.target === backdrop && dismissable) close(); });
  document.addEventListener('keydown', onKey);
  document.body.append(backdrop);
  return { el: box, close };
}

export function confirmDialog(message, { okLabel = 'OK', danger = false, title = 'Please confirm' } = {}) {
  return new Promise(resolve => {
    let ok = false;
    modal({
      title, body: h('p', { style: { whiteSpace: 'pre-wrap', margin: 0 } }, message),
      actions: [{ label: okLabel, kind: danger ? 'danger' : 'primary', onClick: () => { ok = true; } }],
      onClose: () => resolve(ok),
    });
  });
}

export function promptDialog(title, { label = '', value = '', placeholder = '', okLabel = 'OK', multiline = false } = {}) {
  return new Promise(resolve => {
    let result = null;
    const input = multiline
      ? h('textarea', { value, placeholder, rows: 6 })
      : h('input', { type: 'text', value, placeholder });
    const m = modal({
      title,
      body: h('label', { class: 'field' }, label, input),
      actions: [{ label: okLabel, kind: 'primary', onClick: () => { result = input.value.trim(); } }],
      onClose: () => resolve(result || null),
    });
    setTimeout(() => { input.focus(); if (!multiline) input.select(); }, 30);
    if (!multiline) input.addEventListener('keydown', e => e.key === 'Enter' && m.el.querySelector('.modal-foot .primary').click());
  });
}

export function contextMenu(x, y, items) {
  document.querySelector('.ctx-menu')?.remove();
  const menu = h('div', { class: 'ctx-menu' }, items.map(it => it === '-' ? h('hr') :
    h('button', { class: it.danger ? 'danger' : '', onclick: () => { menu.remove(); it.action(); } }, it.label)));
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, innerWidth - r.width - 8) + 'px';
  menu.style.top = Math.min(y, innerHeight - r.height - 8) + 'px';
  setTimeout(() => document.addEventListener('mousedown', function off(e) {
    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', off); }
  }), 0);
}

// ---------------------------------------------------------------- formatting

export function fmtBytes(n) {
  if (n == null) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${units[i]}`;
}

export function toDate(v) {
  return typeof v === 'number' ? new Date(v * 1000) : new Date(v);
}

export function fmtDate(v) {
  if (!v) return '';
  return toDate(v).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function timeAgo(v) {
  if (!v) return '';
  const s = (Date.now() - toDate(v).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return toDate(v).toLocaleDateString();
}

export function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

export function fmtHours(hours) {
  if (hours == null) return '—';
  return hours < 1 ? `${Math.round(hours * 60)} min` : `${+hours.toFixed(1)} h`;
}

export const STATUS_LABELS = { todo: 'To do', in_progress: 'In progress', blocked: 'Blocked', review: 'Review', done: 'Done' };

export function statusBadge(status) {
  return h('span', { class: `badge ${status}` }, STATUS_LABELS[status] || status);
}

export function progressBar(pct, done = false) {
  return h('div', { class: `progress${done ? ' done' : ''}`, title: `${pct}%` }, h('span', { style: { width: `${Math.max(0, Math.min(100, pct))}%` } }));
}

// ---------------------------------------------------------------- Markdown

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
export const escapeHtml = esc;

/** Small, safe Markdown renderer (input is HTML-escaped first). Handles unterminated code fences while streaming. */
export function renderMarkdown(src) {
  const blocks = [];
  src = String(src ?? '').replace(/\r\n/g, '\n');
  src = src.replace(/^ {0,3}```([\w+#.-]*)[^\n]*\n([\s\S]*?)(?:\n {0,3}```[^\S\n]*(?=\n|$)|(?![\s\S]))/gm, (_, lang, code) => {
    blocks.push(`<pre><code${lang ? ` data-lang="${esc(lang)}"` : ''}>${esc(code)}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  return renderBlocks(src.split('\n'), blocks);
}

function renderBlocks(lines, blocks) {
  let html = '';
  let i = 0;
  const isBlockStart = l => /^\s*(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|\u0000\d+\u0000\s*$)/.test(l) || /^(\s*[-*_]){3,}\s*$/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = line.trim().match(/^\u0000(\d+)\u0000$/))) { html += blocks[+m[1]]; i++; continue; }
    if ((m = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/))) { html += `<h${m[1].length}>${inline(m[2], blocks)}</h${m[1].length}>`; i++; continue; }
    if (/^(\s*[-*_]){3,}\s*$/.test(line)) { html += '<hr>'; i++; continue; }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ''));
      html += `<blockquote>${renderBlocks(quote, blocks)}</blockquote>`;
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const [listHtml, next] = renderList(lines, i, blocks);
      html += listHtml; i = next; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const row = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => inline(c.trim(), blocks));
      html += '<table><thead><tr>' + row(line).map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) html += '<tr>' + row(lines[i++]).map(c => `<td>${c}</td>`).join('') + '</tr>';
      html += '</tbody></table>';
      continue;
    }
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) para.push(lines[i++]);
    html += `<p>${para.map(l => inline(l.trim(), blocks)).join('<br>')}</p>`;
  }
  return html;
}

function renderList(lines, i, blocks) {
  const itemRx = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const first = lines[i].match(itemRx);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  while (i < lines.length) {
    const line = lines[i];
    const m = line.match(itemRx);
    if (m && m[1].length === base && /\d/.test(m[2]) === ordered) { items.push({ text: m[3], sub: [] }); i++; continue; }
    if (!line.trim()) {
      const next = lines[i + 1] || '';
      const nm = next.match(itemRx);
      const indent = next.match(/^\s*/)[0].length;
      if (next.trim() && ((nm && nm[1].length === base && /\d/.test(nm[2]) === ordered) || indent > base)) { i++; continue; }
      break;
    }
    const indent = line.match(/^\s*/)[0].length;
    if (indent > base && items.length) { items[items.length - 1].sub.push(line.slice(base)); i++; continue; }
    if (!m && items.length && !/^\s*(#{1,6}\s|>|\u0000)/.test(line)) { items[items.length - 1].text += ' ' + line.trim(); i++; continue; }
    break;
  }
  const tag = ordered ? 'ol' : 'ul';
  const body = items.map(it => {
    const task = it.text.match(/^\[([ xX])\]\s+(.*)$/);
    const text = task ? `<input type="checkbox" disabled${task[1] !== ' ' ? ' checked' : ''}> ${inline(task[2], blocks)}` : inline(it.text, blocks);
    return `<li>${text}${it.sub.length ? renderBlocks(it.sub, blocks) : ''}</li>`;
  }).join('');
  return [`<${tag}>${body}</${tag}>`, i];
}

function inline(text, blocks) {
  const codes = [];
  let t = text.replace(/`([^`]+)`/g, (_, c) => { codes.push(`<code>${esc(c)}</code>`); return `\u0001${codes.length - 1}\u0001`; });
  t = esc(t);
  t = t.replace(/!?\[([^\]]+)\]\(([^)\s]+)\)/g, (all, label, url) =>
    /^(https?:|mailto:|#|\/)/i.test(url) ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>` : label);
  t = t.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  t = t.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>').replace(/(^|[^\w])__([^_]+?)__(?!\w)/g, '$1<strong>$2</strong>');
  t = t.replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|[^_\w])_(?!\s)([^_]+?)_(?!\w)/g, '$1<em>$2</em>');
  t = t.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  t = t.replace(/\u0001(\d+)\u0001/g, (_, n) => codes[+n]);
  return t.replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[+n] || '');
}

/** Adds copy buttons to code blocks rendered by renderMarkdown. */
export function enhanceMarkdown(el) {
  for (const pre of el.querySelectorAll('pre')) {
    if (pre.querySelector('.copy')) continue;
    const btn = h('button', { class: 'copy sm' }, 'Copy');
    btn.onclick = () => {
      navigator.clipboard?.writeText(pre.querySelector('code')?.innerText ?? pre.innerText)
        .then(() => { btn.textContent = 'Copied'; setTimeout(() => (btn.textContent = 'Copy'), 1200); });
    };
    pre.append(btn);
  }
  return el;
}

/** Adds a copy icon (shown on hover) to each paragraph-level block: paragraphs, headings, quotes, list items. */
export function addParagraphCopy(el) {
  const blocks = el.querySelectorAll(':scope > :is(p, h1, h2, h3, h4, h5, h6, blockquote), :scope > :is(ul, ol) > li');
  for (const node of blocks) {
    if (node.querySelector(':scope > .pcopy')) continue;
    const btn = h('button', { class: 'pcopy', title: 'Copy paragraph', 'aria-label': 'Copy paragraph' });
    btn.onclick = e => {
      e.stopPropagation();
      // Hide code-block "Copy" labels so they don't end up in the copied text.
      const labels = [...node.querySelectorAll('pre .copy')];
      labels.forEach(b => (b.hidden = true));
      const text = node.innerText.trim();
      labels.forEach(b => (b.hidden = false));
      navigator.clipboard?.writeText(text).then(() => {
        btn.classList.add('done');
        setTimeout(() => btn.classList.remove('done'), 1200);
      });
    };
    node.classList.add('pcopy-host');
    node.append(btn);
  }
  return el;
}

export function md(text) {
  return enhanceMarkdown(h('div', { class: 'md', html: renderMarkdown(text) }));
}

export function modelOptions(selected) {
  return (state.info?.models || [{ id: selected, label: selected }]).map(m => h('option', { value: m.id, selected: m.id === selected }, m.label));
}

export function effortOptions(selected) {
  return (state.info?.efforts || ['low', 'medium', 'high', 'xhigh', 'max']).map(e => h('option', { value: e, selected: e === selected }, e));
}
