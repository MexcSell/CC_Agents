"""Team work: the agents of a project collaborate on its tasks.

A task the user adds goes to the project's lead. An agent with reports (a lead or manager) breaks its
task into sub-tasks for the agents below it (create_subtasks), runs them (delegate: each assignee works
on its sub-task in its own conversation, several in parallel, and the lead waits for their reports),
reviews the results (review_subtask) and integrates them. Every agent can consult a teammate
(ask_agent: a read-only conversation with that teammate), ask the user (ask_user: the run pauses until
the user answers in the UI) and record lessons learned (record_learning), which are fed into its later
briefings. Delegations, reports, reviews, consultations, questions and lessons are kept as the root
task's team discussion.

The tool handlers run on the calling agent's run thread and block while teammates work; cancelling
that run cancels the runs it is waiting for.
"""

import json
import logging
import threading
import time
from pathlib import Path

from . import db, tools as T

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


def task_conversation(tid: int) -> dict | None:
    row = db.one("SELECT id FROM conversations WHERE task_id = ? AND kind = 'task' ORDER BY id DESC LIMIT 1", (tid,))
    return db.get_conversation(row["id"]) if row else None


def last_reply(conv_id: int) -> str:
    for m in reversed(db.list_messages(conv_id)):
        if m["role"] != "assistant":
            continue
        content = m["content"]
        text = content if isinstance(content, str) else "\n\n".join(
            b["text"] for b in content if b.get("type") == "text" and b.get("text", "").strip())
        if text.strip():
            return text.strip()
    return ""


def discuss(task: dict, sender: str, recipient: str, kind: str, text: str) -> None:
    db.execute("INSERT INTO discussions(root_task_id, task_id, sender, recipient, kind, text, created_at) "
               "VALUES(?, ?, ?, ?, ?, ?, ?)", (root_of(task)["id"], task["id"], sender, recipient, kind, text, db.now()))


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
        content = agents.task_kickoff_message(project, agent, task, note)
    else:
        content = agents.task_followup_message(project, agent, task, note, conv)
    if images:
        content = img.save(conv["id"], images) + [{"type": "text", "text": content}]
    if task["status"] == "todo":
        db.update_task(tid, {"status": "in_progress"})
    run = runs.start(conv["id"], "task", conv["title"], content, agents.task_spec(project, agent, tid), task_id=tid)
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

def create_subtasks(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    team = reports_of(project["agents"], me["id"])
    if not team:
        raise T.ToolError("Nobody reports to you, so you cannot delegate. Do the work yourself.")
    created = []
    ts = db.now()
    for item in args["subtasks"]:
        title = (item.get("title") or "").strip()
        if not title:
            raise T.ToolError("Every sub-task needs a title.")
        who = find_member(team, item.get("assignee", ""))
        hours = item.get("estimated_hours")
        tid = db.execute(
            "INSERT INTO tasks(project_id, parent_id, assignee_id, title, description, estimated_hours, created_at, "
            "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (project["id"], task["id"], who["id"], title, item.get("description", ""),
             float(hours) if isinstance(hours, (int, float)) and hours > 0 else None, ts, ts))
        db.add_task_event(tid, "agent", f"Created by {me['name']} as part of #{task['id']}, assigned to {who['name']}")
        created.append((tid, title, who))
    lines = [f"- #{tid} {title} → {who['name']} [{who['key']}]" for tid, title, who in created]
    db.add_task_event(task["id"], "agent", f"{me['name']} created {len(created)} sub-task(s)")
    discuss(task, me["name"], "team", "plan", "Work breakdown:\n" + "\n".join(lines))
    run.emit("task_update", task=db.get_task(task["id"]))
    return "Created sub-tasks:\n" + "\n".join(lines) + "\nRun them with delegate."


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
    message = (args.get("message") or "").strip()
    limit = max(1, int(db.get_settings()["max_parallel_agents"]))
    pending, active, failed = list(subs), {}, {}
    while pending or active:
        if run.cancel.is_set():
            for child in active.values():
                child.cancel.set()
            return "Stopped: the run was cancelled while sub-tasks were running."
        while pending and len(active) < limit:
            sub = pending.pop(0)
            who = team[sub["assignee_id"]]
            try:
                child, _ = start_task(sub["id"], message, sender=me)
            except runs.RunBusy:
                conv = task_conversation(sub["id"])
                child = runs.active_for(conv["id"]) if conv else None
                if child is None:
                    failed[sub["id"]] = "could not be started"
                    continue
            except TeamError as e:
                failed[sub["id"]] = str(e)
                continue
            active[sub["id"]] = child
            discuss(sub, me["name"], who["name"], "delegate", message or f"Please carry out #{sub['id']}: {sub['title']}")
            run.emit("notice", level="info", text=f"→ {who['name']} is working on #{sub['id']} {sub['title']}")
        for tid, child in list(active.items()):
            if child.done:
                del active[tid]
                if child.status == "error":
                    failed[tid] = "the agent's run failed (see its work log)"
                who = team[db.get_task(tid)["assignee_id"]]
                run.emit("notice", level="info", text=f"← {who['name']} finished #{tid}")
        time.sleep(0.4)

    reports = []
    for sub in subs:
        sub = db.get_task(sub["id"])
        who = team[sub["assignee_id"]]
        conv = task_conversation(sub["id"])
        reply = last_reply(conv["id"]) if conv else ""
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
            "or send it back with review_subtask.\n\n" + "\n\n".join(reports))


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
    return f"#{sub['id']} is now {fields['status']}.{then}"


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


def record_learning(run, spec, args: dict) -> str:
    project, me, task = _context(spec)
    scope, text = args["scope"], " ".join(args["lesson"].split())
    if not text:
        raise T.ToolError("The lesson is empty.")
    if scope == "role" and not me["template"]:
        scope = "team"
    if db.one("SELECT 1 FROM learnings WHERE text = ? AND scope = ? AND "
              "(agent_id = ? OR project_id = ? OR template = ?)", (text, scope, me["id"], project["id"], me["template"])):
        return "This lesson is already recorded."
    db.execute("INSERT INTO learnings(project_id, agent_id, template, scope, text, task_id, author, created_at) "
               "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
               (project["id"], me["id"], me["template"], scope, text, task["id"], me["name"], db.now()))
    discuss(task, me["name"], {"self": me["name"], "team": "team", "role": f"every @{me['template']}"}[scope],
            "learning", text)
    where = {"self": "for you in this project", "team": "for the whole project team",
             "role": f"for every agent based on @{me['template']}, in all projects"}[scope]
    return f"Lesson recorded {where}."


HANDLERS = {
    "create_subtasks": create_subtasks, "delegate": delegate, "review_subtask": review_subtask,
    "ask_agent": ask_agent, "ask_user": ask_user, "record_learning": record_learning,
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
