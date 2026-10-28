"""Agent Group web server: REST API under /api, SSE for agent runs, static frontend at /."""

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents, db, fsops, maintenance, runs
from .config import (ACCESS_TOKEN, APP_ROOT, EFFORTS, HOST, MODELS, PORT, RESTART_EXIT_CODE,
                     STATIC_DIR)

log = logging.getLogger("agent_group")
STARTED_AT = time.time()
STOPPING = threading.Event()
MODEL_IDS = {m["id"] for m in MODELS}

app = FastAPI(title="Agent Group", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- middleware

@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if ACCESS_TOKEN and path.startswith("/api") and path not in ("/api/health", "/api/login"):
        token = request.cookies.get("agent_token") or request.query_params.get("token") or ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
        if not hmac.compare_digest(token, ACCESS_TOKEN):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    response = await call_next(request)
    if not path.startswith("/api"):
        # Let the developer agent's frontend edits show up on a plain reload.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(fsops.FsError)
async def fs_error(_: Request, exc: fsops.FsError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(PermissionError)
async def perm_error(_: Request, exc: PermissionError):
    return JSONResponse({"detail": f"Permission denied: {exc.filename or exc}"}, status_code=403)


def not_found(what: str = "Not found"):
    raise HTTPException(404, what)


# ---------------------------------------------------------------- health, auth, system

@app.get("/api/health")
def health():
    return {"ok": True, "pid": os.getpid(), "started_at": STARTED_AT}


class Login(BaseModel):
    token: str


@app.post("/api/login")
def login(body: Login):
    if not ACCESS_TOKEN:
        return {"ok": True}
    if not hmac.compare_digest(body.token, ACCESS_TOKEN):
        raise HTTPException(401, "Wrong access token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("agent_token", ACCESS_TOKEN, httponly=True, samesite="strict", max_age=30 * 86400)
    return resp


@app.get("/api/system/info")
def system_info():
    return {
        "pid": os.getpid(), "started_at": STARTED_AT, "app_root": str(APP_ROOT), "port": PORT,
        "supervised": maintenance.under_supervisor(), "models": MODELS, "efforts": EFFORTS,
        "home": str(Path.home()), "python": sys.version.split()[0],
    }


@app.get("/api/dashboard")
def dashboard():
    by_status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    return {
        "topics": db.one("SELECT COUNT(*) n FROM topics")["n"],
        "conversations": db.one("SELECT COUNT(*) n FROM conversations WHERE kind = 'chat'")["n"],
        "projects": db.one("SELECT COUNT(*) n FROM projects")["n"],
        "tasks": by_status,
        "active_runs": runs.active_runs(),
        "recent_tasks": db.query(
            "SELECT t.id, t.title, t.status, t.progress, t.updated_at, p.name AS project, p.id AS project_id "
            "FROM tasks t JOIN projects p ON p.id = t.project_id ORDER BY t.updated_at DESC LIMIT 8"),
        "has_credentials": _has_credentials(),
    }


@app.post("/api/system/restart")
def restart(force: bool = False):
    if not maintenance.under_supervisor():
        raise HTTPException(409, "The server was not started through supervisor.py, so it cannot restart "
                                 "itself. Start it with ./start.sh.")
    if not force:
        ok, output = maintenance.preflight()
        if not ok:
            raise HTTPException(422, "Preflight failed; restart refused.\n" + output)
    threading.Timer(0.5, maintenance.request_restart).start()
    return {"ok": True, "interrupted_runs": len(runs.active_runs())}


@app.get("/api/system/logs", response_class=PlainTextResponse)
def logs(lines: int = 300):
    return maintenance.tail_log(max(1, min(lines, 5000)))


@app.get("/api/system/snapshots")
def snapshots():
    return maintenance.list_snapshots()


class SnapshotIn(BaseModel):
    label: str = "manual"


@app.post("/api/system/snapshots")
def create_snapshot(body: SnapshotIn):
    return maintenance.create_snapshot(body.label)


@app.post("/api/system/snapshots/{name}/restore")
def restore_snapshot(name: str):
    maintenance.create_snapshot("before-restore")
    try:
        maintenance.restore_snapshot(name)
    except FileNotFoundError:
        not_found("Snapshot not found")
    restarting = maintenance.under_supervisor()
    if restarting:
        threading.Timer(0.5, maintenance.request_restart).start()
    return {"ok": True, "restarting": restarting}


@app.delete("/api/system/snapshots/{name}")
def delete_snapshot(name: str):
    from .config import SNAPSHOT_DIR
    path = SNAPSHOT_DIR / Path(name).name
    if not path.is_file():
        not_found("Snapshot not found")
    path.unlink()
    return {"ok": True}


# ---------------------------------------------------------------- settings

def _has_credentials() -> bool:
    return bool(db.get_settings()["api_key"] or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _public_settings() -> dict:
    s = db.get_settings()
    key = s.pop("api_key")
    s["api_key_set"] = bool(key)
    s["api_key_hint"] = f"…{key[-4:]}" if key else ""
    s["env_key_present"] = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return s


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    clean = {}
    for key, value in body.items():
        if key == "api_key":
            if isinstance(value, str) and (value.strip() or body.get("clear_api_key")):
                clean["api_key"] = value.strip()
        elif key.endswith("_model"):
            if value not in MODEL_IDS:
                raise HTTPException(400, f"Unknown model {value}")
            clean[key] = value
        elif key.endswith("_effort"):
            if value not in EFFORTS:
                raise HTTPException(400, f"Unknown effort {value}")
            clean[key] = value
        elif key in ("use_fallback", "chat_web_search", "allow_commands"):
            clean[key] = bool(value)
        elif key == "max_agent_turns":
            clean[key] = max(1, min(int(value), 500))
    if body.get("clear_api_key"):
        clean["api_key"] = ""
    db.save_settings(clean)
    return _public_settings()


# ---------------------------------------------------------------- topics

class TopicIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    instructions: str = ""


def _topic_or_404(tid: int) -> dict:
    return db.one("SELECT * FROM topics WHERE id = ?", (tid,)) or not_found("Topic not found")


@app.get("/api/topics")
def list_topics():
    return db.query(
        "SELECT t.*, COUNT(c.id) AS conversation_count, MAX(c.updated_at) AS last_activity "
        "FROM topics t LEFT JOIN conversations c ON c.topic_id = t.id AND c.kind = 'chat' "
        "GROUP BY t.id ORDER BY COALESCE(MAX(c.updated_at), t.updated_at) DESC")


@app.post("/api/topics")
def create_topic(body: TopicIn):
    ts = db.now()
    tid = db.execute("INSERT INTO topics(title, description, instructions, created_at, updated_at) "
                     "VALUES(?, ?, ?, ?, ?)", (body.title.strip(), body.description, body.instructions, ts, ts))
    return _topic_or_404(tid)


@app.put("/api/topics/{tid}")
def update_topic(tid: int, body: TopicIn):
    _topic_or_404(tid)
    db.execute("UPDATE topics SET title = ?, description = ?, instructions = ?, updated_at = ? WHERE id = ?",
               (body.title.strip(), body.description, body.instructions, db.now(), tid))
    return _topic_or_404(tid)


@app.delete("/api/topics/{tid}")
def delete_topic(tid: int):
    _topic_or_404(tid)
    _cancel_conversations("SELECT id FROM conversations WHERE topic_id = ?", (tid,))
    db.execute("DELETE FROM topics WHERE id = ?", (tid,))
    return {"ok": True}


@app.delete("/api/topics/{tid}/conversations")
def clear_topic(tid: int):
    _topic_or_404(tid)
    _cancel_conversations("SELECT id FROM conversations WHERE topic_id = ?", (tid,))
    db.execute("DELETE FROM conversations WHERE topic_id = ?", (tid,))
    return {"ok": True}


def _cancel_conversations(sql: str, params) -> None:
    for row in db.query(sql, params):
        run = runs.active_for(row["id"])
        if run:
            run.cancel.set()


# ---------------------------------------------------------------- conversations

class ConversationIn(BaseModel):
    kind: str = "chat"
    topic_id: int | None = None
    title: str = ""


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageIn(BaseModel):
    text: str = ""


def _conv_or_404(cid: int) -> dict:
    return db.get_conversation(cid) or not_found("Conversation not found")


@app.get("/api/conversations")
def list_conversations(kind: str = "chat", topic_id: int | None = None):
    sql = ("SELECT c.id, c.kind, c.topic_id, c.task_id, c.title, c.created_at, c.updated_at, "
           "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
           "FROM conversations c WHERE c.kind = ?")
    params: list = [kind]
    if topic_id is not None:
        sql += " AND c.topic_id = ?"
        params.append(topic_id)
    rows = db.query(sql + " ORDER BY c.updated_at DESC", params)
    for r in rows:
        r["active"] = runs.active_for(r["id"]) is not None
    return rows


@app.post("/api/conversations")
def create_conversation(body: ConversationIn):
    if body.kind == "chat":
        topic = _topic_or_404(body.topic_id) if body.topic_id is not None else None
        options = {"web_search": db.get_settings()["chat_web_search"]}
        return db.create_conversation("chat", body.title.strip() or "New conversation",
                                      agents.chat_system_prompt(topic), topic_id=body.topic_id, options=options)
    if body.kind == "dev":
        return db.create_conversation("dev", body.title.strip() or "Developer session", agents.DEV_SYSTEM)
    raise HTTPException(400, "kind must be 'chat' or 'dev' (task conversations are created by running a task)")


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int):
    conv = _conv_or_404(cid)
    run = runs.active_for(cid)
    if run:
        with run.lock:  # messages and resume point must be read together
            conv["messages"] = db.list_messages(cid)
            conv["active_run"] = run.info()
    else:
        conv["messages"] = db.list_messages(cid)
        conv["active_run"] = None
    return conv


@app.patch("/api/conversations/{cid}")
def rename_conversation(cid: int, body: ConversationPatch):
    _conv_or_404(cid)
    db.execute("UPDATE conversations SET title = ? WHERE id = ?", (body.title.strip(), cid))
    return _conv_or_404(cid)


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int):
    _conv_or_404(cid)
    _cancel_conversations("SELECT ? AS id", (cid,))
    db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    return {"ok": True}


@app.post("/api/conversations/{cid}/messages")
def send_message(cid: int, body: MessageIn):
    conv = _conv_or_404(cid)
    if conv["kind"] == "task":
        return run_task(conv["task_id"], TaskRunIn(message=body.text))
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message is empty")
    if conv["kind"] == "chat":
        spec = agents.chat_spec(conv)
    else:
        spec = agents.dev_spec()
    if conv["title"] in ("New conversation", "Developer session") and not db.list_messages(cid):
        title = " ".join(text.split())[:70]
        db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, cid))
        conv["title"] = title
    try:
        run = runs.start(cid, conv["kind"], conv["title"], text, spec)
    except runs.RunBusy as e:
        raise HTTPException(409, str(e))
    return {"run": run.info()}


@app.get("/api/conversations/{cid}/export")
def export_conversation(cid: int):
    conv = _conv_or_404(cid)
    lines = [f"# {conv['title']}", "", f"_Exported from Agent Group · {db.now()}_", ""]
    for m in db.list_messages(cid):
        content = m["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        parts = []
        for b in blocks:
            if b["type"] == "text" and b.get("text", "").strip():
                parts.append(b["text"])
            elif b["type"] == "tool_use":
                parts.append(f"> 🔧 `{b['name']}` {json.dumps(b['input'], ensure_ascii=False)[:300]}")
            elif b["type"] == "tool_result":
                text = b["content"] if isinstance(b["content"], str) else json.dumps(b["content"])[:300]
                parts.append("> ↳ " + text[:300].replace("\n", " "))
        if parts:
            who = "You" if m["role"] == "user" else "Claude"
            lines += [f"## {who} · {m['created_at']}", "", "\n\n".join(parts), ""]
    name = "".join(c if c.isalnum() else "-" for c in conv["title"])[:50].strip("-") or "conversation"
    return Response("\n".join(lines), media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"'})


# ---------------------------------------------------------------- runs

@app.get("/api/runs")
def list_runs():
    return {"active": runs.active_runs(), "recent": runs.recent_runs()}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run = runs.get(run_id) or not_found("Run not found")
    run.cancel.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, after: int = 0):
    run = runs.get(run_id) or not_found("Run not found")

    async def stream():
        idx = max(0, after)
        last_sent = time.time()
        while True:
            with run.lock:
                batch = run.events[idx:]
                finished = run.done
            for ev in batch:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            idx += len(batch)
            if batch:
                last_sent = time.time()
            if (finished and not batch) or STOPPING.is_set():
                break
            if time.time() - last_sent > 15:
                yield ": keep-alive\n\n"
                last_sent = time.time()
            await asyncio.sleep(0.05)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- projects & agents

class AgentIn(BaseModel):
    name: str = Field(default="Project Agent", min_length=1, max_length=100)
    role: str = ""
    instructions: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "high"


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    repo_path: str
    create_repo: bool = False
    agent: AgentIn | None = None


class ProjectPatch(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    repo_path: str
    create_repo: bool = False


def _check_agent(a: AgentIn) -> None:
    if a.model not in MODEL_IDS:
        raise HTTPException(400, f"Unknown model {a.model}")
    if a.effort not in EFFORTS:
        raise HTTPException(400, f"Unknown effort {a.effort}")


def _check_repo(path: str, create: bool) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise HTTPException(400, "The repository path must be absolute, e.g. /home/me/my-project")
    p = Path(os.path.normpath(p))
    if p == Path("/"):
        raise HTTPException(400, "The filesystem root cannot be a project repository")
    if not p.exists():
        if not create:
            raise HTTPException(400, f"{p} does not exist. Tick 'create the folder' to create it.")
        p.mkdir(parents=True)
    if not p.is_dir():
        raise HTTPException(400, f"{p} is not a directory")
    return str(p)


def _project_or_404(pid: int) -> dict:
    return db.get_project(pid) or not_found("Project not found")


@app.get("/api/projects")
def list_projects():
    projects = db.query("SELECT * FROM projects ORDER BY updated_at DESC")
    for p in projects:
        p["agent"] = db.one("SELECT id, name, model, effort FROM agents WHERE project_id = ?", (p["id"],))
        stats = db.one("SELECT COUNT(*) total, SUM(status = 'done') done, SUM(status = 'in_progress') active, "
                       "AVG(progress) avg_progress, SUM(estimated_hours) est FROM tasks WHERE project_id = ?",
                       (p["id"],))
        p["stats"] = {k: (v or 0) for k, v in stats.items()}
        p["running"] = sum(1 for r in runs.active_runs() if r["kind"] == "task" and r["task_id"] in
                           {t["id"] for t in db.query("SELECT id FROM tasks WHERE project_id = ?", (p["id"],))})
        p["repo_exists"] = Path(p["repo_path"]).is_dir()
    return projects


@app.post("/api/projects")
def create_project(body: ProjectIn):
    agent = body.agent or AgentIn()
    _check_agent(agent)
    repo = _check_repo(body.repo_path, body.create_repo)
    ts = db.now()
    with db.transaction():
        pid = db._conn.execute(
            "INSERT INTO projects(name, description, repo_path, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (body.name.strip(), body.description, repo, ts, ts)).lastrowid
        db._conn.execute(
            "INSERT INTO agents(project_id, name, role, instructions, model, effort, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, agent.name.strip(), agent.role, agent.instructions, agent.model, agent.effort, ts, ts))
    return _project_or_404(pid)


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    p = _project_or_404(pid)
    p["repo_exists"] = Path(p["repo_path"]).is_dir()
    return p


@app.put("/api/projects/{pid}")
def update_project(pid: int, body: ProjectPatch):
    _project_or_404(pid)
    repo = _check_repo(body.repo_path, body.create_repo)
    db.execute("UPDATE projects SET name = ?, description = ?, repo_path = ?, updated_at = ? WHERE id = ?",
               (body.name.strip(), body.description, repo, db.now(), pid))
    return _project_or_404(pid)


@app.put("/api/projects/{pid}/agent")
def update_agent(pid: int, body: AgentIn):
    _project_or_404(pid)
    _check_agent(body)
    db.execute("UPDATE agents SET name = ?, role = ?, instructions = ?, model = ?, effort = ?, updated_at = ? "
               "WHERE project_id = ?",
               (body.name.strip(), body.role, body.instructions, body.model, body.effort, db.now(), pid))
    return _project_or_404(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    """Unregister the project. Files in its repository are never touched."""
    _project_or_404(pid)
    _cancel_conversations("SELECT c.id FROM conversations c JOIN tasks t ON t.id = c.task_id "
                          "WHERE t.project_id = ?", (pid,))
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------- tasks

class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    estimated_hours: float | None = None
    next_actions: list[str] = []


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    progress: int | None = None
    estimated_hours: float | None = None
    clear_estimate: bool = False
    summary: str | None = None
    insights: list[dict] | None = None
    next_actions: list[dict] | None = None


class TaskRunIn(BaseModel):
    message: str = ""


TASK_STATUSES = {"todo", "in_progress", "blocked", "review", "done"}


def _task_or_404(tid: int) -> dict:
    return db.get_task(tid) or not_found("Task not found")


def _task_conversation(tid: int) -> dict | None:
    row = db.one("SELECT id FROM conversations WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,))
    return db.get_conversation(row["id"]) if row else None


def _with_runtime(task: dict) -> dict:
    conv = _task_conversation(task["id"])
    task["conversation_id"] = conv["id"] if conv else None
    run = runs.active_for(conv["id"]) if conv else None
    task["running"] = run is not None
    task["run_id"] = run.id if run else None
    return task


@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: int):
    _project_or_404(pid)
    return [_with_runtime(t) for t in db.list_tasks(pid)]


@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, body: TaskIn):
    _project_or_404(pid)
    ts = db.now()
    actions = json.dumps([{"text": a, "done": False} for a in body.next_actions if a.strip()])
    tid = db.execute("INSERT INTO tasks(project_id, title, description, estimated_hours, next_actions, "
                     "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                     (pid, body.title.strip(), body.description, body.estimated_hours, actions, ts, ts))
    db.add_task_event(tid, "user", "Task created and assigned to the project agent")
    return _with_runtime(db.get_task(tid))


@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    task = _with_runtime(_task_or_404(tid))
    task["events"] = db.query("SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 200", (tid,))
    return task


@app.put("/api/tasks/{tid}")
def update_task(tid: int, body: TaskPatch):
    task = _task_or_404(tid)
    fields = body.model_dump(exclude_none=True, exclude={"clear_estimate"})
    if "status" in fields and fields["status"] not in TASK_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(TASK_STATUSES)}")
    if "progress" in fields:
        fields["progress"] = max(0, min(100, fields["progress"]))
    if body.clear_estimate:
        fields["estimated_hours"] = None
    if "insights" in fields:
        fields["insights"] = [{"text": str(i.get("text", "")), "at": i.get("at") or db.now(),
                               "source": i.get("source", "user")} for i in fields["insights"] if i.get("text")]
    if "next_actions" in fields:
        fields["next_actions"] = [{"text": str(a.get("text", "")), "done": bool(a.get("done"))}
                                  for a in fields["next_actions"] if a.get("text")]
    updated = db.update_task(tid, fields)
    changed = [k for k in fields if task.get(k) != updated.get(k)]
    if changed:
        db.add_task_event(tid, "user", "Edited: " + ", ".join(changed))
    return _with_runtime(updated)


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int):
    _task_or_404(tid)
    _cancel_conversations("SELECT id FROM conversations WHERE task_id = ?", (tid,))
    db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    return {"ok": True}


@app.post("/api/tasks/{tid}/run")
def run_task(tid: int, body: TaskRunIn):
    task = _task_or_404(tid)
    project = _project_or_404(task["project_id"])
    if not Path(project["repo_path"]).is_dir():
        raise HTTPException(400, f"The project repository {project['repo_path']} does not exist")
    conv = _task_conversation(tid)
    if conv is None:
        conv = db.create_conversation("task", f"Task #{tid}: {task['title']}",
                                      agents.task_system_prompt(project, project["agent"]), task_id=tid)
        content = agents.task_kickoff_message(task, body.message)
    else:
        content = agents.task_followup_message(task, body.message, conv, project)
    if task["status"] == "todo":
        db.update_task(tid, {"status": "in_progress"})
    try:
        run = runs.start(conv["id"], "task", conv["title"], content, agents.task_spec(project, tid), task_id=tid)
    except runs.RunBusy as e:
        raise HTTPException(409, str(e))
    db.add_task_event(tid, "user", "Agent run started" + (f": {body.message.strip()[:200]}" if body.message.strip() else ""))
    return {"run": run.info(), "conversation_id": conv["id"]}


# ---------------------------------------------------------------- file system

def _root(root: str | None) -> Path:
    r = Path(root or "/").expanduser()
    if not r.is_absolute() or not r.is_dir():
        raise HTTPException(400, f"Invalid root directory: {root}")
    return r.resolve()


@app.get("/api/fs/list")
def fs_list(path: str = "/", root: str | None = None, hidden: bool = True):
    r = _root(root)
    p = fsops.confine(r, path)
    parent = None if p.resolve() == r else str(p.parent)
    return {"path": str(p), "root": str(r), "parent": parent, "entries": fsops.list_dir(p, hidden)}


@app.get("/api/fs/read")
def fs_read(path: str, root: str | None = None):
    return fsops.read_file(fsops.confine(_root(root), path))


@app.get("/api/fs/raw")
def fs_raw(path: str, root: str | None = None, download: bool = False):
    p = fsops.confine(_root(root), path)
    if not p.is_file():
        not_found("File not found")
    return FileResponse(p, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


class FsWrite(BaseModel):
    path: str
    content: str = ""
    root: str | None = None
    create_only: bool = False


class FsPath(BaseModel):
    path: str
    root: str | None = None


class FsMove(BaseModel):
    source: str
    destination: str
    root: str | None = None
    overwrite: bool = False


@app.post("/api/fs/write")
def fs_write(body: FsWrite):
    return fsops.write_file(fsops.confine(_root(body.root), body.path), body.content, body.create_only)


@app.post("/api/fs/mkdir")
def fs_mkdir(body: FsPath):
    return fsops.make_dir(fsops.confine(_root(body.root), body.path))


@app.post("/api/fs/delete")
def fs_delete(body: FsPath):
    r = _root(body.root)
    fsops.delete(fsops.confine(r, body.path), r)
    return {"ok": True}


@app.post("/api/fs/move")
def fs_move(body: FsMove):
    r = _root(body.root)
    return fsops.move(fsops.confine(r, body.source), fsops.confine(r, body.destination), r, body.overwrite)


# ---------------------------------------------------------------- static frontend

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ---------------------------------------------------------------- entry point

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not _is_loopback(HOST) and not ACCESS_TOKEN:
        sys.exit(f"Refusing to listen on {HOST} without AGENT_TOKEN: this server can read, write and "
                 "execute anything the user running it can. Set AGENT_TOKEN or use AGENT_HOST=127.0.0.1.")
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="info",
                                           timeout_graceful_shutdown=3))

    def stop():
        STOPPING.set()
        server.should_exit = True

    maintenance.on_restart(stop)
    log.info("Agent Group listening on http://%s:%s (supervised=%s)", HOST, PORT, maintenance.under_supervisor())
    server.run()
    runs.cancel_all()
    if maintenance.restart_requested():
        log.info("Exiting for restart")
        sys.exit(RESTART_EXIT_CODE)
    if not server.started:
        sys.exit(1)


if __name__ == "__main__":
    main()
