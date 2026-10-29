// Project teams: the team tree and agent editor, the agent group picker, questions the agents ask the
// user, the team discussion of a task, and the knowledge (lessons learned) of a project.

import { api, h, fill, go, field, state, projectBase, toast, modal, confirmDialog, md, timeAgo, fmtDate, modelOptions, effortOptions } from './core.js';
import { autocomplete, rankMatches, agentTemplateFields } from './templates.js';
import { pickDirectory } from './files.js';

// Fills an agent form from a chosen template: its role when the role is still empty, its default model
// and effort, and its name when `name` is given and still untouched.
export const applyTemplate = (t, { role, model, effort, name }) => {
  if (!t) return;
  if (!role.value.trim() && t.role) role.value = t.role;
  if (t.model) model.value = t.model;
  if (t.effort) effort.value = t.effort;
  if (name && !name.dataset.touched) name.value = t.name;
};

const modelLabel = id => (state.info?.models || []).find(m => m.id === id)?.label || id;

// ================================================================ team tree

/** The team as an indented tree. Callbacks add buttons: onEdit(agent), onAdd(agent) (add a report), onRemove(agent). */
export function teamTree(agents, { onEdit, onAdd, onRemove, compact = false } = {}) {
  return h('div', { class: `team-tree${compact ? ' compact' : ''}` }, agents.map(a => h('div', {
    class: `team-node${a.depth === 0 ? ' lead' : ''}`, style: { '--depth': a.depth }, onclick: onEdit ? () => onEdit(a) : null,
  },
  h('span', { class: 'tn-ico', title: a.depth === 0 ? 'Lead: receives the project’s tasks' : '' }, a.depth === 0 ? '👑' : '🤖'),
  h('div', { class: 'grow', style: { minWidth: 0 } },
    h('div', { class: 'row', style: { gap: '6px', flexWrap: 'wrap' } }, h('b', {}, a.name),
      h('span', { class: 'mono small muted' }, a.key),
      a.template ? h('code', { class: 'sym' }, `@${a.template}`) : null),
    !compact && a.role ? h('div', { class: 'small muted ellipsis' }, a.role) : null),
  compact ? null : h('span', { class: 'small muted', style: { whiteSpace: 'nowrap' } }, `${modelLabel(a.model)} · ${a.effort}`),
  onAdd ? h('button', { class: 'ghost icon sm', title: `Add an agent reporting to ${a.name}`, onclick: e => { e.stopPropagation(); onAdd(a); } }, '+') : null,
  onRemove && a.depth > 0 ? h('button', { class: 'ghost icon sm', title: 'Remove from the team', onclick: e => { e.stopPropagation(); onRemove(a); } }, '🗑') : null)));
}

/** Add (agent null) or edit an agent of a project team. */
export function agentModal(project, agent, templates, { parentId = null, onSaved }) {
  const a = agent || { name: '', role: '', instructions: '', model: project.agent?.model, effort: project.agent?.effort, template: '', subagents: [], parent_id: parentId };
  const isLead = agent && agent.depth === 0;
  const name = h('input', { type: 'text', value: a.name, placeholder: 'e.g. Backend Developer' });
  if (agent) name.dataset.touched = '1';
  name.addEventListener('input', () => { name.dataset.touched = '1'; });
  const role = h('input', { type: 'text', value: a.role, placeholder: 'e.g. Senior Python developer' });
  const instructions = h('textarea', { rows: 4, value: a.instructions, placeholder: 'Conventions, constraints and preferences for this agent' });
  const model = h('select', {}, modelOptions(a.model));
  const effort = h('select', {}, effortOptions(a.effort));
  const tpl = agentTemplateFields(templates, { template: a.template, subagents: a.subagents, onTemplate: t => applyTemplate(t, { role, model, effort, name }) });
  const below = new Set();
  if (agent) {  // an agent cannot report to itself or to anyone below it
    let grew = true;
    below.add(agent.id);
    while (grew) {
      grew = false;
      for (const x of project.agents) if (below.has(x.parent_id) && !below.has(x.id)) { below.add(x.id); grew = true; }
    }
  }
  const parent = h('select', {}, project.agents.filter(x => !below.has(x.id)).map(x =>
    h('option', { value: x.id, selected: x.id === a.parent_id }, `${'  '.repeat(x.depth)}${x.name}`)));
  modal({
    title: agent ? `Edit ${agent.name}` : 'Add an agent to the team',
    wide: true,
    body: h('div', { class: 'form-grid' },
      tpl.templateField,
      h('div', { class: 'form-grid two' }, field('Name', name), field('Role', role)),
      isLead ? h('div', { class: 'small muted' }, '👑 The lead receives the tasks you add to the project and delegates them down the team.')
        : field('Reports to', parent, 'Its lead assigns it sub-tasks and reviews its work.'),
      field('Instructions', instructions),
      tpl.subagentsField,
      h('div', { class: 'form-grid two' }, field('Model', model), field('Effort', effort))),
    actions: [{
      label: agent ? 'Save agent' : 'Add agent', kind: 'primary', onClick: async () => {
        if (!name.value.trim()) { toast('A name is required.', 'error'); return false; }
        const body = { name: name.value, role: role.value, instructions: instructions.value, model: model.value, effort: effort.value, parent_id: isLead ? null : +parent.value, ...tpl.get() };
        try {
          const p = agent ? await api('PUT', `/api/agents/${agent.id}`, body) : await api('POST', `/api/projects/${project.id}/agents`, body);
          toast(agent ? 'Agent saved' : 'Agent added', 'ok');
          onSaved(p);
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => (agent ? name : tpl.templateField.querySelector('input')).focus(), 30);
}

// ================================================================ group picker

/** An input for an agent group symbol (#slug) with autocomplete, and a preview of the team it creates. */
export function groupPicker(groups, { value = '', onChange } = {}) {
  const items = groups.map(g => ({ value: g.symbol, label: g.name, hint: `${g.members.length} agents · ${g.category || g.description}` }));
  const input = h('input', { type: 'text', class: 'mono', value: value ? `#${value.replace(/^#/, '')}` : '', placeholder: 'Type # to choose an agent group' });
  const preview = h('div', { class: 'group-preview' });
  const find = () => groups.find(g => g.slug === input.value.trim().replace(/^#/, '').toLowerCase());
  const show = () => {
    const g = find();
    fill(preview, !input.value.trim() ? h('div', { class: 'small muted' }, 'Choose a group to see its team.')
      : g ? [h('div', { class: 'small' }, h('b', {}, g.name), ` — ${g.description}`), groupTree(g.members)]
        : h('div', { class: 'small', style: { color: 'var(--err)' } }, `⚠ No group ${input.value.trim()}`));
    onChange?.(g || null);
  };
  autocomplete(input, { source: q => rankMatches(items, q, 30), onPick: it => { input.value = it.value; show(); } });
  input.addEventListener('input', show);
  show();
  return { input, preview, get: () => input.value.trim(), group: find };
}

/** A group's members ({name, template, depth}) as a compact tree. */
export function groupTree(members) {
  return h('div', { class: 'team-tree compact' }, members.map(m => h('div', { class: `team-node${m.depth === 0 ? ' lead' : ''}`, style: { '--depth': m.depth } },
    h('span', { class: 'tn-ico' }, m.depth === 0 ? '👑' : '🤖'),
    h('span', { class: 'grow ellipsis' }, h('b', {}, m.name), ' ', h('code', { class: 'sym' }, `@${m.template}`)))));
}

// ================================================================ team & settings tab

export async function teamTab(el, project, onSaved) {
  const [templates, groups] = await Promise.all([api('GET', '/api/templates'), api('GET', '/api/groups')]);
  const treeHost = h('div');
  const reload = async p => { project = p; onSaved(p); renderTree(); };
  const edit = a => agentModal(project, a, templates, { onSaved: reload });
  const add = parent => agentModal(project, null, templates, { parentId: parent?.id ?? project.agent.id, onSaved: reload });
  const remove = async a => {
    const reports = project.agents.filter(x => x.parent_id === a.id).length;
    if (!await confirmDialog(`Remove ${a.name} from the team?${reports ? `\n\nIts ${reports} report(s) will report to its lead instead.` : ''}\nTasks assigned to it move to its lead.`, { okLabel: 'Remove', danger: true })) return;
    reload(await api('DELETE', `/api/agents/${a.id}`));
  };
  const applyGroup = () => {
    const picker = groupPicker(groups);
    modal({
      title: 'Apply an agent group',
      wide: true,
      body: h('div', { class: 'form-grid' },
        h('div', { class: 'notice warn' }, `This replaces the current team (${project.agents.length} agent(s)) with the group’s team. Tasks move to the new lead; each agent’s own lessons are removed, team and role lessons stay.`),
        field('Agent group', picker.input), picker.preview),
      actions: [{
        label: 'Replace the team', kind: 'danger', onClick: async () => {
          if (!picker.group()) { toast('Choose a group.', 'error'); return false; }
          try { reload(await api('PUT', `/api/projects/${project.id}/team`, { group: picker.get() })); toast('Team replaced', 'ok'); } catch { return false; }
        },
      }],
    });
    setTimeout(() => picker.input.focus(), 30);
  };
  const renderTree = () => fill(treeHost, teamTree(project.agents, { onEdit: edit, onAdd: add, onRemove: remove }));

  const name = h('input', { type: 'text', value: project.name });
  const desc = h('textarea', { rows: 3, value: project.description });
  const repo = h('input', { type: 'text', class: 'mono', value: project.repo_path });
  const create = h('input', { type: 'checkbox' });
  const dev = project.system === 'dev';   // the development team of this website: its repository is the site's source
  el.append(h('div', { class: 'two-col', style: { alignItems: 'start' } },
    h('div', { class: 'card stack' },
      h('div', { class: 'row' }, h('h2', { class: 'grow', style: { margin: 0 } }, `👥 Team (${project.agents.length})`),
        h('button', { class: 'sm', onclick: () => add(null) }, '+ Add agent'),
        h('button', { class: 'sm', onclick: applyGroup }, '⇄ Apply group…')),
      h('div', { class: 'small muted' }, `Tasks you add go to the lead (👑), who breaks them into sub-tasks and delegates them down the team; indentation shows who reports to whom. Agents consult each other, ask you when only you can decide, and record what they learn. Click an agent to edit it.`),
      treeHost),
    h('div', { class: 'stack' },
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, dev ? 'Development team' : 'Project'),
        field('Name', name),
        field('Description', desc, dev ? 'Every agent of the team reads this in its briefing.' : undefined),
        dev ? h('div', { class: 'small muted' }, 'Works in this website’s source folder ', h('code', {}, project.repo_path), '.') : null,
        dev ? null : h('label', { class: 'field' }, 'Repository folder', h('div', { class: 'row' }, repo, h('button', {
          onclick: async () => {
            const picked = await pickDirectory({ title: 'Choose the project repository', start: repo.value || '/', okLabel: 'Use this folder' });
            if (picked) repo.value = picked;
          },
        }, '📁 Browse…'))),
        dev ? null : h('label', { class: 'check' }, create, 'Create the folder if it does not exist'),
        h('div', {}, h('button', {
          class: 'primary', onclick: async () => {
            const p = await api('PUT', `/api/projects/${project.id}`, { name: name.value, description: desc.value, repo_path: repo.value, create_repo: create.checked });
            toast(dev ? 'Saved' : 'Project saved', 'ok');
            onSaved({ ...p, repo_exists: true });
          },
        }, dev ? 'Save' : 'Save project'))),
      dev ? null : h('div', { class: 'card stack' },   // the built-in Developer project cannot be deleted
        h('h2', { style: { margin: 0 } }, 'Delete project'),
        h('div', { class: 'small muted' }, 'Removes the project, its team, tasks, agent logs and the team’s lessons from Agent Group. Files in the repository are not touched.'),
        h('div', {}, h('button', {
          class: 'danger', onclick: async () => { if (await deleteProject(project)) go('#/projects'); },
        }, '🗑 Delete project'))))));
  renderTree();
}

/** Ask for confirmation, then delete the project (stopping its running agents). Returns true when deleted.
 *  Files in the repository are never touched. */
export async function deleteProject(project) {
  const running = project.running || project.setup?.running ? '\n\nAgents working on it are stopped.' : '';
  if (!await confirmDialog(`Delete the project "${project.name}"?\n\nIts team, tasks, agent logs and lessons are removed from Agent Group. The files in ${project.repo_path} stay.${running}`,
  { okLabel: 'Delete', danger: true })) return false;
  await api('DELETE', `/api/projects/${project.id}`);
  toast(`Project "${project.name}" deleted`, 'ok');
  return true;
}

// ================================================================ questions

/** Open questions with answer controls, then answered ones collapsed. `onChange` runs after an answer. */
export function questionsCard(questions, { currentTask, projectId, onChange } = {}) {
  const open = questions.filter(q => q.status === 'open');
  const done = questions.filter(q => q.status !== 'open');
  if (!open.length && !done.length) return null;
  const answer = async (q, text) => {
    if (!text?.trim()) { toast('Write an answer first.', 'error'); return; }
    const res = await api('POST', `/api/questions/${q.id}/answer`, { answer: text });
    toast(res.delivered ? `${q.agent_name} continues with your answer` : res.resumed ? 'The team lead continues with your answer' : 'Answer saved', 'ok');
    onChange?.();
  };
  const item = q => {
    const box = h('textarea', { rows: 2, placeholder: 'Your answer…' });
    box.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) answer(q, box.value); });
    return h('div', { class: 'question' },
      h('div', { class: 'row small' }, h('b', {}, `❓ ${q.agent_name}`),
        q.task_id !== currentTask ? h('a', { href: `${projectBase(q.project_id ?? projectId)}/tasks/${q.task_id}` }, `on #${q.task_id} ${q.task_title || ''}`) : null,
        h('span', { class: 'grow muted', style: { textAlign: 'right' } }, timeAgo(q.created_at))),
      md(q.question),
      q.options?.length ? h('div', { class: 'row wrap', style: { gap: '6px' } }, q.options.map(o => h('button', { class: 'sm', onclick: () => answer(q, o) }, o))) : null,
      box,
      h('div', { class: 'row' }, h('button', { class: 'sm primary', onclick: () => answer(q, box.value) }, 'Answer'),
        h('button', {
          class: 'sm ghost', onclick: async () => {
            await api('POST', `/api/questions/${q.id}/dismiss`);
            onChange?.();
          },
        }, 'Dismiss'), h('span', { class: 'small muted' }, 'Ctrl+Enter to send')));
  };
  return h('div', { class: `card pad-sm${open.length ? ' questions-open' : ''}` },
    open.length ? [h('h3', {}, `The team needs your input (${open.length})`), open.map(item)] : null,
    done.length ? h('details', {}, h('summary', { class: 'small muted', style: { cursor: 'pointer' } }, `Answered questions (${done.length})`),
      done.map(q => h('div', { class: 'small', style: { marginTop: '8px' } }, h('b', {}, q.agent_name), ': ', q.question,
        h('div', { class: 'muted' }, '↳ ', q.status === 'answered' ? q.answer : 'dismissed')))) : null);
}

// ================================================================ discussion

const KIND_ICON = { plan: '🗂', delegate: '➡️', report: '⬅️', review: '✔️', question: '❓', answer: '💬', learning: '🧠', note: '📝' };

/** The team discussion of a task tree, loaded on demand. */
export function discussionCard(taskId, count) {
  const body = h('div', { class: 'discussion' }, h('div', { class: 'small muted' }, 'Loading…'));
  let loaded = false;
  const load = async () => {
    const entries = await api('GET', `/api/tasks/${taskId}/discussion`, undefined, { quiet: true });
    loaded = true;
    fill(body, entries.length ? entries.map(e => h('div', { class: `disc-entry ${e.kind}` },
      h('div', { class: 'row small' }, h('span', {}, KIND_ICON[e.kind] || '•'), h('b', {}, e.sender), h('span', { class: 'muted' }, '→'), h('b', {}, e.recipient || 'team'),
        h('span', { class: 'muted' }, e.kind), e.task_id ? h('span', { class: 'muted ellipsis' }, `· #${e.task_id} ${e.task_title || ''}`) : null,
        h('span', { class: 'grow muted', style: { textAlign: 'right' }, title: fmtDate(e.created_at) }, timeAgo(e.created_at))),
      md(e.text))) : h('div', { class: 'small muted' }, 'No team discussion yet. Delegations, reports, reviews, consultations, questions and lessons show up here.'));
  };
  const summary = h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, `💬 Team discussion (${count})`);
  const card = h('details', { class: 'card pad-sm', ontoggle: e => { if (e.target.open) load(); } }, summary, body);
  card.refresh = n => {
    if (n !== undefined && n !== count) {
      count = n;
      summary.textContent = `💬 Team discussion (${n})`;
      if (card.open || loaded) load();
    }
  };
  return card;
}

// ================================================================ knowledge

export async function knowledgeTab(el, project) {
  const host = h('div', { class: 'stack' });
  el.append(host);
  const text = h('textarea', { rows: 2, placeholder: 'e.g. "Always run make lint before committing" or "The customer prefers British English."' });
  const target = h('select', {}, h('option', { value: 'team:' }, 'The whole team'),
    project.agents.map(a => h('option', { value: `self:${a.id}` }, `${a.name} only`)),
    project.agents.filter(a => a.template).map(a => h('option', { value: `role:${a.id}` }, `Every @${a.template} agent, in all projects`)));
  const add = async () => {
    if (!text.value.trim()) return;
    const [scope, id] = target.value.split(':');
    await api('POST', `/api/projects/${project.id}/learnings`, { text: text.value, scope, agent_id: id ? +id : null });
    text.value = '';
    toast('Lesson added', 'ok');
    render();
  };
  const render = async () => {
    const rows = await api('GET', `/api/projects/${project.id}/learnings`, undefined, { quiet: true });
    const byAgent = Object.fromEntries(project.agents.map(a => [a.id, a]));
    const groupsOf = [
      ['👥 Team lessons', 'Shared by every agent of this project.', rows.filter(r => r.scope === 'team')],
      ...project.agents.map(a => [`🤖 ${a.name}`, 'Its own lessons in this project.', rows.filter(r => r.scope === 'self' && r.agent_id === a.id)]),
      ...[...new Set(rows.filter(r => r.scope === 'role').map(r => r.template))].map(t => [`🧩 @${t}`, 'Know-how for every agent based on this template, in all projects.', rows.filter(r => r.scope === 'role' && r.template === t)]),
    ].filter(([, , list]) => list.length);
    const remove = async r => {
      if (!await confirmDialog(`Delete this lesson?\n\n${r.text}`, { okLabel: 'Delete', danger: true })) return;
      await api('DELETE', `/api/learnings/${r.id}`);
      render();
    };
    fill(host,
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, '🧠 Knowledge'),
        h('div', { class: 'small muted' }, 'Agents record lessons while they work: conventions, pitfalls, techniques that worked, your feedback. The lessons that apply to an agent are part of every briefing it gets, so the team improves over time. You can add lessons too.'),
        h('div', { class: 'form-grid two' }, field('Lesson', text), field('For', target)),
        h('div', {}, h('button', { class: 'primary', onclick: add }, 'Add lesson'))),
      project.brief ? h('div', { class: 'card pad-sm' },
        h('h3', {}, '📋 Project brief', h('span', { class: 'small muted', style: { fontWeight: 400 } }, ' · The setup agent’s analysis of the project and its repository. Part of every agent’s briefing.')),
        md(project.brief)) : null,
      groupsOf.length ? groupsOf.map(([title, about, list]) => h('div', { class: 'card pad-sm' },
        h('h3', {}, title, h('span', { class: 'small muted', style: { fontWeight: 400 } }, ` · ${about}`)),
        h('ul', { class: 'item-list' }, list.map(r => h('li', {},
          h('span', { class: 'txt' }, r.text, h('div', { class: 'small muted' }, `${r.author === 'user' ? '👤 you' : `🤖 ${r.author}`}${r.task_id ? ` · task #${r.task_id}` : ''} · ${timeAgo(r.created_at)}${r.scope === 'role' && byAgent[r.agent_id] === undefined && r.project_id !== project.id ? ' · from another project' : ''}`)),
          h('button', { class: 'ghost sm', title: 'Delete', onclick: () => remove(r) }, '✕'))))))
        : h('div', { class: 'card empty small' }, 'No lessons yet. They accumulate as the team works on tasks.'));
  };
  await render();
}
