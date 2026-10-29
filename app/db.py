"""SQLite storage. One shared connection guarded by a lock; rows come back as dicts."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    repo_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    progress INTEGER NOT NULL DEFAULT 0,
    estimated_hours REAL,
    time_spent_seconds REAL NOT NULL DEFAULT 0,
    insights TEXT NOT NULL DEFAULT '[]',
    next_actions TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_templates (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    rules TEXT NOT NULL DEFAULT '[]',
    expertise TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]',
    subagents TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_conv_kind ON conversations(kind, topic_id, task_id);
"""

with _lock:
    _conn.executescript(SCHEMA)
    # Migrations for databases created by earlier versions.
    if "engine" not in {r["name"] for r in _conn.execute("PRAGMA table_info(messages)")}:
        _conn.execute("ALTER TABLE messages ADD COLUMN engine TEXT NOT NULL DEFAULT 'api'")
    if "usage" not in {r["name"] for r in _conn.execute("PRAGMA table_info(messages)")}:
        _conn.execute("ALTER TABLE messages ADD COLUMN usage TEXT")
    agent_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(agents)")}
    if "template" not in agent_cols:   # slug of the agent template the agent is based on, '' for none
        _conn.execute("ALTER TABLE agents ADD COLUMN template TEXT NOT NULL DEFAULT ''")
    if "subagents" not in agent_cols:  # JSON list of template slugs: the agent's internal agents
        _conn.execute("ALTER TABLE agents ADD COLUMN subagents TEXT NOT NULL DEFAULT '[]'")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> int:
    """Run a write statement; returns lastrowid."""
    with _lock:
        return _conn.execute(sql, params).lastrowid


def transaction():
    """Context manager for multi-statement writes: `with db.transaction(): ...`."""
    return _Tx()


class _Tx:
    def __enter__(self):
        _lock.acquire()
        _conn.execute("BEGIN")

    def __exit__(self, exc_type, *_):
        try:
            _conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _lock.release()


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "engine": "claude_code",     # "claude_code" (local Claude Code login) or "api" (Anthropic API key)
    "api_key": "",
    "chat_model": "claude-opus-5-5",
    "chat_effort": "medium",
    "dev_model": "claude-opus-5-5",
    "dev_effort": "high",
    "project_model": "claude-opus-5-5",
    "project_effort": "high",
    "use_fallback": True,
    "chat_web_search": False,
    "allow_commands": True,
    "max_agent_turns": 80,
}


def get_settings() -> dict:
    stored = {r["key"]: json.loads(r["value"]) for r in query("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def save_settings(values: dict) -> dict:
    with transaction():
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                _conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
    return get_settings()


# ---------------------------------------------------------------- conversations

def create_conversation(kind: str, title: str, system_prompt: str, *, topic_id=None, task_id=None,
                        options: dict | None = None) -> dict:
    ts = now()
    cid = execute(
        "INSERT INTO conversations(kind, topic_id, task_id, title, system_prompt, options, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, topic_id, task_id, title, system_prompt, json.dumps(options or {}), ts, ts),
    )
    return get_conversation(cid)


def get_conversation(cid: int) -> dict | None:
    row = one("SELECT * FROM conversations WHERE id = ?", (cid,))
    if row:
        row["options"] = json.loads(row["options"])
    return row


def list_messages(cid: int) -> list[dict]:
    rows = query("SELECT id, role, content, engine, usage, created_at FROM messages WHERE conversation_id = ? "
                 "ORDER BY id", (cid,))
    for r in rows:
        r["content"] = json.loads(r["content"])
        r["usage"] = json.loads(r["usage"]) if r["usage"] else None
    return rows


def add_message(cid: int, role: str, content, engine: str = "api", usage: dict | None = None) -> int:
    """Store a message. `engine` records which engine produced it ("api" or "claude_code"); `usage` holds
    the token counts of the model request that produced an assistant message."""
    ts = now()
    with transaction():
        mid = _conn.execute(
            "INSERT INTO messages(conversation_id, role, content, engine, usage, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (cid, role, json.dumps(content, ensure_ascii=False), engine, json.dumps(usage) if usage else None, ts),
        ).lastrowid
        _conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, cid))
    return mid


def set_message_usage(mid: int, usage: dict) -> None:
    with transaction():
        _conn.execute("UPDATE messages SET usage = ? WHERE id = ?", (json.dumps(usage), mid))


def update_conversation_options(cid: int, patch: dict) -> dict:
    with transaction():
        row = _conn.execute("SELECT options FROM conversations WHERE id = ?", (cid,)).fetchone()
        options = {**json.loads(row["options"]), **patch} if row else dict(patch)
        _conn.execute("UPDATE conversations SET options = ? WHERE id = ?", (json.dumps(options), cid))
    return options


# ---------------------------------------------------------------- tasks

def get_task(tid: int) -> dict | None:
    row = one("SELECT * FROM tasks WHERE id = ?", (tid,))
    if row:
        row["insights"] = json.loads(row["insights"])
        row["next_actions"] = json.loads(row["next_actions"])
    return row


def list_tasks(project_id: int) -> list[dict]:
    rows = query("SELECT * FROM tasks WHERE project_id = ? ORDER BY id DESC", (project_id,))
    for r in rows:
        r["insights"] = json.loads(r["insights"])
        r["next_actions"] = json.loads(r["next_actions"])
    return rows


TASK_FIELDS = ("title", "description", "status", "progress", "estimated_hours", "summary",
               "insights", "next_actions", "time_spent_seconds")


def update_task(tid: int, fields: dict) -> dict:
    sets, params = [], []
    for key in TASK_FIELDS:
        if key in fields:
            value = fields[key]
            if key in ("insights", "next_actions"):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key} = ?")
            params.append(value)
    if sets:
        sets.append("updated_at = ?")
        params += [now(), tid]
        execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
    return get_task(tid)


def add_task_event(tid: int, source: str, text: str) -> None:
    execute("INSERT INTO task_events(task_id, source, text, created_at) VALUES(?, ?, ?, ?)",
            (tid, source, text, now()))


def get_project(pid: int) -> dict | None:
    project = one("SELECT * FROM projects WHERE id = ?", (pid,))
    if project:
        project["agent"] = agent = one("SELECT * FROM agents WHERE project_id = ?", (pid,))
        if agent:
            agent["subagents"] = json.loads(agent["subagents"])
    return project
