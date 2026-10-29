// Agent templates: the Templates page, the template editor, and the autocomplete widgets used by the
// editor and by the project agent forms. A template is referred to by its symbol, "@" + slug.

import { api, h, fill, field, toast, modal, confirmDialog, modelOptions, effortOptions } from './core.js';

// ================================================================ autocomplete

/** Rank `items` ({value, label, hint}) against query `q`: prefix matches, then word starts, then substrings. */
export function rankMatches(items, q, limit = 8) {
  const query = q.trim().toLowerCase().replace(/^@/, '');  // "@rev" finds @code-reviewer
  if (!query) return items.slice(0, limit);
  const words = query.split(/\s+/);
  const wordStart = new RegExp(`(^|[^a-z0-9])${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`);
  const scored = [];
  for (const it of items) {
    const text = `${it.value} ${it.label || ''}`.toLowerCase();
    if (!words.every(w => text.includes(w))) continue;
    const score = it.value.toLowerCase().replace(/^@/, '').startsWith(query) ? 0 : wordStart.test(text) ? 1 : 2;
    scored.push([score, it]);
  }
  return scored.sort((a, b) => a[0] - b[0]).slice(0, limit).map(([, it]) => it);
}

/**
 * Attach a suggestion dropdown to a text input. `source(query)` returns [{value, label?, hint?}];
 * `onPick(item)` is called when one is chosen (click, or Enter/Tab on the highlighted item; Tab takes
 * the first item when none is highlighted). The dropdown is fixed-positioned on <body>, so modals with
 * scrolling bodies do not clip it. Returns { isOpen, highlighted, close, refresh }.
 */
export function autocomplete(input, { source, onPick }) {
  let items = [];
  let active = -1;
  const menu = h('div', { class: 'ac-menu', role: 'listbox' });
  menu.addEventListener('mousedown', e => e.preventDefault());  // keep focus in the input

  const place = () => {
    if (!input.isConnected) { close(); return; }  // the form was closed
    const r = input.getBoundingClientRect();
    menu.style.minWidth = `${Math.max(r.width, 240)}px`;
    menu.style.left = `${Math.min(r.left, innerWidth - Math.max(r.width, 240) - 8)}px`;
    const below = innerHeight - r.bottom;
    const height = menu.offsetHeight;
    menu.style.top = below < height + 8 && r.top > below ? `${r.top - height - 4}px` : `${r.bottom + 4}px`;
  };
  const render = () => {
    fill(menu, items.map((it, i) => h('div', {
      class: `ac-item${i === active ? ' active' : ''}`, role: 'option',
      onmouseenter: () => { active = i; mark(); },
      onclick: () => pick(it),
    }, h('span', { class: 'v' }, it.value), it.label ? h('span', { class: 'l' }, it.label) : null,
    it.hint ? h('span', { class: 'hint' }, it.hint) : null)),
    h('div', { class: 'ac-foot' }, '↑↓ choose · Tab or Enter accept · Esc close'));
  };
  const mark = () => menu.querySelectorAll('.ac-item').forEach((el, i) => el.classList.toggle('active', i === active));
  const close = () => {
    menu.remove();
    removeEventListener('scroll', place, true);
    removeEventListener('resize', place);
  };
  const refresh = () => {
    items = source(input.value) || [];
    active = -1;
    if (!items.length || document.activeElement !== input) { close(); return; }
    render();
    if (!menu.isConnected) {
      document.body.append(menu);
      addEventListener('scroll', place, true);
      addEventListener('resize', place);
    }
    place();
  };
  const pick = it => { close(); onPick(it); };

  input.setAttribute('autocomplete', 'off');
  input.addEventListener('input', refresh);
  input.addEventListener('focus', refresh);
  input.addEventListener('blur', close);  // clicks on the menu don't blur: its mousedown is prevented
  input.addEventListener('keydown', e => {
    if (!menu.isConnected) {
      if (e.key === 'ArrowDown') { refresh(); e.preventDefault(); }
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length + 1) % (items.length + 1);
      if (active === items.length) active = -1;
      mark();
      menu.querySelector('.ac-item.active')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Escape') {
      close();
      e.stopPropagation();  // don't close the surrounding modal
    } else if (e.key === 'Tab' && !e.shiftKey && items.length) {
      pick(items[Math.max(active, 0)]);
    } else if (e.key === 'Enter' && active >= 0) {
      pick(items[active]);
    } else return;
    e.preventDefault();
  });
  return { isOpen: () => menu.isConnected, close, refresh };
}

/**
 * An input that collects a list of values as removable chips, with autocomplete.
 * `suggestions()` returns the candidate strings or {value, label, hint} items; chosen values are left
 * out. `strict`: only values from the suggestions are accepted. `block`: one chip per line (for long
 * values such as rules). Read the values with el.getValues().
 */
export function chipInput({ values = [], suggestions, placeholder = '', strict = false, block = false, separator = null, onChange }) {
  let list = [...values];
  const items = () => (suggestions() || []).map(s => (typeof s === 'string' ? { value: s } : s));
  const input = h('input', { type: 'text', placeholder });
  const chips = h('div', { class: 'chip-list' });
  const el = h('div', { class: `chips${block ? ' block' : ''}`, onclick: e => { if (e.target === el || e.target === chips) input.focus(); } }, chips, input);

  const renderChips = () => fill(chips, list.map((v, i) => h('span', { class: 'chip', title: v },
    h('span', { class: 'txt' }, v),
    h('button', { type: 'button', class: 'x', title: 'Remove', onclick: e => { e.stopPropagation(); list.splice(i, 1); changed(); } }, '✕'))));
  const changed = () => { renderChips(); onChange?.(list); };
  const add = raw => {
    let v = raw.trim().replace(/\s+/g, ' ');
    if (!v) return;
    if (strict) {
      const match = items().find(it => it.value.toLowerCase() === v.toLowerCase() || it.value.toLowerCase() === `@${v.toLowerCase()}`);
      if (!match) { toast(`"${v}" is not one of the suggestions.`, 'error'); return; }
      v = match.value;
    }
    if (!list.some(x => x.toLowerCase() === v.toLowerCase())) list.push(v);
    input.value = '';
    changed();
  };
  const ac = autocomplete(input, {
    source: q => rankMatches(items().filter(it => !list.some(x => x.toLowerCase() === it.value.toLowerCase())), q, 10),
    onPick: it => { add(it.value); setTimeout(() => ac.refresh(), 0); },
  });
  input.addEventListener('keydown', e => {
    if (e.defaultPrevented) return;  // handled by the autocomplete
    if (e.key === 'Enter' || (separator && e.key === separator)) {
      if (input.value.trim()) { e.preventDefault(); add(input.value); ac.refresh(); }
      else if (e.key === 'Enter') e.preventDefault();
    } else if (e.key === 'Backspace' && !input.value && list.length) {
      list.pop();
      changed();
    }
  });
  input.addEventListener('paste', e => {
    const text = e.clipboardData?.getData('text') || '';
    if (!text.includes('\n')) return;
    e.preventDefault();
    text.split('\n').forEach(add);  // one value per pasted line
  });
  // Text typed but not yet added counts as a value when the form is saved.
  el.getValues = () => { if (input.value.trim() && !strict) add(input.value); return [...list]; };
  el.setValues = v => { list = [...v]; changed(); };
  el.input = input;
  renderChips();
  return el;
}

// A form field around a chip input. A <div>, not a <label> like field(): clicking a label would
// activate its first button, a chip's remove button.
const chipField = (label, input, hint) => h('div', { class: 'field' }, label, input, hint ? h('span', { class: 'hint' }, hint) : null);

// ================================================================ template references

const templateItems = templates => templates.map(t => ({ value: t.symbol, label: t.name, hint: t.description || t.role }));

/** One-line summary of what a template contains. */
export function templateSummary(t) {
  const n = (k, one, many) => (t[k].length ? `${t[k].length} ${t[k].length === 1 ? one : many}` : null);
  return [t.role || null, n('expertise', 'area of expertise', 'areas of expertise'), n('skills', 'skill', 'skills'),
    n('rules', 'rule', 'rules'), t.subagents.length ? `internal: ${t.subagents.map(s => '@' + s).join(' ')}` : null]
    .filter(Boolean).join(' · ') || 'empty template';
}

/**
 * The template and internal-agent fields of a project agent form. `onTemplate(template)` runs when a
 * template is chosen, so the form can take over its role, model and effort. get() returns
 * { template, subagents } as symbols for the API.
 */
export function agentTemplateFields(templates, { template = '', subagents = [], onTemplate } = {}) {
  const find = sym => templates.find(t => t.slug === sym.trim().replace(/^@/, '').toLowerCase());
  const input = h('input', { type: 'text', class: 'mono', value: template ? `@${template}` : '', placeholder: templates.length ? 'Type @ to choose a template (optional)' : 'No templates yet' });
  const info = h('span', { class: 'hint' });
  const showInfo = () => {
    const sym = input.value.trim();
    const t = sym && find(sym);
    fill(info, !sym ? ['Optional. The agent takes the template’s rules, expertise and skills. ', h('a', { href: '#/templates' }, 'Manage templates')]
      : t ? [h('b', {}, t.name), ' — ', templateSummary(t)]
        : h('span', { style: { color: 'var(--err)' } }, `⚠ No template ${sym.startsWith('@') ? sym : '@' + sym}`));
  };
  autocomplete(input, {
    source: q => rankMatches(templateItems(templates), q, 10),
    onPick: it => { input.value = it.value; showInfo(); onTemplate?.(find(it.value)); },
  });
  input.addEventListener('input', showInfo);
  input.addEventListener('change', () => { const t = find(input.value); if (t) onTemplate?.(t); });
  showInfo();
  const internal = chipInput({
    values: subagents.map(s => `@${s}`), strict: true,
    placeholder: templates.length ? 'Type @ to add an internal agent' : 'Create templates to add internal agents',
    suggestions: () => templateItems(templates),
  });
  return {
    templateField: h('label', { class: 'field' }, 'Based on template', input, info),
    subagentsField: chipField('Internal agents', internal, 'Templates this agent can delegate work to as sub-agents (Claude Code engine only). Internal agents of its template are included automatically.'),
    get: () => ({ template: input.value.trim(), subagents: internal.getValues() }),
  };
}

// ================================================================ template editor

function templateModal(template, { templates, vocab, onSaved, copy = false }) {
  const t = template || { name: '', slug: '', description: '', role: '', instructions: '', rules: [], expertise: [], skills: [], subagents: [], model: '', effort: '' };
  const editing = template && !copy;
  const name = h('input', { type: 'text', value: copy ? `${t.name} (copy)` : t.name, placeholder: 'e.g. Code reviewer' });
  const slug = h('input', { type: 'text', class: 'mono', value: editing ? t.symbol : '', placeholder: '@code-reviewer' });
  const slugify = s => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  const autoSlug = () => { if (!slug.dataset.touched) slug.value = name.value.trim() ? `@${slugify(name.value)}` : ''; };
  name.addEventListener('input', autoSlug);
  slug.addEventListener('input', () => { slug.dataset.touched = '1'; });
  if (!editing) autoSlug();
  const desc = h('input', { type: 'text', value: t.description, placeholder: 'e.g. Reviews code changes for bugs, security issues and style' });
  const role = h('input', { type: 'text', value: t.role, placeholder: 'e.g. Senior software engineer focused on code quality' });
  const expertise = chipInput({ values: t.expertise, suggestions: () => vocab.expertise, separator: ',', placeholder: 'Type to search, Enter or comma to add…' });
  const skills = chipInput({ values: t.skills, suggestions: () => vocab.skills, separator: ',', placeholder: 'Type to search, Enter or comma to add…' });
  const rules = chipInput({ values: t.rules, suggestions: () => vocab.rules, block: true, placeholder: 'Type a rule or search suggestions, Enter to add…' });
  const others = templates.filter(x => !editing || x.id !== t.id);
  const subagents = chipInput({
    values: t.subagents.map(s => `@${s}`), strict: true, placeholder: others.length ? 'Type @ to add an internal agent' : 'No other templates yet',
    suggestions: () => templateItems(others),
  });
  const instructions = h('textarea', { rows: 4, value: t.instructions, placeholder: 'Any further guidance, in free text' });
  const model = h('select', {}, h('option', { value: '' }, 'Agent’s own model'), modelOptions(t.model));
  const effort = h('select', {}, h('option', { value: '' }, 'Agent’s own effort'), effortOptions(t.effort));
  if (!t.model) model.value = '';
  if (!t.effort) effort.value = '';

  modal({
    title: editing ? `Edit template ${t.symbol}` : 'New agent template',
    wide: true,
    body: h('div', { class: 'form-grid' },
      h('div', { class: 'form-grid two' }, field('Name', name),
        field('Symbol', slug, 'How agents refer to this template, e.g. when you add it as an internal agent.')),
      field('Description', desc, 'When to use it. Agents that have it as an internal agent read this to decide when to delegate.'),
      field('Role', role),
      chipField('Areas of expertise', expertise),
      chipField('Skills', skills),
      chipField('Rules', rules, 'Rules the agent must follow. Paste several lines to add one rule per line.'),
      chipField('Internal agents', subagents, 'Templates an agent based on this one can delegate work to.'),
      field('Additional instructions', instructions),
      h('div', { class: 'form-grid two' }, field('Default model', model, 'Taken over when a project agent picks this template; used by internal agents.'), field('Default effort', effort))),
    actions: [{
      label: editing ? 'Save template' : 'Create template', kind: 'primary', onClick: async () => {
        if (!name.value.trim()) { toast('A name is required.', 'error'); return false; }
        const body = {
          name: name.value, slug: slug.value.trim(), description: desc.value, role: role.value, instructions: instructions.value,
          expertise: expertise.getValues(), skills: skills.getValues(), rules: rules.getValues(), subagents: subagents.getValues(),
          model: model.value, effort: effort.value,
        };
        try {
          const saved = editing ? await api('PUT', `/api/templates/${t.id}`, body) : await api('POST', '/api/templates', body);
          toast(`Template ${saved.symbol} saved`, 'ok');
          onSaved(saved);
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => name.focus(), 30);
}

// ================================================================ page

export async function templatesPage(view) {
  const page = h('div', { class: 'page' });
  view.append(page);
  let [templates, vocab] = await Promise.all([api('GET', '/api/templates'), api('GET', '/api/templates/vocabulary')]);
  const reload = async () => {
    [templates, vocab] = await Promise.all([api('GET', '/api/templates', undefined, { quiet: true }), api('GET', '/api/templates/vocabulary', undefined, { quiet: true })]);
    render();
  };
  const edit = (t, copy = false) => templateModal(t, { templates, vocab, copy, onSaved: reload });
  const remove = async t => {
    const uses = t.used_by.map(u => (u.project ? `• ${u.agent} in "${u.project}" (${u.as === 'template' ? 'based on it' : 'internal agent'})` : `• template ${u.symbol} (internal agent)`));
    const msg = `Delete the template ${t.symbol} "${t.name}"?` + (uses.length ? `\n\nIt is used by:\n${uses.join('\n')}\n\nThese references are removed.` : '');
    if (!await confirmDialog(msg, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/templates/${t.id}`);
    toast('Template deleted', 'ok');
    reload();
  };
  const chipRow = (label, values) => (values.length
    ? h('div', { class: 'tpl-row' }, h('span', { class: 'lbl' }, label), h('div', { class: 'chip-list' }, values.slice(0, 6).map(v => h('span', { class: 'chip sm', title: v }, h('span', { class: 'txt' }, v))), values.length > 6 ? h('span', { class: 'small muted' }, `+${values.length - 6}`) : null))
    : null);
  const card = t => h('div', { class: 'card tpl-card', onclick: () => edit(t) },
    h('div', { class: 'row' },
      h('div', { class: 'grow', style: { minWidth: 0 } }, h('h2', { class: 'ellipsis', style: { margin: 0 } }, t.name), h('code', { class: 'sym' }, t.symbol)),
      h('button', { class: 'ghost icon sm', title: `Copy ${t.symbol}`, onclick: e => { e.stopPropagation(); navigator.clipboard?.writeText(t.symbol).then(() => toast(`${t.symbol} copied`, 'ok')); } }, '⧉'),
      h('button', { class: 'ghost icon sm', title: 'Duplicate', onclick: e => { e.stopPropagation(); edit(t, true); } }, '⎘'),
      h('button', { class: 'ghost icon sm', title: 'Delete', onclick: e => { e.stopPropagation(); remove(t); } }, '🗑')),
    t.description ? h('div', { class: 'small muted' }, t.description) : null,
    t.role ? h('div', { class: 'small' }, h('b', {}, 'Role: '), t.role) : null,
    chipRow('Expertise', t.expertise),
    chipRow('Skills', t.skills),
    t.rules.length ? h('div', { class: 'small muted' }, `📏 ${t.rules.length} rule${t.rules.length === 1 ? '' : 's'}`) : null,
    t.subagents.length ? h('div', { class: 'small' }, '🤝 Internal agents: ', t.subagents.map(s => h('code', { class: 'sym' }, `@${s}`))) : null,
    h('div', { class: 'row small muted', style: { marginTop: 'auto' } },
      h('span', { class: 'grow' }, t.used_by.length ? `Used ${t.used_by.length}×` : 'Not used yet'),
      h('a', { href: `#/projects?new=1&template=${t.slug}`, onclick: e => e.stopPropagation() }, 'New project with it →')));
  const render = () => fill(page,
    h('div', { class: 'page-head' }, h('h1', {}, 'Agent templates'), h('div', { class: 'spacer' }),
      h('button', { class: 'primary', onclick: () => edit(null) }, '+ New template')),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Reusable agent profiles: role, areas of expertise, skills and rules. Each template has a symbol such as ',
      h('code', {}, '@code-reviewer'), '. Use it to base a project agent on the template, or to give an agent internal agents it can delegate to.'),
    templates.length ? h('div', { class: 'cards' }, templates.map(card))
      : h('div', { class: 'card empty' }, h('div', { class: 'big' }, '🧩'),
        h('p', {}, 'No templates yet. Create one, then pick it when you set up a project agent.'),
        h('button', { class: 'primary', onclick: () => edit(null) }, 'Create your first template')));
  render();
}
