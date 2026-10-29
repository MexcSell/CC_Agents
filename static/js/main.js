// Entry point: hash router, server health polling and restart handling.

import { api, h, fill, state, banner, resizer } from './core.js';
import * as pages from './pages.js';

const routes = [
  [/^#?\/?$/, pages.dashboardPage, 'dashboard'],
  [/^#\/chat(?:\/(\d+))?(?:\/(\d+))?$/, pages.chatPage, 'chat'],
  [/^#\/dev(?:\/(\d+))?$/, pages.devPage, 'dev'],
  [/^#\/projects$/, pages.projectsPage, 'projects'],
  [/^#\/projects\/(\d+)(?:\/(tasks|files|agent))?(?:\/(\d+))?$/, pages.projectPage, 'projects'],
  [/^#\/files$/, pages.filesPage, 'files'],
  [/^#\/settings$/, pages.settingsPage, 'settings'],
  [/^#\/c\/(\d+)$/, pages.conversationRedirect, null],
];

let cleanup = null;
let token = 0;
let currentHash = location.hash;

async function route() {
  if (state.dirty.size && location.hash !== currentHash) {
    if (!window.confirm('You have unsaved file changes. Leave this page and discard them?')) {
      history.replaceState(null, '', currentHash || '#/');
      return;
    }
    state.dirty.clear();
  }
  currentHash = location.hash;
  const [path, qs] = (location.hash || '#/').split('?');
  const match = routes.map(([rx, page, nav]) => [path.match(rx), page, nav]).find(([m]) => m);
  if (!match) { location.replace('#/'); return; }
  const [m, page, nav] = match;
  const mine = ++token;
  if (cleanup) {
    try { cleanup(); } catch (e) { console.error(e); }
    cleanup = null;
  }
  document.querySelectorAll('#sidebar a.nav').forEach(a => a.classList.toggle('active', a.dataset.nav === nav));
  const container = h('div', { style: { height: '100%' } });
  fill(document.getElementById('view'), container);
  try {
    const dispose = await page(container, m.slice(1), new URLSearchParams(qs || ''));
    if (mine !== token) { dispose?.(); return; }
    cleanup = dispose || null;
  } catch (e) {
    if (mine !== token) return;
    console.error(e);
    fill(container, h('div', { class: 'page' }, h('div', { class: 'notice error' }, `Could not load this page: ${e.message}`)));
  }
}

function setServerStatus(ok) {
  document.getElementById('server-status').title = ok ? 'Server online' : 'Server offline';
  fill(document.getElementById('server-status'), h('span', { class: `dot ${ok ? 'ok' : 'err'}` }), ok ? 'server online' : 'server offline');
}

function setRunStatus(active) {
  const el = document.getElementById('run-status');
  fill(el, active.length ? h('a', { href: '#/' }, h('span', { class: 'dot run' }), `${active.length} agent${active.length > 1 ? 's' : ''} working`) : '');
}

function onRestarted(pid) {
  state.pid = pid;
  state.expectRestart = false;
  state.down = false;
  if (state.dirty.size) {
    banner('The server restarted. Reload the page to load the latest version.', [h('button', { class: 'primary sm', onclick: () => location.reload() }, 'Reload')]);
    setTimeout(poll, 5000);
  } else {
    banner('Server restarted — reloading…');
    setTimeout(() => location.reload(), 700);
  }
}

async function poll() {
  let delay = 5000;
  try {
    const res = await fetch('/api/health', { cache: 'no-store' });
    const j = await res.json();
    if (state.pid && j.pid !== state.pid) { onRestarted(j.pid); return; }
    state.pid = j.pid;
    if (state.down) { state.down = false; banner(null); }
    setServerStatus(true);
    const runs = await api('GET', '/api/runs', undefined, { quiet: true }).catch(() => null);
    if (runs) setRunStatus(runs.active);
  } catch {
    state.down = true;
    setServerStatus(false);
    banner(state.expectRestart ? 'Restarting the server…' : 'The server is unreachable. Waiting for it to come back…');
    delay = 1000;
  }
  if (state.expectRestart) delay = 1000;
  setTimeout(poll, delay);
}

async function init() {
  try {
    state.info = await api('GET', '/api/system/info', undefined, { quiet: true });
    state.pid = state.info.pid;
  } catch { /* the poller reports it */ }
  const sidebar = document.getElementById('sidebar');
  sidebar.after(resizer(sidebar, { key: 'sidebar', width: 208, min: 160, max: 360, collapseAt: 130, collapsed: 60, flex: document.getElementById('view') }));
  window.addEventListener('hashchange', route);
  window.addEventListener('beforeunload', e => {
    if (state.dirty.size) { e.preventDefault(); e.returnValue = ''; }
  });
  route();
  poll();
}

init();
