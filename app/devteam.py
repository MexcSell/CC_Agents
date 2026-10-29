"""The development team of this website: a built-in project (projects.system = 'dev') whose repository is
Agent Group's own source directory. It is managed on the Developer page instead of the Projects page.

Its agents work like any project team (tasks, sub-tasks, delegation, consultations, questions, lessons),
with the Developer agent's safeguards and tools: protected directories, a source snapshot before the
first change of each task tree, the server log for everyone, and restart_server for the lead only
(a restart interrupts every running agent, so it is the last step of a finished change).
"""

import threading

from . import db, maintenance

SYSTEM = "dev"
DEFAULT_GROUP = "site-dev-team"
NAME = "Agent Group (this website)"
DESCRIPTION = ("The Agent Group web application itself: a FastAPI + SQLite backend and a plain JavaScript "
               "frontend. The team develops, fixes and maintains it and restarts the server when needed.")

_snapshotted: set[int] = set()
_lock = threading.Lock()


def is_dev(project: dict | None) -> bool:
    return bool(project) and project.get("system") == SYSTEM


def project_id() -> int | None:
    row = db.one("SELECT id FROM projects WHERE system = ? ORDER BY id LIMIT 1", (SYSTEM,))
    return row["id"] if row else None


def project() -> dict | None:
    pid = project_id()
    return db.get_project(pid) if pid else None


def snapshot_before_change(root_task_id: int) -> None:
    """Snapshot the source before the first file change of a task tree (once per tree and server run)."""
    with _lock:   # held while archiving, so teammates writing at the same time wait for the one snapshot
        if root_task_id not in _snapshotted:
            maintenance.create_snapshot(f"before-team-task-{root_task_id}")
            _snapshotted.add(root_task_id)
