// Agent templates: the Templates page, the template editor, and the autocomplete widgets used by the
// editor and by the project agent forms. A template is referred to by its symbol, "@" + slug.

import { api, h, fill, go, field, toast, modal, confirmDialog, modelOptions, effortOptions } from './core.js';

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
  el.peekValues = () => [...list];  // without committing typed text
  el.addValue = add;
  el.input = input;
  renderChips();
  return el;
}

// A form field around a chip input. A <div>, not a <label> like field(): clicking a label would
// activate its first button, a chip's remove button.
const chipField = (label, input, hint) => h('div', { class: 'field' }, label, input, hint ? h('span', { class: 'hint' }, hint) : null);

// ================================================================ template references

const templateItems = templates => templates.map(t => ({ value: t.symbol, label: t.name, hint: [t.category, t.description || t.role].filter(Boolean).join(' — ') }));

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
    source: q => rankMatches(templateItems(templates), q, 12),
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

/**
 * The template editor. Every field offers candidates (a "Suggested" row and the autocomplete list) that
 * the server ranks from the fields above it: a category narrows the names, a name the descriptions and
 * roles, the chosen expertise the skills, and so on (POST /api/templates/suggest).
 */
function templateModal(template, { templates, onSaved, copy = false }) {
  const t = template || { name: '', slug: '', category: '', area: '', language: '', description: '', role: '', instructions: '', rules: [], expertise: [], skills: [], subagents: [], model: '', effort: '' };
  const editing = template && !copy;
  let sugg = {};

  const category = h('input', { type: 'text', value: t.category || '', placeholder: 'Choose a category or type a new one' });
  const area = h('select', { onchange: () => { renderHints(); refresh(0); } });
  const language = h('select', { onchange: () => { renderHints(); refresh(0); } });
  const fillSelect = (sel, items, none, current) => fill(sel, h('option', { value: '' }, none),
    items.map(it => h('option', { value: it.value, selected: it.value === current }, `${it.icon ? it.icon + ' ' : ''}${it.label}`)));
  fillSelect(area, [], '—', t.area);
  fillSelect(language, [], '—', t.language);
  const name = h('input', { type: 'text', value: copy ? `${t.name} (copy)` : t.name, placeholder: 'e.g. Code Reviewer' });
  const slug = h('input', { type: 'text', class: 'mono', value: editing ? t.symbol : '', placeholder: '@code-reviewer' });
  const slugify = s => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  const autoSlug = () => { if (!slug.dataset.touched) slug.value = name.value.trim() ? `@${slugify(name.value)}` : ''; };
  name.addEventListener('input', autoSlug);
  slug.addEventListener('input', () => { slug.dataset.touched = '1'; });
  if (!editing) autoSlug();
  const desc = h('input', { type: 'text', value: t.description, placeholder: 'When to use this agent' });
  const role = h('input', { type: 'text', value: t.role, placeholder: 'e.g. Senior engineer who reviews code for correctness' });
  const changedList = () => { renderHints(); refresh(0); };
  const chips = (key, opts) => chipInput({ values: t[key], suggestions: () => sugg[key] || [], onChange: changedList, ...opts });
  const expertise = chips('expertise', { separator: ',', placeholder: 'Type to search, Enter or comma to add…' });
  const skills = chips('skills', { separator: ',', placeholder: 'Type to search, Enter or comma to add…' });
  const rules = chips('rules', { block: true, placeholder: 'Type a rule or search the suggestions, Enter to add…' });
  const subagents = chipInput({
    values: t.subagents.map(s => `@${s}`), strict: true, onChange: changedList,
    placeholder: templates.length ? 'Type @ to add an internal agent' : 'No other templates yet',
    suggestions: () => sugg.subagents || [],
  });
  const instructions = h('textarea', { rows: 4, value: t.instructions, placeholder: 'Working method, output format, anything else' });
  const model = h('select', {}, h('option', { value: '' }, 'Agent’s own model'), modelOptions(t.model));
  const effort = h('select', {}, h('option', { value: '' }, 'Agent’s own effort'), effortOptions(t.effort));
  if (!t.model) model.value = '';
  if (!t.effort) effort.value = '';

  // ---- suggestions from the server, refreshed whenever a field changes
  const draft = () => ({
    slug: slug.value, category: category.value, area: area.value || t.area || '', language: language.value || t.language || '',
    name: name.value, description: desc.value, role: role.value,
    expertise: expertise.peekValues(), skills: skills.peekValues(), rules: rules.peekValues(), subagents: subagents.peekValues(),
  });
  let timer = null;
  let seq = 0;
  const load = async () => {
    clearTimeout(timer);
    const mine = ++seq;
    let res;
    try { res = await api('POST', '/api/templates/suggest', draft(), { quiet: true }); } catch { return; }
    if (mine !== seq) return;
    sugg = res;
    if (area.options.length === 1) fillSelect(area, res.area, 'Any functional area', t.area);
    if (language.options.length === 1) fillSelect(language, res.language, 'Not language-specific', t.language);
    renderHints();
  };
  const refresh = (delay = 250) => { clearTimeout(timer); timer = setTimeout(load, delay); };

  const setText = (input, value) => { input.value = value; input.dispatchEvent(new Event('input')); };
  const pickName = it => {
    if (!category.value.trim() && it.category) category.value = it.category;
    setText(name, it.value);
  };
  // Text inputs: autocomplete over the candidates; any edit refreshes the fields below.
  const textField = (input, key, onPick = it => setText(input, it.value)) => {
    autocomplete(input, { source: q => rankMatches(sugg[key] || [], q, 12), onPick: it => { onPick(it); refresh(0); } });
    input.addEventListener('input', () => { renderHints(); refresh(); });
  };
  textField(category, 'category');
  textField(name, 'name', pickName);
  textField(desc, 'description');
  textField(role, 'role');
  instructions.addEventListener('input', () => renderHints());

  // "Suggested" rows: the top candidates, one click to use. Text fields show them while empty.
  const rows = {};
  const row = key => (rows[key] = h('div', { class: 'sugg-row', 'data-key': key }));
  const has = (chip, v) => chip.peekValues().some(x => x.toLowerCase() === v.toLowerCase());
  const pills = (key, items, onPick, block = false) => fill(rows[key], items.length ? [
    h('span', { class: 'lbl' }, 'Suggested'),
    items.map(it => h('button', {
      type: 'button', class: `pill${block ? ' block' : ''}`, title: it.hint ? `${it.value}\n— ${it.hint}` : it.value, onclick: () => onPick(it),
    }, it.icon ? `${it.icon} ` : '', it.value, it.label ? h('span', { class: 'muted' }, ` ${it.label}`) : null)),
  ] : null);
  const renderHints = () => {
    const list = key => sugg[key] || [];
    pills('category', category.value.trim() ? [] : list('category'), it => { setText(category, it.value); refresh(0); });
    pills('name', name.value.trim() ? [] : list('name').slice(0, 8), it => { pickName(it); refresh(0); });
    pills('description', desc.value.trim() ? [] : list('description').slice(0, 2), it => { setText(desc, it.value); refresh(0); }, true);
    pills('role', role.value.trim() ? [] : list('role').slice(0, 3), it => { setText(role, it.value); refresh(0); }, true);
    const chipHints = (key, chip, max, block) => pills(key, list(key).filter(it => !has(chip, it.value)).slice(0, max), it => chip.addValue(it.value), block);
    chipHints('expertise', expertise, 10);
    chipHints('skills', skills, 10);
    chipHints('rules', rules, 5, true);
    chipHints('subagents', subagents, 6);
    pills('instructions', list('instructions').filter(it => !instructions.value.includes(it.value)).slice(0, 3), it => {
      instructions.value = instructions.value.trim() ? `${instructions.value.trim()}\n\n${it.value}` : it.value;
      renderHints();
    }, true);
  };

  // Fill every empty field from the template that best matches what is filled in.
  const fillEmpty = async () => {
    await load();
    const b = sugg.best;
    if (!b) { toast('Choose a category and a name (or role) first; the suggestions build on them.', 'info'); return; }
    if (!category.value.trim()) category.value = b.category;
    if (!area.value && b.area) area.value = b.area;
    if (!language.value && b.language) language.value = b.language;
    if (!name.value.trim()) setText(name, b.name);
    if (!desc.value.trim()) desc.value = b.description;
    if (!role.value.trim()) role.value = b.role;
    for (const [chip, key] of [[expertise, 'expertise'], [skills, 'skills'], [rules, 'rules'], [subagents, 'subagents']]) {
      if (!chip.peekValues().length) chip.setValues(b[key]);
    }
    if (!instructions.value.trim()) instructions.value = b.instructions;
    if (!model.value && b.model) model.value = b.model;
    if (!effort.value && b.effort) effort.value = b.effort;
    toast(`Filled the empty fields from ${b.name}`, 'ok');
    await load();
  };

  const withHints = (wrapper, key) => { wrapper.append(row(key)); return wrapper; };
  modal({
    title: editing ? `Edit template ${t.symbol}` : 'New agent template',
    wide: true,
    body: h('div', { class: 'form-grid' },
      h('div', { class: 'small muted', style: { marginTop: '-6px' } }, 'Each field suggests candidates based on the fields above it. Start with the category, functional area, language and name, or use “✨ Fill empty fields”.'),
      withHints(field('Category', category), 'category'),
      h('div', { class: 'form-grid two' }, field('Functional area', area), field('Language', language)),
      h('div', { class: 'form-grid two' },
        withHints(field('Name', name), 'name'),
        field('Symbol', slug, 'How agents refer to this template, e.g. when you add it as an internal agent.')),
      withHints(field('Description', desc, 'When to use it. Agents that have it as an internal agent read this to decide when to delegate.'), 'description'),
      withHints(field('Role', role), 'role'),
      withHints(chipField('Areas of expertise', expertise), 'expertise'),
      withHints(chipField('Skills', skills), 'skills'),
      withHints(chipField('Rules', rules, 'Rules the agent must follow. Paste several lines to add one rule per line.'), 'rules'),
      withHints(chipField('Internal agents', subagents, 'Templates an agent based on this one can delegate work to.'), 'subagents'),
      withHints(field('Additional instructions', instructions), 'instructions'),
      h('div', { class: 'form-grid two' }, field('Default model', model, 'Taken over when a project agent picks this template; used by internal agents.'), field('Default effort', effort))),
    actions: [
      { label: '✨ Fill empty fields', onClick: async () => { await fillEmpty(); return false; } },
      {
        label: editing ? 'Save template' : 'Create template', kind: 'primary', onClick: async () => {
          if (!name.value.trim()) { toast('A name is required.', 'error'); return false; }
          const body = {
            name: name.value, slug: slug.value.trim(), category: category.value, area: area.value, language: language.value,
            description: desc.value, role: role.value, instructions: instructions.value,
            expertise: expertise.getValues(), skills: skills.getValues(), rules: rules.getValues(), subagents: subagents.getValues(),
            model: model.value, effort: effort.value,
          };
          try {
            const saved = editing ? await api('PUT', `/api/templates/${t.id}`, body) : await api('POST', '/api/templates', body);
            toast(`Template ${saved.symbol} saved`, 'ok');
            onSaved(saved);
          } catch { return false; }
        },
      },
    ],
  });
  load();
  setTimeout(() => (editing ? name : category).focus(), 30);
}

// ================================================================ page

const pageTabs = sub => h('div', { class: 'tabs', style: { marginBottom: '14px' } },
  [['', '🧩 Templates'], ['groups', '👥 Agent groups']].map(([key, label]) =>
    h('button', { class: (sub || '') === key ? 'active' : '', onclick: () => go(key ? '#/templates/groups' : '#/templates') }, label)));

export async function templatesPage(view, [sub]) {
  const page = h('div', { class: 'page' });
  view.append(page);
  if (sub === 'groups') return groupsView(page);
  let templates = [];
  let library = { categories: [], areas: [], languages: [], presets: [] };
  let query = '';
  let only = localStorage.getItem('tpl-category') || '';
  let areaFilter = localStorage.getItem('tpl-area') || '';
  let langFilter = localStorage.getItem('tpl-language') || '';
  const listEl = h('div');
  const filterEl = h('div', { class: 'row wrap', style: { gap: '6px', marginBottom: '14px' } });
  const restoreBtn = h('button', { onclick: () => installPresets() });

  const load = async quiet => {
    [templates, library] = await Promise.all([api('GET', '/api/templates', undefined, { quiet }), api('GET', '/api/templates/presets', undefined, { quiet })]);
    render();
  };
  const reload = () => load(true);
  const edit = (t, copy = false) => templateModal(t, { templates, copy, onSaved: reload });
  const remove = async t => {
    const uses = t.used_by.map(u => (u.project ? `• ${u.agent} in "${u.project}" (${u.as === 'template' ? 'based on it' : 'internal agent'})`
      : u.group ? `• group ${u.symbol} (member)` : `• template ${u.symbol} (internal agent)`));
    const msg = `Delete the template ${t.symbol} "${t.name}"?` + (uses.length ? `\n\nIt is used by:\n${uses.join('\n')}\n\nThese references are removed (group members based on it leave their group).` : '');
    if (!await confirmDialog(msg, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/templates/${t.id}`);
    toast('Template deleted', 'ok');
    reload();
  };
  const missing = () => library.presets.filter(p => !p.installed);
  const installPresets = async () => {
    const names = missing().map(p => p.name);
    if (!await confirmDialog(`Add ${names.length} starter template(s)?\n\n${names.join(', ')}`, { okLabel: 'Add', title: 'Starter templates' })) return;
    const res = await api('POST', '/api/templates/presets/install', {});
    toast(`${res.installed} starter template(s) added`, 'ok');
    reload();
  };
  const areaOf = id => library.areas.find(a => a.id === id);
  const langOf = id => library.languages.find(l => l.id === id);

  const chipRow = (label, values) => (values.length
    ? h('div', { class: 'tpl-row' }, h('span', { class: 'lbl' }, label), h('div', { class: 'chip-list' }, values.slice(0, 4).map(v => h('span', { class: 'chip sm', title: v }, h('span', { class: 'txt' }, v))), values.length > 4 ? h('span', { class: 'small muted' }, `+${values.length - 4}`) : null))
    : null);
  const card = t => h('div', { class: 'card tpl-card', onclick: () => edit(t) },
    h('div', { class: 'row' },
      h('div', { class: 'grow', style: { minWidth: 0 } }, h('h2', { class: 'ellipsis', style: { margin: 0 } }, t.name), h('code', { class: 'sym' }, t.symbol)),
      h('button', { class: 'ghost icon sm', title: `Copy ${t.symbol}`, onclick: e => { e.stopPropagation(); navigator.clipboard?.writeText(t.symbol).then(() => toast(`${t.symbol} copied`, 'ok')); } }, '⧉'),
      h('button', { class: 'ghost icon sm', title: 'Duplicate', onclick: e => { e.stopPropagation(); edit(t, true); } }, '⎘'),
      h('button', { class: 'ghost icon sm', title: 'Delete', onclick: e => { e.stopPropagation(); remove(t); } }, '🗑')),
    t.area || t.language ? h('div', { class: 'row small', style: { gap: '6px' } },
      t.area && areaOf(t.area) ? h('span', { class: 'badge' }, `${areaOf(t.area).icon} ${areaOf(t.area).name}`) : null,
      t.language ? h('span', { class: 'badge accent' }, langOf(t.language)?.name || t.language) : null) : null,
    t.description ? h('div', { class: 'small muted' }, t.description) : null,
    t.role ? h('div', { class: 'small' }, h('b', {}, 'Role: '), t.role) : null,
    chipRow('Expertise', t.expertise),
    chipRow('Skills', t.skills),
    t.rules.length ? h('div', { class: 'small muted' }, `📏 ${t.rules.length} rule${t.rules.length === 1 ? '' : 's'}`) : null,
    t.subagents.length ? h('div', { class: 'small' }, '🤝 ', t.subagents.map(s => h('code', { class: 'sym' }, `@${s}`))) : null,
    h('div', { class: 'row small muted', style: { marginTop: 'auto' } },
      h('span', { class: 'grow' }, t.used_by.length ? `Used ${t.used_by.length}×` : 'Not used yet'),
      h('a', { href: `#/projects?new=1&template=${t.slug}`, onclick: e => e.stopPropagation() }, 'New project with it →')));

  // Categories in the library's order, then the user's own, then templates without one.
  const groups = () => {
    const known = library.categories.map(c => ({ ...c, items: [] }));
    const byName = new Map(known.map(c => [c.name.toLowerCase(), c]));
    for (const t of templates) {
      const key = (t.category || '').toLowerCase();
      if (!byName.has(key)) {
        const g = { name: t.category || 'Uncategorized', icon: t.category ? '🏷' : '📁', items: [] };
        byName.set(key, g);
        known.push(g);
      }
      byName.get(key).items.push(t);
    }
    return known.filter(g => g.items.length);
  };
  const matches = t => (!areaFilter || t.area === areaFilter) && (!langFilter || t.language === langFilter)
    && (!query || [t.name, t.symbol, t.description, t.role, ...t.expertise, ...t.skills].join(' ').toLowerCase().includes(query));
  const select = (value, none, items, key, set) => h('select', {
    style: { width: 'auto' }, onchange: e => { set(e.target.value); if (e.target.value) localStorage.setItem(key, e.target.value); else localStorage.removeItem(key); render(); },
  }, h('option', { value: '' }, none), items.map(it => h('option', { value: it.id, selected: it.id === value }, `${it.icon ? it.icon + ' ' : ''}${it.name}`)));
  const render = () => {
    const all = groups();
    if (only && !all.some(g => g.name === only)) only = '';
    restoreBtn.hidden = !missing().length;
    fill(restoreBtn, `＋ Starter templates (${missing().length})`);
    fill(filterEl, all.length > 1 ? [
      h('button', { class: `sm${only ? '' : ' primary'}`, onclick: () => { only = ''; localStorage.removeItem('tpl-category'); render(); } }, `All (${templates.length})`),
      all.map(g => h('button', { class: `sm${only === g.name ? ' primary' : ''}`, onclick: () => { only = g.name; localStorage.setItem('tpl-category', only); render(); } }, `${g.icon} ${g.name} (${g.items.length})`)),
      h('div', { class: 'grow' }),
      select(areaFilter, 'All functional areas', library.areas.filter(a => templates.some(t => t.area === a.id)), 'tpl-area', v => { areaFilter = v; }),
      select(langFilter, 'All languages', library.languages.filter(l => templates.some(t => t.language === l.id)), 'tpl-language', v => { langFilter = v; }),
    ] : null);
    const shown = all.filter(g => !only || g.name === only).map(g => ({ ...g, items: g.items.filter(matches) })).filter(g => g.items.length);
    fill(listEl, !templates.length
      ? h('div', { class: 'card empty' }, h('div', { class: 'big' }, '🧩'),
        h('p', {}, 'No templates yet. Add the starter templates or create your own, then pick one when you set up a project agent.'),
        h('div', { class: 'row', style: { justifyContent: 'center' } },
          missing().length ? h('button', { class: 'primary', onclick: installPresets }, `Add ${missing().length} starter templates`) : null,
          h('button', { onclick: () => edit(null) }, 'Create a template')))
      : shown.length ? shown.map(g => h('section', { class: 'tpl-group' },
        h('h3', {}, `${g.icon} ${g.name}`, h('span', { class: 'muted small' }, ` · ${g.items.length}`), g.about ? h('span', { class: 'muted small', style: { fontWeight: 400 } }, ` — ${g.about}`) : null),
        h('div', { class: 'cards' }, g.items.map(card))))
        : h('div', { class: 'card empty small' }, 'No templates match.'));
  };

  const search = h('input', { type: 'text', placeholder: 'Search templates…', style: { width: '220px' }, oninput: e => { query = e.target.value.trim().toLowerCase(); render(); } });
  fill(page,
    h('div', { class: 'page-head' }, h('h1', {}, 'Agent templates'), h('div', { class: 'spacer' }), search, restoreBtn,
      h('button', { class: 'primary', onclick: () => edit(null) }, '+ New template')),
    pageTabs(''),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Reusable agent profiles by functional area and language: role, areas of expertise, skills and rules. Each template has a symbol such as ',
      h('code', {}, '@python-developer'), '. Agent groups combine templates into teams; you can also base a single project agent on a template.'),
    filterEl, listEl);
  await load(false);
}

// ================================================================ agent groups

/** Add (group null), edit or copy an agent group: its members, each with a template, a name and a lead. */
function groupModal(group, { templates, onSaved, copy = false }) {
  const editing = group && !copy;
  const g = group || { name: '', slug: '', category: '', description: '', members: [] };
  let n = 0;
  const newKey = () => `m${Date.now().toString(36)}${n++}`;
  let members = g.members.map(m => ({ key: m.key, template: m.template, name: m.name, parent: m.parent }));
  const name = h('input', { type: 'text', value: copy ? `${g.name} (copy)` : g.name, placeholder: 'e.g. Python Web Team' });
  const slug = h('input', { type: 'text', class: 'mono', value: editing ? g.symbol : '', placeholder: '#python-web-team' });
  const slugify = s => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  const autoSlug = () => { if (!slug.dataset.touched) slug.value = name.value.trim() ? `#${slugify(name.value)}` : ''; };
  name.addEventListener('input', autoSlug);
  slug.addEventListener('input', () => { slug.dataset.touched = '1'; });
  if (!editing) autoSlug();
  const category = h('input', { type: 'text', value: g.category, placeholder: 'e.g. Language teams' });
  const desc = h('input', { type: 'text', value: g.description, placeholder: 'What this team is good for' });
  const rows = h('div', { class: 'member-rows' });
  const byTemplate = slug => templates.find(t => t.slug === slug.replace(/^@/, '').toLowerCase());

  // Members in hierarchy order with their depth; members whose manager is missing go under the lead.
  const ordered = () => {
    const keys = new Set(members.map(m => m.key));
    const root = members.find(m => !m.parent || !keys.has(m.parent)) || members[0];
    const out = [];
    const walk = (m, depth) => { out.push({ m, depth }); members.filter(c => c !== m && c.parent === m.key).forEach(c => walk(c, depth + 1)); };
    if (root) { root.parent = null; walk(root, 0); }
    members.filter(m => !out.some(o => o.m === m)).forEach(m => { m.parent = root.key; out.push({ m, depth: 1 }); });
    return out;
  };
  const below = m => {
    const set = new Set([m.key]);
    let grew = true;
    while (grew) { grew = false; for (const x of members) if (set.has(x.parent) && !set.has(x.key)) { set.add(x.key); grew = true; } }
    return set;
  };
  const render = () => {
    const list = ordered();
    fill(rows, list.length ? list.map(({ m, depth }) => {
      const tplInput = h('input', { type: 'text', class: 'mono', value: m.template ? `@${m.template}` : '', placeholder: '@template' });
      autocomplete(tplInput, {
        source: q => rankMatches(templateItems(templates), q, 12),
        onPick: it => {
          const t = byTemplate(it.value);
          if (t && (!m.name || m.name === byTemplate(`@${m.template}`)?.name)) m.name = t.name;
          m.template = t?.slug || '';
          render();
        },
      });
      tplInput.addEventListener('change', () => { const t = byTemplate(tplInput.value); if (t) { m.template = t.slug; render(); } });
      const nameInput = h('input', { type: 'text', value: m.name, placeholder: 'Name', oninput: e => { m.name = e.target.value; } });
      const exclude = below(m);
      const parent = h('select', {
        onchange: e => {
          const v = e.target.value;
          if (!v) {  // this member becomes the lead; the old lead reports to it
            const old = members.find(x => !x.parent);
            if (old && old !== m) old.parent = m.key;
            m.parent = null;
          } else m.parent = v;
          render();
        },
      }, h('option', { value: '', selected: !m.parent }, '— leads the group'),
      list.filter(o => !exclude.has(o.m.key)).map(o => h('option', { value: o.m.key, selected: o.m.key === m.parent }, `reports to ${o.m.name || '(unnamed)'}`)));
      return h('div', { class: 'member-row', style: { '--depth': depth } },
        h('span', { class: 'tn-ico' }, depth === 0 ? '👑' : '🤖'), nameInput, tplInput, parent,
        h('button', { type: 'button', class: 'ghost icon sm', title: 'Add a member reporting to this one', onclick: () => { members.push({ key: newKey(), template: '', name: '', parent: m.key }); render(); } }, '+'),
        h('button', {
          type: 'button', class: 'ghost icon sm', title: 'Remove', onclick: () => {
            members = members.filter(x => x !== m);
            members.forEach(x => { if (x.parent === m.key) x.parent = m.parent; });
            render();
          },
        }, '✕'));
    }) : h('div', { class: 'small muted' }, 'No members yet. Add the lead first.'));
  };
  render();
  modal({
    title: editing ? `Edit group ${g.symbol}` : 'New agent group',
    wide: true,
    body: h('div', { class: 'form-grid' },
      h('div', { class: 'form-grid two' }, field('Name', name), field('Symbol', slug, 'How you refer to the group, e.g. when you register a project.')),
      h('div', { class: 'form-grid two' }, field('Category', category), field('Description', desc)),
      h('div', { class: 'field' }, 'Members',
        h('span', { class: 'hint' }, 'One member leads the group and receives the project’s tasks; the others report to a member above them. Leads delegate sub-tasks to the members below them.'),
        rows,
        h('div', {}, h('button', { type: 'button', class: 'sm', onclick: () => { members.push({ key: newKey(), template: '', name: '', parent: ordered()[0]?.m.key || null }); render(); } }, '+ Add member')))),
    actions: [{
      label: editing ? 'Save group' : 'Create group', kind: 'primary', onClick: async () => {
        if (!name.value.trim()) { toast('A name is required.', 'error'); return false; }
        const body = { name: name.value, slug: slug.value.trim(), category: category.value, description: desc.value,
          members: ordered().map(({ m }) => ({ key: m.key, template: m.template, name: m.name, parent: m.parent })) };
        try {
          const saved = editing ? await api('PUT', `/api/groups/${g.id}`, body) : await api('POST', '/api/groups', body);
          toast(`Group ${saved.symbol} saved`, 'ok');
          onSaved(saved);
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => name.focus(), 30);
}

async function groupsView(page) {
  let groups = [];
  let templates = [];
  let presets = [];
  let query = '';
  const listEl = h('div');
  const restoreBtn = h('button', { onclick: () => installPresets() });
  const load = async quiet => {
    [groups, templates, presets] = await Promise.all([api('GET', '/api/groups', undefined, { quiet }), api('GET', '/api/templates', undefined, { quiet }),
      api('GET', '/api/groups/presets', undefined, { quiet })]);
    render();
  };
  const reload = () => load(true);
  const edit = (g, copy = false) => groupModal(g, { templates, copy, onSaved: reload });
  const remove = async g => {
    if (!await confirmDialog(`Delete the group ${g.symbol} "${g.name}"?\n\nProjects created from it keep their teams.`, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/groups/${g.id}`);
    toast('Group deleted', 'ok');
    reload();
  };
  const missing = () => presets.filter(p => !p.installed);
  const installPresets = async () => {
    const names = missing().map(p => p.name);
    if (!await confirmDialog(`Add ${names.length} starter group(s)?\n\n${names.join(', ')}`, { okLabel: 'Add', title: 'Starter groups' })) return;
    const res = await api('POST', '/api/groups/presets/install', {});
    toast(`${res.installed} starter group(s) added`, 'ok');
    reload();
  };
  const tree = members => h('div', { class: 'team-tree compact' }, members.map(m => h('div', { class: `team-node${m.depth === 0 ? ' lead' : ''}`, style: { '--depth': m.depth } },
    h('span', { class: 'tn-ico' }, m.depth === 0 ? '👑' : '🤖'),
    h('span', { class: 'grow ellipsis', title: m.role }, h('b', {}, m.name), ' ', h('code', { class: 'sym' }, `@${m.template}`)))));
  const card = g => h('div', { class: 'card tpl-card', onclick: () => edit(g) },
    h('div', { class: 'row' },
      h('div', { class: 'grow', style: { minWidth: 0 } }, h('h2', { class: 'ellipsis', style: { margin: 0 } }, g.name), h('code', { class: 'sym' }, g.symbol)),
      h('button', { class: 'ghost icon sm', title: 'Duplicate', onclick: e => { e.stopPropagation(); edit(g, true); } }, '⎘'),
      h('button', { class: 'ghost icon sm', title: 'Delete', onclick: e => { e.stopPropagation(); remove(g); } }, '🗑')),
    g.description ? h('div', { class: 'small muted' }, g.description) : null,
    tree(g.members),
    h('div', { class: 'row small muted', style: { marginTop: 'auto' } },
      h('span', { class: 'grow' }, `👥 ${g.members.length} agents`),
      h('a', { href: `#/projects?new=1&group=${g.slug}`, onclick: e => e.stopPropagation() }, 'New project with this team →')));
  const matches = g => !query || [g.name, g.symbol, g.description, g.category, ...g.members.flatMap(m => [m.name, m.template])].join(' ').toLowerCase().includes(query);
  const render = () => {
    restoreBtn.hidden = !missing().length;
    fill(restoreBtn, `＋ Starter groups (${missing().length})`);
    const cats = [...new Set(groups.map(g => g.category || 'Other'))];
    const shown = cats.map(c => ({ c, items: groups.filter(g => (g.category || 'Other') === c && matches(g)) })).filter(x => x.items.length);
    fill(listEl, !groups.length
      ? h('div', { class: 'card empty' }, h('div', { class: 'big' }, '👥'), h('p', {}, 'No agent groups yet.'),
        h('div', { class: 'row', style: { justifyContent: 'center' } },
          missing().length ? h('button', { class: 'primary', onclick: installPresets }, `Add ${missing().length} starter groups`) : null,
          h('button', { onclick: () => edit(null) }, 'Create a group')))
      : shown.length ? shown.map(({ c, items }) => h('section', { class: 'tpl-group' },
        h('h3', {}, c, h('span', { class: 'muted small' }, ` · ${items.length}`)), h('div', { class: 'cards' }, items.map(card))))
        : h('div', { class: 'card empty small' }, 'No groups match.'));
  };
  const search = h('input', { type: 'text', placeholder: 'Search groups…', style: { width: '220px' }, oninput: e => { query = e.target.value.trim().toLowerCase(); render(); } });
  fill(page,
    h('div', { class: 'page-head' }, h('h1', {}, 'Agent groups'), h('div', { class: 'spacer' }), search, restoreBtn,
      h('button', { class: 'primary', onclick: () => edit(null) }, '+ New group')),
    pageTabs('groups'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Teams of agent templates with a hierarchy, such as ', h('code', {}, '#python-team'),
      '. Registering a project with a group creates the whole team. The lead receives your tasks, breaks them into sub-tasks and delegates them down the hierarchy; the agents consult each other, ask you when only you can decide, and record what they learn.'),
    listEl);
  await load(false);
}
