// Conversation view: renders stored messages and follows a live agent run over Server-Sent Events.

import { api, h, fill, md, renderMarkdown, enhanceMarkdown, addParagraphCopy, fmtBytes, fmtDate, state, banner, toast } from './core.js';

// Attached images: the types and limits app/images.py accepts.
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
const MAX_IMAGES = 8;
const MAX_IMAGE_BYTES = 3_750_000;
const MAX_IMAGE_EDGE = 2000;   // long edge of an image that has to be re-encoded to fit MAX_IMAGE_BYTES

const toDataUrl = blob => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(r.error);
  r.readAsDataURL(blob);
});

/** The image as a data: URL, downscaled and re-encoded only when it is too large to send as is. */
async function prepareImage(file) {
  const bitmap = await createImageBitmap(file);
  if (file.size <= MAX_IMAGE_BYTES && Math.max(bitmap.width, bitmap.height) <= 8000) return toDataUrl(file);
  const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = h('canvas', { width: Math.round(bitmap.width * scale), height: Math.round(bitmap.height * scale) });
  const encode = (type, quality) => {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (type === 'image/jpeg') { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height); }
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return new Promise(resolve => canvas.toBlob(resolve, type, quality));
  };
  let blob = await encode(file.type === 'image/webp' ? 'image/webp' : file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png', 0.9);
  if (!blob || blob.size > MAX_IMAGE_BYTES) blob = await encode('image/jpeg', 0.85);
  if (!blob || blob.size > MAX_IMAGE_BYTES) throw new Error('too large');
  return toDataUrl(blob);
}

function viewImage(src) {
  const close = () => { el.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  const el = h('div', { class: 'modal-backdrop lightbox', title: 'Close', onclick: close }, h('img', { src, alt: 'Attached image' }));
  document.addEventListener('keydown', onKey);
  document.body.append(el);
}

export function toolSummary(name, input) {
  input = input || {};
  const size = s => (typeof s === 'string' ? ` (${fmtBytes(new Blob([s]).size)})` : '');
  if (name.startsWith('mcp__agent__')) return toolSummary(name.slice('mcp__agent__'.length), input);
  switch (name) {
    // Claude Code engine tools
    case 'Read': return `${input.file_path || ''}${input.offset ? `:${input.offset}` : ''}`;
    case 'Write': return `${input.file_path || ''}${size(input.content)}`;
    case 'Edit': case 'MultiEdit': return input.file_path || '';
    case 'NotebookEdit': return input.notebook_path || '';
    case 'Bash': return `$ ${input.command || ''}`;
    case 'Glob': return `${input.pattern || ''}${input.path ? ` in ${input.path}` : ''}`;
    case 'Grep': return `/${input.pattern || ''}/${input.glob ? ` ${input.glob}` : ''}${input.path ? ` in ${input.path}` : ''}`;
    case 'WebSearch': return input.query || '';
    case 'WebFetch': return input.url || '';
    case 'TodoWrite': return `${(input.todos || []).length} to-dos`;
    case 'Task': case 'Agent': return input.description || '';
    case 'ToolSearch': return (input.query || '').replace(/^select:/, '');
    // API engine tools
    case 'list_files': return `${input.path || '.'}${input.depth ? `  depth ${input.depth}` : ''}`;
    case 'read_file': return `${input.path || ''}${input.start_line ? `:${input.start_line}-${input.end_line || ''}` : ''}`;
    case 'write_file': return `${input.path || ''}${size(input.content)}`;
    case 'edit_file': case 'delete_path': case 'make_directory': return input.path || '';
    case 'move_path': return `${input.source || ''} → ${input.destination || ''}`;
    case 'search_files': return `/${input.pattern || ''}/ ${input.glob || ''} ${input.path || ''}`;
    case 'run_command': return `$ ${input.command || ''}`;
    case 'update_task': return ['status', 'progress', 'estimated_hours'].filter(k => k in input).map(k => `${k}=${input[k]}`).join(' ') || Object.keys(input).join(', ');
    case 'restart_server': return input.reason || '';
    case 'read_server_log': return `${input.lines || 120} lines`;
    case 'web_search': return input.query || '';
    default: return JSON.stringify(input).slice(0, 160);
  }
}

function resultText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map(c => c.text ?? JSON.stringify(c)).join('\n');
  return JSON.stringify(content, null, 2);
}

export class ConversationView {
  /**
   * opts: convId, title, emptyText, placeholder, sendLabel, hint, agentName,
   *       sendFn(text, images) -> {run, conversation_id?}, allowEmptySend, headerActions [nodes],
   *       onTaskUpdate(task), onRunState(running), noHead,
   *       copyParagraphs (copy icon on each paragraph of the agent's replies)
   */
  constructor(el, opts = {}) {
    this.el = el;
    this.opts = opts;
    this.run = null;
    this.es = null;
    this.lastSeq = -1;
    this.blocks = new Map();
    this.turnEl = null;
    this.currentBubble = null;
    this.disposed = false;
    this.build();
  }

  build() {
    this.titleEl = h('div', { class: 'title ellipsis' }, this.opts.title || '');
    this.statusEl = h('span', { class: 'small muted' });
    const head = h('div', { class: 'conv-head' }, this.titleEl, this.statusEl, this.opts.headerActions || []);
    this.inner = h('div', { class: 'conv-inner' });
    this.scroller = h('div', { class: 'conv-scroll' }, this.inner);
    this.input = h('textarea', { rows: 1, placeholder: this.opts.placeholder || 'Message Claude… (Enter to send, Shift+Enter for a new line)' });
    this.input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); }
    });
    this.input.addEventListener('input', () => this.autosize());
    // Pasted images are attached; any text on the clipboard is pasted as usual.
    this.input.addEventListener('paste', e => {
      const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
      if (files.length) this.addImages(files);
    });
    this.sendBtn = h('button', { class: 'primary', onclick: () => this.send() }, this.opts.sendLabel || 'Send');
    this.stopBtn = h('button', { class: 'danger', hidden: true, onclick: () => this.stop() }, '■ Stop');
    this.attachments = [];
    this.tray = h('div', { class: 'attachments', hidden: true });
    const fileInput = h('input', {
      type: 'file', accept: IMAGE_TYPES.join(','), multiple: true, hidden: true,
      onchange: e => { this.addImages([...e.target.files]); e.target.value = ''; },
    });
    const attachBtn = h('button', { class: 'ghost icon attach', title: 'Attach images (you can also paste or drop them)', onclick: () => fileInput.click() }, '📎');
    const composer = h('div', { class: 'composer' },
      this.tray,
      h('div', { class: 'composer-inner' }, fileInput, attachBtn, this.input, this.sendBtn, this.stopBtn),
      this.opts.hint ? h('div', { class: 'hint' }, this.opts.hint) : null);
    const hasFiles = e => e.dataTransfer?.types.includes('Files');
    composer.addEventListener('dragover', e => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      composer.classList.add('dropping');
    });
    composer.addEventListener('dragleave', e => { if (!composer.contains(e.relatedTarget)) composer.classList.remove('dropping'); });
    composer.addEventListener('drop', e => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      composer.classList.remove('dropping');
      this.addImages([...e.dataTransfer.files]);
    });
    fill(this.el, h('div', { class: 'conv' }, this.opts.noHead ? null : head, this.scroller, composer));
  }

  setTitle(title) { this.titleEl.textContent = title; }

  setSendLabel(label) { this.sendBtn.textContent = label; }

  autosize() {
    this.input.style.height = 'auto';
    this.input.style.height = Math.min(this.input.scrollHeight, 240) + 'px';
  }

  focus() { this.input.focus(); }

  async addImages(files) {
    for (const file of files) {
      if (!IMAGE_TYPES.includes(file.type)) { toast(`${file.name}: only PNG, JPEG, GIF and WebP images can be attached.`, 'error'); continue; }
      if (this.attachments.length >= MAX_IMAGES) { toast(`At most ${MAX_IMAGES} images per message.`, 'error'); break; }
      try {
        this.attachments.push(await prepareImage(file));
      } catch {
        toast(`${file.name} could not be read or is too large.`, 'error');
      }
      this.renderTray();
    }
  }

  renderTray() {
    fill(this.tray, this.attachments.map((src, i) => h('div', { class: 'thumb' },
      h('img', { src, alt: 'Attached image', onclick: () => viewImage(src) }),
      h('button', { class: 'remove', title: 'Remove image', onclick: () => { this.attachments.splice(i, 1); this.renderTray(); } }, '×'))));
    this.tray.hidden = !this.attachments.length;
  }

  async load() {
    const mine = this.loadSeq = (this.loadSeq || 0) + 1;
    if (!this.opts.convId) { this.renderMessages([]); this.setRunning(false); return; }
    const conv = await api('GET', `/api/conversations/${this.opts.convId}`);
    if (this.disposed || mine !== this.loadSeq) return;
    this.conv = conv;
    if (!this.opts.title) this.setTitle(conv.title);
    this.renderMessages(conv.messages);
    if (conv.active_run) this.attach(conv.active_run.id, conv.active_run.resume_from);
    else this.setRunning(false);
    this.scrollToBottom(true);
  }

  /** Show another conversation in the same view. The current one stays on screen until the new one has loaded. */
  async switchTo(opts) {
    Object.assign(this.opts, opts);
    this.detach();
    this.run = null;
    this.blocks.clear();
    this.setRunning(false);
    if (this.opts.title) this.setTitle(this.opts.title);
    this.input.value = '';
    this.autosize();
    this.attachments = [];
    this.renderTray();
    await this.load();
  }

  // ------------------------------------------------------------ rendering stored history

  renderMessages(messages) {
    fill(this.inner);
    this.currentBubble = null;
    this.turnEl = null;
    if (!messages.length) {
      this.inner.append(h('div', { class: 'empty' }, h('div', { class: 'big' }, '💬'), this.opts.emptyText || 'No messages yet.'));
      return;
    }
    const results = new Map();
    for (const m of messages) {
      if (m.role === 'user' && Array.isArray(m.content)) {
        for (const b of m.content) if (b.type === 'tool_result') results.set(b.tool_use_id, b);
      }
    }
    for (const m of messages) {
      if (m.role === 'user') {
        const blocks = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : m.content;
        const texts = blocks.filter(b => b.type === 'text').map(b => b.text);
        const images = blocks.filter(b => b.type === 'image' && b.upload).map(b => `/api/uploads/${b.upload}`);
        if (texts.length || images.length) this.addUserBubble(texts.join('\n\n'), m.created_at, images);
      } else if (m.role === 'assistant') {
        const bubble = this.ensureAssistantBubble(m.created_at);
        const turn = h('div', { class: 'stack', style: { gap: '10px' } });
        for (const b of m.content) {
          const node = this.renderBlock(b, results);
          if (node) turn.append(node);
        }
        if (turn.childNodes.length) bubble.append(turn);
      }
    }
  }

  addUserBubble(text, when, images = []) {
    this.inner.querySelector(':scope > .empty')?.remove();
    this.currentBubble = null;
    const long = text.length > 900;
    const body = h('div', {}, long ? text.slice(0, 600) + '…' : text);
    if (long) {
      const more = h('a', { href: '#', class: 'small', style: { display: 'block', marginTop: '6px' } }, 'Show full message');
      more.onclick = e => { e.preventDefault(); body.textContent = text; };
      body.append(more);
    }
    const bubble = h('div', { class: 'bubble' },
      images.length ? h('div', { class: 'msg-images' }, images.map(src => h('img', { src, alt: 'Attached image', loading: 'lazy', onclick: () => viewImage(src) }))) : null,
      text ? body : null);
    this.inner.append(h('div', { class: 'msg user' }, h('div', { class: 'who' }, 'You', when ? h('span', { class: 'muted' }, '· ' + fmtDate(when)) : null), bubble));
  }

  ensureAssistantBubble(when) {
    if (this.currentBubble) return this.currentBubble;
    this.inner.querySelector(':scope > .empty')?.remove();
    const bubble = h('div', { class: 'bubble' });
    this.inner.append(h('div', { class: 'msg assistant' },
      h('div', { class: 'who' }, this.opts.agentName || 'Claude', when ? h('span', { class: 'muted' }, '· ' + fmtDate(when)) : null), bubble));
    this.currentBubble = bubble;
    return bubble;
  }

  renderBlock(b, results) {
    switch (b.type) {
      case 'text':
        if (!b.text?.trim()) return null;
        return this.opts.copyParagraphs ? addParagraphCopy(md(b.text)) : md(b.text);
      case 'thinking':
        return b.thinking?.trim() ? this.thinkingEl(b.thinking, false) : null;
      case 'tool_use': {
        const card = this.toolCard(b.id, b.name, b.input);
        const r = results.get(b.id);
        if (r) this.fillToolResult(card, r.content, r.is_error);
        else this.setState(card, 'err', 'no result');
        return card;
      }
      case 'server_tool_use': {
        const card = this.toolCard(b.id, b.name, b.input);
        this.setState(card, 'ok', '✓');
        return card;
      }
      case 'web_search_tool_result':
        return this.searchResults(b);
      case 'fallback':
        return h('div', { class: 'notice info inline-notice' }, `${b.from?.model || 'The model'} declined; continued on ${b.to?.model || 'a fallback model'}.`);
      case 'redacted_thinking':
      case 'server_tool_result':
        return null;
      default:
        return h('div', { class: 'small muted' }, `[${b.type}]`);
    }
  }

  thinkingEl(text, live) {
    const body = h('div', { class: 'body' }, text);
    const el = h('details', { class: 'thinking' }, h('summary', {}, live ? 'Thinking…' : 'Thinking'), body);
    el._body = body;
    return el;
  }

  searchResults(b) {
    if (!Array.isArray(b.content)) return h('div', { class: 'notice warn inline-notice' }, `Web search error: ${b.content?.error_code || 'unknown'}`);
    return h('div', { class: 'small' }, h('div', { class: 'muted' }, `🔎 ${b.content.length} search results`),
      h('ul', { style: { margin: '4px 0', paddingLeft: '18px' } },
        b.content.slice(0, 8).map(r => h('li', {}, h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.title || r.url)))));
  }

  toolCard(id, name, input) {
    const stateEl = h('span', { class: 'tstate' });
    const arg = h('span', { class: 'targ' }, input ? toolSummary(name, input) : '');
    const inputBox = h('div');
    const resultBox = h('div');
    const card = h('details', { class: 'tool', 'data-tool-id': id },
      h('summary', {}, h('span', {}, '🔧'), h('span', { class: 'tname', title: name }, name.replace(/^mcp__agent__/, '')), arg, stateEl),
      h('div', { class: 'tbody' }, inputBox, resultBox));
    card._parts = { stateEl, arg, inputBox, resultBox, name };
    if (input) this.fillToolInput(card, input);
    return card;
  }

  fillToolInput(card, input) {
    const box = card._parts.inputBox;
    fill(box);
    for (const [k, v] of Object.entries(input || {})) {
      const text = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
      box.append(h('div', { class: 'lbl' }, k), h('pre', {}, text));
    }
  }

  fillToolResult(card, content, isError) {
    fill(card._parts.resultBox, h('div', { class: 'lbl' }, isError ? 'Error' : 'Result'), h('pre', {}, resultText(content)));
    this.setState(card, isError ? 'err' : 'ok', isError ? '✗ error' : '✓');
    if (isError) card.open = false;
  }

  setState(card, kind, text) {
    const el = card._parts.stateEl;
    el.className = `tstate ${kind}`;
    el.textContent = text;
  }

  findCard(id) {
    return this.inner.querySelector(`[data-tool-id="${CSS.escape(id)}"]`);
  }

  addNotice(text, level = 'info') {
    const host = this.currentBubble || this.inner;
    host.append(h('div', { class: `notice ${level === 'warn' ? 'warn' : level === 'error' ? 'error' : 'info'} inline-notice` }, text));
  }

  // ------------------------------------------------------------ live runs

  attach(runId, after) {
    if (this.disposed) return;
    this.detach();
    this.run = { id: runId };
    this.lastSeq = (after || 0) - 1;
    this.setRunning(true);
    this.connect();
  }

  connect() {
    if (this.disposed || !this.run) return;
    const es = new EventSource(`/api/runs/${this.run.id}/events?after=${this.lastSeq + 1}`);
    this.es = es;
    es.onmessage = e => {
      const ev = JSON.parse(e.data);
      if (ev.seq <= this.lastSeq) return;
      this.lastSeq = ev.seq;
      try { this.handle(ev); } catch (err) { console.error(err); }
    };
    es.onerror = () => {
      es.close();
      if (this.es !== es) return;
      this.es = null;
      if (!this.disposed && this.run) setTimeout(() => this.recover(), 1500);
    };
  }

  /** The event stream broke: reconnect if the run is still alive, otherwise reload from storage. */
  async recover() {
    if (this.disposed || !this.run || this.es) return;
    let conv;
    try { conv = await api('GET', `/api/conversations/${this.opts.convId}`, undefined, { quiet: true }); } catch {
      setTimeout(() => this.recover(), 2500);   // server down (e.g. restarting)
      return;
    }
    if (conv.active_run && conv.active_run.id === this.run.id) { this.connect(); return; }
    this.run = null;
    this.setRunning(false);
    this.opts.onRunState?.(false);
    await this.load();
  }

  detach() {
    if (this.es) { const es = this.es; this.es = null; es.close(); }
  }

  handle(ev) {
    switch (ev.type) {
      case 'turn_start': this.startTurn(); break;
      case 'block_start': this.blockStart(ev); break;
      case 'text': this.appendText(ev.index, ev.text); break;
      case 'thinking': this.appendThinking(ev.index, ev.text); break;
      case 'tool_input_progress': {
        const b = this.blocks.get(ev.index);
        if (b?.card) this.setState(b.card, 'run', `writing ${fmtBytes(ev.bytes)}…`);
        break;
      }
      case 'block_stop': this.blockStop(ev.index); break;
      case 'turn_discarded': this.turnEl?.remove(); this.turnEl = null; this.blocks.clear(); break;
      case 'tool_start': {
        const card = this.findCard(ev.id);
        if (card) {
          this.fillToolInput(card, ev.input);
          card._parts.arg.textContent = toolSummary(ev.name, ev.input);
          this.setState(card, 'run', 'running…');
        }
        break;
      }
      case 'tool_result': {
        const card = this.findCard(ev.tool_use_id);
        if (card) this.fillToolResult(card, ev.content, ev.is_error);
        break;
      }
      case 'task_update': this.opts.onTaskUpdate?.(ev.task); break;
      case 'notice': this.addNotice(ev.text, ev.level); break;
      case 'error': this.addNotice(ev.text, 'error'); break;
      case 'done': this.finish(ev); break;
      default: break;
    }
    this.scrollToBottom();
  }

  startTurn() {
    this.blocks.clear();
    if (this.turnEl?.isConnected && this.turnEl.children.length === 1 && this.turnEl.firstChild.classList.contains('pending')) return;
    const bubble = this.ensureAssistantBubble();
    this.turnEl = h('div', { class: 'stack', style: { gap: '10px' } }, h('div', { class: 'small muted pending' }, h('span', { class: 'dot run' }), 'Working…'));
    bubble.append(this.turnEl);
  }

  blockStart(ev) {
    if (!this.turnEl) this.startTurn();
    this.turnEl.querySelector('.pending')?.remove();
    let entry;
    switch (ev.block_type) {
      case 'text': {
        const el = h('div', { class: 'md cursor' });
        entry = { type: 'text', el, buf: '' };
        break;
      }
      case 'thinking': {
        const el = this.thinkingEl('', true);
        el.hidden = true;
        entry = { type: 'thinking', el, buf: '' };
        break;
      }
      case 'tool_use':
      case 'server_tool_use': {
        const card = this.toolCard(ev.id, ev.name, null);
        this.setState(card, 'run', ev.block_type === 'tool_use' ? 'preparing…' : 'searching…');
        entry = { type: 'tool', el: card, card };
        break;
      }
      case 'fallback':
        entry = { type: 'other', el: h('div', { class: 'notice info inline-notice' }, `${ev.from_model} declined; continuing on ${ev.to_model}.`) };
        break;
      default:
        if (ev.block_type.endsWith('_tool_result') && ev.block) {
          const card = this.findCard(ev.block.tool_use_id);
          if (card) this.setState(card, 'ok', '✓');
          const node = this.renderBlock(ev.block, new Map());
          entry = { type: 'other', el: node || h('span') };
        } else {
          entry = { type: 'other', el: h('span') };
        }
    }
    this.blocks.set(ev.index, entry);
    this.turnEl.append(entry.el);
  }

  appendText(index, text) {
    const b = this.blocks.get(index);
    if (!b) return;
    b.buf += text;
    if (b.pending) return;
    b.pending = true;
    requestAnimationFrame(() => {
      b.pending = false;
      b.el.innerHTML = renderMarkdown(b.buf);
      this.scrollToBottom();
    });
  }

  appendThinking(index, text) {
    const b = this.blocks.get(index);
    if (!b) return;
    b.buf += text;
    if (b.buf.trim()) b.el.hidden = false;
    b.el._body.textContent = b.buf;
  }

  blockStop(index) {
    const b = this.blocks.get(index);
    if (!b) return;
    if (b.type === 'text') {
      b.el.classList.remove('cursor');
      b.el.innerHTML = renderMarkdown(b.buf);
      enhanceMarkdown(b.el);
      if (this.opts.copyParagraphs) addParagraphCopy(b.el);
      if (!b.buf.trim()) b.el.remove();
    } else if (b.type === 'thinking') {
      b.el.querySelector('summary').textContent = 'Thinking';
    } else if (b.type === 'tool' && b.card._parts.stateEl.textContent.startsWith('preparing')) {
      this.setState(b.card, 'run', 'queued');
    } else if (b.type === 'tool' && b.card._parts.stateEl.textContent.startsWith('writing')) {
      this.setState(b.card, 'run', 'queued');
    }
  }

  finish(ev) {
    this.detach();
    this.run = null;
    this.turnEl?.querySelector('.pending')?.remove();
    this.setRunning(false);
    if (ev.status === 'cancelled') this.addNotice('Stopped by user.', 'warn');
    if (ev.restart) {
      state.expectRestart = true;
      banner('Restarting the server…');
    }
    this.opts.onRunState?.(false);
  }

  setRunning(running) {
    this.sendBtn.hidden = running;
    this.stopBtn.hidden = !running;
    fill(this.statusEl, running ? h('span', {}, h('span', { class: 'dot run' }), 'working') : '');
  }

  async send() {
    const text = this.input.value.trim();
    const images = [...this.attachments];
    if (this.run || (!text && !images.length && !this.opts.allowEmptySend)) return;
    this.sendBtn.disabled = true;
    try {
      const res = this.opts.sendFn
        ? await this.opts.sendFn(text, images)
        : await api('POST', `/api/conversations/${this.opts.convId}/messages`, { text, images });
      if (!res) return;
      if (res.conversation_id) this.opts.convId = res.conversation_id;
      this.input.value = '';
      this.autosize();
      this.attachments = [];
      this.renderTray();
      this.addUserBubble(text || (images.length ? '' : '▶ Run the agent'), undefined, images);
      this.attach(res.run.id, res.run.resume_from);
      this.startTurn();
      this.scrollToBottom(true);
      this.opts.onRunState?.(true);
    } catch { /* api() already showed the error */ } finally {
      this.sendBtn.disabled = false;
    }
  }

  async stop() {
    if (!this.run) return;
    await api('POST', `/api/runs/${this.run.id}/cancel`);
  }

  scrollToBottom(force = false) {
    const s = this.scroller;
    if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 160) s.scrollTop = s.scrollHeight;
  }

  dispose() {
    this.disposed = true;
    this.detach();
  }
}
