"""Client-side tools the agents can call, plus a small validator for their inputs.

Tool definitions are module constants so the `tools` array sent to the API is
byte-identical from request to request: the tool set is part of the prefix that
preserved thinking and prompt caching check. Behaviour that depends on settings
(e.g. shell access) is gated when a tool runs, never by changing the tool list.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import fsops

MAX_RESULT_CHARS = 60_000


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


FILE_TOOLS = [
    _tool("list_files",
          "List the directory tree under a path in the workspace (directories end with '/'). "
          "Heavy folders like .git and node_modules are shown but not expanded.",
          {"path": {"type": "string", "description": "Directory, relative to the workspace root. Default '.'"},
           "depth": {"type": "integer", "description": "How many levels to expand (1-6). Default 2."}},
          []),
    _tool("read_file",
          "Read a text file from the workspace. Output lines are prefixed with line numbers.",
          {"path": {"type": "string", "description": "File path, relative to the workspace root."},
           "start_line": {"type": "integer", "description": "First line to return (1-based). Optional."},
           "end_line": {"type": "integer", "description": "Last line to return, inclusive. Optional."}},
          ["path"]),
    _tool("write_file",
          "Create a file or overwrite it with the full new contents. Parent directories are created.",
          {"path": {"type": "string", "description": "File path, relative to the workspace root."},
           "content": {"type": "string", "description": "The complete file contents."}},
          ["path", "content"]),
    _tool("edit_file",
          "Replace an exact snippet of text in a file. `old_text` must match exactly once unless "
          "`replace_all` is true. Prefer this over write_file for small changes to large files.",
          {"path": {"type": "string"},
           "old_text": {"type": "string", "description": "Exact existing text, including whitespace."},
           "new_text": {"type": "string", "description": "Replacement text."},
           "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."}},
          ["path", "old_text", "new_text"]),
    _tool("make_directory",
          "Create a directory (and missing parents).",
          {"path": {"type": "string"}}, ["path"]),
    _tool("move_path",
          "Move or rename a file or directory. If `destination` is an existing directory, the source "
          "is moved inside it.",
          {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"]),
    _tool("delete_path",
          "Delete a file or a directory (recursively).",
          {"path": {"type": "string"}}, ["path"]),
    _tool("search_files",
          "Search file contents under a directory with a regular expression. Returns matching lines "
          "as path:line: text.",
          {"pattern": {"type": "string", "description": "Python regular expression."},
           "path": {"type": "string", "description": "Directory to search. Default '.'"},
           "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'."}},
          ["pattern"]),
    _tool("run_command",
          "Run a shell command (bash) with the workspace root as working directory. Returns exit code, "
          "stdout and stderr. Use for builds, tests, git, package managers. Do not start long-running "
          "servers in the foreground; they are killed at the timeout.",
          {"command": {"type": "string"},
           "timeout_seconds": {"type": "integer", "description": "1-600. Default 120."}},
          ["command"]),
]

TASK_TOOLS = [
    _tool("update_task",
          "Update the record of the task you are working on. Call it when you start (estimate, plan), "
          "whenever progress meaningfully changes, when an idea or insight emerges, and when you finish. "
          "Omit fields you are not changing.",
          {"status": {"type": "string", "enum": ["todo", "in_progress", "blocked", "review", "done"]},
           "progress": {"type": "integer", "description": "Percent complete, 0-100."},
           "estimated_hours": {"type": "number", "description": "Total estimated effort for the whole task, in hours."},
           "add_insights": {"type": "array", "items": {"type": "string"},
                            "description": "New ideas, discoveries or risks that emerged while working."},
           "next_actions": {"type": "array", "items": {"type": "string"},
                            "description": "The full, current list of upcoming action items (replaces the old list)."},
           "summary": {"type": "string", "description": "Short status summary of what has been done so far."},
           "log": {"type": "string", "description": "One-line activity log entry."}},
          []),
]

# Every project agent: collaborate with teammates and the user, and learn.
TEAM_TOOLS = [
    _tool("ask_agent",
          "Consult a teammate: ask another agent of your project team a question and get its answer. Use it to "
          "agree on interfaces, check an approach, get a quick review or draw on a teammate's expertise. The "
          "teammate can read the repository but does not change it. Ask specific questions.",
          {"agent": {"type": "string", "description": "The teammate's key (or name) from your team list."},
           "question": {"type": "string", "description": "The question, with the context the teammate needs."}},
          ["agent", "question"]),
    _tool("ask_user",
          "Ask the user a question and wait for the answer. Only for what only the user can decide or provide: "
          "requirements, preferences, access or credentials, permission for risky or irreversible steps. Do not "
          "ask about things you can find out yourself or decide reasonably; state your assumption instead. Your "
          "work pauses until the user answers.",
          {"question": {"type": "string", "description": "A clear, self-contained question."},
           "options": {"type": "array", "items": {"type": "string"},
                       "description": "Optional suggested answers the user can pick from."}},
          ["question"]),
    _tool("record_learning",
          "Record a lesson learned so it is available in future work: a convention of this repository, a pitfall "
          "and how to avoid it, a technique that worked, feedback from the user or a reviewer. One or two "
          "concrete sentences. Record lessons before you finish a task.",
          {"lesson": {"type": "string"},
           "scope": {"type": "string", "enum": ["self", "team", "role"],
                     "description": "self: for you in this project. team: for every agent of this project. role: "
                                    "for every agent based on your template, in all projects (general know-how)."}},
          ["lesson", "scope"]),
    _tool("post_to_discussion",
          "Share an opinion, a proposal, a status note or a result image (e.g. a screenshot or a chart) with the "
          "team and the user in the team discussion. It does not wait for answers; use ask_user or ask_agent for "
          "that.",
          {"text": {"type": "string", "description": "The message, in Markdown."},
           "images": {"type": "array", "items": {"type": "string"},
                      "description": "Optional PNG, JPEG, GIF or WebP files to attach (at most 8, each up to 3.75 "
                                     "MB): paths relative to the project repository, or absolute."},
           "to": {"type": "string", "description": "Who it is for: a teammate's key or name, 'user' or 'team'. "
                                                   "Default 'team'."}},
          ["text"]),
]

# Agents that others report to: plan, delegate and review.
LEAD_TOOLS = [
    _tool("create_subtasks",
          "Break your task down: create sub-tasks and assign each to a member of your team below you (see the "
          "team list). Give each a description with acceptance criteria. depends_on fixes the execution order; "
          "files lets delegate keep parallel work from editing the same files, so list them for every sub-task "
          "that changes code. One member can take several sub-tasks; they run as separate instances of it. "
          "Returns the sub-task ids; run them with delegate.",
          {"subtasks": {"type": "array", "items": {
              "type": "object",
              "properties": {"title": {"type": "string"},
                             "description": {"type": "string", "description": "What to do and the acceptance criteria."},
                             "assignee": {"type": "string", "description": "Key (or name) of the team member."},
                             "estimated_hours": {"type": "number"},
                             "depends_on": {"type": "array", "items": {"type": ["integer", "string"]},
                                            "description": "Sub-tasks that must finish before this one starts: ids "
                                                           "of existing sub-tasks of your task, or exact titles of "
                                                           "earlier items in this call."},
                             "files": {"type": "array", "items": {"type": "string"},
                                       "description": "Repository-relative files, folders or glob patterns this "
                                                      "sub-task creates or changes."}},
              "required": ["title", "description", "assignee"]}}},
          ["subtasks"]),
    _tool("delegate",
          "Have the assignees work on sub-tasks and wait for their reports. Give the whole set in one call: "
          "independent sub-tasks run in parallel; a sub-task starts only after its depends_on have finished and "
          "while no running sub-task shares its files. Call it again with a message to request changes, give "
          "feedback or pass on an answer.",
          {"task_ids": {"type": "array", "items": {"type": "integer"}},
           "message": {"type": "string", "description": "Instructions, feedback or answers for the assignees. "
                                                        "Optional the first time."}},
          ["task_ids"]),
    _tool("review_subtask",
          "Record your decision on a sub-task after reviewing its result: accept it (done), send it back for "
          "changes (revise; then delegate it again with your feedback), mark it blocked, or cancel it.",
          {"task_id": {"type": "integer"},
           "decision": {"type": "string", "enum": ["accept", "revise", "block", "cancel"]},
           "comment": {"type": "string", "description": "Reasoning or feedback for the assignee."},
           "lesson": {"type": "string", "description": "Optional reusable lesson for the assignee from this review "
                                                       "(a convention, a pitfall), recorded as its learning."},
           "lesson_scope": {"type": "string", "enum": ["self", "team", "role"],
                            "description": "self (default): the assignee's own lesson. team: for the whole project "
                                           "team. role: for every agent with the assignee's template."}},
          ["task_id", "decision"]),
]

# A teammate being consulted reads the repository but does not change it.
CONSULT_TOOLS = [t for t in FILE_TOOLS if t["name"] in ("list_files", "read_file", "search_files")]

_MEMBER = {"type": "object",
           "properties": {"key": {"type": "string", "description": "Short handle, unique in the group, e.g. 'backend-dev'."},
                          "template": {"type": "string", "description": "The member's template symbol, e.g. '@python-developer'."},
                          "name": {"type": "string", "description": "Display name, e.g. 'Senior Python Developer'."},
                          "parent": {"type": "string", "description": "Key of the member it reports to. Omit for the lead "
                                                                      "(exactly one member)."}},
           "required": ["key", "template"]}

# The setup agent of a new project: analyse it, staff its team, plan its tasks (app/project_setup.py).
SETUP_TOOLS = [
    _tool("list_templates",
          "List the agent templates (reusable agent profiles, referred to as @slug) with their area, language, role "
          "and expertise. Optionally filter by words that must all appear in a template.",
          {"query": {"type": "string", "description": "Optional filter words, e.g. 'python' or 'qa'."}},
          []),
    _tool("list_groups",
          "List the agent groups (#slug): hierarchical teams of templates with a lead. Shows each group's members and "
          "who reports to whom. Optionally filter by words that must all appear in a group.",
          {"query": {"type": "string", "description": "Optional filter words."}},
          []),
    _tool("create_template",
          "Create a new agent template for a role the project needs and no existing template covers. Returns its "
          "symbol. Do not duplicate existing templates.",
          {"name": {"type": "string", "description": "e.g. 'Rust Embedded Developer'."},
           "slug": {"type": "string", "description": "Optional symbol without '@'; derived from the name if omitted."},
           "category": {"type": "string", "description": "Library category, like those of the existing templates."},
           "area": {"type": "string", "description": "Functional area, like the existing templates' (e.g. developer, qa, "
                                                     "reviewer, lead, manager, devops, designer, writer)."},
           "language": {"type": "string", "description": "Programming language id if specific to one, e.g. 'rust'."},
           "description": {"type": "string"},
           "role": {"type": "string", "description": "One sentence: who the agent is."},
           "instructions": {"type": "string", "description": "How the agent works."},
           "rules": {"type": "array", "items": {"type": "string"}},
           "expertise": {"type": "array", "items": {"type": "string"}},
           "skills": {"type": "array", "items": {"type": "string"}}},
          ["name", "description", "role"]),
    _tool("create_group",
          "Create a new agent group (a team) when no existing group fits the project. Members are templates; exactly "
          "one member (the lead) has no parent and receives the project's tasks. Create missing templates first.",
          {"name": {"type": "string", "description": "e.g. 'Rust Embedded Team'."},
           "slug": {"type": "string", "description": "Optional symbol without '#'."},
           "category": {"type": "string"},
           "description": {"type": "string"},
           "members": {"type": "array", "items": _MEMBER}},
          ["name", "description", "members"]),
    _tool("set_project_team",
          "Make an agent group the project's team: its members become the project's agents (replacing the current "
          "ones). Call it once you have chosen or created the group, before creating tasks.",
          {"group": {"type": "string", "description": "The group's symbol, e.g. '#python-team'."}},
          ["group"]),
    _tool("save_project_brief",
          "Save the consolidated project analysis. It is shown to the user and included in every project agent's "
          "briefing, so write what the team needs to know: goal and scope, requirements, the repository's current "
          "state (stack, structure, what exists, what is missing or broken), constraints, risks and open questions. "
          "Markdown. Calling it again replaces the brief.",
          {"brief": {"type": "string"}},
          ["brief"]),
    _tool("create_tasks",
          "Create the project's tasks (they are not started). Each task goes to a team member: usually the lead, who "
          "breaks it down and delegates when the user runs it; a specialist directly for self-contained work. Order "
          "them in the sequence they should be done.",
          {"tasks": {"type": "array", "items": {
              "type": "object",
              "properties": {"title": {"type": "string"},
                             "description": {"type": "string", "description": "What to do, context and acceptance criteria."},
                             "assignee": {"type": "string", "description": "Key (or name) of a team member. Default: the lead."},
                             "estimated_hours": {"type": "number"},
                             "next_actions": {"type": "array", "items": {"type": "string"},
                                              "description": "First concrete steps."}},
              "required": ["title", "description"]}}},
          ["tasks"]),
]

# Leads staff their team for a task: find templates, create a missing one, add members below themselves.
LEAD_TOOLS += [t for t in SETUP_TOOLS if t["name"] in ("list_templates", "create_template")] + [
    _tool("add_team_member",
          "Add an agent built from a template to your team, reporting to you, when the task needs a specialist your "
          "team lacks. Find the template with list_templates first. You can then assign it sub-tasks with "
          "create_subtasks or consult it with ask_agent.",
          {"template": {"type": "string", "description": "The template's symbol, e.g. '@python-developer'."},
           "name": {"type": "string", "description": "Display name. Default: the template's name."},
           "key": {"type": "string", "description": "Short handle to address the member. Default: derived from the name."}},
          ["template"]),
]

WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10}

DEV_TOOLS = [
    _tool("restart_server",
          "Restart the Agent Group web server so code changes take effect. A preflight check "
          "(byte-compile and import of the app) runs first; if it fails, the restart is refused and "
          "the error is returned. The restart happens right after your current turn ends.",
          {"reason": {"type": "string", "description": "Why the restart is needed."}},
          ["reason"]),
    _tool("read_server_log",
          "Read the last lines of the web server log (stdout/stderr of the server process, "
          "including tracebacks).",
          {"lines": {"type": "integer", "description": "Number of lines from the end. Default 120."}},
          []),
]

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


# ---------------------------------------------------------------- validation

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate(tool_def: dict, args) -> str | None:
    """Return an error message if `args` does not match the tool's schema, else None.

    With eager input streaming the API does not validate tool input, and the SDK's
    tolerant parser can hand back a truncated object, so every call is checked here.
    """
    return _check_object(tool_def["input_schema"], args, "")


def _is(value, type_: str | list) -> bool:
    if isinstance(type_, list):   # one of several types, e.g. ["integer", "string"]
        return any(_is(value, t) for t in type_)
    if isinstance(value, bool) and type_ in ("integer", "number"):
        return False
    return isinstance(value, _TYPES[type_])


def _check_object(schema: dict, args, where: str) -> str | None:
    if not isinstance(args, dict):
        return f"{where or 'tool input'} must be a JSON object"
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required field '{where}{key}'"
    for key, value in args.items():
        spec = schema["properties"].get(key)
        name = f"{where}{key}"
        if spec is None:
            return f"unknown field '{name}'"
        if not _is(value, spec["type"]):
            return f"field '{name}' must be {spec['type']}"
        if spec["type"] == "array":
            items = spec.get("items", {"type": "string"})
            for i, v in enumerate(value):
                if not _is(v, items["type"]):
                    kinds = items["type"] if isinstance(items["type"], list) else [items["type"]]
                    return f"field '{name}' must be an array of {' or '.join(k + 's' for k in kinds)}"
                if items["type"] == "object":
                    problem = _check_object(items, v, f"{name}[{i}].")
                    if problem:
                        return problem
        if "enum" in spec and value not in spec["enum"]:
            return f"field '{name}' must be one of {spec['enum']}"
    return None


# ---------------------------------------------------------------- execution

class ToolError(Exception):
    pass


@dataclass
class Workspace:
    """Executes file and shell tools inside one root directory."""
    root: Path
    allow_commands: bool = True
    protected: tuple[str, ...] = ()
    before_write: Callable[[], None] | None = None
    changed: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.root = Path(self.root).resolve()

    def path(self, rel: str | None, must_write: bool = False) -> Path:
        try:
            p = fsops.confine(self.root, rel or ".")
        except fsops.FsError as e:
            raise ToolError(str(e))
        if must_write:
            rel_parts = p.resolve().relative_to(self.root.resolve()).parts
            if rel_parts and rel_parts[0] in self.protected:
                raise ToolError(f"'{rel_parts[0]}/' is protected and cannot be modified with file tools.")
            if self.before_write:
                self.before_write()
        return p

    def display(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root)) or "."
        except ValueError:
            return str(p)

    def run(self, name: str, args: dict) -> str:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            raise ToolError(f"unknown tool {name}")
        try:
            return handler(**args)
        except fsops.FsError as e:
            raise ToolError(str(e))
        except OSError as e:
            raise ToolError(f"{type(e).__name__}: {e}")

    def t_list_files(self, path: str = ".", depth: int = 2) -> str:
        p = self.path(path)
        if not p.is_dir():
            raise ToolError(f"Not a directory: {path}")
        return f"{p}/\n" + fsops.tree(p, depth=max(1, min(depth, 6)))

    def t_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        info = fsops.read_file(self.path(path))
        if info["binary"]:
            return f"{path} is a binary file ({info['size']} bytes); it cannot be shown as text."
        lines = info["content"].splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        out = [f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1)]
        header = f"{path} — lines {start}-{end} of {len(lines)}" if lines else f"{path} — empty file"
        return header + "\n" + "\n".join(out)

    def t_write_file(self, path: str, content: str) -> str:
        p = self.path(path, must_write=True)
        existed = p.exists()
        fsops.write_file(p, content)
        self.changed.append(self.display(p))
        return f"{'Updated' if existed else 'Created'} {self.display(p)} ({len(content.encode())} bytes)."

    def t_edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        p = self.path(path, must_write=True)
        info = fsops.read_file(p)
        if info["binary"]:
            raise ToolError("Cannot edit a binary file.")
        text = info["content"]
        count = text.count(old_text)
        if not old_text or count == 0:
            raise ToolError("old_text was not found in the file. Read the file and copy the text exactly.")
        if count > 1 and not replace_all:
            raise ToolError(f"old_text matches {count} times; add surrounding context or set replace_all.")
        text = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
        fsops.write_file(p, text)
        self.changed.append(self.display(p))
        return f"Edited {self.display(p)} ({count if replace_all else 1} replacement(s))."

    def t_make_directory(self, path: str) -> str:
        p = self.path(path, must_write=True)
        fsops.make_dir(p)
        return f"Created directory {self.display(p)}/"

    def t_move_path(self, source: str, destination: str) -> str:
        src = self.path(source, must_write=True)
        dst = self.path(destination, must_write=True)
        moved = fsops.move(src, dst, self.root)
        self.changed.append(self.display(Path(moved["path"])))
        return f"Moved {self.display(src)} -> {self.display(Path(moved['path']))}"

    def t_delete_path(self, path: str) -> str:
        p = self.path(path, must_write=True)
        fsops.delete(p, self.root)
        self.changed.append(self.display(p))
        return f"Deleted {self.display(p)}"

    def t_search_files(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}")
        base = self.path(path)
        hits: list[str] = []
        files = base.rglob(glob or "*") if base.is_dir() else [base]
        for f in files:
            if len(hits) >= 200:
                hits.append("... (more matches truncated)")
                break
            if any(part in fsops.SKIP_DIRS for part in f.relative_to(self.root).parts[:-1]) or not f.is_file():
                continue
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if fsops.is_binary(data):
                continue
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.display(f)}:{n}: {line.strip()[:240]}")
                    if len(hits) >= 200:
                        break
        return "\n".join(hits) if hits else "No matches."

    def t_run_command(self, command: str, timeout_seconds: int = 120) -> str:
        if not self.allow_commands:
            raise ToolError("Shell commands are disabled in Settings (Allow agents to run commands).")
        timeout = max(1, min(timeout_seconds, 600))
        try:
            proc = subprocess.run(["bash", "-lc", command], cwd=self.root, capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace")
            return f"Timed out after {timeout}s.\nstdout (partial):\n{clip(out, 20_000)}"
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        return (f"exit code: {proc.returncode}\n--- stdout ---\n{clip(out, 30_000)}"
                f"\n--- stderr ---\n{clip(err, 15_000)}")


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} characters omitted] ...\n\n{text[-half:]}"


def format_args(args) -> str:
    return json.dumps(args, ensure_ascii=False)[:2000]
