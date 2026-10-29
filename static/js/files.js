// File explorer (tree + editor) and a directory picker, both backed by /api/fs/*.

import { api, h, fill, enc, toast, modal, confirmDialog, promptDialog, contextMenu, fmtBytes, state, resizer } from './core.js';

// Ctrl (or Alt, as on macOS) held while dropping copies instead of moving.
const copyKey = e => e.ctrlKey || e.altKey;

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

function parentOf(path) {
  const p = path.replace(/\/+$/, '');
  const i = p.lastIndexOf('/');
  return i <= 0 ? '/' : p.slice(0, i);
}

/** Whether `path` is `dir` or lies inside it. */
function inside(path, dir) {
  return dir === '/' ? path.startsWith('/') : path === dir || path.startsWith(dir + '/');
}

/** Common prefix of names, compared case-insensitively; keeps the casing of the first name. */
function commonPrefix(names) {
  let common = names[0];
  for (const n of names) while (!n.toLowerCase().startsWith(common.toLowerCase())) common = common.slice(0, -1);
  return common;
}

/** Entries whose name starts with `partial`: case-sensitive matches if there are any, otherwise case-insensitive ones. */
function matchNames(entries, partial, name = e => e.name) {
  const exact = entries.filter(e => name(e).startsWith(partial));
  if (exact.length) return exact;
  const lower = partial.toLowerCase();
  return entries.filter(e => name(e).toLowerCase().startsWith(lower));
}

// Item copied with "Copy", shared by every explorer so it survives navigation and re-renders.
let clipboard = null;   // { path, name, is_dir }

function join(dir, name) {
  if (name.startsWith('/')) return name;
  return (dir.endsWith('/') ? dir : dir + '/') + name;
}

function icon(e) {
  if (e.is_dir) return '📁';
  if (e.is_link) return '🔗';
  if (IMAGE_EXT.test(e.name)) return '🖼';
  if (/\.(py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|rb|php|sh|css|html|json|ya?ml|toml|sql)$/i.test(e.name)) return '📜';
  if (/\.(md|txt|rst|log|csv)$/i.test(e.name)) return '📝';
  if (/\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/i.test(e.name)) return '📦';
  return '📄';
}

export class FileExplorer {
  /**
   * opts: root (absolute directory the explorer is confined to), start (initial directory),
   *       fallbacks (folders to show when start cannot be opened), storageKey (localStorage key the
   *       folder shown is saved under), onNavigate(dir)
   */
  constructor(el, opts = {}) {
    this.el = el;
    this.root = opts.root || '/';
    this.base = opts.start || this.root;
    this.opts = opts;
    this.children = new Map();   // dir path -> entries
    this.expanded = new Set();
    this.selected = null;
    this.matches = new Set();   // entries highlighted by Tab completion in the path box
    this.file = null;
    this.showHidden = localStorage.getItem('fx-hidden') !== '0';
    this.build();
    this.refresh((opts.fallbacks || []).filter(d => d !== this.base));
  }

  rootQuery() { return `root=${enc(this.root)}`; }

  build() {
    this.pathInput = h('input', { type: 'text', value: this.base, spellcheck: 'false', autocomplete: 'off', title: 'Folder shown in the tree. Type a path and press Enter; Tab completes it.' });
    this.pathInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') this.go(this.pathInput.value.trim());
      if (e.key === 'Tab' && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); this.complete(); }
    });
    this.pathInput.addEventListener('input', () => { if (this.matches.size) { this.matches.clear(); this.renderTree(); } });
    // While the last copy is still selected, Paste goes next to it again, not inside it.
    this.pasteBtn = h('button', { class: 'sm', hidden: true, onclick: () => this.paste(this.selected && this.selected === this.lastCopy ? parentOf(this.lastCopy) : this.currentDir()) }, '📋 Paste');
    const hiddenBtn = h('button', { class: 'sm', title: 'Show or hide dotfiles' }, this.showHidden ? 'Hide dotfiles' : 'Show dotfiles');
    hiddenBtn.onclick = () => {
      this.showHidden = !this.showHidden;
      localStorage.setItem('fx-hidden', this.showHidden ? '1' : '0');
      hiddenBtn.textContent = this.showHidden ? 'Hide dotfiles' : 'Show dotfiles';
      this.refresh();
    };
    const toolbar = h('div', { class: 'fx-toolbar' },
      h('button', { class: 'sm', title: 'Parent folder', onclick: () => this.up() }, '⬆'),
      this.pathInput,
      h('button', { class: 'sm', title: 'Refresh', onclick: () => this.refresh() }, '⟳'),
      h('button', { class: 'sm', onclick: () => this.newFile() }, '+ File'),
      h('button', { class: 'sm', onclick: () => this.newFolder() }, '+ Folder'),
      this.pasteBtn,
      hiddenBtn);
    this.renderPasteBtn();
    this.tree = h('div', { class: 'fx-tree' });
    // Dropping on empty tree space moves the item into the folder shown at the top (copies it with Ctrl or Alt held).
    this.tree.addEventListener('dragover', e => {
      if (!e.dataTransfer.types.includes('text/x-fx-path')) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = copyKey(e) ? 'copy' : 'move';
      this.tree.classList.add('drop-target');
    });
    this.tree.addEventListener('dragleave', e => { if (e.target === this.tree) this.tree.classList.remove('drop-target'); });
    this.tree.addEventListener('drop', e => {
      e.preventDefault();
      this.tree.classList.remove('drop-target');
      const src = e.dataTransfer.getData('text/x-fx-path');
      if (src && copyKey(e)) this.copy(src, this.base);
      else if (src && parentOf(src) !== this.base) this.move(src, this.base);
    });
    this.tree.addEventListener('contextmenu', e => {
      if (e.target !== this.tree) return;
      e.preventDefault();
      contextMenu(e.clientX, e.clientY, [
        { label: 'New file here', action: () => this.newFile(this.base) },
        { label: 'New folder here', action: () => this.newFolder(this.base) },
        ...(this.canPaste() ? [{ label: `Paste here (${clipboard.name})`, action: () => this.paste(this.base) }] : []),
        { label: 'Refresh', action: () => this.refresh() },
      ]);
    });
    this.editor = h('div', { class: 'fx-editor' });
    this.renderEditorPlaceholder();
    const gutter = resizer(this.tree, { key: 'fx-tree', width: 320, min: 180, max: 1200, collapseAt: 0, flex: this.editor });
    fill(this.el, h('div', { class: 'fx' }, toolbar, h('div', { class: 'fx-main' }, this.tree, gutter, this.editor)));
  }

  // ------------------------------------------------------------ navigation

  async list(dir) {
    const res = await api('GET', `/api/fs/list?path=${enc(dir)}&${this.rootQuery()}&hidden=${this.showHidden ? 1 : 0}`, undefined, { quiet: true });
    this.children.set(dir, res.entries);
    return res;
  }

  async refresh(fallbacks = []) {
    this.pathInput.value = this.base;
    try {
      await this.list(this.base);
    } catch (e) {
      if (fallbacks.length) {
        // A saved folder that is gone would fail on every visit (keep it while the server is unreachable).
        const key = this.opts.storageKey;
        if (key && e.status && localStorage.getItem(key) === this.base) localStorage.removeItem(key);
        this.base = fallbacks[0];
        return this.refresh(fallbacks.slice(1));
      }
      fill(this.tree, h('div', { class: 'empty small' }, `Cannot open ${this.base}: ${e.message}`));
      return;
    }
    await Promise.all([...this.expanded].map(d => this.list(d).catch(() => this.expanded.delete(d))));
    this.renderTree();
  }

  async go(path) {
    if (!path) return;
    if (!path.startsWith('/')) path = join(this.base, path);
    path = path.replace(/\/+$/, '') || '/';
    const typed = this.pathInput.value;
    try {
      await this.list(path);
    } catch (e) {
      toast(e.message, 'error');
      this.pathInput.value = this.base;
      return;
    }
    this.base = path;
    this.expanded.clear();
    this.selected = null;
    this.matches.clear();
    if (this.pathInput.value === typed) this.pathInput.value = path;   // unless typed on meanwhile
    this.renderTree();
    if (this.opts.storageKey) localStorage.setItem(this.opts.storageKey, path);
    this.opts.onNavigate?.(path);
  }

  /** Tab in the path box: complete the last path segment against the entries of its folder and show them in the tree. */
  async complete() {
    const value = this.pathInput.value.trim();
    if (!value) return;
    const abs = value.startsWith('/') ? value : join(this.base, value);
    const i = abs.lastIndexOf('/');
    const dir = i <= 0 ? '/' : abs.slice(0, i).replace(/\/+$/, '') || '/';
    const partial = abs.slice(i + 1);
    // go() writes the folder into the path box only if nothing was typed meanwhile; complete it only then.
    const goTo = async path => { await this.go(path); return this.base === path && this.pathInput.value === path; };
    if (!partial) { if (dir !== this.base && await goTo(dir)) this.pathInput.value = join(dir, ''); return; }
    let res;
    try {
      res = await api('GET', `/api/fs/list?path=${enc(dir)}&${this.rootQuery()}&hidden=${this.showHidden || partial.startsWith('.') ? 1 : 0}`, undefined, { quiet: true });
    } catch { return; }
    if (this.pathInput.value.trim() !== value) return;   // typed on meanwhile
    const found = matchNames(res.entries, partial);
    if (!found.length) return;
    if (found.length === 1 && found[0].is_dir) {
      if (await goTo(found[0].path)) this.pathInput.value = join(found[0].path, '');
      return;
    }
    if (dir !== this.base && !await goTo(dir)) return;
    if (found.length === 1) {
      this.selected = found[0].path;
      this.pathInput.value = found[0].path;
    } else {
      this.selected = null;
      this.matches = new Set(found.map(e => e.path));
      const common = commonPrefix(found.map(e => e.name));
      this.pathInput.value = join(dir, common.length > partial.length ? common : partial);
    }
    this.renderTree();
    this.tree.querySelector('.fx-node.selected, .fx-node.match')?.scrollIntoView({ block: 'nearest' });
  }

  up() {
    if (this.base === this.root || this.base === '/') return;
    this.go(parentOf(this.base));
  }

  renderTree() {
    const rows = [];
    const walk = (dir, depth) => {
      for (const e of this.children.get(dir) || []) {
        rows.push(this.nodeEl(e, depth));
        if (e.is_dir && this.expanded.has(e.path)) {
          if (!this.children.has(e.path)) rows.push(h('div', { class: 'fx-node muted', style: { paddingLeft: `${24 + (depth + 1) * 16}px` } }, 'loading…'));
          else if (!this.children.get(e.path).length) rows.push(h('div', { class: 'fx-node muted small', style: { paddingLeft: `${24 + (depth + 1) * 16}px` } }, 'empty'));
          else walk(e.path, depth + 1);
        }
      }
    };
    walk(this.base, 0);
    if (!rows.length) rows.push(h('div', { class: 'empty small' }, 'This folder is empty. Right-click to create a file or folder.'));
    fill(this.tree, ...rows);
  }

  nodeEl(e, depth) {
    const open = this.expanded.has(e.path);
    const row = h('div', {
      class: `fx-node${this.selected === e.path ? ' selected' : ''}${this.matches.has(e.path) ? ' match' : ''}`,
      style: { paddingLeft: `${8 + depth * 16}px` }, draggable: 'true', title: e.path,
    },
      h('span', { class: 'caret' }, e.is_dir ? (open ? '▾' : '▸') : ''),
      h('span', { class: 'ico' }, e.is_dir && open ? '📂' : icon(e)),
      h('span', { class: 'name' }, e.name),
      h('span', { class: 'size' }, e.is_dir ? '' : fmtBytes(e.size)),
      h('span', { class: 'acts' },
        h('button', { title: 'Rename or move', onclick: ev => { ev.stopPropagation(); this.renameOrMove(e); } }, '✎'),
        h('button', { title: 'Delete', onclick: ev => { ev.stopPropagation(); this.remove(e); } }, '🗑')));
    row.addEventListener('click', () => this.activate(e));
    if (e.is_dir) row.addEventListener('dblclick', () => this.go(e.path));
    row.addEventListener('contextmenu', ev => { ev.preventDefault(); this.menu(ev, e); });
    row.addEventListener('dragstart', ev => {
      ev.dataTransfer.setData('text/x-fx-path', e.path);
      ev.dataTransfer.effectAllowed = 'copyMove';
    });
    if (e.is_dir) {
      row.addEventListener('dragover', ev => {
        if (!ev.dataTransfer.types.includes('text/x-fx-path')) return;
        ev.preventDefault();
        ev.stopPropagation();
        ev.dataTransfer.dropEffect = copyKey(ev) ? 'copy' : 'move';
        row.classList.add('drop');
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop'));
      row.addEventListener('drop', ev => {
        ev.preventDefault();
        ev.stopPropagation();
        row.classList.remove('drop');
        const src = ev.dataTransfer.getData('text/x-fx-path');
        if (src && src !== e.path && copyKey(ev)) this.copy(src, e.path);
        else if (src && src !== e.path && parentOf(src) !== e.path) this.move(src, e.path);
      });
    }
    return row;
  }

  async activate(e) {
    this.selected = e.path;
    this.matches.clear();
    if (e.is_dir) {
      if (this.expanded.has(e.path)) this.expanded.delete(e.path);
      else {
        this.expanded.add(e.path);
        this.renderTree();
        try { await this.list(e.path); } catch (err) { toast(err.message, 'error'); this.expanded.delete(e.path); }
      }
      this.renderTree();
    } else {
      this.renderTree();
      await this.open(e.path);
    }
  }

  findEntry(path) {
    for (const entries of this.children.values()) {
      const e = entries.find(x => x.path === path);
      if (e) return e;
    }
    return null;
  }

  currentDir() {
    const e = this.selected && this.findEntry(this.selected);
    if (!e) return this.base;
    return e.is_dir ? e.path : parentOf(e.path);
  }

  menu(ev, e) {
    const items = [];
    if (e.is_dir) {
      items.push({ label: this.expanded.has(e.path) ? 'Collapse' : 'Expand', action: () => this.activate(e) });
      items.push({ label: 'Open as top folder', action: () => this.go(e.path) });
      items.push({ label: 'New file inside…', action: () => this.newFile(e.path) });
      items.push({ label: 'New folder inside…', action: () => this.newFolder(e.path) });
      if (this.canPaste()) items.push({ label: `Paste into (${clipboard.name})`, action: () => this.paste(e.path) });
    } else {
      items.push({ label: 'Open', action: () => { this.selected = e.path; this.renderTree(); this.open(e.path); } });
      items.push({ label: 'Download', action: () => window.open(`/api/fs/raw?path=${enc(e.path)}&${this.rootQuery()}&download=1`) });
    }
    items.push('-');
    items.push({ label: 'Rename…', action: () => this.rename(e) });
    items.push({ label: 'Move to…', action: () => this.moveTo(e) });
    items.push('-');
    items.push({ label: 'Copy', action: () => this.copyToClipboard(e) });
    items.push({ label: 'Duplicate', action: () => this.copy(e.path, parentOf(e.path)) });
    items.push({ label: 'Copy to…', action: () => this.copyTo(e) });
    items.push({ label: 'Copy path', action: () => navigator.clipboard?.writeText(e.path).then(() => toast('Path copied', 'ok')) });
    items.push('-');
    items.push({ label: 'Delete', danger: true, action: () => this.remove(e) });
    contextMenu(ev.clientX, ev.clientY, items);
  }

  // ------------------------------------------------------------ operations

  async newFile(dir = this.currentDir()) {
    const name = await promptDialog('New file', { label: `File name (in ${dir})`, placeholder: 'notes.md', okLabel: 'Create' });
    if (!name) return;
    const path = join(dir, name);
    await api('POST', '/api/fs/write', { path, content: '', root: this.root, create_only: true });
    if (dir !== this.base) this.expanded.add(dir);
    this.selected = path;
    await this.refresh();
    await this.open(path);
    toast(`Created ${path}`, 'ok');
  }

  async newFolder(dir = this.currentDir()) {
    const name = await promptDialog('New folder', { label: `Folder name (in ${dir})`, placeholder: 'src', okLabel: 'Create' });
    if (!name) return;
    const path = join(dir, name);
    await api('POST', '/api/fs/mkdir', { path, root: this.root });
    if (dir !== this.base) this.expanded.add(dir);
    this.expanded.add(path);
    await this.refresh();
    toast(`Created ${path}/`, 'ok');
  }

  async rename(e) {
    const name = await promptDialog('Rename', { label: `New name for ${e.name}`, value: e.name, okLabel: 'Rename' });
    if (!name || name === e.name) return;
    if (name.includes('/')) { toast('Use "Move to…" to move into another folder.', 'error'); return; }
    await this.move(e.path, join(parentOf(e.path), name));
  }

  async moveTo(e) {
    const dest = await pickDirectory({ title: `Move ${e.name} to…`, start: parentOf(e.path), root: this.root, okLabel: 'Move here' });
    if (!dest || dest === parentOf(e.path)) return;
    await this.move(e.path, dest);
  }

  async renameOrMove(e) {
    const dest = await promptDialog('Rename or move', {
      label: 'New path: change the name to rename it, or the folder to move it', value: e.path, okLabel: 'Apply',
    });
    if (!dest || dest === e.path) return;
    await this.move(e.path, dest);
  }

  // A copy never overwrites: the server picks "name (copy)", "name (copy 2)"… when the target exists.
  async copy(src, destination) {
    let res;
    try {
      res = await api('POST', '/api/fs/copy', { source: src, destination, root: this.root, rename_if_exists: true });
    } catch { return; }
    // Expand the folders down to the copy so it is visible.
    for (let d = parentOf(res.path); d !== this.base && inside(d, this.base) && d !== '/'; d = parentOf(d)) this.expanded.add(d);
    this.selected = this.lastCopy = res.path;
    this.matches.clear();
    await this.refresh();
    this.tree.querySelector('.fx-node.selected')?.scrollIntoView({ block: 'nearest' });
    toast(`Copied to ${res.path}`, 'ok');
  }

  async copyTo(e) {
    const dest = await pickDirectory({ title: `Copy ${e.name} to…`, start: parentOf(e.path), root: this.root, okLabel: 'Copy here' });
    if (dest) await this.copy(e.path, dest);
  }

  copyToClipboard(e) {
    clipboard = { path: e.path, name: e.name, is_dir: e.is_dir };
    this.renderPasteBtn();
    toast(`${e.name} copied: right-click a folder and choose "Paste into", or use Paste in the toolbar`, 'ok');
  }

  canPaste() { return !!clipboard && inside(clipboard.path, this.root); }

  async paste(dir) {
    if (this.canPaste()) await this.copy(clipboard.path, dir);
  }

  renderPasteBtn() {
    this.pasteBtn.hidden = !this.canPaste();
    if (clipboard) this.pasteBtn.title = `Paste ${clipboard.path} into the selected folder`;
  }

  async move(src, destination) {
    let res;
    try {
      res = await api('POST', '/api/fs/move', { source: src, destination, root: this.root });
    } catch { return; }
    const moved = (p) => (p === src || p.startsWith(src + '/')) ? res.path + p.slice(src.length) : p;
    this.expanded = new Set([...this.expanded].map(moved));
    if (this.selected) this.selected = moved(this.selected);
    if (this.file) { this.file.path = moved(this.file.path); this.renderEditorHead(); }
    if (clipboard) clipboard.path = moved(clipboard.path);
    this.renderPasteBtn();
    await this.refresh();
    toast(`Moved to ${res.path}`, 'ok');
  }

  async remove(e) {
    const what = e.is_dir ? `the folder ${e.path} and everything inside it` : e.path;
    if (!await confirmDialog(`Delete ${what}?\nThis cannot be undone.`, { okLabel: 'Delete', danger: true })) return;
    try {
      await api('POST', '/api/fs/delete', { path: e.path, root: this.root });
    } catch { return; }
    if (this.file && (this.file.path === e.path || this.file.path.startsWith(e.path + '/'))) this.closeFile(true);
    this.expanded.delete(e.path);
    if (this.selected === e.path) this.selected = null;
    if (clipboard && inside(clipboard.path, e.path)) { clipboard = null; this.renderPasteBtn(); }
    await this.refresh();
    toast(`Deleted ${e.path}`, 'ok');
  }

  // ------------------------------------------------------------ editor

  async open(path) {
    if (this.file?.dirty && this.file.path !== path && !await confirmDialog(`Discard unsaved changes to ${this.file.path}?`, { okLabel: 'Discard', danger: true })) return;
    let res;
    try {
      res = await api('GET', `/api/fs/read?path=${enc(path)}&${this.rootQuery()}`);
    } catch { return; }
    this.file = { path, size: res.size, binary: res.binary, truncated: res.truncated, dirty: false, original: res.content };
    state.dirty.delete(this);
    this.renderEditor(res);
  }

  renderEditorPlaceholder() {
    fill(this.editor, h('div', { class: 'empty' }, h('div', { class: 'big' }, '📄'),
      'Select a file to open it.', h('div', { class: 'small' }, 'Drag items onto folders to move them (hold Ctrl or Alt to copy) · right-click for more actions')));
  }

  renderEditorHead() {
    if (!this.file || !this.head) return;
    const f = this.file;
    fill(this.head,
      f.dirty ? h('span', { class: 'dirty-dot', title: 'Unsaved changes' }, '●') : null,
      h('span', { class: 'path', title: f.path }, f.path),
      h('span', { class: 'small muted' }, fmtBytes(f.size)),
      !f.binary && !f.truncated ? h('button', { class: 'sm primary', disabled: !f.dirty, onclick: () => this.save() }, 'Save') : null,
      h('button', { class: 'sm', title: 'Reload from disk', onclick: () => { if (this.file) { this.file.dirty = false; this.open(this.file.path); } } }, '⟳'),
      h('button', { class: 'sm', title: 'Download', onclick: () => window.open(`/api/fs/raw?path=${enc(f.path)}&${this.rootQuery()}&download=1`) }, '⤓'),
      h('button', { class: 'sm', title: 'Close', onclick: () => this.closeFile() }, '✕'));
  }

  renderEditor(res) {
    this.head = h('div', { class: 'fx-editor-head' });
    let body;
    const raw = `/api/fs/raw?path=${enc(res.path)}&${this.rootQuery()}`;
    if (res.binary) {
      body = IMAGE_EXT.test(res.path)
        ? h('div', { class: 'fx-preview' }, h('img', { src: raw, alt: res.path }))
        : h('div', { class: 'empty' }, h('div', { class: 'big' }, '📦'), 'Binary file — it cannot be edited here.',
          h('div', {}, h('a', { href: raw + '&download=1' }, 'Download')));
    } else {
      const ta = h('textarea', { class: 'code', spellcheck: 'false', value: res.content, readonly: res.truncated });
      const indent = /^\t/m.test(res.content) ? '\t' : /\.(py|java|kt|rs|go|c|cpp|cs|php)$/.test(res.path) ? '    ' : '  ';
      ta.addEventListener('input', () => {
        const dirty = ta.value !== this.file.original;
        if (dirty !== this.file.dirty) {
          this.file.dirty = dirty;
          dirty ? state.dirty.add(this) : state.dirty.delete(this);
          this.renderEditorHead();
        }
      });
      ta.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); this.save(); }
        else if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); document.execCommand('insertText', false, indent); }
      });
      this.textarea = ta;
      body = res.truncated
        ? h('div', { style: { display: 'grid', gridTemplateRows: 'auto 1fr', minHeight: 0 } },
          h('div', { class: 'notice warn small', style: { borderRadius: 0 } }, 'File is larger than 5 MB: showing the beginning, read-only.'), ta)
        : ta;
    }
    fill(this.editor, this.head, body);
    this.renderEditorHead();
  }

  async save() {
    if (!this.file || !this.textarea || !this.file.dirty) return;
    const content = this.textarea.value;
    try {
      const res = await api('POST', '/api/fs/write', { path: this.file.path, content, root: this.root });
      this.file.original = content;
      this.file.dirty = false;
      this.file.size = res.size;
      state.dirty.delete(this);
      this.renderEditorHead();
      toast(`Saved ${this.file.path}`, 'ok', 2000);
      this.refresh();
    } catch { /* error already shown */ }
  }

  async closeFile(force = false) {
    if (!force && this.file?.dirty && !await confirmDialog('Discard unsaved changes?', { okLabel: 'Discard', danger: true })) return;
    this.file = null;
    this.textarea = null;
    state.dirty.delete(this);
    this.renderEditorPlaceholder();
  }

  dispose() { state.dirty.delete(this); }
}

/** Modal folder picker. Resolves to the chosen absolute path, or null. */
export function pickDirectory({ title = 'Choose a folder', start = '/', root = '/', okLabel = 'Select', allowCreate = true } = {}) {
  return new Promise(resolve => {
    let current = start;
    let chosen = null;
    let listed = null;      // the folder (as typed or opened) whose subfolders are listed
    let dirs = [];          // subfolders of `listed`, each { entry, node }
    let selected = null;    // the subfolder matching the typed name
    let seq = 0;
    let pending = null;
    const pathInput = h('input', { type: 'text', class: 'mono', value: start, spellcheck: 'false', autocomplete: 'off' });
    const list = h('div', { class: 'dir-picker' });
    const norm = p => (p.length > 1 ? p.replace(/\/+$/, '') : p) || '/';
    // Split the typed path into the folder to list and the partial name of one of its subfolders.
    const split = value => {
      const i = value.lastIndexOf('/');
      return { dir: i <= 0 ? '/' : norm(value.slice(0, i)), prefix: i < 0 ? '/' : value.slice(0, i + 1), partial: value.slice(i + 1) };
    };
    const matches = partial => matchNames(dirs, partial, d => d.entry.name);
    const highlight = () => {
      const { dir, partial } = split(pathInput.value.trim());
      selected = null;
      if (dir === listed && partial) {
        const found = matches(partial);
        selected = found.find(d => d.entry.name === partial) || found[0] || null;
      }
      for (const d of dirs) d.node.classList.toggle('selected', d === selected);
      selected?.node.scrollIntoView({ block: 'nearest' });
    };
    const load = (dir, { typed = false, fallbackUp = false } = {}) => {
      const token = ++seq;
      const run = async () => {
        try {
          const res = await api('GET', `/api/fs/list?path=${enc(dir)}&root=${enc(root)}&hidden=1`, undefined, { quiet: true });
          if (token !== seq) return;
          current = res.path;
          listed = typed ? norm(dir) : norm(res.path);
          if (!typed) pathInput.value = current;
          dirs = res.entries.filter(e => e.is_dir).map(entry => ({
            entry,
            node: h('div', { class: 'fx-node', onclick: () => load(entry.path), title: entry.path },
              h('span', { class: 'ico' }, '📁'), h('span', { class: 'name' }, entry.name)),
          }));
          fill(list,
            res.parent ? h('div', { class: 'fx-node', onclick: () => load(res.parent) }, h('span', { class: 'ico' }, '⬆'), h('span', { class: 'name' }, '..')) : null,
            ...dirs.map(d => d.node),
            dirs.length ? null : h('div', { class: 'empty small' }, 'No subfolders'));
          highlight();
        } catch (e) {
          if (token !== seq) return;
          if (fallbackUp && dir !== '/' && dir !== root) return load(parentOf(dir), { fallbackUp: true });
          if (typed) highlight(); else toast(e.message, 'error');
        }
      };
      return (pending = run());
    };
    // Follow the typed path: list its folder and select the subfolder whose name is being typed.
    const follow = () => {
      const value = pathInput.value.trim();
      if (!value.startsWith('/')) return highlight();
      const { dir } = split(value);
      if (dir !== listed) return load(dir, { typed: true });
      highlight();
    };
    const complete = async () => {
      await follow();
      await pending;
      const value = pathInput.value.trim();
      const { dir, prefix, partial } = split(value);
      if (dir !== listed) return;
      const found = matches(partial);
      if (!found.length) return;
      if (found.length === 1) {
        pathInput.value = prefix + found[0].entry.name + '/';
      } else {
        const common = commonPrefix(found.map(d => d.entry.name));
        if (common.length <= partial.length) return;
        pathInput.value = prefix + common;
      }
      follow();
    };
    pathInput.addEventListener('input', follow);
    pathInput.addEventListener('keydown', e => {
      if (e.key === 'Tab' && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); complete(); }
      if (e.key === 'Enter') { e.preventDefault(); load(selected ? selected.entry.path : pathInput.value.trim()); }
    });
    const newFolder = h('button', { class: 'sm', onclick: async () => {
      const name = await promptDialog('New folder', { label: `Create inside ${current}`, okLabel: 'Create' });
      if (!name) return;
      try { await api('POST', '/api/fs/mkdir', { path: join(current, name), root }); load(join(current, name)); } catch { /* shown */ }
    } }, '+ New folder');
    modal({
      title,
      body: h('div', { class: 'stack' }, h('div', { class: 'row' }, pathInput, allowCreate ? newFolder : null), list,
        h('div', { class: 'small muted' }, 'Click a folder to open it, or type a path (Tab completes it); the folder shown in the path box is selected.')),
      actions: [{ label: okLabel, kind: 'primary', onClick: async () => {
        await pending;
        const value = norm(pathInput.value.trim());
        if (value === listed || value === current) { chosen = current; return; }
        if (selected && norm(selected.entry.path) === value) { chosen = selected.entry.path; return; }
        try {
          const res = await api('GET', `/api/fs/list?path=${enc(value)}&root=${enc(root)}&hidden=1`, undefined, { quiet: true });
          chosen = res.path;
        } catch (e) { toast(e.message, 'error'); return false; }
      } }],
      onClose: () => resolve(chosen),
    });
    load(start, { fallbackUp: true });
  });
}
