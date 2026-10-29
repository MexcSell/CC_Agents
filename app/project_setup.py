"""Project setup: when a project is registered, a setup agent analyses it and prepares its team and work.

The agent reads the project details the user gave (a description, or an address such as a URL or a
document to read) and the current state of the repository folder, and consolidates both into the
project brief, which becomes part of every project agent's briefing. It then looks for agent templates
and agent groups that fit the project, creates the templates and the group that are missing, makes that
group the project's team, and plans the project's tasks. It does not carry out any task: the tasks wait
for the user to run them.

The setup conversation (kind "setup", options.project_id) stays with the project; the user can message
the agent there to adjust the analysis, the team or the plan.
"""

import json
import subprocess
from pathlib import Path

from . import db, devteam, fsops, groups, team, templates, tools as T
from .agents import AgentSpec

KIND = "setup"
OVERVIEW_CHARS = 8000

SYSTEM_PROMPT = """You are the setup agent of Agent Group, a workspace where teams of Claude agents carry out \
software projects. A user has just registered a project. Your job is to prepare it so that its team can start:

1. Analyse. Read the project details the user gave. If they contain an address (a URL, or the path of a \
document), read it: fetch web pages with your web tools, read files with the file tools. Then examine the \
repository folder to learn its current state: what exists (stack, languages, frameworks, structure, build and \
test setup, documentation, recent history), what is missing, unfinished or broken. An empty folder means the \
project starts from scratch.
2. Consolidate. Save a project brief with save_project_brief: goal and scope, requirements, the current state \
of the repository, technical decisions and constraints, risks, and open questions (with the assumptions you \
made). Every agent of the project reads it, so make it accurate, concrete and compact.
3. Staff the team. Look through the existing agent groups (list_groups) and templates (list_templates) and \
choose the group that fits the project's stack and work best. If none fits well, create one with \
create_group from existing templates; create a template (create_template) only for a role that no existing \
template covers, and never duplicate one. Then make the group the project's team with set_project_team. Keep \
the team proportionate to the project: a small project does not need a large organisation.
4. Plan. Create the project's tasks with create_tasks: the work needed to reach the goal from the current \
state, in the order it should be done, each with a clear description and acceptance criteria. Make them \
substantial units of work (a feature, a component, a test suite, a release step), not single edits; the lead \
breaks each task down further when it runs. Assign most tasks to the lead; assign self-contained specialist \
work directly to the right member. Estimate hours.

Rules:
- Do not carry out the project's work. Do not change, create or delete files in the repository. The tasks \
you create are not started; the user starts them.
- Do not ask for permission or wait for answers. If information is missing, make reasonable assumptions, \
state them in the brief and in the task descriptions, and list the questions the user should answer.
- When the user writes to you later, adjust the brief, the team or the tasks as asked. Do not create tasks \
that already exist.
- Finish with a short report in Markdown: the project in two or three sentences, the team you chose or \
created and why, the tasks you created, and the open questions."""

class SetupError(Exception):
    """A setup run that cannot be started; the message is shown to the user."""


# ---------------------------------------------------------------- the setup conversation and run

def conversation(pid: int) -> dict | None:
    row = db.one("SELECT id FROM conversations WHERE kind = ? AND json_extract(options, '$.project_id') = ? "
                 "ORDER BY id DESC LIMIT 1", (KIND, pid))
    return db.get_conversation(row["id"]) if row else None


def status(pid: int) -> dict | None:
    """The project's setup conversation and whether the setup agent is working, or None before setup."""
    from . import runs
    conv = conversation(pid)
    if not conv:
        return None
    run = runs.active_for(conv["id"])
    return {"conversation_id": conv["id"], "running": run is not None, "run_id": run.id if run else None}


def spec(project: dict) -> AgentSpec:
    s = db.get_settings()
    return AgentSpec(kind=KIND, model=s["project_model"], effort=s["project_effort"],
                     tools=T.CONSULT_TOOLS + T.SETUP_TOOLS + [T.WEB_FETCH_TOOL, T.WEB_SEARCH_TOOL],
                     workspace=T.Workspace(project["repo_path"], allow_commands=False),
                     max_turns=s["max_agent_turns"], engine=s["engine"], project_id=project["id"])


def start(pid: int, message: str = ""):
    """Start the setup agent on project `pid`: the analysis, or (when it ran before) the next message of the
    setup conversation. Returns (run, conversation). Raises SetupError or runs.RunBusy."""
    from . import runs
    project = db.get_project(pid)
    if not project:
        raise SetupError("Project not found")
    if devteam.is_dev(project):
        raise SetupError("The development team of this website is set up in the Developer project on the Projects page")
    if not Path(project["repo_path"]).is_dir():
        raise SetupError(f"The project repository {project['repo_path']} does not exist")
    conv = conversation(pid)
    if conv is None:
        conv = db.create_conversation(KIND, f"Setup: {project['name']}", SYSTEM_PROMPT, options={"project_id": pid})
        content = kickoff_message(project, message)
    else:
        content = followup_message(project, message)
    return runs.start(conv["id"], KIND, conv["title"], content, spec(project)), conv


def _git_summary(repo: Path) -> str:
    if not (repo / ".git").exists():
        return "Not a git repository."
    try:
        out = subprocess.run(["git", "log", "--oneline", "-n", "15"], cwd=repo, capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "A git repository (its history could not be read)."
    log = out.stdout.strip()
    return f"Git repository. Recent commits:\n{log}" if log else "A git repository without commits."


def _state(project: dict) -> str:
    """The project's current team and tasks."""
    roster = "\n".join(f"{'  ' * a['depth']}- {a['name']} [{a['key']}]" + (f" @{a['template']}" if a["template"] else "")
                       for a in project["agents"]) or "(none)"
    names = {a["id"]: a["name"] for a in project["agents"]}
    tasks = db.query("SELECT id, title, status, assignee_id FROM tasks WHERE project_id = ? AND parent_id IS NULL "
                     "ORDER BY id", (project["id"],))
    listing = "\n".join(f"- #{t['id']} [{t['status']}] {t['title']} → {names.get(t['assignee_id'], '?')}"
                        for t in tasks) or "(none)"
    return f"Current team:\n{roster}\n\nCurrent tasks:\n{listing}"


def kickoff_message(project: dict, note: str) -> str:
    repo = Path(project["repo_path"])
    overview = T.clip(fsops.tree(repo, depth=2), OVERVIEW_CHARS)
    parts = [
        f'Set up the project "{project["name"]}".',
        "Project details or address, from the user:\n" + (project["description"].strip() or "(none given)"),
        f"Repository folder: {repo}\n{_git_summary(repo)}\n\nTop of the folder:\n{overview}",
        _state(project) + "\n(The current team is the default single agent; replace it with set_project_team.)",
    ]
    if note.strip():
        parts.append(f"Note from the user:\n{note.strip()}")
    return "\n\n".join(parts)


def followup_message(project: dict, note: str) -> str:
    text = note.strip() or ("Review the project again: check the details and the repository for changes, update "
                            "the brief, and adjust the team and the plan where needed.")
    return f"{text}\n\n{_state(project)}"


# ---------------------------------------------------------------- tools

def _project(spec: AgentSpec) -> dict:
    project = db.get_project(spec.project_id) if spec.project_id else None
    if not project:
        raise T.ToolError("This conversation is not attached to a project.")
    return project


def _matches(words: list[str], *fields) -> bool:
    hay = " ".join(" ".join(f) if isinstance(f, list) else str(f or "") for f in fields).lower()
    return all(w in hay for w in words)


def list_templates(run, spec, args: dict) -> str:
    words = (args.get("query") or "").lower().split()
    rows = [t for t in templates.list_all()
            if _matches(words, t["slug"], t["name"], t["category"], t["area"], t["language"], t["description"],
                        t["role"], t["expertise"], t["skills"])]
    if not rows:
        return "No template matches." + (" Try fewer or other words." if words else "")
    lines = []
    for t in rows:
        kind = "/".join(filter(None, [t["area"], t["language"]]))
        expertise = f" Expertise: {', '.join(t['expertise'][:8])}." if t["expertise"] else ""
        lines.append(f"- @{t['slug']} — {t['name']}" + (f" [{kind}]" if kind else "") + f": {t['role'] or t['description']}.{expertise}")
    return f"{len(rows)} template(s):\n" + "\n".join(lines)


def list_groups(run, spec, args: dict) -> str:
    words = (args.get("query") or "").lower().split()
    rows = [g for g in groups.list_all()
            if _matches(words, g["slug"], g["name"], g["category"], g["description"],
                        [f"{m['name']} {m['template']} {m['language']} {m['area']}" for m in g["members"]])]
    if not rows:
        return "No group matches." + (" Try fewer or other words." if words else "")
    out = []
    for g in rows:
        members = "\n".join(f"{'  ' * (m['depth'] + 1)}- {m['name']} [{m['key']}] @{m['template']}" for m in g["members"])
        out.append(f"#{g['slug']} — {g['name']}" + (f" ({g['category']})" if g["category"] else "")
                   + (f": {g['description']}" if g["description"] else "") + f"\n{members}")
    return f"{len(rows)} group(s):\n\n" + "\n\n".join(out)


def create_template(run, spec, args: dict) -> str:
    fields = {k: args.get(k, "" if k not in ("rules", "expertise", "skills") else []) for k in
              ("name", "slug", "category", "area", "language", "description", "role", "instructions", "rules",
               "expertise", "skills")}
    fields["subagents"] = []
    try:
        t = templates.save(fields)
    except templates.TemplateError as e:
        raise T.ToolError(str(e))
    run.emit("notice", level="info", text=f"Created the agent template {t['symbol']} ({t['name']})")
    return f"Created the template {t['symbol']} ({t['name']})."


def create_group(run, spec, args: dict) -> str:
    try:
        g = groups.save({"name": args["name"], "slug": args.get("slug", ""), "category": args.get("category", ""),
                         "description": args.get("description", ""), "members": args["members"]})
    except groups.GroupError as e:
        raise T.ToolError(str(e))
    run.emit("notice", level="info", text=f"Created the agent group {g['symbol']} ({g['name']})")
    members = "\n".join(f"{'  ' * m['depth']}- {m['name']} [{m['key']}] @{m['template']}" for m in g["members"])
    return f"Created the group {g['symbol']} ({g['name']}):\n{members}"


def set_project_team(run, spec, args: dict) -> str:
    from . import runs
    project = _project(spec)
    group = groups.by_slug(args["group"])
    if not group:
        raise T.ToolError(f"There is no group {args['group']}. Use list_groups, or create the group first.")
    tasks = {r["id"] for r in db.query("SELECT id FROM tasks WHERE project_id = ?", (project["id"],))}
    if any(r["kind"] == "task" and r["task_id"] in tasks for r in runs.active_runs()):
        raise T.ToolError("Agents are working on this project's tasks, so the team cannot be replaced now.")
    s = db.get_settings()
    groups.apply_to_project(project["id"], group, s["project_model"], s["project_effort"])
    roster = "\n".join(f"{'  ' * a['depth']}- {a['name']} [{a['key']}]" + (f" — {a['role']}" if a["role"] else "")
                       for a in db.list_agents(project["id"]))
    run.emit("notice", level="info", text=f"The project's team is now {group['symbol']} ({group['name']})")
    return f"The project's team is now {group['symbol']} ({group['name']}). Address members by their key:\n{roster}"


def save_project_brief(run, spec, args: dict) -> str:
    project = _project(spec)
    brief = args["brief"].strip()
    if not brief:
        raise T.ToolError("The brief is empty.")
    db.execute("UPDATE projects SET brief = ?, updated_at = ? WHERE id = ?", (brief, db.now(), project["id"]))
    run.emit("notice", level="info", text="Saved the project brief")
    return "Project brief saved."


def create_tasks(run, spec, args: dict) -> str:
    project = _project(spec)
    if not project["agents"]:
        raise T.ToolError("The project has no team yet; call set_project_team first.")
    ts = db.now()
    planned = []
    for item in args["tasks"]:
        title = (item.get("title") or "").strip()
        if not title:
            raise T.ToolError("Every task needs a title.")
        planned.append((item, title, team.find_member(project["agents"], item["assignee"])
                        if (item.get("assignee") or "").strip() else project["agent"]))
    created = []
    for item, title, who in planned:
        hours = item.get("estimated_hours")
        actions = [{"text": a.strip(), "done": False} for a in item.get("next_actions") or [] if a.strip()]
        tid = db.execute(
            "INSERT INTO tasks(project_id, assignee_id, title, description, estimated_hours, next_actions, created_at, "
            "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (project["id"], who["id"], title, item.get("description", ""),
             float(hours) if isinstance(hours, (int, float)) and hours > 0 else None,
             json.dumps(actions, ensure_ascii=False), ts, ts))
        db.add_task_event(tid, "agent", f"Planned by the setup agent and assigned to {who['name']}")
        created.append(f"- #{tid} {title} → {who['name']} [{who['key']}]")
    run.emit("notice", level="info", text=f"Created {len(created)} task(s)")
    return "Created tasks (not started):\n" + "\n".join(created)


HANDLERS = {
    "list_templates": list_templates, "list_groups": list_groups, "create_template": create_template,
    "create_group": create_group, "set_project_team": set_project_team, "save_project_brief": save_project_brief,
    "create_tasks": create_tasks,
}
