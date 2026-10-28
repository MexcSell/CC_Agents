"""Code snapshots, preflight checks and restart requests for the self-modifying site."""

import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from .config import APP_ROOT, SERVER_LOG, SNAPSHOT_DIR

SNAPSHOT_EXCLUDE = {".venv", "data", ".git", "__pycache__", "node_modules"}
KEEP_SNAPSHOTS = 30

_restart_lock = threading.Lock()
_restart_requested = threading.Event()
_restart_callbacks: list = []


def _excluded(rel: Path) -> bool:
    return any(part in SNAPSHOT_EXCLUDE for part in rel.parts)


def create_snapshot(label: str = "") -> dict:
    """Archive the app's source (everything except venv, data and caches)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40].strip("-")
    name = f"{stamp}{'-' + safe if safe else ''}.tar.gz"
    path = SNAPSHOT_DIR / name
    n = 1
    while path.exists():
        path = SNAPSHOT_DIR / name.replace(".tar.gz", f"-{n}.tar.gz")
        n += 1
    with tarfile.open(path, "w:gz") as tar:
        for p in sorted(APP_ROOT.rglob("*")):
            rel = p.relative_to(APP_ROOT)
            if _excluded(rel) or p.is_dir():
                continue
            tar.add(p, arcname=str(rel), recursive=False)
    _prune()
    return snapshot_info(path)


def snapshot_info(path: Path) -> dict:
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "created": st.st_mtime}


def list_snapshots() -> list[dict]:
    return [snapshot_info(p) for p in sorted(SNAPSHOT_DIR.glob("*.tar.gz"), reverse=True)]


def _prune() -> None:
    for old in sorted(SNAPSHOT_DIR.glob("*.tar.gz"), reverse=True)[KEEP_SNAPSHOTS:]:
        old.unlink(missing_ok=True)


def restore_snapshot(name: str) -> None:
    """Replace the app source with the snapshot's contents (venv and data are untouched)."""
    path = SNAPSHOT_DIR / Path(name).name
    if not path.is_file():
        raise FileNotFoundError(name)
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            if m.name.startswith("/") or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                raise ValueError(f"Unsafe snapshot member: {m.name}")
        for child in APP_ROOT.iterdir():
            if child.name in SNAPSHOT_EXCLUDE:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        tar.extractall(APP_ROOT, members=members, filter="data")


def preflight() -> tuple[bool, str]:
    """Byte-compile the app and import the server module in a fresh interpreter."""
    env = {**os.environ, "AGENT_PREFLIGHT": "1"}
    steps = [
        [sys.executable, "-m", "compileall", "-q", "app"],
        [sys.executable, "-c", "import app.server"],
    ]
    for cmd in steps:
        proc = subprocess.run(cmd, cwd=APP_ROOT, capture_output=True, text=True, timeout=120, env=env)
        if proc.returncode != 0:
            return False, f"$ {' '.join(cmd[1:])}\n{proc.stdout}\n{proc.stderr}".strip()
    return True, "Preflight passed: app compiles and imports cleanly."


def tail_log(lines: int = 200) -> str:
    if not SERVER_LOG.exists():
        return "(no server log yet — the server writes one when started through supervisor.py)"
    with open(SERVER_LOG, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 256_000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


# ---------------------------------------------------------------- restart

def on_restart(callback) -> None:
    """Register the function that actually stops the server (set by server.py)."""
    _restart_callbacks.append(callback)


def request_restart() -> None:
    with _restart_lock:
        if _restart_requested.is_set():
            return
        _restart_requested.set()
    for cb in _restart_callbacks:
        cb()


def restart_requested() -> bool:
    return _restart_requested.is_set()


def under_supervisor() -> bool:
    return os.environ.get("AGENT_SUPERVISED") == "1"
