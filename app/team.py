"""Team work: the agents of a project collaborate on its tasks.

A task the user adds goes to the project's lead. An agent with reports (a lead or manager) breaks its
task into sub-tasks for the agents below it (create_subtasks), runs them (delegate: each assignee works
on its sub-task in its own conversation, several in parallel, and the lead waits for their reports),
reviews the results (review_subtask, which can pass a lesson on to the assignee) and integrates them.
Sub-tasks carry an execution order (depends_on) and the files they change; delegate starts each one when its
dependencies have finished and no running sub-task shares its files. One member can work on several
sub-tasks at once, each in its own conversation (an instance of that agent). Every agent can consult a teammate
(ask_agent: a read-only conversation with that teammate), ask the user (ask_user: the run pauses until
the user answers in the UI) and record lessons learned (record_learning), which are fed into its later
briefings. Delegations, reports, reviews, consultations, questions and lessons are kept as the root
task's team discussion; agents can also post notes and images there (post_to_discussion). The user writes
to a task's lead in the discussion (post_user_message), and the lead's answer is posted back as a reply.

The tool handlers run on the calling agent's run thread and block while teammates work; cancelling
that run cancels the runs it is waiting for.
"""

import fnmatch
import json
import logging
import re
import threading
import time
from pathlib import Path

from . import db, groups, templates, tools as T

log = logging.getLogger("agent_group.team")

TERMINAL = {"done", "cancelled"}
REPORT_CHARS = 4000


class TeamError(Exception):
    """A request that cannot be carried out; the message is shown to the user."""


# ---------------------------------------------------------------- lookups

def root_of(task: dict) -> dict:
    seen = set()
    while task["parent_id"] and task["id"] not in seen:
        seen.add(task["id"])
        parent = db.get_task(task["parent_id"])
        if not parent:
            break
        task = parent
    return task


def is_below(task: dict, ancestor_id: int) -> bool:
    seen = set()
    while task and task["parent_id"] and task["id"] not in seen:
        if task["parent_id"] == ancestor_id:
            return True
        seen.add(task["id"])
        task = db.get_task(task["parent_id"])
    return False


def reports_of(agents: list[dict], aid: int) -> list[dict]:
    """Every agent below `aid` in the hierarchy (direct and indirect reports)."""
    out, frontier = [], {aid}
    while frontier:
        nxt = [a for a in agents if a["parent_id"] in frontier and a not in out]
        out += nxt
        frontier = {a["id"] for a in nxt}
    return out


def assignee(project: dict, task: dict) -> dict:
    """The agent a task is assigned to; the lead when unassigned or the assignee no longer exists."""
    return next((a for a in project["agents"] if a["id"] == task["assignee_id"]), project["agent"])


def find_member(candidates: list[dict], ref: str) -> dict:
    ref_n = (ref or "").strip().lstrip("@").lower()
    for a in candidates:
        if ref_n in (a["key"].lower(), a["name"].lower()):
            return a
    names = ", ".join(f"{a['key']} ({a['name']})" for a in candidates) or "nobody"
    raise T.ToolError(f"No teammate '{ref}' here. You can address: {names}.")


TITLE_CHARS = 80


def title_from(description: str) -> str:
    """A task title from its description: the first non-empty line without Markdown markers, at most
    TITLE_CHARS characters (cut at a word boundary, with '…')."""
    def clean(line: str) -> str:
        if line.strip().startswith(("```", "~~~")):  # code fence
            return ""
        line = re.sub(r"^(?:(?:#+|>+|[-*+]|\d+[.)])(?:\s+|$))*", "", line.strip())
        # emphasis and code markers; a single _ inside a word (snake_case) stays
        return " ".join(re.sub(r"`|(?<!\w)[*_]+|[*_]+(?!\w)", "", line).split())
    title = next(filter(None, map(clean, (description or "").splitlines())), "")
    if len(title) > TITLE_CHARS:
        cut = title[:TITLE_CHARS]
        if title[TITLE_CHARS] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        title = cut.rstrip(" ,;:") + "…"
    return title


def unique_key(pid: int, name: str, exclude: int | None = None) -> str:
    """A key for an agent of project `pid`, derived from `name` and not used by its other agents."""
    taken = {r["key"] for r in db.query("SELECT key FROM agents WHERE project_id = ? AND id IS NOT ?", (pid, exclude))}
    key = base = groups.slugify(name) or "agent"
    n = 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


def task_conversation(tid: int) -> dict | None:
    row = db.one("SELECT id FROM conversations WHERE task_id = ? AND kind = 'task' ORDER BY id DESC LIMIT 1", (tid,))
    return db.get_conversation(row["id"]) if row else None


def last_reply(conv_id: int, after: int | None = None) -> str:
    """The conversation's last assistant text. With `after` (the id of a user message): the answer to that
    message, from the messages between it and the next user message (tool results do not count)."""
    messages = db.list_messages(conv_id)
    if after is not None:
        messages = [m for m in messages if m["id"] > after]
        prompt = next((i for i, m in enumerate(messages) if m["role"] == "user" and (isinstance(m["content"], str)
                       or any(b.get("type") != "tool_result" for b in m["content"]))), None)
        messages = messages[:prompt]
    for m in reversed(messages):
        if m["role"] != "assistant":
            continue
        content = m["content"]
        text = content if isinstance(content, str) else "\n\n".join(
            b["text"] for b in content if b.get("type") == "text" and b.get("text", "").strip())
        if text.strip():
            return text.strip()
    return ""


def discuss(task: dict, sender: str, recipient: str, kind: str, text: str,
            images: list[tuple[bytes, str]] = ()) -> int:
    """Add an entry to the team discussion of the task's tree, with decoded `images` (images.decode).
    Returns the entry id."""
    from . import images as img
    did = db.execute("INSERT INTO discussions(root_task_id, task_id, sender, recipient, kind, text, created_at) "
                     "VALUES(?, ?, ?, ?, ?, ?, ?)",
                     (root_of(task)["id"], task["id"], sender, recipient, kind, text, db.now()))
    if images:
        db.execute("UPDATE discussions SET images = ? WHERE id = ?",
                   (json.dumps(img.discussion_save(did, list(images))), did))
    return did


def _context(spec) -> tuple[dict, dict, dict]:
    project = db.get_project(spec.project_id)
    task = db.get_task(spec.task_id) if spec.task_id else None
    me = next((a for a in (project or {}).get("agents", []) if a["id"] == spec.agent_id), None)
    if not project or not task or not me:
        raise T.ToolError("This conversation is not attached to a project task and agent.")
    return project, me, task


# ---------------------------------------------------------------- running a task

def start_task(tid: int, message: str = "", images: list[str] | None = None, sender: dict | None = None):
    """Start the assignee of task `tid` on it: a new conversation (when the task has none, or was
    reassigned) or the next message of the existing one. `sender` is the teammate who sends the message
    (None: the user). Returns (run, conversation). Raises TeamError or runs.RunBusy."""
    from . import agents, images as img, runs
    task = db.get_task(tid)
    if not task:
        raise TeamError("Task not found")
    project = db.get_project(task["project_id"])
    if not Path(project["repo_path"]).is_dir():
        raise TeamError(f"The project repository {project['repo_path']} does not exist")
    agent = assignee(project, task)
    note = message.strip()
    if sender is not None:
        who = f"{sender['name']}" + (f" ({sender['role']})" if sender["role"] else "")
        note = f"Message from {who}, who assigned this task to you:\n{note}" if note else \
            f"{who} assigned this task to you. Please carry it out."
    conv = task_conversation(tid)
    if conv is None or conv["options"].get("agent_id", agent["id"]) != agent["id"]:
        conv = db.create_conversation("task", f"Task #{tid}: {task['title']}", agents.task_system_prompt(project, agent),
                                      task_id=tid, options={"agent_id": agent["id"]})
        attached = img.task_to_conversation(task, conv["id"])   # the description's images
        content = agents.task_kickoff_message(project, agent, task, note, len(attached))
    else:
        # Images added to the description since they were last sent
        attached = img.task_to_conversation(task, conv["id"], conv["options"].get("task_images", []))
        content = agents.task_followup_message(project, agent, task, note, conv, len(attached))
    if images or attached:
        content = attached + img.save(conv["id"], images or []) + [{"type": "text", "text": content}]
    if task["status"] == "todo":
        db.update_task(tid, {"status": "in_progress"})
    run = runs.start(conv["id"], "task", conv["title"], content, agents.task_spec(project, agent, tid), task_id=tid)
    if attached:
        sent = conv["options"].get("task_images", []) + [b["upload"].split("/", 1)[1] for b in attached]
        db.update_conversation_options(conv["id"], {"task_images": sent})
    by = sender["name"] if sender else "user"
    db.add_task_event(tid, "agent" if sender else "user",
                      f"{agent['name']} started" + (f" by {by}" if sender else "") +
                      (f": {message.strip()[:200]}" if message.strip() else ""))
    return run, conv


def _wait(child, parent) -> None:
    """Wait for a child run; stopping the parent stops the child."""
    while not child.done:
        if parent.cancel.is_set():
            child.cancel.set()
        time.sleep(0.4)


# ---------------------------------------------------------------- lead tools

FINISHED = {"done", "review", "cancelled"}


def _norm(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


GLOB = re.compile(r"[*?\[]")


def shared_files(a: list[str], b: list[str]) -> list[str]:
    """The entries of `a` that overlap an entry of `b`: the same path, a folder and something in it, a glob
    pattern matching the other, a folder (a last segment without '.') and a glob that can match something in
    it, or two globs whose literal prefixes (up to the first wildcard) nest. Sub-tasks without files never conflict."""
    def prefix(p: str) -> str:
        m = GLOB.search(p)
        return p[:m.start()] if m else p

    def folder_glob(path: str, pattern: str) -> bool:   # '*' matches '/' too, so 'app/*.py' covers 'app/sub/x.py'
        return "." not in path.rsplit("/", 1)[-1] and (path + "/").startswith(prefix(pattern))

    def overlap(x: str, y: str) -> bool:
        gx, gy = bool(GLOB.search(x)), bool(GLOB.search(y))
        if gx and gy:
            return prefix(x).startswith(prefix(y)) or prefix(y).startswith(prefix(x))
        return (x == y or x.startswith(y + "/") or y.startswith(x + "/")
                or fnmatch.fnmatchcase(x, y) or fnmatch.fnmatchcase(y, x)
                or (gy and folder_glob(x, y)) or (gx and folder_glob(y, x)))
    return [f for f in a if any(overlap(_norm(f), _norm(g)) for g in b if _norm(f) and _norm(g))]


def _after(deps: list[int]) -> str:
    return "after " + ", ".join(f"#{d}" for d in deps)


def create_subtasks(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    team = reports_of(project["agents"], me["id"])
    if not team:
        raise T.ToolError("Nobody reports to you yet. Add members with add_team_member, or do the work yourself.")
    siblings = {r["id"]: {**r, "files": json.loads(r["files"]), "depends_on": json.loads(r["depends_on"])}
                for r in db.query("SELECT id, title, status, files, depends_on FROM tasks WHERE parent_id = ?",
                                  (task["id"],))}
    # Check every item first, so that a mistake creates nothing; depends_on entries resolve to an existing
    # sub-task id or to the index of an earlier item of this call
    items = []
    for item in args["subtasks"]:
        title = (item.get("title") or "").strip()
        if not title:
            raise T.ToolError("Every sub-task needs a title.")
        who = find_member(team, item.get("assignee", ""))
        deps = []
        for ref in item.get("depends_on") or []:
            m = re.fullmatch(r"#?\s*(\d+)", str(ref).strip())
            earlier = next((i for i, it in enumerate(items) if it["title"] == str(ref).strip()), None)
            if m and int(m.group(1)) in siblings:
                deps.append(int(m.group(1)))
            elif earlier is not None:
                deps.append(("item", earlier))
            else:
                valid = ", ".join([f"#{i} {t['title']}" for i, t in siblings.items()] +
                                  [f'"{it["title"]}"' for it in items]) or "none (this is the first sub-task)"
                raise T.ToolError(f"Sub-task \"{title}\": depends_on {ref!r} is neither a sub-task of #{task['id']} "
                                  f"nor the exact title of an earlier item of this call. Valid: {valid}.")
        files = list(dict.fromkeys(filter(None, map(_norm, item.get("files") or []))))
        items.append({"title": title, "who": who, "deps": deps, "files": files, "item": item})
    created = []
    ts = db.now()
    with db.transaction():
        for it in items:
            deps = list(dict.fromkeys(created[d[1]][0] if isinstance(d, tuple) else d for d in it["deps"]))
            hours = it["item"].get("estimated_hours")
            tid = db.execute(
                "INSERT INTO tasks(project_id, parent_id, assignee_id, title, description, estimated_hours, depends_on, "
                "files, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project["id"], task["id"], it["who"]["id"], it["title"], it["item"].get("description", ""),
                 float(hours) if isinstance(hours, (int, float)) and hours > 0 else None,
                 json.dumps(deps), json.dumps(it["files"], ensure_ascii=False), ts, ts))
            db.add_task_event(tid, "agent", f"Created by {me['name']} as part of #{task['id']}, assigned to "
                                            f"{it['who']['name']}" + (f", {_after(deps)}" if deps else ""))
            created.append((tid, it["title"], it["who"], deps, it["files"]))
    lines = [f"- #{tid} {title} → {who['name']} [{who['key']}]" + (f", {_after(deps)}" if deps else "")
             + (f"\n  files: {', '.join(files)}" if files else "") for tid, title, who, deps, files in created]
    # Shared files without an order between them: delegate keeps them from running at the same time
    graph = {i: t["depends_on"] for i, t in siblings.items()} | {c[0]: c[3] for c in created}

    def reaches(a: int, b: int) -> bool:
        seen, todo = set(), [a]
        while todo:
            x = todo.pop()
            if x == b:
                return True
            if x not in seen:
                seen.add(x)
                todo += graph.get(x, [])
        return False
    others = [(c[0], c[4]) for c in created] + [(i, t["files"]) for i, t in siblings.items()
                                               if t["status"] not in FINISHED]
    warnings = []
    for n, (tid, _, _, _, files) in enumerate(created):
        for other, other_files in others[n + 1:]:
            shared = shared_files(files, other_files)
            if shared and not reaches(tid, other) and not reaches(other, tid):
                warnings.append(f"⚠ #{tid} and #{other} share {', '.join(shared)} with no dependency between them: "
                                "delegate will run them one after the other.")
    db.add_task_event(task["id"], "agent", f"{me['name']} created {len(created)} sub-task(s)")
    discuss(task, me["name"], "team", "plan", "Work breakdown:\n" + "\n".join(lines + warnings))
    run.emit("task_update", task=db.get_task(task["id"]))
    return ("Created sub-tasks:\n" + "\n".join(lines + warnings) +
            "\nRun them with delegate, all in one call: it keeps the order and runs independent ones in parallel.")


def delegate(run, spec, args: dict) -> str:
    from . import runs
    project, me, task = _context(spec)
    team = {a["id"]: a for a in reports_of(project["agents"], me["id"])}
    subs = []
    for tid in dict.fromkeys(args["task_ids"]):
        sub = db.get_task(tid)
        if not sub or not is_below(sub, task["id"]):
            raise T.ToolError(f"#{tid} is not a sub-task of your task #{task['id']}.")
        if sub["assignee_id"] not in team:
            raise T.ToolError(f"#{tid} is assigned to someone outside your team; reassign it with create_subtasks.")
        if sub["status"] == "cancelled":
            raise T.ToolError(f"#{tid} was cancelled.")
        subs.append(sub)
    if not subs:
        raise T.ToolError("Give the ids of the sub-tasks to run.")
    ids = {s["id"] for s in subs}
    for sub in subs:
        for d in sub["depends_on"]:
            dep = db.get_task(d) if d not in ids else None
            if dep and dep["status"] not in FINISHED:
                raise T.ToolError(f"#{sub['id']} runs after #{d} \"{dep['title']}\", which is {dep['status']}. "
                                  f"Include #{d} in the same delegate call, or finish it first.")
    message = (args.get("message") or "").strip()
    # Several sub-tasks of one member run as separate instances of it; each learns about the others
    notes = {}
    for sub in subs:
        peers = [s for s in subs if s["assignee_id"] == sub["assignee_id"] and s is not sub]
        if peers:
            others = " and ".join(f"#{s['id']} \"{s['title']}\"" + (f" (files: {', '.join(s['files'])})"
                                                                      if s["files"] else "") for s in peers)
            notes[sub["id"]] = (f"You run as a separate instance of {team[sub['assignee_id']]['name']} for this "
                                f"sub-task. Other instances of you work on {others}, possibly at the same time. Stay "
                                "within your own sub-task's files and agree through ask_agent (e.g. with "
                                f"{me['name']}) before touching theirs.")
    limit = max(1, int(db.get_settings()["max_parallel_agents"]))
    files_of, assignee_of = {s["id"]: s["files"] for s in subs}, {s["id"]: s["assignee_id"] for s in subs}
    pending, active, failed = list(subs), {}, {}
    ok, ended, ran, waiting, timeline = set(), set(), set(), {}, []

    def note(text: str) -> None:
        timeline.append(text)
        run.emit("notice", level="info", text=text)
    while pending or active:
        if run.cancel.is_set():
            for child in active.values():
                child.cancel.set()
            return "Stopped: the run was cancelled while sub-tasks were running."
        moved = False   # whether a sub-task left `pending` in this scan
        for sub in list(pending):
            if len(active) >= limit:
                break
            deps = [d for d in sub["depends_on"] if d in ids]
            bad = next((d for d in deps if d in ended and d not in ok), None)
            if bad:
                pending.remove(sub)
                ended.add(sub["id"])
                moved = True
                failed[sub["id"]] = f"not started: #{bad} did not finish"
                note(f"✗ #{sub['id']} not started: #{bad} did not finish")
                continue
            wait = next((d for d in deps if d not in ended), None)
            clash = next(((t, f) for t in active for f in shared_files(sub["files"], files_of[t])[:1]), None) \
                if not wait else None
            reason = f"waits for #{wait}" if wait else f"waits: shares {clash[1]} with #{clash[0]}" if clash else ""
            if reason:
                if waiting.get(sub["id"]) != reason:
                    waiting[sub["id"]] = reason
                    note(f"⏸ #{sub['id']} {reason}")
                continue
            pending.remove(sub)
            moved = True
            who = team[sub["assignee_id"]]
            text = "\n\n".join(filter(None, [message, notes.get(sub["id"])]))
            try:
                child, _ = start_task(sub["id"], text, sender=me)
            except runs.RunBusy:
                conv = task_conversation(sub["id"])
                child = runs.active_for(conv["id"]) if conv else None
                if child is None:
                    failed[sub["id"]] = "could not be started"
            except TeamError as e:
                failed[sub["id"]], child = str(e), None
            except Exception as e:   # keep waiting for the sub-tasks already running
                log.exception("delegate: starting #%s failed", sub["id"])
                failed[sub["id"]], child = f"could not be started: {type(e).__name__}: {e}", None
            if child is None:
                ended.add(sub["id"])
                why = failed[sub["id"]]
                note(f"✗ #{sub['id']} " + (why if why.startswith("could not") else f"could not be started: {why}"))
                continue
            active[sub["id"]] = child
            ran.add(sub["id"])
            discuss(sub, me["name"], who["name"], "delegate", message or f"Please carry out #{sub['id']}: {sub['title']}")
            mine = sorted(t for t in active if assignee_of[t] == sub["assignee_id"])
            instance = f" (instance {mine.index(sub['id']) + 1})" if sub["id"] in notes else ""
            note(f"→ {who['name']}{instance} is working on #{sub['id']} {sub['title']}")
        if pending and not active and not moved:
            # Nothing runs and nothing can start (e.g. a dependency cycle): report the rest instead of waiting
            for sub in pending:
                failed[sub["id"]] = f"not started: {waiting.get(sub['id'], 'could not be scheduled')}"
                note(f"✗ #{sub['id']} {failed[sub['id']]}")
            pending = []
        for tid, child in list(active.items()):
            if child.done:
                del active[tid]
                ended.add(tid)
                status = db.get_task(tid)["status"]
                if child.status == "error":
                    failed[tid] = "the agent's run failed (see its work log)"
                elif child.status == "cancelled":
                    failed[tid] = "the run was stopped"
                elif status in ("blocked", "cancelled", "todo"):
                    failed[tid] = f"ended {status}"
                else:
                    ok.add(tid)
                note(f"← {team[assignee_of[tid]]['name']} finished #{tid}" + (f" ({failed[tid]})" if tid in failed else ""))
        time.sleep(0.4)

    reports = []
    for sub in subs:
        sub = db.get_task(sub["id"])
        who = team[sub["assignee_id"]]
        conv = task_conversation(sub["id"])
        reply = last_reply(conv["id"]) if conv and sub["id"] in ran else ""   # not a stale one of an earlier run
        open_q = db.query("SELECT question FROM questions WHERE task_id = ? AND status = 'open'", (sub["id"],))
        if reply:
            discuss(sub, who["name"], me["name"], "report", T.clip(reply, 3000))
        reports.append("\n".join(filter(None, [
            f"### #{sub['id']} {sub['title']} — {who['name']} [{who['key']}]",
            f"status: {sub['status']}, progress: {sub['progress']}%",
            f"problem: {failed[sub['id']]}" if sub["id"] in failed else "",
            f"summary: {sub['summary']}" if sub["summary"] else "",
            "open questions to the user: " + "; ".join(q["question"] for q in open_q) if open_q else "",
            f"report:\n{T.clip(reply, REPORT_CHARS)}" if reply else "report: (none)",
        ])))
    run.emit("task_update", task=db.get_task(task["id"]))
    return ("Reports from your team. Review each result (read the changed files, run the tests), then accept it "
            "or send it back with review_subtask.\n\nOrder of execution:\n" + "\n".join(timeline) + "\n\n"
            + "\n\n".join(reports))


REVIEW = {"accept": "done", "revise": "in_progress", "block": "blocked", "cancel": "cancelled"}


def review_subtask(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    sub = db.get_task(args["task_id"])
    if not sub or not is_below(sub, task["id"]):
        raise T.ToolError(f"#{args['task_id']} is not a sub-task of your task #{task['id']}.")
    decision, comment = args["decision"], (args.get("comment") or "").strip()
    fields = {"status": REVIEW[decision]}
    if decision == "accept":
        fields["progress"] = 100
    db.update_task(sub["id"], fields)
    who = assignee(project, sub)
    db.add_task_event(sub["id"], "agent", f"{me['name']}: {decision}" + (f" — {comment}" if comment else ""))
    discuss(sub, me["name"], who["name"], "review", f"{decision}" + (f": {comment}" if comment else ""))
    run.emit("task_update", task=db.get_task(task["id"]))
    then = " Now delegate it again with your feedback as the message." if decision == "revise" else ""
    lesson = " ".join((args.get("lesson") or "").split())
    if lesson:
        scope = save_learning(project, who, sub, args.get("lesson_scope") or "self", lesson, me["name"])
        then += f" Lesson recorded {LEARNING_WHERE[scope](who)}." if scope else f" {who['name']} already has this lesson."
    return f"#{sub['id']} is now {fields['status']}.{then}"


def list_templates(run, spec, args: dict) -> str:
    from . import project_setup
    return project_setup.list_templates(run, spec, args)


def create_template(run, spec, args: dict) -> str:
    from . import project_setup
    return project_setup.create_template(run, spec, args)


def add_team_member(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    t = templates.by_slug(args["template"])
    if not t:
        raise T.ToolError(f"There is no template {args['template']}. Find one with list_templates, or create it with "
                          "create_template if no template covers the role.")
    same = next((a for a in project["agents"] if a["template"] == t["slug"]), None)
    if same:
        boss = next((a for a in project["agents"] if a["id"] == same["parent_id"]), None)
        where = "reports to you" if boss is me else f"reports to {boss['name']} [{boss['key']}]" if boss else "is the lead"
        raise T.ToolError(f"The team already has {same['name']} [{same['key']}] ({t['symbol']}), who {where}. Use that "
                          "member: assign it work (through its lead if it is not below you) or consult it with ask_agent. "
                          "One member can take several sub-tasks in parallel (each runs as its own instance), so no "
                          "duplicate member is needed.")
    name = (args.get("name") or "").strip() or t["name"]
    s, ts = db.get_settings(), db.now()
    with db.transaction():
        key = unique_key(project["id"], (args.get("key") or "").strip() or name)
        db._conn.execute(
            "INSERT INTO agents(project_id, parent_id, key, sort, name, role, instructions, model, effort, template, "
            "subagents, created_at, updated_at) SELECT ?, ?, ?, COALESCE(MAX(sort), -1) + 1, ?, ?, '', ?, ?, ?, '[]', "
            "?, ? FROM agents WHERE project_id = ?",
            (project["id"], me["id"], key, name, t["role"], t["model"] or s["project_model"],
             t["effort"] or s["project_effort"], t["slug"], ts, ts, project["id"]))
    db.add_task_event(task["id"], "agent", f"{me['name']} added {name} ({t['symbol']}) to the team")
    discuss(task, me["name"], "team", "plan", f"Added {name} [{key}] ({t['symbol']}) to the team, reporting to {me['name']}")
    run.emit("notice", level="info", text=f"Added {name} ({t['symbol']}) to the team")
    return (f"Added {name} [{key}] ({t['symbol']}) to your team, reporting to you. Assign it work with "
            f"create_subtasks (assignee \"{key}\") or consult it with ask_agent.")


# ---------------------------------------------------------------- everyone

def ask_agent(run, spec, args: dict) -> str:
    from . import agents, runs
    project, me, task = _context(spec)
    other = find_member([a for a in project["agents"] if a["id"] != me["id"]], args["agent"])
    question = args["question"].strip()
    row = db.one("SELECT id FROM conversations WHERE kind = 'consult' AND task_id = ? "
                 "AND json_extract(options, '$.asker') = ? AND json_extract(options, '$.agent') = ? "
                 "ORDER BY id DESC LIMIT 1", (task["id"], me["id"], other["id"]))
    conv = db.get_conversation(row["id"]) if row else db.create_conversation(
        "consult", f"{me['name']} asks {other['name']} (#{task['id']})", agents.consult_system_prompt(project, other),
        task_id=task["id"], options={"asker": me["id"], "agent": other["id"]})
    role = f" ({me['role']})" if me["role"] else ""
    content = (f"{me['name']}{role} asks you about task #{task['id']} \"{task['title']}\":\n\n{question}")
    discuss(task, me["name"], other["name"], "question", question)
    run.emit("notice", level="info", text=f"Consulting {other['name']}…")
    try:
        child = runs.start(conv["id"], "consult", conv["title"], content, agents.consult_spec(project, other))
    except runs.RunBusy:
        raise T.ToolError(f"{other['name']} is already answering a question from you; wait for that answer.")
    _wait(child, run)
    if run.cancel.is_set():
        return "Stopped before the answer arrived."
    answer = last_reply(conv["id"]) or "(no answer)"
    discuss(task, other["name"], me["name"], "answer", T.clip(answer, 3000))
    return f"{other['name']} answers:\n\n{T.clip(answer, 12000)}"


_waiters: dict[int, threading.Event] = {}
_waiters_lock = threading.Lock()


def ask_user(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    question = args["question"].strip()
    options = [o.strip() for o in args.get("options") or [] if o.strip()]
    qid = db.execute("INSERT INTO questions(task_id, root_task_id, agent_id, agent_name, question, options, created_at) "
                     "VALUES(?, ?, ?, ?, ?, ?, ?)", (task["id"], root_of(task)["id"], me["id"], me["name"], question,
                                                     json.dumps(options, ensure_ascii=False), db.now()))
    discuss(task, me["name"], "user", "question", question + ("\nOptions: " + " / ".join(options) if options else ""))
    db.add_task_event(task["id"], "agent", f"{me['name']} asked the user: {question[:200]}")
    ev = threading.Event()
    with _waiters_lock:
        _waiters[qid] = ev
    run.emit("question", question_id=qid, agent=me["name"], text=question, options=options)
    run.emit("notice", level="warn", text=f"❓ {me['name']} is waiting for your answer: {question}")
    try:
        while not ev.wait(0.5):
            if run.cancel.is_set():
                return ("The run was stopped before the user answered. The question stays open; the answer will "
                        "reach the team when it arrives.")
    finally:
        with _waiters_lock:
            _waiters.pop(qid, None)
    q = db.one("SELECT * FROM questions WHERE id = ?", (qid,))
    if q["status"] == "dismissed":
        return "The user dismissed the question without answering. Proceed with your best judgment and state your assumption."
    return f"The user answered: {q['answer']}"


def answer_question(qid: int, answer: str | None) -> dict:
    """Store the user's answer (None: dismiss). A waiting agent continues at once; if none waits any more
    (its run ended), the root task's lead is started with the answer."""
    q = db.one("SELECT * FROM questions WHERE id = ?", (qid,))
    if not q:
        raise TeamError("Question not found")
    if q["status"] != "open":
        raise TeamError("This question was already answered")
    status = "answered" if answer is not None else "dismissed"
    db.execute("UPDATE questions SET answer = ?, status = ?, answered_at = ? WHERE id = ?",
               (answer, status, db.now(), qid))
    task = db.get_task(q["task_id"])
    discuss(task, "user", q["agent_name"], "answer", answer if answer is not None else "(dismissed the question)")
    db.add_task_event(task["id"], "user", f"Answered {q['agent_name']}'s question" if answer is not None
                      else f"Dismissed {q['agent_name']}'s question")
    with _waiters_lock:
        waiter = _waiters.get(qid)
    resumed = False
    if waiter:
        waiter.set()
    elif answer is not None:
        root = root_of(task)
        note = (f"The user answered a question that {q['agent_name']} asked about task #{task['id']} "
                f"\"{task['title']}\".\nQuestion: {q['question']}\nAnswer: {answer}\n"
                "Continue the work with this answer (delegate again to the teammate who asked, if it was not you).")
        try:
            start_task(root["id"], note)
            resumed = True
        except Exception as e:  # busy or repository missing: the answer is stored and shown in the task record
            log.info("question %s answered, root task %s not resumed: %s", qid, root["id"], e)
    return {"ok": True, "delivered": bool(waiter), "resumed": resumed}


USER_NOTE = ("The user wrote in the team discussion: {text}\nAnalyse it and act on it: answer, adjust the plan, "
             "create sub-tasks and delegate the work to the right teammates, or do it yourself.")

# Root task id -> messages from the user (note, images) not yet sent to its assignee, in order. A root task is
# in here (possibly with an empty list) while a request or a _deliver thread owns its queue: starting a message,
# waiting for the run to post the reply, or waiting for a busy assignee. Others only append.
_queues: dict[int, list[tuple[str, list[str]]]] = {}
_queues_lock = threading.Lock()


def post_user_message(pid: int, text: str, encoded: list[str], task_id: int | None) -> dict:
    """A message from the user in the project's team discussion. Without `task_id` it becomes a new task for
    the lead; otherwise it goes to the assignee of that task's root task, at once or, when that one is busy,
    after its current run (queued). Its answer is posted to the discussion as a reply.
    Returns {"entry_id", "task_id" (the root task), "started", "queued"}."""
    from . import images as img
    project = db.get_project(pid)
    if not project:
        raise TeamError("Project not found")
    text = (text or "").strip()
    files = img.decode(encoded)
    if not text and not files:
        raise TeamError("Write a message or attach an image")
    if not project["agent"]:
        raise TeamError("This project has no agents")
    if not Path(project["repo_path"]).is_dir():
        raise TeamError(f"The project repository {project['repo_path']} does not exist")
    if task_id is None:
        lead, ts = project["agent"], db.now()
        root_id = db.execute("INSERT INTO tasks(project_id, assignee_id, title, description, created_at, updated_at) "
                             "VALUES(?, ?, ?, ?, ?, ?)",
                             (pid, lead["id"], title_from(text) or "Message from the user", text, ts, ts))
        if files:
            db.update_task(root_id, {"images": img.task_save(root_id, files)})
        db.add_task_event(root_id, "user", f"Task created from the team discussion and assigned to {lead['name']}")
        message, attach = "", []   # the kickoff contains the description and its images
    else:
        task = db.get_task(task_id)
        if not task or task["project_id"] != pid:
            raise TeamError(f"Task #{task_id} is not a task of this project")
        root = root_of(task)
        root_id, lead = root["id"], assignee(project, root)
        message, attach = USER_NOTE.format(text=text or "(see the attached images)"), encoded
    did = discuss(db.get_task(root_id), "user", lead["name"], "instruction", text, files)
    with _queues_lock:
        queued = root_id in _queues   # earlier messages are starting or wait: this one goes after them
        _queues.setdefault(root_id, []).append((message, attach))
    if queued:
        return {"entry_id": did, "task_id": root_id, "started": False, "queued": True}
    # This request owns the root task's queue until _deliver hands it back
    try:
        run, dropped = _start_next(root_id)
    except Exception as e:
        _release(root_id, e)
        raise
    threading.Thread(target=_deliver, args=(root_id, run), name=f"discussion-{root_id}", daemon=True).start()
    return {"entry_id": did, "task_id": root_id, "started": run is not None, "queued": not run and not dropped}


def _start_next(root_id: int):
    """Start the assignee of a root task on its first queued message, unless it is busy. Only the queue's owner
    calls this, so the first message cannot change meanwhile. Returns (run or None, whether it was dropped)."""
    from . import runs
    with _queues_lock:
        message, attach = _queues[root_id][0]
    conv = task_conversation(root_id)
    if conv and runs.active_for(conv["id"]):
        return None, False
    try:
        run, _ = start_task(root_id, message, attach)
    except runs.RunBusy:
        return None, False
    except Exception as e:   # e.g. the task or its repository is gone: the entry stays in the discussion
        log.exception("discussion: delivering a message to task #%s failed", root_id)
        run = None
        root = db.get_task(root_id)
        if root:
            reason = str(e) if isinstance(e, TeamError) else f"{type(e).__name__}: {e}"
            discuss(root, assignee(db.get_project(root["project_id"]), root)["name"], "user", "reply",
                    f"(Not delivered: {reason})")
    with _queues_lock:
        _queues[root_id].pop(0)
    return run, run is None


def _deliver(root_id: int, run=None) -> None:
    """Post the reply to the `run` started for a message from the user, then send the root task's queued
    messages one after the other, each when the assignee's run has finished."""
    try:
        while True:
            if run:
                _post_reply(run, root_id)
            with _queues_lock:
                if not _queues[root_id]:
                    del _queues[root_id]
                    return
            run, dropped = _start_next(root_id)
            if not run and not dropped:
                time.sleep(0.5)
    except Exception as e:
        _release(root_id, e)


def _release(root_id: int, error: Exception) -> None:
    """The owner of a root task's queue failed: drop its waiting messages, so that later ones are delivered
    again, and say so in the discussion."""
    log.exception("discussion: the message queue of task #%s failed", root_id)
    with _queues_lock:
        waiting = len(_queues.pop(root_id, []))
    try:
        root = db.get_task(root_id)
        if root:
            discuss(root, assignee(db.get_project(root["project_id"]), root)["name"], "user", "reply",
                    f"(Not delivered: {type(error).__name__}: {error}"
                    + (f"; {waiting} waiting message(s) dropped" if waiting else "") + ")")
    except Exception:
        log.exception("discussion: could not note the failure on task #%s", root_id)


def _post_reply(run, root_id: int) -> None:
    """Wait for a run started by a message from the user, then post the assignee's answer to the discussion."""
    while not run.done:
        time.sleep(0.4)
    root = db.get_task(root_id)
    if not root:
        return
    after = next((e["message_id"] for e in run.events if e["type"] == "user_message"), None)
    reply = last_reply(run.conv_id, after)
    if not reply and run.status == "completed":
        return
    lead = assignee(db.get_project(root["project_id"]), root)
    discuss(root, lead["name"], "user", "reply", T.clip(reply, 3000) if reply else
            f"(No answer: the run {'was stopped' if run.status == 'cancelled' else 'failed'}.)")


LEARNING_WHERE = {"self": lambda a: f"for {a['name']} in this project", "team": lambda a: "for the whole project team",
                  "role": lambda a: f"for every agent based on @{a['template']}, in all projects"}


def save_learning(project: dict, agent: dict, task: dict, scope: str, text: str, author: str) -> str | None:
    """Record a lesson of `agent` (written by `author`). Returns the scope used ('role' becomes 'team' for an
    agent without a template), or None when the lesson is already recorded."""
    if scope == "role" and not agent["template"]:
        scope = "team"
    owner = {"self": ("agent_id", agent["id"]), "team": ("project_id", project["id"]),
             "role": ("template", agent["template"])}[scope]
    if db.one(f"SELECT 1 FROM learnings WHERE text = ? AND scope = ? AND {owner[0]} = ?", (text, scope, owner[1])):
        return None
    db.execute("INSERT INTO learnings(project_id, agent_id, template, scope, text, task_id, author, created_at) "
               "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
               (project["id"], agent["id"], agent["template"], scope, text, task["id"], author, db.now()))
    discuss(task, author, {"self": agent["name"], "team": "team", "role": f"every @{agent['template']}"}[scope],
            "learning", text)
    return scope


def record_learning(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    text = " ".join(args["lesson"].split())
    if not text:
        raise T.ToolError("The lesson is empty.")
    scope = save_learning(project, me, task, args["scope"], text, me["name"])
    if not scope:
        return "This lesson is already recorded."
    where = "for you in this project" if scope == "self" else LEARNING_WHERE[scope](me)
    return f"Lesson recorded {where}."


def post_to_discussion(run, spec, args: dict) -> str:
    from . import images as img
    project, me, task = _context(spec)
    text = (args.get("text") or "").strip()
    if not text:
        raise T.ToolError("The text is empty.")
    paths = args.get("images") or []
    if len(paths) > img.MAX_IMAGES:
        raise T.ToolError(f"At most {img.MAX_IMAGES} images.")
    files = []
    for ref in paths:
        p = Path(ref).expanduser()
        p = p if p.is_absolute() else Path(project["repo_path"]) / p
        if not p.is_file():
            raise T.ToolError(f"{ref}: no such file.")
        if p.stat().st_size > img.MAX_BYTES:
            raise T.ToolError(f"{ref} is larger than {img.MAX_BYTES / 1e6:.2f} MB.")
        data = p.read_bytes()
        media_type = img.sniff(data)
        if not media_type:
            raise T.ToolError(f"{ref} is not a PNG, JPEG, GIF or WebP image.")
        files.append((data, media_type))
    to = (args.get("to") or "team").strip()
    recipient = to.lower() if to.lower() in ("team", "user") else \
        find_member([a for a in project["agents"] if a["id"] != me["id"]], to)["name"]
    discuss(task, me["name"], recipient, "note", text, files)
    return "Posted to the team discussion" + (f" with {len(files)} image(s)." if files else ".")


HANDLERS = {
    "create_subtasks": create_subtasks, "delegate": delegate, "review_subtask": review_subtask,
    "list_templates": list_templates, "create_template": create_template, "add_team_member": add_team_member,
    "ask_agent": ask_agent, "ask_user": ask_user, "record_learning": record_learning,
    "post_to_discussion": post_to_discussion,
}


# ---------------------------------------------------------------- learnings in briefings

def learnings_for(agent: dict, since: str | None = None, limit: int = 30) -> list[dict]:
    """Lessons that apply to an agent: its own, its project team's and its template's (newest first)."""
    sql = ("SELECT * FROM learnings WHERE ((scope = 'self' AND agent_id = ?) OR (scope = 'team' AND project_id = ?) "
           "OR (scope = 'role' AND template != '' AND template = ?))")
    params: list = [agent["id"], agent["project_id"], agent["template"]]
    if since:
        sql += " AND created_at > ?"
        params.append(since)
    return db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))


def learnings_text(agent: dict, since: str | None = None) -> str:
    rows = learnings_for(agent, since)
    if not rows:
        return ""
    label = {"self": "your own", "team": "team", "role": "role"}
    lines = [f"- ({label[r['scope']]}) {r['text']}" for r in reversed(rows)]
    head = "New lessons recorded since your last briefing" if since else "Lessons learned in earlier work (apply them)"
    return f"{head}:\n" + "\n".join(lines)
