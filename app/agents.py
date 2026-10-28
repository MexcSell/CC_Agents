"""System prompts and tool sets for the three agent types: chat, developer and project agents.

A conversation's system prompt is rendered once, when the conversation is created, and
stored with it. Later edits to topic or agent settings never rewrite an existing
conversation's prefix (that would invalidate its thinking blocks and prompt cache);
they reach the agent through the next user message instead.
"""

from dataclasses import dataclass, field

from . import db, maintenance, tools as T
from .config import APP_ROOT, DEV_PROTECTED_DIRS, PORT


@dataclass
class AgentSpec:
    kind: str                      # "chat" | "dev" | "task"
    model: str
    effort: str
    tools: list = field(default_factory=list)
    workspace: T.Workspace | None = None
    task_id: int | None = None
    max_turns: int = 80

    @property
    def tool_index(self) -> dict:
        return {t["name"]: t for t in self.tools if "input_schema" in t}


# ---------------------------------------------------------------- chat

def chat_system_prompt(topic: dict | None) -> str:
    parts = ["You are Claude, talking with the user inside Agent Group, a workspace for working with "
             "Claude agents. Answer in Markdown when formatting helps; use fenced code blocks for code."]
    if topic:
        parts.append(f'This conversation belongs to the topic "{topic["title"]}".')
        if topic["description"].strip():
            parts.append(f"Topic description:\n{topic['description'].strip()}")
        if topic["instructions"].strip():
            parts.append(f"Instructions for conversations in this topic:\n{topic['instructions'].strip()}")
    return "\n\n".join(parts)


def chat_spec(conv: dict) -> AgentSpec:
    s = db.get_settings()
    tools = [T.WEB_SEARCH_TOOL] if conv["options"].get("web_search") else []
    return AgentSpec(kind="chat", model=s["chat_model"], effort=s["chat_effort"], tools=tools, max_turns=8)


# ---------------------------------------------------------------- developer

DEV_SYSTEM = f"""You are the Developer agent of Agent Group, the web application you are running inside. \
Your workspace is the application's own source directory, {APP_ROOT}. You develop, modify, debug and \
restart this website when the user asks.

Architecture:
- supervisor.py: starts the server (`python -m app.server`) as a child process, restarts it when it \
exits with code 3, and restores the latest snapshot if a restarted server keeps crashing on startup.
- app/server.py: FastAPI app. REST API under /api, Server-Sent Events for agent runs, serves static/ at /.
- app/db.py: SQLite schema and helpers (database file in data/).
- app/agents.py: system prompts and tool sets of the chat, developer and project agents.
- app/runs.py: run manager and the streaming Claude tool-use loop (Anthropic Python SDK).
- app/tools.py: tool definitions and executors (file tools, shell, task updates, restart).
- app/fsops.py: filesystem operations behind the file browser and the file tools.
- app/maintenance.py: source snapshots, preflight check, restart requests, server log.
- static/: single-page frontend in plain JavaScript ES modules, no build step. index.html (shell and \
sidebar), style.css (all styles, light/dark tokens), js/main.js (hash router, server health polling), \
js/core.js (API client, DOM helper h(), dialogs, Markdown), js/pages.js (every page), \
js/conversation.js (live agent conversation view over SSE), js/files.js (file explorer).
- The server listens on port {PORT}. Health check: `curl -s localhost:{PORT}/api/health`.

How to work:
- Read the files you are about to change and follow their existing style.
- Changes under static/ take effect when the browser reloads; tell the user to reload. Changes to \
Python files need restart_server.
- restart_server runs a preflight check (byte-compile and import) and refuses if it fails. The restart \
happens after your turn ends, so make it your last step and summarise your changes before calling it.
- A snapshot of the source is saved automatically before your first file change in each run; the user \
can restore snapshots from the Developer page.
- File tools cannot modify {", ".join(d + "/" for d in DEV_PROTECTED_DIRS)}. Install Python packages \
with run_command (`.venv/bin/pip install ...`) and add them to requirements.txt.
- Use read_server_log to investigate errors.
- When you finish, reply briefly: what changed, and anything the user needs to do."""


def dev_spec() -> AgentSpec:
    s = db.get_settings()
    snapshot_taken = []

    def snapshot_once():
        if not snapshot_taken:
            snapshot_taken.append(maintenance.create_snapshot("before-dev-change"))

    ws = T.Workspace(APP_ROOT, allow_commands=s["allow_commands"], protected=DEV_PROTECTED_DIRS,
                     before_write=snapshot_once)
    return AgentSpec(kind="dev", model=s["dev_model"], effort=s["dev_effort"],
                     tools=T.FILE_TOOLS + T.DEV_TOOLS, workspace=ws, max_turns=s["max_agent_turns"])


# ---------------------------------------------------------------- project agents

def task_system_prompt(project: dict, agent: dict) -> str:
    role = f" Your role: {agent['role'].strip()}" if agent["role"].strip() else ""
    instructions = agent["instructions"].strip() or "(none)"
    return f"""You are {agent['name']}, the agent assigned to the project "{project['name']}" in Agent Group.{role}

Project description:
{project['description'].strip() or '(none)'}

Your workspace root is the project's repository: {project['repo_path']}
File tool paths are relative to it, and run_command runs in it.

Each conversation is about one task that the user assigned to you. How to carry it out:
- Start by looking at the repository as needed, then call update_task with status "in_progress", \
estimated_hours for the whole task, and your next_actions plan.
- Keep the task record current while you work: raise progress (0-100) as milestones complete, use \
add_insights when an idea, discovery, risk or alternative emerges, keep next_actions as the current list \
of upcoming steps, and add a log entry for notable steps.
- When the work is done, call update_task with status "done", progress 100, a summary of what you did, \
and any follow-up items as next_actions.
- If you are blocked or need a decision, set status "blocked" (or "review" when the user should check \
your result), explain in summary, and end your turn with a clear question.
- Work autonomously; routine steps do not need permission.

Instructions from the user for this agent (they may be updated later; updates arrive in user messages):
{instructions}"""


def task_spec(project: dict, task_id: int) -> AgentSpec:
    s = db.get_settings()
    agent = project["agent"]
    ws = T.Workspace(project["repo_path"], allow_commands=s["allow_commands"])
    return AgentSpec(kind="task", model=agent["model"], effort=agent["effort"],
                     tools=T.FILE_TOOLS + T.TASK_TOOLS, workspace=ws, task_id=task_id,
                     max_turns=s["max_agent_turns"])


def task_record_text(task: dict) -> str:
    est = f"{task['estimated_hours']:g} h" if task["estimated_hours"] is not None else "not estimated"
    insights = "\n".join(f"  - {i['text']}" for i in task["insights"]) or "  (none)"
    actions = "\n".join(f"  - [{'x' if a.get('done') else ' '}] {a['text']}" for a in task["next_actions"]) or "  (none)"
    return (f"Current task record:\n- status: {task['status']}\n- progress: {task['progress']}%\n"
            f"- estimated total time: {est}\n- summary: {task['summary'] or '(none)'}\n"
            f"- insights:\n{insights}\n- next actions:\n{actions}")


def task_kickoff_message(task: dict, note: str) -> str:
    return (f"Task #{task['id']}: {task['title']}\n\n{task['description'].strip() or '(no description)'}\n\n"
            f"{task_record_text(task)}\n\n{note.strip() or 'Please start working on this task.'}")


def task_followup_message(task: dict, note: str, conv: dict, project: dict) -> str:
    parts = [note.strip() or "Continue working on the task."]
    parts.append(task_record_text(task) + "\n(The user may have edited this record since your last turn.)")
    current_prompt = task_system_prompt(project, project["agent"])
    if current_prompt != conv["system_prompt"]:
        parts.append("The project or agent settings were updated. Current agent instructions:\n"
                     + (project["agent"]["instructions"].strip() or "(none)"))
    return "\n\n".join(parts)
