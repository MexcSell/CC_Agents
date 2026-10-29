// Page renderers. Each page is `async (container, routeParams, query) => cleanup | undefined`.

import {
  api, h, fill, enc, go, field, state, projectBase, toast, modal, banner, confirmDialog, promptDialog, contextMenu, md, resizer,
  fmtDate, timeAgo, fmtDuration, fmtHours, statusBadge, progressBar, STATUS_LABELS, modelOptions, effortOptions,
} from './core.js';
import { ConversationView } from './conversation.js';
import { FileExplorer, pickDirectory } from './files.js';
import { teamTab, knowledgeTab, questionsCard, discussionCard, deleteProject } from './team.js';

const runDot = () => h('span', { class: 'dot run', title: 'Agent working' });

// Pane heading and list-item icon; the icons are what remains when a pane is collapsed.
const paneTitle = (ico, text, attrs = {}) => h('h2', attrs, h('span', { class: 'pico', title: `${text} · click to expand` }, ico), h('span', { class: 'txt' }, text));
const itemIcon = (title, active) => h('span', { class: 'li-ico', title }, ([...(title || '').trim()][0] || '?').toUpperCase(), active ? runDot() : null);
const newBtn = (title, onclick) => h('button', { class: 'sm primary', title, onclick }, '+', h('span', { class: 'lbl' }, ' New'));
// Explanatory note under a pane heading: one truncated line until clicked. Expanded notes (keyed by text)
// stay open across list re-renders until the page reloads.
const openNotes = new Set();
const paneNote = (text, cls = '') => h('details', {
  class: `small muted pane-note ${cls}`, open: openNotes.has(text),
  ontoggle: e => e.target.open ? openNotes.add(text) : openNotes.delete(text),
}, h('summary', { title: 'Show / hide' }, text));
// Re-render a list pane, keeping the scroll position of its .pane-body.
const refill = (pane, ...children) => {
  const top = pane.querySelector('.pane-body')?.scrollTop || 0;
  fill(pane, ...children);
  const body = pane.querySelector('.pane-body');
  if (body) body.scrollTop = top;
};

// ================================================================ dashboard

export async function dashboardPage(view) {
  const page = h('div', { class: 'page' });
  view.append(page);
  const stat = (label, num, href, sub) => h('a', { class: 'card stat', href, style: { color: 'inherit', textDecoration: 'none' } },
    h('span', { class: 'lbl' }, label), h('span', { class: 'num' }, num), sub ? h('span', { class: 'small muted' }, sub) : null);
  const render = async () => {
    const d = await api('GET', '/api/dashboard', undefined, { quiet: true });
    const typing = document.activeElement;
    if (page.contains(typing) && typing.tagName === 'TEXTAREA' && typing.value) return;  // an answer is being written
    const t = d.tasks;
    const total = Object.values(t).reduce((a, b) => a + b, 0);
    fill(page,
      h('div', { class: 'page-head' }, h('h1', {}, 'Dashboard'), h('div', { class: 'spacer' }),
        h('a', { class: 'btn', href: '#/chat' }, '💬 Chat with Claude'),
        h('a', { class: 'btn', href: '#/projects?new=1' }, '📋 Register a project'),
        h('a', { class: 'btn', href: '#/dev' }, '🛠 Change this site')),
      d.has_credentials ? null : h('div', { class: 'notice warn', style: { marginBottom: '16px' } }, d.engine === 'claude_code'
        ? ['Claude Code is not logged in on this server. Run ', h('code', {}, 'claude'), ' in a terminal on the server and log in, or ',
          h('a', { href: '#/settings' }, 'switch to an API key in Settings'), '.']
        : ['No Anthropic API key is configured yet. ', h('a', { href: '#/settings' }, 'Add one in Settings'), ' so the agents can talk to Claude.']),
      d.questions.length ? h('div', { style: { marginBottom: '16px' } }, questionsCard(d.questions, { onChange: render })) : null,
      h('div', { class: 'cards', style: { marginBottom: '16px' } },
        stat('Topics', d.topics, '#/chat'),
        stat('Conversations', d.conversations, '#/chat'),
        stat('Projects', d.projects, '#/projects'),
        stat('Tasks', total, '#/projects', `${t.in_progress || 0} in progress · ${t.blocked || 0} blocked · ${t.done || 0} done`)),
      h('div', { class: 'two-col' },
        h('div', { class: 'card' }, h('h2', {}, 'Running agents'),
          d.active_runs.length
            ? h('div', { class: 'stack', style: { gap: '8px' } }, d.active_runs.map(r => h('a', { href: `#/c/${r.conversation_id}`, class: 'row' },
              runDot(), h('span', { class: 'badge' }, r.kind), h('span', { class: 'grow ellipsis' }, r.title), h('span', { class: 'small muted' }, fmtDuration(Date.now() / 1000 - r.started)))))
            : h('div', { class: 'muted' }, 'No agents are running right now.')),
        h('div', { class: 'card' }, h('h2', {}, 'Recently updated tasks'),
          d.recent_tasks.length
            ? h('div', { class: 'stack', style: { gap: '10px' } }, d.recent_tasks.map(task => h('a', { href: `${projectBase(task.project_id)}/tasks/${task.id}`, style: { color: 'inherit' } },
              h('div', { class: 'row' }, h('span', { class: 'grow ellipsis' }, h('b', {}, task.title), h('span', { class: 'muted small' }, ` · ${task.project}`)), statusBadge(task.status)),
              h('div', { style: { marginTop: '5px' } }, progressBar(task.progress, task.status === 'done')))))
            : h('div', { class: 'muted' }, 'No tasks yet.'))),
      h('div', { class: 'card', style: { marginTop: '14px' } }, h('h2', {}, 'How Agent Group works'),
        h('ul', { style: { margin: 0, paddingLeft: '18px', lineHeight: 1.8 } },
          h('li', {}, h('b', {}, 'Chat: '), 'create topics and hold conversations with Claude about them. History is stored; delete single conversations or clear a topic.'),
          h('li', {}, h('b', {}, 'Projects: '), 'register a project with its repository folder and an agent group. The team’s lead breaks each task into sub-tasks and delegates them; the agents consult each other, ask you when only you can decide, report estimate, progress, insights and next actions, and record what they learn.'),
          h('li', {}, h('b', {}, 'Templates & groups: '), 'reusable agent profiles by functional area and language (symbols like @python-developer), and hierarchical agent groups (like #python-team) that become a project’s team.'),
          h('li', {}, h('b', {}, 'Files: '), 'browse the whole file system or a project repository: create, open, edit, move and delete files.'),
          h('li', {}, h('b', {}, 'Developer: '), 'an agent, and a development team of agents (#site-dev-team), that develop and run this website itself and restart it, with automatic code snapshots.'))));
  };
  await render();
  const timer = setInterval(() => render().catch(() => {}), 5000);
  return () => clearInterval(timer);
}

// ================================================================ chat

function topicModal(topic, onSaved) {
  const title = h('input', { type: 'text', value: topic?.title || '', placeholder: 'e.g. Rust learning, Marketing ideas' });
  const desc = h('textarea', { rows: 3, value: topic?.description || '', placeholder: 'What is this topic about?' });
  const instr = h('textarea', { rows: 4, value: topic?.instructions || '', placeholder: 'Optional guidance for Claude in every conversation of this topic, e.g. "Act as a patient Rust mentor. Keep answers short."' });
  modal({
    title: topic ? 'Edit topic' : 'New topic',
    body: h('div', { class: 'form-grid' }, field('Title', title), field('Description', desc),
      field('Instructions for Claude', instr, 'Used when a new conversation in this topic starts.')),
    actions: [{
      label: topic ? 'Save' : 'Create topic', kind: 'primary', onClick: async () => {
        if (!title.value.trim()) { toast('A title is required.', 'error'); return false; }
        const body = { title: title.value, description: desc.value, instructions: instr.value };
        try {
          onSaved(topic ? await api('PUT', `/api/topics/${topic.id}`, body) : await api('POST', '/api/topics', body));
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => title.focus(), 30);
}

export async function chatPage(view, params) {
  let topics = await api('GET', '/api/topics');
  let topicId = null;
  let convId = null;
  let topic = null;

  const topicsPane = h('div', { class: 'pane' });
  const convsPane = h('div', { class: 'pane' });
  const mainPane = h('div', { class: 'pane main' });
  view.append(h('div', { class: 'split chat' },
    topicsPane, resizer(topicsPane, { key: 'chat-topics', width: 240, flex: mainPane }),
    convsPane, resizer(convsPane, { key: 'chat-convs', width: 270, flex: mainPane }),
    mainPane));

  const editTopic = t => topicModal(t, saved => go(`#/chat/${saved.id}`));

  const renderTopics = () => refill(topicsPane,
    h('div', { class: 'pane-head' }, paneTitle('🏷', 'Topics'), newBtn('New topic', () => editTopic(null))),
    h('div', { class: 'pane-body' }, topics.length
      ? topics.map(t => h('div', { class: `list-item${t.id === topicId ? ' active' : ''}`, onclick: () => go(`#/chat/${t.id}`) },
        itemIcon(t.title),
        h('div', { class: 'grow' }, h('div', { class: 'title ellipsis' }, t.title),
          h('div', { class: 'small muted ellipsis' }, `${t.conversation_count} conversation${t.conversation_count === 1 ? '' : 's'}${t.last_activity ? ' · ' + timeAgo(t.last_activity) : ''}`)),
        h('div', { class: 'actions' }, h('button', { class: 'ghost icon sm', title: 'Edit topic', onclick: e => { e.stopPropagation(); editTopic(t); } }, '✎'))))
      : h('div', { class: 'empty small' }, 'Topics group your conversations. Create one to begin.')));

  let convs = [];
  const loadConvs = async () => {
    if (!topic) return;
    const tid = topic.id;
    const list = await api('GET', `/api/conversations?kind=chat&topic_id=${tid}`, undefined, { quiet: true });
    if (topic?.id !== tid) return;  // another topic was selected meanwhile
    convs = list;
    renderConvs();
    const current = convs.find(c => c.id === convId);
    if (current && cv) cv.setTitle(current.title);
    const t = topics.find(x => x.id === topic.id);
    if (t) { t.conversation_count = convs.length; renderTopics(); }
  };

  const deleteConv = async c => {
    if (!await confirmDialog(`Delete the conversation "${c.title}" and its history?`, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/conversations/${c.id}`);
    toast('Conversation deleted', 'ok');
    if (c.id === convId) go(`#/chat/${topic.id}`); else loadConvs();
  };
  const renameConv = async () => {
    const c = convs.find(x => x.id === convId);
    if (!c) return;
    const title = await promptDialog('Rename conversation', { value: c.title, okLabel: 'Rename' });
    if (!title) return;
    await api('PATCH', `/api/conversations/${convId}`, { title });
    loadConvs();
  };
  const topicMenu = e => contextMenu(e.clientX, e.clientY, [
    { label: 'Edit topic…', action: () => editTopic(topic) },
    '-',
    {
      label: 'Clear history (delete all conversations)…', danger: true, action: async () => {
        if (!await confirmDialog(`Delete all ${convs.length} conversation(s) in "${topic.title}"?`, { okLabel: 'Delete all', danger: true })) return;
        await api('DELETE', `/api/topics/${topic.id}/conversations`);
        toast('History cleared', 'ok');
        go(`#/chat/${topic.id}`);
      },
    },
    {
      label: 'Delete topic…', danger: true, action: async () => {
        if (!await confirmDialog(`Delete the topic "${topic.title}" and all of its conversations?`, { okLabel: 'Delete topic', danger: true })) return;
        await api('DELETE', `/api/topics/${topic.id}`);
        toast('Topic deleted', 'ok');
        go('#/chat');
      },
    },
  ]);

  const renderConvs = () => {
    if (!topic) {
      fill(convsPane, h('div', { class: 'pane-head' }, paneTitle('💬', 'Conversations')), h('div', { class: 'empty small' }, 'Select or create a topic.'));
      return;
    }
    refill(convsPane,
      h('div', { class: 'pane-head' }, paneTitle('💬', topic.title, { class: 'ellipsis', title: topic.title }),
        newBtn('New conversation', () => go(`#/chat/${topic.id}`)),
        h('button', { class: 'sm ghost icon', title: 'Topic actions', onclick: topicMenu }, '⋯')),
      topic.description ? paneNote(topic.description) : null,
      h('div', { class: 'pane-body' }, convs.length
        ? convs.map(c => h('div', { class: `list-item${c.id === convId ? ' active' : ''}`, onclick: () => go(`#/chat/${topic.id}/${c.id}`) },
          itemIcon(c.title, c.active),
          h('div', { class: 'grow' }, h('div', { class: 'title ellipsis' }, c.active ? runDot() : null, c.title),
            h('div', { class: 'small muted' }, `${timeAgo(c.updated_at)} · ${c.message_count} messages`)),
          h('div', { class: 'actions' },
            h('a', { class: 'btn ghost icon sm', title: 'Export as Markdown', href: `/api/conversations/${c.id}/export`, onclick: e => e.stopPropagation() }, '⤓'),
            h('button', { class: 'ghost icon sm', title: 'Delete', onclick: e => { e.stopPropagation(); deleteConv(c); } }, '🗑'))))
        : h('div', { class: 'empty small' }, 'No conversations yet. Write a message to start one.')));
  };

  let cv = null;
  const convActions = [
    h('button', { class: 'sm ghost', title: 'Rename', onclick: renameConv }, '✎'),
    h('button', { class: 'sm ghost', title: 'Export as Markdown', onclick: () => { location.href = `/api/conversations/${convId}/export`; } }, '⤓'),
    h('button', { class: 'sm ghost danger', title: 'Delete conversation', onclick: () => deleteConv(convs.find(c => c.id === convId) || { id: convId, title: 'this conversation' }) }, '🗑'),
  ];
  const convOpts = () => ({
    convId,
    title: convId ? '' : `New conversation · ${topic.title}`,
    emptyText: `Start a conversation about "${topic.title}".`,
    sendFn: convId ? null : async (text, images) => {
      if (!text && !images.length) return null;
      const t = topic;
      const c = await api('POST', '/api/conversations', { kind: 'chat', topic_id: t.id });
      convId = c.id;
      cv.opts.sendFn = null;
      history.replaceState(null, '', `#/chat/${t.id}/${c.id}`);
      convActions.forEach(b => { b.hidden = false; });
      const res = await api('POST', `/api/conversations/${c.id}/messages`, { text, images });
      return { ...res, conversation_id: c.id };
    },
  });

  // Show the topic and conversation of the route; called on load and on every selection.
  let first = true;
  let seq = 0;
  const show = async ([topicParam, convParam]) => {
    const mine = ++seq;
    if (!first) {
      const list = await api('GET', '/api/topics', undefined, { quiet: true });  // titles may have changed
      if (mine !== seq) return;
      topics = list;
    }
    first = false;
    const prevTopic = topicId;
    topicId = topicParam ? +topicParam : topics[0]?.id;
    convId = convParam ? +convParam : null;
    if (!topicParam && topicId) history.replaceState(null, '', `#/chat/${topicId}`);
    topic = topics.find(t => t.id === topicId) || null;
    if (topicId !== prevTopic) convs = [];
    renderTopics();
    renderConvs();
    if (!topic) {
      cv?.dispose();
      cv = null;
      fill(mainPane, h('div', { class: 'empty', style: { margin: 'auto' } }, h('div', { class: 'big' }, '💬'),
        h('p', {}, 'Create a topic, then start conversations with Claude about it.'),
        h('button', { class: 'primary', onclick: () => editTopic(null) }, '+ Create a topic')));
      return;
    }
    convActions.forEach(b => { b.hidden = !convId; });
    let loading;
    if (cv) loading = cv.switchTo(convOpts());
    else {
      cv = new ConversationView(mainPane, { ...convOpts(), copyParagraphs: true, headerActions: convActions, onRunState: () => loadConvs() });
      loading = cv.load();
    }
    await Promise.all([loading, loadConvs()]);
    cv?.focus();
  };

  await show(params);
  return {
    update: show,
    dispose: () => cv?.dispose(),
  };
}

// ================================================================ developer

export async function devPage(view, [convParam]) {
  let sessions = await api('GET', '/api/conversations?kind=dev');
  let convId = convParam ? +convParam : null;
  const listPane = h('div', { class: 'pane' });
  const mainPane = h('div', { class: 'pane main' });
  const sidePane = h('div', { class: 'pane side' });
  view.append(h('div', { class: 'split dev' },
    listPane, resizer(listPane, { key: 'dev-sessions', width: 250, flex: mainPane }),
    mainPane,
    resizer(sidePane, { key: 'dev-server', width: 360, min: 260, max: 640, collapseAt: 200, side: 'left', flex: mainPane }), sidePane));

  const refresh = async () => {
    sessions = await api('GET', '/api/conversations?kind=dev', undefined, { quiet: true });
    renderList();
    const current = sessions.find(s => s.id === convId);
    if (current) cv.setTitle(current.title);
  };
  const renameSession = async s => {
    if (!s) return;
    const title = await promptDialog('Rename session', { value: s.title, okLabel: 'Rename' });
    if (!title) return;
    await api('PATCH', `/api/conversations/${s.id}`, { title });
    refresh();
  };
  const renderList = () => refill(listPane,
    h('div', { class: 'pane-head' }, paneTitle('🛠', 'Developer sessions'), newBtn('New session', () => go('#/dev'))),
    paneNote('The Developer agent reads and edits this website’s source code and restarts the server when needed. For larger work, give a task to the development team.'),
    h('div', { class: 'list-item dev-team-link', title: 'A team of agents that develops and runs this website', onclick: () => go('#/dev/team') },
      h('span', { class: 'li-ico' }, '👥'),
      h('div', { class: 'grow' }, h('div', { class: 'title' }, 'Development team'), h('div', { class: 'small muted' }, 'Tasks for the site’s agent team')),
      h('span', { class: 'muted' }, '›')),
    h('div', { class: 'pane-body' }, sessions.length
      ? sessions.map(s => h('div', { class: `list-item${s.id === convId ? ' active' : ''}`, onclick: () => go(`#/dev/${s.id}`) },
        itemIcon(s.title, s.active),
        h('div', { class: 'grow' }, h('div', { class: 'title ellipsis' }, s.active ? runDot() : null, s.title), h('div', { class: 'small muted' }, timeAgo(s.updated_at))),
        h('div', { class: 'actions' },
          h('button', { class: 'ghost icon sm', title: 'Rename session', onclick: e => { e.stopPropagation(); renameSession(s); } }, '✎'),
          h('button', {
            class: 'ghost icon sm', title: 'Delete session', onclick: async e => {
              e.stopPropagation();
              if (!await confirmDialog(`Delete the session "${s.title}"?`, { okLabel: 'Delete', danger: true })) return;
              await api('DELETE', `/api/conversations/${s.id}`);
              if (s.id === convId) go('#/dev'); else refresh();
            },
          }, '🗑'))))
      : h('div', { class: 'empty small' }, 'No sessions yet.')));
  renderList();

  const renameBtn = h('button', { class: 'sm ghost', title: 'Rename', onclick: () => renameSession(sessions.find(s => s.id === convId)) }, '✎');
  renameBtn.hidden = !convId;
  const convOpts = () => ({
    convId,
    title: convId ? '' : 'New developer session',
    sendFn: convId ? null : async (text, images) => {
      if (!text && !images.length) return null;
      const c = await api('POST', '/api/conversations', { kind: 'dev' });
      convId = c.id;
      cv.opts.sendFn = null;
      history.replaceState(null, '', `#/dev/${c.id}`);
      renameBtn.hidden = false;
      const res = await api('POST', `/api/conversations/${c.id}/messages`, { text, images });
      return { ...res, conversation_id: c.id };
    },
  });
  const cv = new ConversationView(mainPane, {
    ...convOpts(),
    agentName: 'Developer agent',
    copyParagraphs: true,
    headerActions: [renameBtn],
    placeholder: 'Describe a change to this website… (Enter to send)',
    emptyText: 'Ask for a change to this website, e.g. "Add a light/dark theme switch to the sidebar" or "Show each project\'s total estimated hours on the dashboard".',
    onRunState: () => { refresh(); renderServerPanel(sidePane); },
  });
  renderServerPanel(sidePane);
  await cv.load();
  cv.focus();
  return {
    // Selecting another session swaps the conversation only; the session list and server panel stay.
    update: async ([param]) => {
      convId = param ? +param : null;
      renameBtn.hidden = !convId;
      renderList();
      await Promise.all([cv.switchTo(convOpts()), refresh()]);
      cv.focus();
    },
    dispose: () => cv.dispose(),
  };
}

async function renderServerPanel(pane) {
  const [info, snaps] = await Promise.all([api('GET', '/api/system/info', undefined, { quiet: true }), api('GET', '/api/system/snapshots', undefined, { quiet: true })]);
  const restart = async () => {
    if (!await confirmDialog('Restart the web server now? Running agents will be interrupted.', { okLabel: 'Restart' })) return;
    try {
      await api('POST', '/api/system/restart');
      state.expectRestart = true;
      banner('Restarting the server…');
    } catch { /* error shown */ }
  };
  const snapshot = async () => {
    const label = await promptDialog('Create snapshot', { label: 'Label', value: 'manual', okLabel: 'Create' });
    if (!label) return;
    await api('POST', '/api/system/snapshots', { label });
    toast('Snapshot created', 'ok');
    renderServerPanel(pane);
  };
  const restore = async s => {
    if (!await confirmDialog(`Restore ${s.name}?\n\nThe current source is snapshotted first, then replaced by this snapshot, and the server restarts.`, { okLabel: 'Restore', danger: true })) return;
    const res = await api('POST', `/api/system/snapshots/${enc(s.name)}/restore`);
    if (res.restarting) { state.expectRestart = true; banner('Snapshot restored — restarting the server…'); }
    else toast('Snapshot restored. Restart the server to load the restored code.', 'ok');
  };
  const removeSnap = async s => {
    if (!await confirmDialog(`Delete snapshot ${s.name}?`, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/system/snapshots/${enc(s.name)}`);
    renderServerPanel(pane);
  };
  fill(pane,
    h('div', { class: 'pane-head' }, paneTitle('🖥', 'Server')),
    h('div', { class: 'pane-body' },
      h('div', { class: 'side-section' },
        h('dl', { class: 'kv' },
          h('dt', {}, 'Status'), h('dd', {}, h('span', { class: 'dot ok' }), 'running'),
          h('dt', {}, 'PID'), h('dd', {}, String(info.pid)),
          h('dt', {}, 'Started'), h('dd', {}, fmtDate(info.started_at)),
          h('dt', {}, 'Supervisor'), h('dd', {}, info.supervised ? 'yes — restarts enabled' : 'no — start with ./start.sh to enable restarts'),
          h('dt', {}, 'Source'), h('dd', { class: 'mono small', style: { wordBreak: 'break-all' } }, info.app_root)),
        h('div', { class: 'row wrap' },
          h('button', { onclick: restart, disabled: !info.supervised }, '⟳ Restart'),
          h('button', { onclick: showServerLog }, '📜 Log'),
          h('a', { class: 'btn', href: `#/files?path=${enc(info.app_root)}` }, '🗂 Source'))),
      h('div', { class: 'side-section' },
        h('div', { class: 'row' }, h('h3', { class: 'grow', style: { margin: 0 } }, 'Code snapshots'), h('button', { class: 'sm', onclick: snapshot }, '+ Snapshot')),
        paneNote('Taken automatically before the Developer agent changes code. If a restart keeps crashing, the supervisor restores the latest one by itself.', 'inline'),
        snaps.length ? h('div', {}, snaps.map(s => h('div', { class: 'snap' },
          h('span', { class: 'grow ellipsis mono', title: `${s.name} · ${fmtDate(s.created)}` }, s.name.replace('.tar.gz', '')),
          h('button', { class: 'sm', onclick: () => restore(s) }, 'Restore'),
          h('button', { class: 'sm ghost', title: 'Delete', onclick: () => removeSnap(s) }, '🗑'))))
          : h('div', { class: 'small muted' }, 'No snapshots yet.'))));
}

function showServerLog() {
  const pre = h('pre', { class: 'log-view' }, 'Loading…');
  const load = async () => {
    pre.textContent = await api('GET', '/api/system/logs?lines=400');
    pre.scrollTop = pre.scrollHeight;
  };
  modal({ title: 'Server log', wide: true, body: pre, actions: [{ label: 'Refresh', onClick: () => { load(); return false; } }] });
  load();
}

// ================================================================ projects

function registerProjectModal() {
  const name = h('input', { type: 'text', placeholder: 'e.g. Website redesign' });
  const repo = h('input', { type: 'text', class: 'mono', placeholder: '/home/me/my-project' });
  const create = h('input', { type: 'checkbox' });
  const browse = h('button', {
    onclick: async e => {
      e.preventDefault();
      const picked = await pickDirectory({ title: 'Choose the project repository', start: repo.value.trim() || '/home', okLabel: 'Use this folder' });
      if (picked) repo.value = picked;
    },
  }, '📁 Browse…');
  const details = h('textarea', { rows: 8, placeholder: 'Describe the project in detail — goals, scope, requirements — or enter an address (e.g. https://…)' });
  modal({
    title: 'Register a project',
    body: h('div', { class: 'form-grid' },
      field('Project name', name),
      h('label', { class: 'field' }, 'Repository folder',
        h('div', { class: 'row' }, repo, browse),
        h('span', { class: 'hint' }, 'The folder the project’s agents work in. Their file tools and commands are confined to it.')),
      h('label', { class: 'check' }, create, 'Create the folder if it does not exist'),
      field('Project details or address', details)),
    actions: [{
      label: 'Register project', kind: 'primary', onClick: async () => {
        if (!name.value.trim() || !repo.value.trim()) { toast('Name and repository folder are required.', 'error'); return false; }
        try {
          const p = await api('POST', '/api/projects', {
            name: name.value, description: details.value, repo_path: repo.value.trim(), create_repo: create.checked,
          });
          toast(`Project "${p.name}" registered. The setup agent is analysing it, staffing its team and planning its tasks.`, 'ok');
          go(`#/projects/${p.id}`);
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => name.focus(), 30);
}

export async function projectsPage(view, _params, query) {
  const page = h('div', { class: 'page' });
  view.append(page);
  const projects = await api('GET', '/api/projects');
  // The built-in Developer project opens on the Developer page and cannot be deleted.
  const card = p => h('div', { class: 'card project-card', onclick: () => go(p.system === 'dev' ? '#/dev/team' : `#/projects/${p.id}`) },
    h('div', { class: 'row' }, h('h2', { class: 'grow ellipsis', style: { margin: 0 } }, p.system === 'dev' ? `🛠 ${p.name}` : p.name),
      p.setup?.running ? h('span', { class: 'badge accent' }, runDot(), 'setting up') : null,
      p.running ? h('span', { class: 'badge accent' }, runDot(), `${p.running} running`) : null,
      p.system === 'dev' ? h('span', { class: 'badge', title: 'The development team of this website. Built in, cannot be deleted.' }, 'built-in') : h('button', {
        class: 'ghost icon sm danger', title: 'Delete project',
        onclick: async e => { e.stopPropagation(); if (await deleteProject(p)) go('#/projects'); },
      }, '🗑')),
    p.description ? h('div', { class: 'muted small', style: { display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' } }, p.description) : null,
    h('div', { class: 'path' }, '📁 ', p.repo_path, p.repo_exists ? '' : '  ⚠ missing'),
    h('div', { class: 'row small', style: { gap: '6px', flexWrap: 'wrap' } }, '👑 ', h('b', {}, p.agent?.name || '—'), p.agent?.template ? h('code', { class: 'sym' }, `@${p.agent.template}`) : null,
      h('span', { class: 'muted' }, `· 👥 ${p.agent_count} agent${p.agent_count === 1 ? '' : 's'}`),
      p.open_questions ? h('span', { class: 'badge review', title: 'Questions for you' }, `❓ ${p.open_questions}`) : null),
    progressBar(Math.round(p.stats.avg_progress || 0), p.stats.total > 0 && p.stats.done === p.stats.total),
    h('div', { class: 'row small muted' },
      h('span', {}, `${p.stats.done}/${p.stats.total} tasks done`),
      h('span', {}, `${p.stats.active} in progress`),
      h('span', { class: 'grow', style: { textAlign: 'right' } }, `est. ${fmtHours(p.stats.est || 0)}`)));
  page.append(
    h('div', { class: 'page-head' }, h('h1', {}, 'Projects'), h('div', { class: 'spacer' }),
      h('button', { class: 'primary', onclick: () => registerProjectModal() }, '+ Register project')),
    projects.length ? h('div', { class: 'cards' }, projects.map(card))
      : h('div', { class: 'card empty' }, h('div', { class: 'big' }, '📋'),
        h('p', {}, 'Register a project with its name, repository folder and details. A setup agent analyses it, puts together its agent team and plans its tasks.'),
        h('button', { class: 'primary', onclick: () => registerProjectModal() }, 'Register your first project')));
  if (query.get('new')) {
    history.replaceState(null, '', '#/projects');
    registerProjectModal();
  }
}

export async function projectPage(view, [pidParam, tab = 'tasks', taskParam], query = null, loaded = null) {
  const pid = +pidParam;
  if (tab === 'agent') tab = 'team';
  let project = loaded || await api('GET', `/api/projects/${pid}`);
  const dev = project.system === 'dev';   // the development team of this website, shown on the Developer page
  const base = projectBase(pid);
  const page = h('div', { class: 'page', style: { maxWidth: '1400px' } });
  const header = h('div');
  const body = h('div');
  view.append(page);
  const tabs = [['tasks', '✅ Tasks'], ['team', '👥 Team & settings'], ['knowledge', '🧠 Knowledge'],
    ...(dev ? [['server', '🖥 Server & snapshots'], ['files', '🗂 Source files']] : [['files', '🗂 Repository files']])];
  const renderHeader = () => fill(header,
    dev ? devSwitch('team') : h('div', { class: 'small', style: { marginBottom: '6px' } }, h('a', { href: '#/projects' }, '← Projects')),
    h('div', { class: 'page-head', style: { marginBottom: '8px' } },
      h('h1', {}, dev ? 'Development team' : project.name),
      h('span', { class: 'badge accent', title: `Lead · ${project.agent.model} · effort ${project.agent.effort}` }, `👑 ${project.agent.name}`),
      h('a', { class: 'badge', href: `${base}/team`, title: project.agents.map(a => `${'  '.repeat(a.depth)}${a.name}`).join('\n') }, `👥 ${project.agents.length} agent${project.agents.length === 1 ? '' : 's'}`),
      project.setup?.running ? h('a', { class: 'badge accent', href: `${base}/tasks`, title: 'The setup agent is analysing the project, staffing its team and planning its tasks' }, runDot(), 'Setting up') : null,
      h('div', { class: 'spacer' }),
      h('a', { class: 'mono small', href: `${base}/files`, title: 'Open the repository' }, `📁 ${project.repo_path}`),
      dev ? null : h('button', { class: 'ghost sm', title: 'Delete this project', onclick: async () => { if (await deleteProject(project)) go('#/projects'); } }, '🗑 Delete')),
    project.repo_exists ? null : h('div', { class: 'notice warn', style: { marginBottom: '10px' } }, `The repository folder ${project.repo_path} does not exist. Fix it under "Team & settings".`),
    project.description ? h('div', { class: 'muted', style: { marginBottom: '10px' } }, project.description) : null,
    h('div', { class: 'tabs' }, tabs.map(([key, label]) =>
      h('button', { class: tab === key ? 'active' : '', onclick: () => go(`${base}/${key}`) }, label))));
  renderHeader();
  page.append(header, body);

  if (tab === 'server' && dev) {
    const host = h('div', { class: 'card dev-server' });
    body.append(host);
    await renderServerPanel(host);
    return;
  }
  if (tab === 'files') {
    if (!project.repo_exists) { body.append(h('div', { class: 'empty' }, 'The repository folder does not exist.')); return; }
    const host = h('div', { style: { height: 'calc(100vh - 210px)', minHeight: '420px' } });
    body.append(host);
    const fx = new FileExplorer(host, { root: project.repo_path, start: project.repo_path });
    return () => fx.dispose();
  }
  if (tab === 'team') {
    await teamTab(body, project, p => { project = { ...project, ...p }; renderHeader(); });
    return;
  }
  if (tab === 'knowledge') {
    await knowledgeTab(body, project);
    return;
  }
  // The setup agent changes the team and the brief: reload the project in place (tasksTab holds it).
  const reloadProject = async () => {
    Object.assign(project, await api('GET', `/api/projects/${pid}`, undefined, { quiet: true }));
    renderHeader();
  };
  const tasks = await tasksTab(body, project, taskParam ? +taskParam : null, reloadProject);
  return {
    // Selecting another task of this project swaps the task detail only.
    update: ([p, t = 'tasks', task]) => (+p === pid && t === 'tasks' ? tasks.select(task ? +task : null) : false),
    dispose: tasks.dispose,
  };
}

// ================================================================ development team

/** Switch between the Developer agent's sessions and the development team. */
function devSwitch(active) {
  return h('div', { class: 'row dev-switch' },
    h('div', { class: 'seg' }, [['sessions', '#/dev', '💬 Developer sessions'], ['team', '#/dev/team', '👥 Development team']].map(([key, href, label]) =>
      h('button', { class: active === key ? 'active' : '', onclick: () => go(href) }, label))));
}

export async function devTeamPage(view, [tab = 'tasks', taskParam]) {
  const { project } = await api('GET', '/api/devteam');   // the built-in Developer project, always there
  if (state.info) state.info.dev_project_id = project.id;
  const res = await projectPage(view, [String(project.id), tab, taskParam], null, project);
  const next = typeof res === 'function' ? { dispose: res } : { ...res };
  return { ...next, update: next.update ? ([t = 'tasks', task]) => next.update([String(project.id), t, task]) : null };
}

function newTaskModal(project, onCreated) {
  const title = h('input', { type: 'text', placeholder: 'e.g. Add a contact form to the landing page' });
  const desc = h('textarea', { rows: 5, placeholder: 'What should be done? Acceptance criteria, links, constraints…' });
  const est = h('input', { type: 'number', min: 0, step: 0.25, placeholder: 'Let the agent estimate' });
  const actions = h('textarea', { rows: 3, placeholder: 'Optional: initial action items, one per line' });
  const start = h('input', { type: 'checkbox', checked: true });
  const assignee = h('select', {}, project.agents.map(a => h('option', { value: a.id }, `${'\u00a0\u00a0'.repeat(a.depth)}${a.depth ? '' : '👑 '}${a.name}`)));
  const team = project.agents.length > 1;
  modal({
    title: 'New task',
    wide: true,
    body: h('div', { class: 'form-grid' }, field('Title', title), field('Description', desc),
      team ? field('Assign to', assignee, `${project.agent.name} (the lead) breaks the task down and delegates the parts to the team. Assign it to someone else to have them do it directly.`) : null,
      h('div', { class: 'form-grid two' }, field('Estimated total time (hours)', est), field('Upcoming action items', actions)),
      h('label', { class: 'check' }, start, 'Start working on it right away')),
    actions: [{
      label: 'Create task', kind: 'primary', onClick: async () => {
        if (!title.value.trim()) { toast('A title is required.', 'error'); return false; }
        try {
          const task = await api('POST', `/api/projects/${project.id}/tasks`, {
            title: title.value, description: desc.value,
            estimated_hours: est.value === '' ? null : +est.value,
            next_actions: actions.value.split('\n').map(s => s.trim()).filter(Boolean),
            assignee_id: +assignee.value,
          });
          if (start.checked) await api('POST', `/api/tasks/${task.id}/run`, { message: '' });
          onCreated(task);
        } catch { return false; }
      },
    }],
  });
  setTimeout(() => title.focus(), 30);
}

async function tasksTab(el, project, taskId, reloadProject) {
  let tasks = await api('GET', `/api/projects/${project.id}/tasks`);
  let filter = localStorage.getItem('task-filter') || 'all';
  const listEl = h('div', { class: 'task-list' });
  const detailEl = h('div', { style: { minWidth: 0 } });
  const filterSel = h('select', {
    style: { width: 'auto' },
    onchange: e => { filter = e.target.value; localStorage.setItem('task-filter', filter); renderList(); },
  }, h('option', { value: 'all' }, 'All tasks'), Object.entries(STATUS_LABELS).map(([k, v]) => h('option', { value: k, selected: k === filter }, v)));
  el.append(h('div', { class: 'tasks-layout' },
    h('div', { class: 'stack' },
      h('div', { class: 'row tasks-toolbar' }, h('button', { class: 'primary', onclick: () => newTaskModal(project, t => go(`${projectBase(project.id)}/tasks/${t.id}`)) }, '+ New task'),
        project.system === 'dev' ? null : h('button', { title: 'Project setup: the analysis, the team and the task plan', onclick: () => go(`${projectBase(project.id)}/tasks`) }, '🧭 Setup'),
        h('div', { class: 'grow' }), filterSel),
      listEl),
    detailEl));

  let rootId = null;  // the top-level task of the task on screen (a sub-task's tree stays highlighted)
  const item = t => h('div', { class: `card task-item${t.id === taskId || t.id === rootId ? ' active' : ''}`, onclick: () => go(`${projectBase(project.id)}/tasks/${t.id}`) },
    h('div', { class: 'row' }, h('b', { class: 'grow ellipsis' }, t.title), t.open_questions ? h('span', { class: 'badge review', title: 'Questions for you' }, `❓ ${t.open_questions}`) : null, t.running || t.team_running ? runDot() : null,
      h('button', { class: 'ghost icon sm danger', title: 'Delete task', onclick: e => { e.stopPropagation(); removeTask(t); } }, '🗑')),
    h('div', { class: 'row small muted' }, statusBadge(t.status), h('span', {}, `#${t.id}`), h('span', {}, `est. ${fmtHours(t.estimated_hours)}`), h('span', { class: 'grow', style: { textAlign: 'right' } }, `${t.progress}%`)),
    progressBar(t.progress, t.status === 'done'),
    h('div', { class: 'row small muted' },
      t.assignee ? h('span', { class: 'ellipsis' }, `🤖 ${t.assignee.name}`) : null,
      t.subtask_count ? h('span', {}, `🗂 ${t.subtasks_done}/${t.subtask_count} sub-tasks`) : null,
      t.team_running ? h('span', {}, `👥 ${t.team_running} working`) : null));
  const removeTask = async t => {
    const subs = t.subtask_count ? ` with its ${t.subtask_count} sub-task${t.subtask_count === 1 ? '' : 's'}` : '';
    if (!await confirmDialog(`Delete task #${t.id} "${t.title}"${subs} and its agent log?`, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/tasks/${t.id}`);
    toast('Task deleted', 'ok');
    if (t.id === taskId || t.id === rootId) go(`${projectBase(project.id)}/tasks`);
    else refreshList().catch(() => {});
  };
  const renderList = () => {
    const shown = tasks.filter(t => filter === 'all' || t.status === filter);
    fill(listEl, ...(shown.length ? shown.map(item)
      : [h('div', { class: 'card empty small' }, tasks.length ? 'No tasks with this status.'
        : project.setup?.running ? 'The setup agent is planning the tasks…' : `No tasks yet. Create one and ${project.agent.name} will plan it and put the team to work.`)]));
  };
  const refreshList = async () => {
    tasks = await api('GET', `/api/projects/${project.id}/tasks`, undefined, { quiet: true });
    renderList();
  };
  renderList();

  let detail = null;
  let closed = false;
  const select = async id => {
    taskId = id;
    rootId = null;
    renderList();
    detail?.dispose();
    detail = null;
    if (!id) {
      // No task selected: the setup agent's work (analysis, team, task plan), or an offer to run it.
      if (project.setup) {
        const d = detail = setupPanel(detailEl, project, {
          onRunState: async running => {
            await reloadProject().catch(() => {});
            refreshList().catch(() => {});
            if (running || closed) return;
            toast('The setup agent has finished. Review the team and the tasks, then run them.', 'ok');
            if (!taskId) select(null);   // refresh the panel's links to the brief and the team
          },
        });
        if (closed) d.dispose();
        await d.load();
        return;
      }
      const analyse = async () => {
        await api('POST', `/api/projects/${project.id}/setup`, { message: '' });
        await reloadProject();
        select(null);
      };
      fill(detailEl, h('div', { class: 'card empty' }, h('div', { class: 'big' }, '✅'),
        h('p', {}, tasks.length ? 'Select a task to see its progress, sub-tasks, the team’s discussion and each agent’s work.' : 'Create a task to put the team to work.'),
        project.system === 'dev' ? null : h('button', { onclick: analyse }, '🧭 Analyse the project and plan its tasks')));
      return;
    }
    const d = detail = new TaskDetail(detailEl, project, id, {
      onChange: t => {
        const i = tasks.findIndex(x => x.id === t.id);
        if (i >= 0) tasks[i] = { ...tasks[i], ...t };
        if (t.id === taskId && rootId !== t.root_id) rootId = t.root_id;
        renderList();
      },
      onDeleted: () => go(`${projectBase(project.id)}/tasks`),
      onReassigned: () => select(id),
      onRunState: () => refreshList(),
    });
    if (closed) d.dispose();
    await d.load();
  };
  await select(taskId);
  const timer = setInterval(() => { if (project.setup?.running || tasks.some(t => t.running || t.team_running)) refreshList().catch(() => {}); }, 4000);
  return {
    select: async id => {
      if (closed) return;
      refreshList().catch(() => {});  // tasks may have been created or deleted
      await select(id);
    },
    dispose: () => { closed = true; clearInterval(timer); detail?.dispose(); },
  };
}

/** The setup agent's conversation: its analysis, the team it chose and the tasks it planned. The user can
 *  message it to adjust them. */
function setupPanel(el, project, { onRunState }) {
  const host = h('div', { class: 'task-conv' });
  const cv = new ConversationView(host, {
    convId: project.setup.conversation_id,
    noHead: true,
    copyParagraphs: true,
    agentName: 'Setup agent',
    allowEmptySend: true,
    placeholder: 'Ask the setup agent to change the analysis, the team or the tasks. Send with no text to analyse the project again.',
    sendFn: text => api('POST', `/api/projects/${project.id}/setup`, { message: text }),
    onRunState,
  });
  return {
    async load() {
      await cv.load();
      if (cv.disposed) return;
      fill(el, h('div', { class: 'card pad-sm' },
        h('div', { class: 'row', style: { marginBottom: '8px' } },
          h('h3', { class: 'grow', style: { margin: 0 } }, '🧭 Project setup'),
          project.brief ? h('a', { class: 'small', href: `${projectBase(project.id)}/knowledge` }, '📋 Project brief') : null,
          h('a', { class: 'small', href: `${projectBase(project.id)}/team` }, `👥 Team (${project.agents.length})`)),
        h('div', { class: 'small muted', style: { marginBottom: '8px' } },
          'The setup agent analyses the project details and the repository, chooses or creates the agent team, and plans the tasks. It does not run them: select a task and press "Run agent" when you are ready.'),
        host));
      cv.scrollToBottom(true);
    },
    dispose: () => cv.dispose(),
  };
}

class TaskDetail {
  constructor(el, project, taskId, callbacks) {
    this.el = el;
    this.project = project;
    this.id = taskId;
    this.cb = callbacks;
    this.info = h('div', { class: 'task-detail' });
    this.convHost = h('div', { class: 'task-conv' });
    // While the agent or its team works, refresh sub-tasks, questions and the discussion.
    this.timer = setInterval(() => {
      if (this.task && (this.task.running || this.task.team_running || this.task.open_questions)) this.reload();
    }, 4000);
  }

  get agent() {
    return this.project.agents.find(a => a.id === this.task?.assignee?.id) || this.project.agent;
  }

  // Builds the detail off-screen and puts it in place once the task and its work log have loaded,
  // so the previously shown task stays visible until then.
  async load() {
    const task = await api('GET', `/api/tasks/${this.id}`);
    if (this.disposed) return;
    this.task = task;
    const agent = this.agent;
    this.discussion = discussionCard(this.id, task.discussion_count);
    this.cb.onChange?.(task);
    this.render();
    const started = !!this.task.conversation_id;
    this.cv = new ConversationView(this.convHost, {
      convId: this.task.conversation_id,
      noHead: true,
      copyParagraphs: true,
      agentName: agent.name,
      allowEmptySend: true,
      sendLabel: started ? 'Send' : '▶ Run agent',
      placeholder: started ? 'Message the agent, or press Send with no text to let it continue…' : 'Optional instructions for the agent, then press Run agent…',
      emptyText: `${agent.name} has not worked on this task yet. Press "Run agent" to start.${this.project.agents.length > 1 && agent.depth === 0 ? ' As the lead, it will break the task down and delegate the parts to the team.' : ''}`,
      sendFn: async (text, images) => {
        const res = await api('POST', `/api/tasks/${this.id}/run`, { message: text, images });
        this.cv.setSendLabel('Send');
        return res;
      },
      onTaskUpdate: t => this.apply(t),
      onRunState: running => {
        this.task.running = running;
        this.render();
        this.cb.onRunState?.(running);
        if (!running) this.reload();
      },
    });
    await this.cv.load();
    if (this.disposed) return;
    fill(this.el, h('div', { class: 'stack' }, this.info, this.discussion,
      h('div', { class: 'card pad-sm' },
        h('div', { class: 'row', style: { marginBottom: '8px' } },
          h('h3', { class: 'grow', style: { margin: 0 } }, `${agent.depth === 0 ? '👑' : '🤖'} ${agent.name} — work log`),
          h('span', { class: 'small muted' }, `${state.info?.engine === 'api' ? 'API' : 'Claude Code'} · ${agent.model} · effort ${agent.effort}`)),
        this.convHost)));
    this.cv.scrollToBottom(true);
  }

  apply(task) {
    Object.assign(this.task, task);
    this.render();
    this.cb.onChange?.(this.task);
  }

  async reload() {
    try { this.apply(await api('GET', `/api/tasks/${this.id}`, undefined, { quiet: true })); } catch { /* ignore */ }
    this.discussion?.refresh(this.task.discussion_count);
  }

  async save(fields) {
    try {
      await api('PUT', `/api/tasks/${this.id}`, fields);
      await this.reload();
    } catch { this.render(); }
  }

  render() {
    // Don't wipe out something the user is typing; re-render when they leave the field.
    const active = document.activeElement;
    if (active && this.info.contains(active) && /^(INPUT|TEXTAREA)$/.test(active.tagName) && active.type !== 'checkbox' && active.value) {
      if (!this.deferred) {
        this.deferred = true;
        active.addEventListener('blur', () => { this.deferred = false; this.render(); }, { once: true });
      }
      return;
    }
    const t = this.task;
    const statusSel = h('select', { onchange: e => this.save({ status: e.target.value }) },
      Object.entries(STATUS_LABELS).map(([k, v]) => h('option', { value: k, selected: k === t.status }, v)));
    const progress = h('input', { type: 'number', min: 0, max: 100, value: t.progress, style: { width: '76px' }, onchange: e => this.save({ progress: +e.target.value }) });
    const estimate = h('input', {
      type: 'number', min: 0, step: 0.25, value: t.estimated_hours ?? '', placeholder: '—', style: { width: '86px' },
      onchange: e => this.save(e.target.value === '' ? { clear_estimate: true } : { estimated_hours: +e.target.value }),
    });
    const metric = (label, ...content) => h('div', { class: 'metric' }, h('span', { class: 'lbl' }, label), ...content);
    const addRow = (placeholder, onAdd) => {
      const input = h('input', { type: 'text', placeholder });
      const add = () => { const v = input.value.trim(); if (v) { input.value = ''; onAdd(v); } };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
      return h('div', { class: 'add-row' }, input, h('button', { class: 'sm', onclick: add }, 'Add'));
    };

    const agents = this.project.agents;
    const assignee = agents.length > 1 ? h('select', { style: { width: 'auto' }, title: 'Assigned to', onchange: async e => { await this.save({ assignee_id: +e.target.value }); this.cb.onReassigned?.(); } },
      agents.map(a => h('option', { value: a.id, selected: a.id === t.assignee?.id }, `${'\u00a0\u00a0'.repeat(a.depth)}${a.depth ? '🤖' : '👑'} ${a.name}`))) : null;
    const pid = this.project.id;
    fill(this.info,
      t.parent ? h('div', { class: 'small' }, '↑ Sub-task of ', h('a', { href: `${projectBase(pid)}/tasks/${t.parent.id}` }, `#${t.parent.id} ${t.parent.title}`)) : null,
      h('h2', { class: 'task-detail-title' }, `#${t.id} · ${t.title}`),
      h('div', { class: 'row wrap' },
        t.running ? h('span', { class: 'badge accent' }, runDot(), 'agent working') : null,
        t.team_running ? h('span', { class: 'badge accent' }, runDot(), `${t.team_running} teammate${t.team_running === 1 ? '' : 's'} working`) : null,
        h('div', { class: 'grow' }),
        assignee,
        statusBadge(t.status),
        h('button', { class: 'sm', onclick: () => this.edit() }, '✎ Edit'),
        h('button', { class: 'sm danger', title: 'Delete task', onclick: () => this.remove() }, '🗑')),
      h('div', { class: 'task-metrics' },
        metric('Status', statusSel),
        metric('Progress', h('div', { class: 'row' }, progress, h('span', { class: 'muted' }, '%')), progressBar(t.progress, t.status === 'done')),
        metric('Estimated total time', h('div', { class: 'row' }, estimate, h('span', { class: 'muted' }, 'h')), h('span', { class: 'small muted' }, t.estimated_hours != null ? `≈ ${fmtHours(t.estimated_hours)}` : 'not estimated yet')),
        metric('Agent time spent', h('span', { class: 'val' }, fmtDuration(t.time_spent_seconds)), h('span', { class: 'small muted' }, `updated ${timeAgo(t.updated_at)}`))),
      questionsCard(t.questions || [], { currentTask: t.id, projectId: pid, onChange: () => this.reload() }),
      t.summary ? h('div', { class: 'card pad-sm' }, h('div', { class: 'small muted', style: { fontWeight: 600, marginBottom: '4px' } }, 'STATUS SUMMARY'), md(t.summary)) : null,
      t.subtasks?.length ? h('div', { class: 'card pad-sm' },
        h('h3', {}, `🗂 Sub-tasks (${t.subtasks.filter(x => x.status === 'done').length}/${t.subtasks.length} done)`),
        h('div', { class: 'subtasks' }, t.subtasks.map(x => h('a', { class: 'subtask', href: `${projectBase(pid)}/tasks/${x.id}` },
          h('span', { class: 'row', style: { gap: '6px', minWidth: 0 } }, x.running || x.team_running ? runDot() : null, h('b', { class: 'ellipsis' }, `#${x.id} ${x.title}`)),
          h('span', { class: 'small muted ellipsis' }, `🤖 ${x.assignee?.name || '—'}`, x.subtask_count ? ` · 🗂 ${x.subtasks_done}/${x.subtask_count}` : '', x.open_questions ? ` · ❓ ${x.open_questions}` : ''),
          statusBadge(x.status),
          progressBar(x.progress, x.status === 'done'))))) : null,
      h('details', { class: 'card pad-sm', open: !t.summary || undefined },
        h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, 'Description'),
        h('div', { style: { marginTop: '8px' } }, t.description ? md(t.description) : h('span', { class: 'muted' }, 'No description.'))),
      h('div', { class: 'two-col' },
        h('div', { class: 'card pad-sm' },
          h('h3', {}, '💡 Ideas & insights'),
          t.insights.length ? h('ul', { class: 'item-list' }, t.insights.map((it, i) => h('li', {},
            h('span', { title: it.source === 'agent' ? 'From the agent' : 'From you' }, it.source === 'agent' ? '🤖' : '👤'),
            h('span', { class: 'txt' }, it.text, h('div', { class: 'small muted' }, timeAgo(it.at))),
            h('button', { class: 'ghost sm', title: 'Remove', onclick: () => this.save({ insights: t.insights.filter((_, j) => j !== i) }) }, '✕'))))
            : h('div', { class: 'small muted' }, 'Ideas and insights that emerge during the work show up here.'),
          addRow('Add an idea or insight…', text => this.save({ insights: [...t.insights, { text, source: 'user' }] }))),
        h('div', { class: 'card pad-sm' },
          h('h3', {}, '➡️ Next action items'),
          t.next_actions.length ? h('ul', { class: 'item-list' }, t.next_actions.map((a, i) => h('li', { class: a.done ? 'done' : '' },
            h('input', { type: 'checkbox', checked: a.done, onchange: e => this.save({ next_actions: t.next_actions.map((x, j) => (j === i ? { ...x, done: e.target.checked } : x)) }) }),
            h('span', { class: 'txt' }, a.text),
            h('button', { class: 'ghost sm', title: 'Remove', onclick: () => this.save({ next_actions: t.next_actions.filter((_, j) => j !== i) }) }, '✕'))))
            : h('div', { class: 'small muted' }, 'No upcoming action items.'),
          addRow('Add an action item…', text => this.save({ next_actions: [...t.next_actions, { text, done: false }] })))),
      h('details', { class: 'card pad-sm' },
        h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, `Activity log (${(t.events || []).length})`),
        h('div', { class: 'log-list', style: { marginTop: '8px' } }, (t.events || []).map(ev => h('div', {},
          h('span', { class: 'muted' }, fmtDate(ev.created_at), ' '),
          h('span', { class: 'src' }, ev.source === 'agent' ? '🤖' : ev.source === 'user' ? '👤' : '⚙'), ev.text)))));
  }

  edit() {
    const title = h('input', { type: 'text', value: this.task.title });
    const desc = h('textarea', { rows: 8, value: this.task.description });
    modal({
      title: 'Edit task', wide: true,
      body: h('div', { class: 'form-grid' }, field('Title', title), field('Description', desc, 'The agent sees the updated record on its next run.')),
      actions: [{ label: 'Save', kind: 'primary', onClick: () => this.save({ title: title.value, description: desc.value }) }],
    });
  }

  async remove() {
    if (!await confirmDialog(`Delete task #${this.task.id} "${this.task.title}" and its agent log?`, { okLabel: 'Delete', danger: true })) return;
    await api('DELETE', `/api/tasks/${this.id}`);
    toast('Task deleted', 'ok');
    this.cb.onDeleted?.();
  }

  dispose() { this.disposed = true; clearInterval(this.timer); this.cv?.dispose(); }
}

// ================================================================ files

export async function filesPage(view, _params, query) {
  const info = state.info || await api('GET', '/api/system/info');
  const start = query.get('path') || info.home || '/';
  const projects = await api('GET', '/api/projects', undefined, { quiet: true }).catch(() => []);
  const host = h('div', { style: { flex: 1, minHeight: 0 } });
  let fx;
  const jump = path => fx.go(path);
  view.append(h('div', { class: 'page', style: { maxWidth: 'none', height: '100%', display: 'flex', flexDirection: 'column' } },
    h('div', { class: 'page-head' },
      h('h1', {}, 'Files'),
      h('span', { class: 'muted small' }, 'The whole file system: create, open, edit, move and delete files'),
      h('div', { class: 'spacer' }),
      h('button', { class: 'sm', onclick: () => jump(info.home) }, '🏠 Home'),
      h('button', { class: 'sm', onclick: () => jump('/') }, '/ Root'),
      h('button', { class: 'sm', onclick: () => jump(info.app_root) }, '◆ Agent Group'),
      projects.slice(0, 6).map(p => h('button', { class: 'sm', title: p.repo_path, onclick: () => jump(p.repo_path) }, `📋 ${p.name}`))),
    host));
  fx = new FileExplorer(host, { root: '/', start, onNavigate: p => history.replaceState(null, '', `#/files?path=${enc(p)}`) });
  return () => fx.dispose();
}

// ================================================================ settings

export async function settingsPage(view) {
  const s = await api('GET', '/api/settings');
  const info = state.info || await api('GET', '/api/system/info');
  const key = h('input', {
    type: 'password', autocomplete: 'off',
    placeholder: s.api_key_set ? `Saved key ${s.api_key_hint} — type a new one to replace it`
      : s.env_key_present ? 'Using the key from the environment — type one to override' : 'sk-ant-…',
  });
  const sel = (k, options) => h('select', { 'data-key': k }, options);
  const fields = {
    chat_model: sel('chat_model', modelOptions(s.chat_model)), chat_effort: sel('chat_effort', effortOptions(s.chat_effort)),
    dev_model: sel('dev_model', modelOptions(s.dev_model)), dev_effort: sel('dev_effort', effortOptions(s.dev_effort)),
    project_model: sel('project_model', modelOptions(s.project_model)), project_effort: sel('project_effort', effortOptions(s.project_effort)),
  };
  const check = (k, label, hint) => {
    const input = h('input', { type: 'checkbox', checked: s[k] });
    fields[k] = input;
    return h('div', {}, h('label', { class: 'check' }, input, h('b', {}, label)), hint ? h('div', { class: 'small muted', style: { marginLeft: '24px' } }, hint) : null);
  };
  const turns = h('input', { type: 'number', min: 1, max: 500, value: s.max_agent_turns, style: { width: '120px' } });
  const parallel = h('input', { type: 'number', min: 1, max: 10, value: s.max_parallel_agents, style: { width: '120px' } });
  const keyStatus = s.api_key_set ? `A key ending in ${s.api_key_hint} is saved.` : s.env_key_present ? 'Using ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the server environment.' : 'No key configured yet.';
  fields.engine = h('select', { 'data-key': 'engine' },
    h('option', { value: 'claude_code', selected: s.engine === 'claude_code' }, 'Claude Code — your Claude subscription'),
    h('option', { value: 'api', selected: s.engine === 'api' }, 'Anthropic API key — billed per token'));
  const cc = s.claude_code;
  const ccStatus = !cc.available
    ? h('div', { class: 'notice error' }, 'The Claude Code CLI was not found. Run ./start.sh to reinstall the dependencies.')
    : cc.logged_in
      ? h('div', { class: 'notice info' }, `✓ Claude Code is logged in on this server (Agent SDK ${cc.sdk_version}).`)
      : h('div', { class: 'notice warn' }, 'Claude Code is not logged in on this server. Open a terminal on the server, run ',
        h('code', {}, 'claude'), ' and log in with your Claude account.');
  const save = async () => {
    const body = { max_agent_turns: +turns.value, max_parallel_agents: +parallel.value };
    for (const [k, el] of Object.entries(fields)) body[k] = el.type === 'checkbox' ? el.checked : el.value;
    if (key.value.trim()) body.api_key = key.value.trim();
    await api('PUT', '/api/settings', body);
    if (state.info) state.info.engine = body.engine;
    toast('Settings saved', 'ok');
    go('#/settings');
  };
  const row = (label, model, effort) => h('div', { class: 'form-grid two' }, field(`${label} model`, model), field('Effort', effort));
  view.append(h('div', { class: 'page', style: { maxWidth: '820px' } },
    h('div', { class: 'page-head' }, h('h1', {}, 'Settings')),
    h('div', { class: 'stack' },
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, 'Engine'),
        field('Agents run on', fields.engine, 'Claude Code uses the Claude login on this server. Usage counts against your plan’s limits, '
          + 'which the agents share with your own Claude Code sessions. The API engine bills an API key per token. '
          + 'Changing the engine also applies to existing conversations from their next message.'),
        ccStatus),
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, 'Anthropic API key'),
        field('API key', key, keyStatus + ' Used only by the API engine. Stored in the local database (data/agent_group.db).'),
        s.api_key_set ? h('div', {}, h('button', {
          class: 'sm danger', onclick: async () => {
            if (!await confirmDialog('Remove the saved API key?', { okLabel: 'Remove', danger: true })) return;
            await api('PUT', '/api/settings', { clear_api_key: true });
            go('#/settings');
          },
        }, 'Remove saved key')) : null),
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, 'Models'),
        row('Chat agent', fields.chat_model, fields.chat_effort),
        row('Developer agent', fields.dev_model, fields.dev_effort),
        row('New project agents (default)', fields.project_model, fields.project_effort),
        h('div', { class: 'small muted' }, 'Existing project agents keep their own model; change it on the project’s “Agent & settings” tab.')),
      h('div', { class: 'card form-grid' },
        h('h2', { style: { margin: 0 } }, 'Agent behaviour'),
        check('allow_commands', 'Allow agents to run shell commands', 'Developer and project agents can run commands (tests, builds, git) inside their workspace.'),
        check('use_fallback', 'Refusal fallback (API engine)', 'If a safety classifier declines a request, the API retries it on a recommended fallback model (not available for Haiku).'),
        check('chat_web_search', 'Web search in new chat conversations', 'Lets the chat agent search the web. Applies to conversations started after the change.'),
        field('Maximum agent steps per run', turns, 'A run stops after this many model turns; send a message to let the agent continue.'),
        field('Agents working in parallel per lead', parallel, 'How many sub-tasks a team lead runs at the same time (1-10). Each runs its own agent session and uses its own share of your plan or API budget.')),
      h('div', {}, h('button', { class: 'primary', onclick: save }, 'Save settings')),
      h('div', { class: 'card' },
        h('h2', {}, 'About this server'),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Source folder'), h('dd', { class: 'mono' }, info.app_root),
          h('dt', {}, 'Port'), h('dd', {}, String(info.port)),
          h('dt', {}, 'Process'), h('dd', {}, String(info.pid)),
          h('dt', {}, 'Supervisor'), h('dd', {}, info.supervised ? 'running (restarts enabled)' : 'not running — start with ./start.sh'),
          h('dt', {}, 'Python'), h('dd', {}, info.python))))));
}

// ================================================================ conversation link resolver

export async function conversationRedirect(_view, [cid]) {
  const c = await api('GET', `/api/conversations/${cid}`);
  if (c.kind === 'chat') location.replace(`#/chat/${c.topic_id}/${c.id}`);
  else if (c.kind === 'dev') location.replace(`#/dev/${c.id}`);
  else {
    const t = await api('GET', `/api/tasks/${c.task_id}`);
    location.replace(`${projectBase(t.project_id)}/tasks/${t.id}`);
  }
}
