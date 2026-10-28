// File explorer (tree + editor) and a directory picker, both backed by /api/fs/*.

import { api, h, fill, enc, toast, modal, confirmDialog, promptDialog, contextMenu, fmtBytes, state } from './core.js';

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

function parentOf(path) {
  const p = path.replace(/\/+$/, '');
  const i = p.lastIndexOf('/');
  return i <= 0 ? '/' : p.slice(0, i);
}

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
  /** opts: root (absolute directory the explorer is confined to), start (initial directory), onNavigate(dir) */
  constructor(el, opts = {}) {
    this.el = el;
    this.root = opts.root || '/';
    this.base = opts.start || this.root;
    this.opts = opts;
    this.children = new Map();   // dir path -> entries
    this.expanded = new Set();
    this.selected = null;
    this.file = null;
    this.showHidden = localStorage.getItem('fx-hidden') !== '0';
    this.build();
    this.refresh();
  }

  rootQuery() { return `root=${enc(this.root)}`; }

  build() {
    this.pathInput = h('input', { type: 'text', value: this.base, title: 'Folder shown in the tree. Type a path and press Enter.' });
    this.pathInput.addEventListener('keydown', e => { if (e.key === 'Enter') this.go(this.pathInput.value.trim()); });
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
      hiddenBtn);
    this.tree = h('div', { class: 'fx-tree' });
    // Dropping on empty tree space moves the item into the folder shown at the top.
    this.tree.addEventListener('dragover', e => {
      if (e.dataTransfer.types.includes('text/x-fx-path')) { e.preventDefault(); this.tree.classList.add('drop-target'); }
    });
    this.tree.addEventListener('dragleave', e => { if (e.target === this.tree) this.tree.classList.remove('drop-target'); });
    this.tree.addEventListener('drop', e => {
      e.preventDefault();
      this.tree.classList.remove('drop-target');
      const src = e.dataTransfer.getData('text/x-fx-path');
      if (src && parentOf(src) !== this.base) this.move(src, this.base);
    });
    this.tree.addEventListener('contextmenu', e => {
      if (e.target !== this.tree) return;
      e.preventDefault();
      contextMenu(e.clientX, e.clientY, [
        { label: 'New file here', action: () => this.newFile(this.base) },
        { label: 'New folder here', action: () => this.newFolder(this.base) },
        { label: 'Refresh', action: () => this.refresh() },
      ]);
    });
    this.editor = h('div', { class: 'fx-editor' });
    this.renderEditorPlaceholder();
    fill(this.el, h('div', { class: 'fx' }, toolbar, h('div', { class: 'fx-main' }, this.tree, this.editor)));
  }

  // ------------------------------------------------------------ navigation

  async list(dir) {
    const res = await api('GET', `/api/fs/list?path=${enc(dir)}&${this.rootQuery()}&hidden=${this.showHidden ? 1 : 0}`, undefined, { quiet: true });
    this.children.set(dir, res.entries);
    return res;
  }

  async refresh() {
    this.pathInput.value = this.base;
    try {
      await this.list(this.base);
    } catch (e) {
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
    this.pathInput.value = path;
    this.renderTree();
    this.opts.onNavigate?.(path);
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
      class: `fx-node${this.selected === e.path ? ' selected' : ''}`,
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
      ev.dataTransfer.effectAllowed = 'move';
    });
    if (e.is_dir) {
      row.addEventListener('dragover', ev => {
        if (!ev.dataTransfer.types.includes('text/x-fx-path')) return;
        ev.preventDefault();
        ev.stopPropagation();
        row.classList.add('drop');
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop'));
      row.addEventListener('drop', ev => {
        ev.preventDefault();
        ev.stopPropagation();
        row.classList.remove('drop');
        const src = ev.dataTransfer.getData('text/x-fx-path');
        if (src && src !== e.path && parentOf(src) !== e.path) this.move(src, e.path);
      });
    }
    return row;
  }

  async activate(e) {
    this.selected = e.path;
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
    } else {
      items.push({ label: 'Open', action: () => { this.selected = e.path; this.renderTree(); this.open(e.path); } });
      items.push({ label: 'Download', action: () => window.open(`/api/fs/raw?path=${enc(e.path)}&${this.rootQuery()}&download=1`) });
    }
    items.push('-');
    items.push({ label: 'Rename…', action: () => this.rename(e) });
    items.push({ label: 'Move to…', action: () => this.moveTo(e) });
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

  async move(src, destination) {
    let res;
    try {
      res = await api('POST', '/api/fs/move', { source: src, destination, root: this.root });
    } catch { return; }
    const moved = (p) => (p === src || p.startsWith(src + '/')) ? res.path + p.slice(src.length) : p;
    this.expanded = new Set([...this.expanded].map(moved));
    if (this.selected) this.selected = moved(this.selected);
    if (this.file) { this.file.path = moved(this.file.path); this.renderEditorHead(); }
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
      'Select a file to open it.', h('div', { class: 'small' }, 'Drag items onto folders to move them · right-click for more actions')));
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
    const pathInput = h('input', { type: 'text', class: 'mono', value: start });
    const list = h('div', { class: 'dir-picker' });
    const load = async (dir, fallbackUp = false) => {
      try {
        const res = await api('GET', `/api/fs/list?path=${enc(dir)}&root=${enc(root)}&hidden=1`, undefined, { quiet: true });
        current = res.path;
        pathInput.value = current;
        const dirs = res.entries.filter(e => e.is_dir);
        fill(list,
          res.parent ? h('div', { class: 'fx-node', onclick: () => load(res.parent) }, h('span', { class: 'ico' }, '⬆'), h('span', { class: 'name' }, '..')) : null,
          ...dirs.map(e => h('div', { class: 'fx-node', onclick: () => load(e.path), title: e.path },
            h('span', { class: 'ico' }, '📁'), h('span', { class: 'name' }, e.name))),
          dirs.length ? null : h('div', { class: 'empty small' }, 'No subfolders'));
      } catch (e) {
        if (fallbackUp && dir !== '/' && dir !== root) return load(parentOf(dir), true);
        toast(e.message, 'error');
      }
    };
    pathInput.addEventListener('keydown', e => { if (e.key === 'Enter') load(pathInput.value.trim()); });
    const newFolder = h('button', { class: 'sm', onclick: async () => {
      const name = await promptDialog('New folder', { label: `Create inside ${current}`, okLabel: 'Create' });
      if (!name) return;
      try { await api('POST', '/api/fs/mkdir', { path: join(current, name), root }); load(join(current, name)); } catch { /* shown */ }
    } }, '+ New folder');
    modal({
      title,
      body: h('div', { class: 'stack' }, h('div', { class: 'row' }, pathInput, allowCreate ? newFolder : null), list,
        h('div', { class: 'small muted' }, 'Click a folder to open it; the folder shown in the path box is selected.')),
      actions: [{ label: okLabel, kind: 'primary', onClick: () => { chosen = current; } }],
      onClose: () => resolve(chosen),
    });
    load(start, true);
  });
}
