"""Filesystem operations used by both the file browser API and the agents' file tools.

Every function takes absolute paths. `confine()` resolves a path against a root and
rejects anything that escapes it, so callers scope operations to a project repository,
the app directory, or (for the system browser) the whole filesystem.
"""

import os
import shutil
import stat
from pathlib import Path

MAX_READ_BYTES = 5 * 1024 * 1024

# Never delete or move these, whatever the caller's root is.
PROTECTED = {Path(p) for p in (
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32", "/media",
    "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/snap", "/srv", "/sys", "/tmp", "/usr", "/var",
)}


class FsError(Exception):
    """A user-facing filesystem error (bad path, missing file, refused operation)."""


def confine(root: str | Path, path: str | Path) -> Path:
    """Resolve `path` (absolute, or relative to `root`) and require it to stay inside `root`."""
    root = Path(root).resolve()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = root / p
    p = Path(os.path.normpath(p))
    # Resolve symlinks of the existing part so a link cannot point outside the root.
    resolved = p.resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise FsError(f"Path is outside the allowed root {root}: {path}")
    return p


def _guard_destructive(p: Path, root: Path) -> None:
    rp = p.resolve()
    if rp in PROTECTED or rp == Path(root).resolve():
        raise FsError(f"Refusing to modify protected path: {p}")
    from .config import APP_ROOT, DATA_DIR
    if rp in (APP_ROOT, DATA_DIR) or APP_ROOT.is_relative_to(rp):
        raise FsError(f"Refusing to modify the Agent Group installation directory: {p}")


def entry(p: Path) -> dict:
    try:
        st = p.lstat()
        is_link = stat.S_ISLNK(st.st_mode)
        is_dir = p.is_dir()
        size = st.st_size if not is_dir else None
        mtime = st.st_mtime
    except OSError:
        is_link, is_dir, size, mtime = False, False, None, None
    return {"name": p.name or str(p), "path": str(p), "is_dir": is_dir, "is_link": is_link,
            "size": size, "mtime": mtime}


def list_dir(p: Path, show_hidden: bool = True) -> list[dict]:
    if not p.exists():
        raise FsError(f"No such directory: {p}")
    if not p.is_dir():
        raise FsError(f"Not a directory: {p}")
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise FsError(f"Permission denied: {p}")
    items = [entry(c) for c in children if show_hidden or not c.name.startswith(".")]
    items.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return items


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError as e:
        # A multibyte character cut at the sample boundary is not binary.
        return e.start < len(data[:8192]) - 4
    return False


def read_file(p: Path) -> dict:
    if not p.exists():
        raise FsError(f"No such file: {p}")
    if p.is_dir():
        raise FsError(f"Is a directory: {p}")
    size = p.stat().st_size
    with open(p, "rb") as f:
        data = f.read(MAX_READ_BYTES + 1)
    truncated = len(data) > MAX_READ_BYTES
    data = data[:MAX_READ_BYTES]
    if is_binary(data):
        return {"path": str(p), "size": size, "binary": True, "content": None, "truncated": truncated}
    return {"path": str(p), "size": size, "binary": False, "truncated": truncated,
            "content": data.decode("utf-8", errors="replace")}


def write_file(p: Path, content: str, create_only: bool = False) -> dict:
    if p.exists() and p.is_dir():
        raise FsError(f"Is a directory: {p}")
    if create_only and p.exists():
        raise FsError(f"Already exists: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.agtmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if p.exists():
        shutil.copymode(p, tmp)
    os.replace(tmp, p)
    return entry(p)


def make_dir(p: Path) -> dict:
    if p.exists():
        raise FsError(f"Already exists: {p}")
    p.mkdir(parents=True)
    return entry(p)


def delete(p: Path, root: Path) -> None:
    if not p.exists() and not p.is_symlink():
        raise FsError(f"No such file or directory: {p}")
    _guard_destructive(p, root)
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def move(src: Path, dst: Path, root: Path, overwrite: bool = False) -> dict:
    """Move or rename. If `dst` is an existing directory, `src` moves inside it."""
    if not src.exists() and not src.is_symlink():
        raise FsError(f"No such file or directory: {src}")
    _guard_destructive(src, root)
    if dst.is_dir() and not dst.is_symlink():
        dst = dst / src.name
    if dst.resolve() == src.resolve():
        raise FsError("Source and destination are the same")
    if src.is_dir() and dst.resolve().is_relative_to(src.resolve()):
        raise FsError("Cannot move a directory inside itself")
    if dst.exists():
        if not overwrite:
            raise FsError(f"Destination already exists: {dst}")
        _guard_destructive(dst, root)
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return entry(dst)


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".next", "dist", "build", ".cache", ".idea"}


def tree(p: Path, depth: int = 2, limit: int = 400) -> str:
    """Indented directory listing for agents. Skips heavy dirs below the top level."""
    lines: list[str] = []

    def walk(d: Path, level: int):
        if len(lines) >= limit:
            return
        try:
            children = sorted(d.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except OSError as e:
            lines.append("  " * level + f"[unreadable: {e.strerror}]")
            return
        for c in children:
            if len(lines) >= limit:
                lines.append("  " * level + "... (listing truncated)")
                return
            if c.is_dir() and not c.is_symlink():
                lines.append("  " * level + c.name + "/")
                if c.name in SKIP_DIRS:
                    continue
                if level + 1 < depth:
                    walk(c, level + 1)
            else:
                size = c.lstat().st_size if c.exists() or c.is_symlink() else 0
                lines.append("  " * level + f"{c.name}  ({_human(size)})")

    walk(p, 0)
    return "\n".join(lines) if lines else "(empty directory)"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
