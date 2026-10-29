"""The Developer project: a built-in project (projects.system = 'dev') whose repository is Agent Group's
own source directory. It is created at startup, listed first on the Projects page, opened on the Developer
page, and cannot be deleted.

Its agents work like any project team (tasks, sub-tasks, delegation, consultations, questions, lessons),
with the Developer agent's safeguards and tools: protected directories, a source snapshot before the
first change of each task tree, the server log for everyone, and restart_server for the lead only
(a restart interrupts every running agent, so it is the last step of a finished change).
"""

import threading

from . import db, groups, maintenance

SYSTEM = "dev"
DEFAULT_GROUP = "site-dev-team"
NAME = "Developer"
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


def ensure_project(repo_path: str, model: str, effort: str) -> int:
    """Create the Developer project with the #site-dev-team team unless it exists. Returns its id."""
    pid = project_id()
    if pid:
        return pid
    ts = db.now()
    with db.transaction():
        pid = db._conn.execute(
            "INSERT INTO projects(name, description, repo_path, system, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (NAME, DESCRIPTION, repo_path, SYSTEM, ts, ts)).lastrowid
        group = groups.by_slug(DEFAULT_GROUP)
        if group and group["members"]:
            groups.insert_team(pid, group, model, effort, ts)
        else:   # the group was deleted: a single lead, the team can be changed under Team & settings
            db._conn.execute(
                "INSERT INTO agents(project_id, key, name, role, instructions, model, effort, template, subagents, "
                "created_at, updated_at) VALUES(?, 'lead', 'Developer', '', '', ?, ?, '', '[]', ?, ?)",
                (pid, model, effort, ts, ts))
    return pid


def snapshot_before_change(root_task_id: int) -> None:
    """Snapshot the source before the first file change of a task tree (once per tree and server run)."""
    with _lock:   # held while archiving, so teammates writing at the same time wait for the one snapshot
        if root_task_id not in _snapshotted:
            maintenance.create_snapshot(f"before-team-task-{root_task_id}")
            _snapshotted.add(root_task_id)
