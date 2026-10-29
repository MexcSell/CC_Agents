// Claude plan usage: the sidebar meter for the model of the page on screen and the "Account & Usage" dialog.

import { api, h, fill, modal, modelOptions } from './core.js';

const REFRESH_MS = 60_000;
let scope = { scope: 'chat' };   // which agent's model the meter follows
let timer = null;
let latest = null;

const fmtTokens = n => n < 1000 ? String(n) : n < 10_000 ? (n / 1000).toFixed(1) + 'k'
  : n < 1_000_000 ? Math.round(n / 1000) + 'k' : (n / 1_000_000).toFixed(1) + 'M';
const pct = n => `${Math.round(n)}%`;
const shortModel = label => label.replace(/^Claude /, '');
const severity = remaining => remaining == null ? '' : remaining <= 10 ? ' err' : remaining <= 25 ? ' warn' : '';

/** "Resets at 11:09 AM" today, otherwise "Resets Sun, Nov 1 at 2:59 AM". */
function fmtReset(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === new Date().toDateString()) return `Resets at ${time}`;
  return `Resets ${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} at ${time}`;
}

function bar(percent, remaining) {
  return h('div', { class: `progress usage-bar${severity(remaining)}` }, h('span', { style: { width: `${Math.max(0, Math.min(100, percent))}%` } }));
}

function query(extra = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...scope, ...extra })) if (v != null) q.set(k, v);
  return q.toString();
}

// ---------------------------------------------------------------- sidebar meter

function renderMeter(el, data) {
  const m = data.model;
  const week = m.tokens.week;
  const left = m.remaining == null ? '—' : `${pct(m.remaining)} left`;
  el.title = `${m.label}\n${week.total.toLocaleString()} tokens this week (${week.requests} requests by Agent Group's agents)\n`
    + (m.remaining == null ? (data.error || 'Plan limits unavailable') : `${pct(m.remaining)} of the ${m.binding} limit left\n${fmtReset(m.resets_at)}`)
    + '\nClick for details';
  fill(el,
    h('div', { class: 'usage-row' }, h('span', { class: 'lbl ellipsis' }, shortModel(m.label)), h('b', { class: 'lbl' }, left)),
    bar(m.percent ?? 0, m.remaining),
    h('div', { class: 'lbl ellipsis' }, `${fmtTokens(week.total)} tok/wk`, m.resets_at ? ` · ${fmtReset(m.resets_at).replace('Resets ', '')}` : ''));
}

async function refresh() {
  clearTimeout(timer);
  timer = setTimeout(refresh, REFRESH_MS);
  const el = document.getElementById('usage-meter');
  if (!el) return;
  try {
    latest = await api('GET', `/api/usage?${query()}`, undefined, { quiet: true });
    renderMeter(el, latest);
    el.hidden = false;
  } catch { /* keep the last reading */ }
}

/** Follow the model of the agent behind the page on screen. */
export function setUsageScope(next) {
  const changed = JSON.stringify(next) !== JSON.stringify(scope);
  scope = next;
  if (changed || !latest) refresh();
}

export function initUsage() {
  const el = h('button', { id: 'usage-meter', class: 'usage-meter', hidden: true, onclick: () => usageDialog() });
  document.querySelector('#sidebar .sidebar-foot').prepend(el);   // the router's first setUsageScope loads it
}

// ---------------------------------------------------------------- dialog

function tokenRow(label, t) {
  const input = t.input + t.cache_read + t.cache_write;
  return h('div', { class: 'usage-kv', title: `Input ${input.toLocaleString()} (uncached ${t.input.toLocaleString()}, cache read ${t.cache_read.toLocaleString()}, cache write ${t.cache_write.toLocaleString()})\nOutput ${t.output.toLocaleString()}\n${t.requests} model requests` },
    h('span', {}, label), h('span', {}, h('b', {}, t.total.toLocaleString()), h('span', { class: 'muted' }, ` · ${fmtTokens(input)} in · ${fmtTokens(t.output)} out`)));
}

function renderDialog(body, data, select) {
  const m = data.model;
  const acc = data.account;
  const kv = (k, v) => v ? h('div', { class: 'usage-kv' }, h('span', { class: 'muted' }, k), h('span', {}, v)) : null;
  fill(body,
    data.error ? h('div', { class: 'notice warn' }, data.error, data.limits.length ? ' Showing the last reading.' : '') : null,
    data.engine === 'api' ? h('div', { class: 'notice info' }, 'Agents currently use the API engine, which is billed per token and does not count against these plan limits.') : null,

    h('div', { class: 'usage-section' }, 'Selected model'),
    h('div', { class: 'usage-model' },
      h('div', { class: 'usage-row' }, select,
        h('div', { class: `usage-left${severity(m.remaining)}` }, m.remaining == null ? '—' : pct(m.remaining), h('span', { class: 'small muted' }, ' left'))),
      m.remaining != null ? bar(m.percent, m.remaining) : null,
      h('div', { class: 'small muted' }, m.remaining == null ? 'Plan limits unavailable.'
        : `${pct(m.percent)} of the ${m.binding} limit used · ${fmtReset(m.resets_at)}`),
      h('div', { class: 'usage-tokens' },
        tokenRow('This session (5h)', m.tokens.session),
        tokenRow('This week (7 day)', m.tokens.week),
        tokenRow('All time', m.tokens.all)),
      h('div', { class: 'small muted' }, 'Tokens spent by Agent Group’s agents on this model; input includes cache reads and writes.')),

    h('div', { class: 'usage-section' }, 'Usage'),
    data.limits.length ? data.limits.map(l => h('div', { class: `usage-limit${l.applies ? '' : ' other'}` },
      h('div', { class: 'usage-row' }, h('span', {}, l.label, l.applies ? null : h('span', { class: 'small muted' }, ' · other model')),
        h('span', {}, pct(l.percent), h('span', { class: 'small muted' }, ` used · ${pct(l.remaining)} left`))),
      bar(l.percent, l.remaining),
      h('div', { class: 'small muted' }, fmtReset(l.resets_at))))
      : h('div', { class: 'small muted' }, 'No plan limits available.'),
    h('a', { class: 'small', href: 'https://claude.ai/settings/usage', target: '_blank', rel: 'noopener' }, 'Manage usage on claude.ai'),

    h('div', { class: 'usage-section' }, 'Account'),
    kv('Auth method', acc.auth), kv('Email', acc.email), kv('Organization', acc.organization), kv('Plan', acc.plan),
    data.fetched_at ? h('div', { class: 'small muted' }, `Limits read ${new Date(data.fetched_at * 1000).toLocaleTimeString()}`) : null);
}

export async function usageDialog() {
  const body = h('div', { class: 'usage-dialog' }, h('div', { class: 'muted' }, 'Loading…'));
  let model = latest?.model.id;
  const select = h('select', { onchange: () => { model = select.value; load(); } });
  const load = async (refresh = false) => {
    try {
      const data = await api('GET', `/api/usage?${query({ model, refresh: refresh ? 1 : null })}`);
      model = data.model.id;
      fill(select, modelOptions(model));
      renderDialog(body, data, select);
      if (data.model.id === latest?.model.id) renderMeter(document.getElementById('usage-meter'), latest = data);
    } catch (e) {
      fill(body, h('div', { class: 'notice error' }, `Could not load usage: ${e.message}`));
    }
  };
  modal({ title: 'Account & Usage', body, cancelLabel: 'Close',
    actions: [{ label: 'Refresh', onClick: async () => { await load(true); return false; } }] });
  load();
}
