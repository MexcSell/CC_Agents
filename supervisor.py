#!/usr/bin/env python3
"""Keeps the Agent Group web server running.

Runs `python -m app.server` as a child process and
- restarts it when it exits with RESTART_EXIT_CODE (a restart requested from the UI or the
  developer agent),
- restarts it with backoff if it crashes,
- restores the most recent source snapshot if a freshly started server crashes three times
  in a row before it has been up for 20 seconds (a broken self-modification).

Stdlib only, so it keeps working even if the app's code is broken.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR") or APP_ROOT / "data").resolve()
LOG_DIR = DATA_DIR / "logs"
SERVER_LOG = LOG_DIR / "server.log"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
RESTART_EXIT_CODE = 3
HEALTHY_AFTER = 20.0
MAX_FAST_FAILURES = 3

child: subprocess.Popen | None = None
stopping = False


def say(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [supervisor] {msg}"
    print(line, flush=True)
    with open(SERVER_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def rotate_log() -> None:
    if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > 5 * 1024 * 1024:
        SERVER_LOG.replace(SERVER_LOG.with_suffix(".log.1"))


def handle_signal(signum, _frame) -> None:
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.send_signal(signal.SIGTERM)


def restore_latest_snapshot(already_restored: set[str]) -> bool:
    candidates = [p for p in sorted(SNAPSHOT_DIR.glob("*.tar.gz"), reverse=True)
                  if p.name not in already_restored and "before-restore" not in p.name]
    if not candidates:
        return False
    snap = candidates[0]
    try:
        from app.maintenance import restore_snapshot
        restore_snapshot(snap.name)
    except Exception as e:  # the app package itself may be what is broken
        say(f"snapshot restore via app.maintenance failed ({e}); extracting directly")
        import shutil
        import tarfile
        with tarfile.open(snap, "r:gz") as tar:
            for child_path in APP_ROOT.iterdir():
                if child_path.name in {".venv", "data", ".git", "supervisor.py"}:
                    continue
                if child_path.is_dir():
                    shutil.rmtree(child_path)
                else:
                    child_path.unlink()
            tar.extractall(APP_ROOT, filter="data")
    already_restored.add(snap.name)
    say(f"restored source snapshot {snap.name}")
    return True


def main() -> int:
    global child
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    env = {**os.environ, "AGENT_SUPERVISED": "1", "PYTHONUNBUFFERED": "1"}
    fast_failures = 0
    restored: set[str] = set()

    while not stopping:
        rotate_log()
        say("starting server")
        started = time.time()
        with open(SERVER_LOG, "ab") as log:
            child = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=APP_ROOT, env=env,
                                     stdout=log, stderr=subprocess.STDOUT)
            code = child.wait()
        uptime = time.time() - started
        if stopping:
            break
        if code == RESTART_EXIT_CODE:
            say("restart requested")
            fast_failures = 0
            continue
        if code == 0:
            say("server exited normally")
            break
        fast_failures = fast_failures + 1 if uptime < HEALTHY_AFTER else 1
        say(f"server exited with code {code} after {uptime:.1f}s (fast failures: {fast_failures})")
        if fast_failures >= MAX_FAST_FAILURES:
            if restore_latest_snapshot(restored):
                fast_failures = 0
                continue
            say("no snapshot left to restore; retrying with backoff")
        time.sleep(min(2 ** fast_failures, 30))

    say("supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
