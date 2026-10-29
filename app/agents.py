"""System prompts and tool sets of the agents: chat, developer, and the project teams' agents (which work
on tasks, and answer teammates' questions in consultations).

A conversation's system prompt is rendered once, when the conversation is created, and
stored with it. Later edits to topic or agent settings never rewrite an existing
conversation's prefix (that would invalidate its thinking blocks and prompt cache);
they reach the agent through the next user message instead.
"""

from dataclasses import dataclass, field

from . import db, devteam, maintenance, team, templates, tools as T
from .config import APP_ROOT, DEV_PROTECTED_DIRS, PORT


@dataclass
class AgentSpec:
    kind: str                      # "chat" | "dev" | "task" | "consult" | "setup" (app/project_setup.py)
    model: str
    effort: str
    tools: list = field(default_factory=list)
    workspace: T.Workspace | None = None
    task_id: int | None = None
    max_turns: int = 80
    engine: str = "api"            # "api" (Messages API, app/runs.py) or "claude_code" (app/claude_code.py)
    web_search: bool = False
    # Internal agents (Claude Code sub-agents) by name: {"description", "prompt", "model", "effort"}.
    subagents: dict = field(default_factory=dict)
    project_id: int | None = None   # project agents: the project and the agent (team member) running
    agent_id: int | None = None

    @property
    def tool_index(self) -> dict:
        return {t["name"]: t for t in self.tools if "input_schema" in t}


# Appended to a conversation's stored system prompt when it runs on Claude Code, whose tools
# have different names. Constant per agent kind, so the prompt stays stable across runs.
CLAUDE_CODE_NOTES = {
    "dev": "\n\nIn this environment you work with Claude Code's built-in tools (Read, Write, Edit, Glob, Grep, "
           "Bash; Bash replaces run_command). The Agent Group tools restart_server and read_server_log are named "
           "mcp__agent__restart_server and mcp__agent__read_server_log.",
    "task": "\n\nIn this environment you work with Claude Code's built-in tools (Read, Write, Edit, Glob, Grep, "
            "Bash; Bash replaces run_command). Agent Group's own tools (update_task and the team tools such as "
            "ask_agent, ask_user, record_learning, create_subtasks, delegate and review_subtask) carry the prefix "
            "mcp__agent__, e.g. mcp__agent__update_task.",
    "consult": "\n\nIn this environment you can read the repository with Claude Code's Read, Glob and Grep tools.",
    "setup": "\n\nIn this environment you read files with Claude Code's Read, Glob and Grep tools and the web with "
             "WebFetch and WebSearch. Agent Group's setup tools carry the prefix mcp__agent__, e.g. "
             "mcp__agent__list_groups and mcp__agent__create_tasks.",
}


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
    web_search = bool(conv["options"].get("web_search"))
    return AgentSpec(kind="chat", model=s["chat_model"], effort=s["chat_effort"],
                     tools=[T.WEB_SEARCH_TOOL] if web_search else [], max_turns=8,
                     engine=s["engine"], web_search=web_search)


# ---------------------------------------------------------------- developer

ARCHITECTURE = f"""Architecture:
- supervisor.py: starts the server (`python -m app.server`) as a child process, restarts it when it \
exits with code 3, and restores the latest snapshot if a restarted server keeps crashing on startup.
- app/server.py: FastAPI app. REST API under /api, Server-Sent Events for agent runs, serves static/ at /.
- app/db.py: SQLite schema and helpers (database file in data/).
- app/agents.py: system prompts and tool sets of the chat, developer and project agents.
- app/templates.py: agent templates (referred to as @slug), context-dependent editor suggestions, internal \
agents; app/template_presets.py: the built-in templates (by functional area and language) and agent groups.
- app/groups.py: agent groups (#slug), hierarchical teams of templates that become a project's agents.
- app/devteam.py: the Developer project, the development team of this website (built in, listed on the Projects page, cannot be deleted).
- app/team.py: team work on tasks: sub-tasks, delegation, consultations, questions to the user, lessons \
learned, the team discussion.
- app/runs.py: run manager and the streaming Claude tool-use loop for the API engine (Anthropic Python SDK).
- app/claude_code.py: the Claude Code engine (Claude Agent SDK, uses the Claude Code login on this machine).
- app/tools.py: tool definitions and executors (file tools, shell, task updates, restart).
- app/fsops.py: filesystem operations behind the file browser and the file tools.
- app/maintenance.py: source snapshots, preflight check, restart requests, server log.
- static/: single-page frontend in plain JavaScript ES modules, no build step. index.html (shell and \
sidebar), style.css (all styles, light/dark tokens), js/main.js (hash router, server health polling), \
js/core.js (API client, DOM helper h(), dialogs, Markdown), js/pages.js (every page), \
js/conversation.js (live agent conversation view over SSE), js/files.js (file explorer), \
js/templates.js (templates and agent groups pages, autocomplete and chip inputs), js/team.js (project \
team, questions, team discussion, knowledge).
- The server listens on port {PORT}. Health check: `curl -s localhost:{PORT}/api/health`."""

DEV_SYSTEM = f"""You are the Developer agent of Agent Group, the web application you are running inside. \
Your workspace is the application's own source directory, {APP_ROOT}. You develop, modify, debug and \
restart this website when the user asks.

{ARCHITECTURE}

How to work:
- Read the files you are about to change and follow their existing style.
- Changes under static/ take effect when the browser reloads; tell the user to reload. Changes to \
Python files need restart_server.
- restart_server runs a preflight check (byte-compile and import) and refuses if it fails. The restart \
happens after your turn ends, so make it your last step and summarise your changes before calling it.
- A snapshot of the source is saved automatically before your first file change in each run; the user \
can restore snapshots from the Developer project's Server & snapshots tab.
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
                     tools=T.FILE_TOOLS + T.DEV_TOOLS, workspace=ws, max_turns=s["max_agent_turns"],
                     engine=s["engine"])


# ---------------------------------------------------------------- project agents

def template_profile(agent: dict) -> str:
    """The profile an agent takes from its template, or '' when it has none."""
    template, _ = templates.resolve_agent(agent)
    if not template:
        return ""
    profile = templates.profile_text(template) or "(no rules, expertise or skills defined)"
    return f"Your profile, from the agent template {template['symbol']} ({template['name']}):\n{profile}"


def team_roster(project: dict, agent: dict) -> str:
    """The project's team as an indented list (indentation = who reports to whom)."""
    lines = []
    for a in project["agents"]:
        role = f" — {a['role']}" if a["role"] else ""
        you = "  ← you" if a["id"] == agent["id"] else ""
        lines.append(f"{'  ' * a['depth']}- {a['name']} [{a['key']}]{role}{you}")
    return "\n".join(lines)


LEAD_METHOD = """You lead the team members below you in the team list and can add members. Each conversation \
is about one task assigned to you, by the user or by your own lead. How to carry it out:
- Understand the task and look at the repository enough to plan. Call update_task with status \
"in_progress", estimated_hours for the whole task and your plan as next_actions.
- Analyse the task's instructions and decide which specialists it needs. If your team lacks one, find a \
fitting template with list_templates and add it below you with add_team_member; create a template \
(create_template) only when no template covers the role, and never duplicate one or a member.
- Break the work into sub-tasks sized for one agent, with a clear description and acceptance criteria, and \
assign each to the best-suited member below you with create_subtasks. Use the specialists: developers \
build, QA tests, reviewers review, and so on. Small coordinating work you may do yourself.
- Run them with delegate. Sub-tasks in one delegate call run in parallel: combine only independent \
sub-tasks that do not edit the same files, and run dependent ones in order.
- Review every report: read the changes and run the tests. Accept good work with review_subtask; \
otherwise send it back (review_subtask "revise") and delegate it again with specific feedback.
- Pass questions from your team to the user only when nobody on the team can answer them.
- Keep your task record current (progress, insights, next actions). When all sub-tasks are accepted and the \
result is verified, set status "done", progress 100 and a summary, and end your turn with a short report."""

MEMBER_METHOD = """Each conversation is about one task assigned to you, usually by your lead as part of a \
larger task, sometimes by the user directly. How to carry it out:
- Look at the repository as needed, then call update_task with status "in_progress", estimated_hours and \
your plan as next_actions.
- Work autonomously; routine steps do not need permission. Keep the task record current: raise progress \
as milestones complete, add insights when an idea, discovery or risk emerges, and keep next_actions current.
- Stay within your task. Teammates work in the same repository at the same time; do not change files that \
belong to someone else's task without agreeing on it first (ask_agent).
- When you are done, verify your work (build, tests), then call update_task with progress 100, a summary \
and status "review" if a teammate assigned the task (they accept it) or "done" if the user did. End your \
turn with a short report: what you did, how you verified it, anything open."""

COLLABORATION = """Collaboration:
- Use ask_agent to consult a teammate when their expertise helps: agree on interfaces, check an approach, \
get a quick review. Keep questions specific.
- Use ask_user only for what only the user can decide or provide (requirements, preferences, access, \
permission for risky or irreversible steps). Your work continues when they answer. For everything else, \
decide reasonably and state your assumption.
- If you are blocked, set status "blocked", explain why in the summary and say what you need.

Learning and growth:
- Your briefings include lessons from earlier work. Apply them.
- Whenever you learn something reusable (a convention of this repository, a pitfall and how to avoid it, a \
technique that worked, feedback from the user or a reviewer), record it with record_learning: scope "self" \
for yourself, "team" for the whole project team, "role" for general know-how useful to every agent with your \
template. Before finishing a task, take a moment to record what you learned."""


DEV_TEAM_COMMON = f"""This project is Agent Group itself: the web application you are running inside, so \
your team develops and maintains the website the user is looking at.

{ARCHITECTURE}

Working on this website:
- Read the files you are about to change and follow their existing style.
- Changes under static/ take effect when the browser reloads. Changes to Python files take effect only \
after a server restart.
- A snapshot of the source is saved automatically before the team's first file change on a task; the user \
can restore snapshots from the Developer project's Server & snapshots tab.
- File tools cannot modify {", ".join(d + "/" for d in DEV_PROTECTED_DIRS)}. Install Python packages with \
run_command (`.venv/bin/pip install ...`) and add them to requirements.txt.
- read_server_log shows the server's output, including tracebacks. On Claude Code it is \
mcp__agent__read_server_log."""

DEV_TEAM_LEAD = """- You are the only one on the team who can restart the server (restart_server; on Claude Code \
mcp__agent__restart_server). A restart interrupts every running agent, your team included, so restart once, \
at the very end: after all sub-tasks are accepted and the change is verified, update your task record (status \
"done", summary), give your final report, and call restart_server as your last action. A preflight check runs \
first and refuses the restart if the app does not compile or import. Restart only when Python files changed."""

DEV_TEAM_MEMBER = """- You cannot restart the server; your lead does that at the end. Verify Python changes \
without a restart: `.venv/bin/python -m py_compile <files>` and an import of the changed modules. In your \
report, say whether a restart is needed."""


def project_brief(project: dict) -> str:
    brief = (project.get("brief") or "").strip()
    return f"\n\nProject brief (analysis of the project and its repository):\n{brief}" if brief else ""


def is_lead(project: dict, agent: dict) -> bool:
    """Whether an agent leads: it has reports, or it is the project's top lead (who can add members)."""
    return agent["depth"] == 0 or any(a["parent_id"] == agent["id"] for a in project["agents"])


def task_system_prompt(project: dict, agent: dict) -> str:
    template, _ = templates.resolve_agent(agent)
    role_text = agent["role"].strip() or (template["role"] if template else "")
    role = f" Your role: {role_text}" if role_text else ""
    instructions = agent["instructions"].strip() or "(none)"
    profile = template_profile(agent)
    profile = f"\n\n{profile}" if profile else ""
    leads = is_lead(project, agent)
    site = ""
    if devteam.is_dev(project):
        site = f"\n\n{DEV_TEAM_COMMON}\n{DEV_TEAM_LEAD if agent['depth'] == 0 else DEV_TEAM_MEMBER}"
    return f"""You are {agent['name']}, an agent of the team working on the project "{project['name']}" in \
Agent Group.{role}{profile}

Project description:
{project['description'].strip() or '(none)'}{project_brief(project)}

Your workspace root is the project's repository: {project['repo_path']}
File tool paths are relative to it, and run_command runs in it.{site}

The project team (indentation shows who reports to whom; address teammates by the key in brackets):
{team_roster(project, agent)}

{LEAD_METHOD if leads else MEMBER_METHOD}

{COLLABORATION}

Instructions from the user for this agent (they may be updated later; updates arrive in user messages):
{instructions}"""


def _subagents(agent: dict) -> dict:
    _, internal = templates.resolve_agent(agent)
    return {t["slug"]: {"description": t["description"] or t["role"] or t["name"],
                        "prompt": templates.subagent_prompt(t), "model": t["model"] or None,
                        "effort": t["effort"] or None} for t in internal}


def task_spec(project: dict, agent: dict, task_id: int) -> AgentSpec:
    s = db.get_settings()
    leads = is_lead(project, agent)
    tools = T.FILE_TOOLS + T.TASK_TOOLS + T.TEAM_TOOLS + (T.LEAD_TOOLS if leads else [])
    if devteam.is_dev(project):
        root_id = team.root_of(db.get_task(task_id))["id"]
        ws = T.Workspace(project["repo_path"], allow_commands=s["allow_commands"], protected=DEV_PROTECTED_DIRS,
                         before_write=lambda: devteam.snapshot_before_change(root_id))
        dev_tools = {t["name"]: t for t in T.DEV_TOOLS}
        tools = tools + [dev_tools["read_server_log"]] + ([dev_tools["restart_server"]] if agent["depth"] == 0 else [])
    else:
        ws = T.Workspace(project["repo_path"], allow_commands=s["allow_commands"])
    return AgentSpec(kind="task", model=agent["model"], effort=agent["effort"], tools=tools, workspace=ws,
                     task_id=task_id, max_turns=s["max_agent_turns"], engine=s["engine"],
                     subagents=_subagents(agent), project_id=project["id"], agent_id=agent["id"])


def consult_system_prompt(project: dict, agent: dict) -> str:
    template, _ = templates.resolve_agent(agent)
    role_text = agent["role"].strip() or (template["role"] if template else "")
    role = f" Your role: {role_text}" if role_text else ""
    profile = template_profile(agent)
    profile = f"\n\n{profile}" if profile else ""
    return f"""You are {agent['name']}, an agent of the team working on the project "{project['name']}" in \
Agent Group.{role}{profile}

A teammate is consulting you. Answer their questions from your expertise, concretely and briefly: give a \
recommendation, the reasoning behind it, and the risks. You may read the project's repository \
({project['repo_path']}; paths are relative to it) to ground your answer, but you do not change anything \
here. If the question needs a decision only the user can make, say so.{project_brief(project)}

The project team:
{team_roster(project, agent)}

Instructions from the user for this agent:
{agent['instructions'].strip() or '(none)'}"""


def consult_spec(project: dict, agent: dict) -> AgentSpec:
    s = db.get_settings()
    ws = T.Workspace(project["repo_path"], allow_commands=False)
    return AgentSpec(kind="consult", model=agent["model"], effort=agent["effort"], tools=T.CONSULT_TOOLS,
                     workspace=ws, max_turns=min(20, s["max_agent_turns"]), engine=s["engine"],
                     project_id=project["id"], agent_id=agent["id"])


def task_record_text(task: dict, project: dict | None = None) -> str:
    est = f"{task['estimated_hours']:g} h" if task["estimated_hours"] is not None else "not estimated"
    insights = "\n".join(f"  - {i['text']}" for i in task["insights"]) or "  (none)"
    actions = "\n".join(f"  - [{'x' if a.get('done') else ' '}] {a['text']}" for a in task["next_actions"]) or "  (none)"
    text = (f"Current task record:\n- status: {task['status']}\n- progress: {task['progress']}%\n"
            f"- estimated total time: {est}\n- summary: {task['summary'] or '(none)'}\n"
            f"- insights:\n{insights}\n- next actions:\n{actions}")
    names = {a["id"]: f"{a['name']} [{a['key']}]" for a in (project or {}).get("agents", [])}
    subs = db.query("SELECT id, title, status, progress, assignee_id FROM tasks WHERE parent_id = ? ORDER BY id",
                    (task["id"],))
    if subs:
        text += "\n- sub-tasks:\n" + "\n".join(
            f"  - #{t['id']} [{t['status']}, {t['progress']}%] {t['title']} → {names.get(t['assignee_id'], '?')}"
            for t in subs)
    questions = db.query("SELECT agent_name, question, answer, status FROM questions WHERE task_id = ? "
                         "ORDER BY id DESC LIMIT 6", (task["id"],))
    if questions:
        text += "\n- questions to the user:\n" + "\n".join(
            f"  - {q['agent_name']} asked: {q['question']}\n    "
            + (f"answer: {q['answer']}" if q["status"] == "answered" else q["status"]) for q in reversed(questions))
    return text


def task_kickoff_message(project: dict, agent: dict, task: dict, note: str) -> str:
    parts = [f"Task #{task['id']}: {task['title']}", task["description"].strip() or "(no description)"]
    if task["parent_id"]:
        parent = db.get_task(task["parent_id"])
        if parent:
            parts.append(f"This is a sub-task of #{parent['id']} \"{parent['title']}\":\n"
                         f"{parent['description'].strip() or '(no description)'}")
    parts.append(task_record_text(task, project))
    lessons = team.learnings_text(agent)
    if lessons:
        parts.append(lessons)
    parts.append(note.strip() or "Please start working on this task.")
    return "\n\n".join(parts)


def task_followup_message(project: dict, agent: dict, task: dict, note: str, conv: dict) -> str:
    parts = [note.strip() or "Continue working on the task."]
    parts.append(task_record_text(task, project) + "\n(The user or your lead may have edited this record since "
                                                   "your last turn.)")
    if task_system_prompt(project, agent) != conv["system_prompt"]:
        profile = template_profile(agent)
        brief = project_brief(project).strip()
        parts.append("The project, team or agent settings were updated. "
                     + (f"{brief}\n\n" if brief else "")
                     + (f"{profile}\n\n" if profile else "")
                     + f"Current team:\n{team_roster(project, agent)}\n\n"
                     + "Current agent instructions:\n" + (agent["instructions"].strip() or "(none)"))
    lessons = team.learnings_text(agent, since=conv["created_at"])
    if lessons:
        parts.append(lessons)
    return "\n\n".join(parts)
