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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents, db, devteam, fsops, groups, images, maintenance, project_setup, runs, team, templates, usage
from .config import (ACCESS_TOKEN, APP_ROOT, EFFORTS, HOST, MODELS, PORT, RESTART_EXIT_CODE,
                     STATIC_DIR)

log = logging.getLogger("agent_group")
STARTED_AT = time.time()
STOPPING = threading.Event()
MODEL_IDS = {m["id"] for m in MODELS}

app = FastAPI(title="Agent Group", docs_url="/api/docs", openapi_url="/api/openapi.json")
# Conversations with tool output run to hundreds of KB of JSON. Event streams are left uncompressed.
app.add_middleware(GZipMiddleware, minimum_size=2048)


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


@app.exception_handler(images.ImageError)
async def image_error(_: Request, exc: images.ImageError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(templates.TemplateError)
async def template_error(_: Request, exc: templates.TemplateError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(groups.GroupError)
async def group_error(_: Request, exc: groups.GroupError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(team.TeamError)
async def team_error(_: Request, exc: team.TeamError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(project_setup.SetupError)
async def setup_error(_: Request, exc: project_setup.SetupError):
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
        "home": str(Path.home()), "python": sys.version.split()[0], "engine": db.get_settings()["engine"],
        "dev_project_id": devteam.project_id(),
    }


@app.get("/api/dashboard")
def dashboard():
    by_status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    return {
        "topics": db.one("SELECT COUNT(*) n FROM topics")["n"],
        "conversations": db.one("SELECT COUNT(*) n FROM conversations WHERE kind = 'chat'")["n"],
        "projects": db.one("SELECT COUNT(*) n FROM projects")["n"],
        "tasks": by_status,
        "active_runs": runs.active_runs(activity=True),
        "recent_tasks": db.query(
            "SELECT t.id, t.title, t.status, t.progress, t.updated_at, p.name AS project, p.id AS project_id "
            "FROM tasks t JOIN projects p ON p.id = t.project_id ORDER BY t.updated_at DESC LIMIT 8"),
        "has_credentials": _has_credentials(),
        "questions": list_questions("open")[:10],
        "engine": db.get_settings()["engine"],
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


@app.get("/api/usage")
def plan_usage(model: str | None = None, scope: str | None = None, project_id: int | None = None,
               refresh: bool = False):
    """Claude plan limits and the tokens spent on `model` (default: the model of the agent behind `scope`)."""
    if model and model not in MODEL_IDS:
        raise HTTPException(400, f"Unknown model {model}")
    return usage.report(model or usage.resolve_model(scope, project_id), force=refresh)


# ---------------------------------------------------------------- settings

def _claude_code_status() -> dict:
    from . import claude_code  # imports the Agent SDK; only needed here and when a run starts
    return claude_code.status()


def _has_credentials() -> bool:
    s = db.get_settings()
    if s["engine"] == "claude_code":
        status = _claude_code_status()
        return status["available"] and status["logged_in"]
    return bool(s["api_key"] or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _public_settings() -> dict:
    s = db.get_settings()
    key = s.pop("api_key")
    s["api_key_set"] = bool(key)
    s["api_key_hint"] = f"…{key[-4:]}" if key else ""
    s["env_key_present"] = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    s["claude_code"] = _claude_code_status()
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
        elif key == "engine":
            if value not in ("claude_code", "api"):
                raise HTTPException(400, "engine must be 'claude_code' or 'api'")
            clean[key] = value
        elif key in ("use_fallback", "chat_web_search", "allow_commands"):
            clean[key] = bool(value)
        elif key == "max_agent_turns":
            clean[key] = max(1, min(int(value), 500))
        elif key == "max_parallel_agents":
            clean[key] = max(1, min(int(value), 10))
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
    images: list[str] = []   # base64 image data, optionally as data: URLs


def _user_content(cid: int, text: str, encoded: list[str]):
    """The stored user message: plain text, or the attached images followed by the text."""
    if not encoded:
        return text
    return images.save(cid, encoded) + ([{"type": "text", "text": text}] if text.strip() else [])


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
    images.remove(cid)
    return {"ok": True}


@app.get("/api/uploads/{cid}/{name}")
def get_upload(cid: int, name: str):
    p = images.path(f"{cid}/{name}") or not_found("Image not found")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.post("/api/conversations/{cid}/messages")
def send_message(cid: int, body: MessageIn):
    conv = _conv_or_404(cid)
    if conv["kind"] == "task":
        return run_task(conv["task_id"], TaskRunIn(message=body.text, images=body.images))
    if conv["kind"] == "setup":
        return run_setup(conv["options"]["project_id"], SetupIn(message=body.text))
    text = body.text.strip()
    if not text and not body.images:
        raise HTTPException(400, "Message is empty")
    if conv["kind"] == "chat":
        spec = agents.chat_spec(conv)
    else:
        spec = agents.dev_spec()
    if conv["title"] in ("New conversation", "Developer session") and not db.list_messages(cid):
        title = " ".join(text.split())[:70] or "Image"
        db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, cid))
        conv["title"] = title
    try:
        run = runs.start(cid, conv["kind"], conv["title"], _user_content(cid, text, body.images), spec)
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
            elif b["type"] == "image":
                parts.append("[image]")
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
    return {"active": runs.active_runs(), "recent": runs.recent_runs(),
            "open_questions": db.one("SELECT COUNT(*) n FROM questions WHERE status = 'open'")["n"]}


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
    template: str = ""            # symbol of the agent template it is based on, e.g. "@code-reviewer"
    subagents: list[str] = []     # symbols of the templates that become its internal agents
    parent_id: int | None = None  # the agent it reports to (team members); ignored for the lead


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    repo_path: str
    create_repo: bool = False
    group: str = ""               # symbol of an agent group: the project gets that whole team
    agent: AgentIn | None = None  # without a group: the single agent (with a group: default model and effort)
    setup: bool = True            # start the setup agent: analysis, team, task plan (app/project_setup.py)


class ProjectPatch(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    repo_path: str
    create_repo: bool = False


class ProjectRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TeamIn(BaseModel):
    group: str
    model: str = ""
    effort: str = ""


def _check_agent(a: AgentIn) -> None:
    if a.model not in MODEL_IDS:
        raise HTTPException(400, f"Unknown model {a.model}")
    if a.effort not in EFFORTS:
        raise HTTPException(400, f"Unknown effort {a.effort}")
    a.template = templates.parse_symbol(a.template)
    if a.template and not templates.by_slug(a.template):
        raise HTTPException(400, f"Unknown agent template {templates.symbol(a.template)}")
    a.subagents = templates.validate_refs(a.subagents)


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


def _project_name(name: str) -> str:
    if not name.strip():
        raise HTTPException(400, "The project name cannot be empty")
    return name.strip()


def _agent_or_404(aid: int) -> dict:
    return db.get_agent(aid) or not_found("Agent not found")


def _unique_key(pid: int, name: str, exclude: int | None = None) -> str:
    return team.unique_key(pid, name, exclude)


def _group_or_400(symbol: str) -> dict:
    group = groups.by_slug(symbol)
    if not group:
        raise HTTPException(400, f"Unknown agent group {groups.symbol(groups.parse_symbol(symbol))}")
    return group


def _running_task_ids() -> set[int]:
    return {r["task_id"] for r in runs.active_runs() if r["kind"] == "task" and r["task_id"]}


@app.get("/api/projects")
def list_projects():
    # The Developer project (app/devteam.py) first, then the user's projects
    projects = db.query("SELECT * FROM projects ORDER BY system = ? DESC, updated_at DESC", (devteam.SYSTEM,))
    running = _running_task_ids()
    for p in projects:
        agents_ = db.list_agents(p["id"])
        p["agent"] = agents_[0] if agents_ else None
        p["agent_count"] = len(agents_)
        stats = db.one("SELECT COUNT(*) total, SUM(status = 'done') done, SUM(status = 'in_progress') active, "
                       "AVG(progress) avg_progress, SUM(estimated_hours) est FROM tasks "
                       "WHERE project_id = ? AND parent_id IS NULL", (p["id"],))
        p["stats"] = {k: (v or 0) for k, v in stats.items()}
        p["running"] = sum(1 for t in db.query("SELECT id FROM tasks WHERE project_id = ?", (p["id"],))
                           if t["id"] in running)
        p["open_questions"] = db.one("SELECT COUNT(*) n FROM questions q JOIN tasks t ON t.id = q.task_id "
                                     "WHERE t.project_id = ? AND q.status = 'open'", (p["id"],))["n"]
        p["repo_exists"] = Path(p["repo_path"]).is_dir()
        p["setup"] = project_setup.status(p["id"])
    return projects


@app.post("/api/projects")
def create_project(body: ProjectIn):
    agent = body.agent or AgentIn(model=db.get_settings()["project_model"], effort=db.get_settings()["project_effort"])
    _check_agent(agent)
    group = _group_or_400(body.group) if body.group.strip() else None
    repo = _check_repo(body.repo_path, body.create_repo)
    ts = db.now()
    with db.transaction():
        pid = db._conn.execute(
            "INSERT INTO projects(name, description, repo_path, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (body.name.strip(), body.description, repo, ts, ts)).lastrowid
        if group:
            groups.insert_team(pid, group, agent.model, agent.effort, ts)
        else:
            db._conn.execute(
                "INSERT INTO agents(project_id, key, name, role, instructions, model, effort, template, subagents, "
                "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, groups.slugify(agent.name) or "lead", agent.name.strip(), agent.role, agent.instructions,
                 agent.model, agent.effort, agent.template, json.dumps(agent.subagents), ts, ts))
    if body.setup:
        project_setup.start(pid)
    return get_project(pid)


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    p = _project_or_404(pid)
    p["repo_exists"] = Path(p["repo_path"]).is_dir()
    p["setup"] = project_setup.status(pid)
    return p


class SetupIn(BaseModel):
    message: str = ""


@app.post("/api/projects/{pid}/setup")
def run_setup(pid: int, body: SetupIn):
    """Start the setup agent (analysis, team, task plan), or send it the next message."""
    _project_or_404(pid)
    try:
        run, conv = project_setup.start(pid, body.message)
    except runs.RunBusy as e:
        raise HTTPException(409, str(e))
    return {"run": run.info(), "conversation_id": conv["id"]}


@app.put("/api/projects/{pid}")
def update_project(pid: int, body: ProjectPatch):
    p = _project_or_404(pid)
    name = _project_name(body.name)
    repo = str(APP_ROOT) if devteam.is_dev(p) else _check_repo(body.repo_path, body.create_repo)
    db.execute("UPDATE projects SET name = ?, description = ?, repo_path = ?, updated_at = ? WHERE id = ?",
               (name, body.description, repo, db.now(), pid))
    return _project_or_404(pid)


@app.patch("/api/projects/{pid}")
def rename_project(pid: int, body: ProjectRename):
    """Rename only; unlike PUT it leaves the repository alone, so it works when the folder is missing."""
    _project_or_404(pid)
    db.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (_project_name(body.name), db.now(), pid))
    return get_project(pid)


def _save_agent(aid: int, body: AgentIn) -> None:
    agent = _agent_or_404(aid)
    _check_agent(body)
    team = db.list_agents(agent["project_id"])
    parent = agent["parent_id"]
    if team and team[0]["id"] != aid and body.parent_id:
        if body.parent_id == aid or body.parent_id in {a["id"] for a in team_reports(team, aid)}:
            raise HTTPException(400, "An agent cannot report to itself or to someone who reports to it")
        if body.parent_id not in {a["id"] for a in team}:
            raise HTTPException(400, "The agent it reports to is not in this project")
        parent = body.parent_id
    db.execute("UPDATE agents SET name = ?, role = ?, instructions = ?, model = ?, effort = ?, template = ?, "
               "subagents = ?, parent_id = ?, key = ?, updated_at = ? WHERE id = ?",
               (body.name.strip(), body.role, body.instructions, body.model, body.effort, body.template,
                json.dumps(body.subagents), parent, _unique_key(agent["project_id"], body.name, aid), db.now(), aid))


def team_reports(team: list[dict], aid: int) -> list[dict]:
    from .team import reports_of
    return reports_of(team, aid)


@app.put("/api/projects/{pid}/agent")
def update_lead(pid: int, body: AgentIn):
    """Update the project's lead agent."""
    p = _project_or_404(pid)
    _save_agent(p["agent"]["id"], body)
    return _project_or_404(pid)


@app.post("/api/projects/{pid}/agents")
def add_agent(pid: int, body: AgentIn):
    p = _project_or_404(pid)
    _check_agent(body)
    parent = body.parent_id if body.parent_id in {a["id"] for a in p["agents"]} else p["agent"]["id"]
    ts = db.now()
    db.execute("INSERT INTO agents(project_id, parent_id, key, sort, name, role, instructions, model, effort, template, "
               "subagents, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (pid, parent, _unique_key(pid, body.name), len(p["agents"]), body.name.strip(), body.role,
                body.instructions, body.model, body.effort, body.template, json.dumps(body.subagents), ts, ts))
    return _project_or_404(pid)


@app.put("/api/agents/{aid}")
def update_agent(aid: int, body: AgentIn):
    _save_agent(aid, body)
    return _project_or_404(_agent_or_404(aid)["project_id"])


@app.delete("/api/agents/{aid}")
def delete_agent(aid: int):
    """Remove a team member. Its reports and its tasks move to the agent it reported to."""
    agent = _agent_or_404(aid)
    team = db.list_agents(agent["project_id"])
    if team[0]["id"] == aid:
        raise HTTPException(400, "The lead cannot be removed. Apply another group or change the lead's settings.")
    with db.transaction():
        db._conn.execute("UPDATE agents SET parent_id = ? WHERE parent_id = ?", (agent["parent_id"], aid))
        db._conn.execute("UPDATE tasks SET assignee_id = ? WHERE assignee_id = ?", (agent["parent_id"], aid))
        db._conn.execute("DELETE FROM learnings WHERE scope = 'self' AND agent_id = ?", (aid,))
        db._conn.execute("DELETE FROM agents WHERE id = ?", (aid,))
    return _project_or_404(agent["project_id"])


@app.put("/api/projects/{pid}/team")
def apply_group(pid: int, body: TeamIn):
    """Replace the project's team with an agent group. Tasks move to the new lead."""
    p = _project_or_404(pid)
    group = _group_or_400(body.group)
    if any(t["id"] in _running_task_ids() for t in db.query("SELECT id FROM tasks WHERE project_id = ?", (pid,))):
        raise HTTPException(409, "Agents are working on this project. Wait for them or stop them first.")
    s = db.get_settings()
    model = body.model if body.model in MODEL_IDS else p["agent"]["model"] if p["agent"] else s["project_model"]
    effort = body.effort if body.effort in EFFORTS else p["agent"]["effort"] if p["agent"] else s["project_effort"]
    groups.apply_to_project(pid, group, model, effort)
    return _project_or_404(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    """Unregister the project. Files in its repository are never touched. The Developer project stays."""
    if devteam.is_dev(_project_or_404(pid)):
        raise HTTPException(403, "The Developer project is built in and cannot be deleted.")
    _cancel_conversations("SELECT c.id FROM conversations c JOIN tasks t ON t.id = c.task_id "
                          "WHERE t.project_id = ?", (pid,))
    setup = "SELECT id FROM conversations WHERE kind = 'setup' AND json_extract(options, '$.project_id') = ?"
    _cancel_conversations(setup, (pid,))
    db.execute(f"DELETE FROM conversations WHERE id IN ({setup})", (pid,))
    db.execute("DELETE FROM learnings WHERE project_id = ? AND scope != 'role'", (pid,))
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------- the Developer project

def _ensure_devteam() -> int:
    s = db.get_settings()
    return devteam.ensure_project(str(APP_ROOT), s["dev_model"], s["dev_effort"])


@app.get("/api/devteam")
def get_devteam():
    """The Developer project (created if it is missing) and the group its team is built from."""
    _ensure_devteam()
    p = devteam.project()
    p["repo_exists"] = True
    p["setup"] = project_setup.status(p["id"])
    return {"project": p, "default_group": devteam.DEFAULT_GROUP}


# ---------------------------------------------------------------- learnings

class LearningIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    scope: str = "team"           # self (one agent: agent_id), team, role (the agent's template, all projects)
    agent_id: int | None = None


@app.get("/api/projects/{pid}/learnings")
def project_learnings(pid: int):
    """Lessons of this project's agents and team, and the role lessons of the templates its agents use."""
    p = _project_or_404(pid)
    used = sorted({a["template"] for a in p["agents"] if a["template"]})
    marks = ",".join("?" * len(used)) or "''"
    return db.query(f"SELECT * FROM learnings WHERE (project_id = ? AND scope != 'role') "
                    f"OR (scope = 'role' AND template IN ({marks})) ORDER BY id DESC", (pid, *used))


@app.post("/api/projects/{pid}/learnings")
def add_learning(pid: int, body: LearningIn):
    p = _project_or_404(pid)
    agent = next((a for a in p["agents"] if a["id"] == body.agent_id), None)
    if body.scope not in ("self", "team", "role"):
        raise HTTPException(400, "scope must be self, team or role")
    if body.scope in ("self", "role") and not agent:
        raise HTTPException(400, "Choose the agent this lesson is for")
    if body.scope == "role" and not agent["template"]:
        raise HTTPException(400, f"{agent['name']} is not based on a template")
    db.execute("INSERT INTO learnings(project_id, agent_id, template, scope, text, task_id, author, created_at) "
               "VALUES(?, ?, ?, ?, ?, NULL, 'user', ?)",
               (pid, agent["id"] if agent else None, agent["template"] if agent else "", body.scope,
                " ".join(body.text.split()), db.now()))
    return project_learnings(pid)


@app.delete("/api/learnings/{lid}")
def delete_learning(lid: int):
    db.one("SELECT id FROM learnings WHERE id = ?", (lid,)) or not_found("Lesson not found")
    db.execute("DELETE FROM learnings WHERE id = ?", (lid,))
    return {"ok": True}


# ---------------------------------------------------------------- agent templates

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = ""                # the symbol, with or without "@"; derived from the name when empty
    category: str = ""
    area: str = ""                # functional area, e.g. "developer" (see template_presets.AREAS)
    language: str = ""            # programming language, e.g. "python" (see template_presets.LANGUAGES)
    description: str = ""
    role: str = ""
    instructions: str = ""
    rules: list[str] = []
    expertise: list[str] = []
    skills: list[str] = []
    subagents: list[str] = []
    model: str = ""               # "" = the model of the agent that uses it
    effort: str = ""


def _template_or_404(tid: int) -> dict:
    return templates.get(tid) or not_found("Template not found")


def _check_template(body: TemplateIn) -> dict:
    if body.model and body.model not in MODEL_IDS:
        raise HTTPException(400, f"Unknown model {body.model}")
    if body.effort and body.effort not in EFFORTS:
        raise HTTPException(400, f"Unknown effort {body.effort}")
    return body.model_dump()


@app.get("/api/templates")
def list_templates():
    return templates.list_all()


class TemplateDraft(BaseModel):
    """A template being edited, as far as it is filled in."""
    slug: str = ""
    category: str = ""
    area: str = ""
    language: str = ""
    name: str = ""
    description: str = ""
    role: str = ""
    expertise: list[str] = []
    skills: list[str] = []
    rules: list[str] = []
    subagents: list[str] = []


@app.post("/api/templates/suggest")
def suggest_template_values(body: TemplateDraft):
    """Candidates for each field of the template editor, depending on the fields before it."""
    return templates.suggest(body.model_dump())


@app.get("/api/templates/presets")
def template_presets():
    """The starter template library: its categories and which of its templates are installed."""
    return {"categories": templates.presets.CATEGORIES, "areas": templates.presets.AREAS,
            "languages": [{"id": l["id"], "name": l["name"]} for l in templates.presets.LANGUAGES],
            "presets": templates.preset_status()}


class PresetInstall(BaseModel):
    slugs: list[str] | None = None   # None: every missing starter template


@app.post("/api/templates/presets/install")
def install_template_presets(body: PresetInstall):
    return {"installed": templates.install_presets(body.slugs)}


@app.post("/api/templates")
def create_template(body: TemplateIn):
    return templates.save(_check_template(body))


@app.get("/api/templates/{tid}")
def get_template(tid: int):
    t = _template_or_404(tid)
    t["used_by"] = templates.usages().get(t["slug"], [])
    return t


@app.put("/api/templates/{tid}")
def update_template(tid: int, body: TemplateIn):
    _template_or_404(tid)
    return templates.save(_check_template(body), tid)


@app.delete("/api/templates/{tid}")
def delete_template(tid: int):
    _template_or_404(tid)
    templates.delete(tid)
    return {"ok": True}


# ---------------------------------------------------------------- agent groups

class GroupMemberIn(BaseModel):
    key: str = ""
    template: str
    name: str = ""
    parent: str | None = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = ""
    category: str = ""
    description: str = ""
    members: list[GroupMemberIn] = []


def _group_or_404(gid: int) -> dict:
    return groups.get(gid) or not_found("Group not found")


@app.get("/api/groups")
def list_groups():
    return groups.list_all()


@app.get("/api/groups/presets")
def group_presets():
    return groups.preset_status()


@app.post("/api/groups/presets/install")
def install_group_presets(body: PresetInstall):
    return {"installed": groups.install_presets(body.slugs)}


@app.post("/api/groups")
def create_group(body: GroupIn):
    return groups.save(body.model_dump())


@app.get("/api/groups/{gid}")
def get_group(gid: int):
    return _group_or_404(gid)


@app.put("/api/groups/{gid}")
def update_group(gid: int, body: GroupIn):
    _group_or_404(gid)
    return groups.save(body.model_dump(), gid)


@app.delete("/api/groups/{gid}")
def delete_group(gid: int):
    _group_or_404(gid)
    groups.delete(gid)
    return {"ok": True}


# ---------------------------------------------------------------- tasks

class TaskIn(BaseModel):
    title: str = Field(default="", max_length=300)    # default: derived from the description
    description: str = ""
    estimated_hours: float | None = None
    next_actions: list[str] = []
    assignee_id: int | None = None    # default: the project's lead
    images: list[str] = []            # base64 image data of the description, optionally as data: URLs


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
    assignee_id: int | None = None
    add_images: list[str] | None = None      # base64 image data, optionally as data: URLs
    remove_images: list[str] | None = None   # names of stored images


class TaskRunIn(BaseModel):
    message: str = ""
    images: list[str] = []


TASK_STATUSES = {"todo", "in_progress", "blocked", "review", "done", "cancelled"}


def _task_or_404(tid: int) -> dict:
    return db.get_task(tid) or not_found("Task not found")


def _subtree(tid: int) -> list[int]:
    return [r["id"] for r in db.query("WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT t.id FROM tasks t "
                                      "JOIN sub ON t.parent_id = sub.id) SELECT id FROM sub", (tid,))]


def _running_by_agent(running: set[int]) -> dict:
    """Running task ids by assignee id, ascending: the parallel instances of each agent."""
    out: dict = {}
    if running:
        marks = ",".join("?" * len(running))
        for r in db.query(f"SELECT id, assignee_id FROM tasks WHERE id IN ({marks}) ORDER BY id", tuple(running)):
            out.setdefault(r["assignee_id"], []).append(r["id"])
    return out


def _with_runtime(task: dict, running: set[int] | None = None, names: dict | None = None,
                  peers: dict | None = None) -> dict:
    running = _running_task_ids() if running is None else running
    conv = team.task_conversation(task["id"])
    task["conversation_id"] = conv["id"] if conv else None
    run = runs.active_for(conv["id"]) if conv else None
    task["running"] = run is not None
    task["run_id"] = run.id if run else None
    tree = _subtree(task["id"])
    task["team_running"] = sum(1 for t in tree[1:] if t in running)
    counts = db.one("SELECT COUNT(*) n, SUM(status = 'done') done FROM tasks WHERE parent_id = ?", (task["id"],))
    task["subtask_count"], task["subtasks_done"] = counts["n"], counts["done"] or 0
    marks = ",".join("?" * len(tree))
    task["open_questions"] = db.one(f"SELECT COUNT(*) n FROM questions WHERE status = 'open' AND task_id IN ({marks})",
                                    tree)["n"]
    if names is None:
        names = {a["id"]: a for a in db.query("SELECT id, name, key FROM agents WHERE project_id = ?",
                                              (task["project_id"],))}
    who = names.get(task["assignee_id"])
    task["assignee"] = {"id": who["id"], "name": who["name"], "key": who["key"]} if who else None
    # Several tasks of one agent run at once as separate instances of it
    mine = (_running_by_agent(running) if peers is None else peers).get(task["assignee_id"], []) \
        if task["id"] in running else []
    task["instances"] = len(mine) if task["id"] in mine else 0
    task["instance"] = mine.index(task["id"]) + 1 if task["id"] in mine else None
    return task


@app.get("/api/projects/{pid}/tasks")
def list_tasks(pid: int, all: bool = False):
    """The project's tasks: the ones the user created (top level), or with all=1 every sub-task too."""
    _project_or_404(pid)
    running = _running_task_ids()
    names = {a["id"]: a for a in db.query("SELECT id, name, key FROM agents WHERE project_id = ?", (pid,))}
    peers = _running_by_agent(running)
    return [_with_runtime(t, running, names, peers) for t in db.list_tasks(pid) if all or not t["parent_id"]]


def _check_assignee(pid: int, aid: int | None) -> int:
    p = _project_or_404(pid)
    if aid is None:
        return p["agent"]["id"]
    if aid not in {a["id"] for a in p["agents"]}:
        raise HTTPException(400, "The assignee is not an agent of this project")
    return aid


@app.post("/api/projects/{pid}/tasks")
def create_task(pid: int, body: TaskIn):
    assignee_id = _check_assignee(pid, body.assignee_id)
    title = body.title.strip() or team.title_from(body.description)
    if not title:
        raise HTTPException(400, "A title or a description is required")
    files = images.task_check([], body.images)
    ts = db.now()
    actions = json.dumps([{"text": a, "done": False} for a in body.next_actions if a.strip()])
    tid = db.execute("INSERT INTO tasks(project_id, assignee_id, title, description, estimated_hours, next_actions, "
                     "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                     (pid, assignee_id, title, body.description, body.estimated_hours, actions, ts, ts))
    if files:
        db.update_task(tid, {"images": images.task_save(tid, files)})
    name = db.get_agent(assignee_id)["name"]
    db.add_task_event(tid, "user", f"Task created and assigned to {name}")
    return _with_runtime(db.get_task(tid))


@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    task = _task_or_404(tid)
    running = _running_task_ids()
    names = {a["id"]: a for a in db.query("SELECT id, name, key FROM agents WHERE project_id = ?", (task["project_id"],))}
    peers = _running_by_agent(running)
    task = _with_runtime(task, running, names, peers)
    task["events"] = db.query("SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 200", (tid,))
    task["subtasks"] = [_with_runtime(db.get_task(r["id"]), running, names, peers)
                        for r in db.query("SELECT id FROM tasks WHERE parent_id = ? ORDER BY id", (tid,))]
    parent = db.get_task(task["parent_id"]) if task["parent_id"] else None
    task["parent"] = {"id": parent["id"], "title": parent["title"]} if parent else None
    tree = _subtree(tid)
    marks = ",".join("?" * len(tree))
    task["questions"] = db.query(f"SELECT * FROM questions WHERE task_id IN ({marks}) ORDER BY status = 'open' DESC, "
                                 f"id DESC LIMIT 50", tree)
    for q in task["questions"]:
        q["options"] = json.loads(q["options"])
    task["root_id"] = team.root_of(task)["id"]
    task["discussion_count"] = db.one("SELECT COUNT(*) n FROM discussions WHERE root_task_id = ?",
                                      (task["root_id"],))["n"]
    return task


def _entries(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["images"] = json.loads(r["images"])
    return rows


@app.get("/api/tasks/{tid}/discussion")
def task_discussion(tid: int):
    """The team discussion of the task tree the task belongs to."""
    root = team.root_of(_task_or_404(tid))
    return _entries(db.query("SELECT d.*, t.title AS task_title FROM discussions d LEFT JOIN tasks t "
                             "ON t.id = d.task_id WHERE d.root_task_id = ? ORDER BY d.id", (root["id"],)))


ENTRY_SQL = ("SELECT d.*, r.title AS root_title, t.title AS task_title FROM discussions d "
             "JOIN tasks r ON r.id = d.root_task_id LEFT JOIN tasks t ON t.id = d.task_id ")


@app.get("/api/projects/{pid}/discussion")
def project_discussion(pid: int, root_id: int | None = None, limit: int = 200):
    """The team discussion of all the project's tasks (or of one root task): the newest `limit` entries, oldest
    first, and every open root task as a thread with its entry count and latest activity."""
    _project_or_404(pid)
    sql, params = ENTRY_SQL + "WHERE r.project_id = ?", [pid]
    if root_id is not None:
        sql += " AND d.root_task_id = ?"
        params.append(root_id)
    rows = db.query(sql + " ORDER BY d.id DESC LIMIT ?", (*params, max(1, min(limit, 500))))
    threads = db.query("SELECT t.id, t.title, t.status, COUNT(d.id) AS count, MAX(d.created_at) AS last_at "
                       "FROM tasks t LEFT JOIN discussions d ON d.root_task_id = t.id WHERE t.project_id = ? "
                       "AND t.parent_id IS NULL AND t.status != 'cancelled' GROUP BY t.id "
                       "ORDER BY last_at IS NULL, last_at DESC, t.id DESC", (pid,))
    return {"entries": _entries(rows[::-1]), "threads": threads}


class DiscussionIn(BaseModel):
    text: str = Field(default="", max_length=100_000)
    images: list[str] = []         # base64 image data, optionally as data: URLs
    task_id: int | None = None     # None: a new task for the lead; else the message goes to its root task


@app.post("/api/projects/{pid}/discussion")
def post_discussion(pid: int, body: DiscussionIn):
    """A message from the user to the lead in the team discussion (see team.post_user_message)."""
    _project_or_404(pid)
    out = team.post_user_message(pid, body.text, body.images, body.task_id)
    entry = _entries(db.query(ENTRY_SQL + "WHERE d.id = ?", (out.pop("entry_id"),)))[0]
    return {"entry": entry, **out}


@app.get("/api/discussion/{did}/images/{name}")
def get_discussion_image(did: int, name: str):
    db.one("SELECT 1 FROM discussions WHERE id = ?", (did,)) or not_found("Image not found")
    p = images.discussion_path(did, name) or not_found("Image not found")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.put("/api/tasks/{tid}")
def update_task(tid: int, body: TaskPatch):
    task = _task_or_404(tid)
    fields = body.model_dump(exclude_none=True, exclude={"clear_estimate", "assignee_id", "add_images",
                                                          "remove_images"})
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
    reassign = body.assignee_id is not None and body.assignee_id != task["assignee_id"]
    aid = _check_assignee(task["project_id"], body.assignee_id) if reassign else None
    removed = set(body.remove_images or [])
    files = images.task_check(task["images"], body.add_images or [], removed)
    # Validation is done: from here on the image files change
    if files or removed:
        images.task_delete(tid, [i["name"] for i in task["images"] if i["name"] in removed])
        fields["images"] = [i for i in task["images"] if i["name"] not in removed] + images.task_save(tid, files)
    if reassign:
        db.execute("UPDATE tasks SET assignee_id = ? WHERE id = ?", (aid, tid))
        db.add_task_event(tid, "user", f"Reassigned to {db.get_agent(aid)['name']}")
    updated = db.update_task(tid, fields)
    changed = [k for k in fields if task.get(k) != updated.get(k)]
    if changed:
        db.add_task_event(tid, "user", "Edited: " + ", ".join(changed))
    return _with_runtime(updated)


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int):
    _task_or_404(tid)
    tree = _subtree(tid)
    _cancel_conversations(f"SELECT id FROM conversations WHERE task_id IN ({','.join('?' * len(tree))})", tree)
    db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    for t in tree:
        images.task_remove(t)
    return {"ok": True}


@app.get("/api/tasks/{tid}/images/{name}")
def get_task_image(tid: int, name: str):
    _task_or_404(tid)
    p = images.task_path(tid, name) or not_found("Image not found")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.post("/api/tasks/{tid}/run")
def run_task(tid: int, body: TaskRunIn):
    """Start the task's assignee on it, or send it the next message."""
    _task_or_404(tid)
    try:
        run, conv = team.start_task(tid, body.message, body.images)
    except runs.RunBusy as e:
        raise HTTPException(409, str(e))
    return {"run": run.info(), "conversation_id": conv["id"]}


# ---------------------------------------------------------------- questions to the user

class AnswerIn(BaseModel):
    answer: str = Field(min_length=1)


@app.get("/api/questions")
def list_questions(status: str = "open", project_id: int | None = None):
    sql = ("SELECT q.*, t.title AS task_title, t.project_id, p.name AS project FROM questions q "
           "JOIN tasks t ON t.id = q.task_id JOIN projects p ON p.id = t.project_id WHERE q.status = ?")
    params: list = [status]
    if project_id is not None:
        sql += " AND t.project_id = ?"
        params.append(project_id)
    rows = db.query(sql + " ORDER BY q.id DESC LIMIT 200", params)
    for r in rows:
        r["options"] = json.loads(r["options"])
    return rows


@app.post("/api/questions/{qid}/answer")
def answer_question(qid: int, body: AnswerIn):
    return team.answer_question(qid, body.answer.strip())


@app.post("/api/questions/{qid}/dismiss")
def dismiss_question(qid: int):
    return team.answer_question(qid, None)


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


class FsCopy(FsMove):
    rename_if_exists: bool = False


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


@app.post("/api/fs/copy")
def fs_copy(body: FsCopy):
    r = _root(body.root)
    return fsops.copy(fsops.confine(r, body.source), fsops.confine(r, body.destination), r, body.overwrite,
                      body.rename_if_exists)


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
    images.prune()
    templates.seed_once()
    groups.seed_once()
    _ensure_devteam()
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
