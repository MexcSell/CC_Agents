# Agent Group

A web workspace where Claude agents work for you:

- **Chat**: create topics, hold conversations with Claude about them, keep or delete the history.
- **Projects**: register a project with its repository folder and an agent group: a hierarchical team of agents. Tasks go to the team's lead, who breaks them into sub-tasks, delegates them down the team, reviews the results and keeps estimate, progress, insights and next actions up to date. Agents consult each other, ask you when only you can decide, and record lessons that improve their later work.
- **Templates & groups**: reusable agent profiles (role, areas of expertise, skills, rules), classified by functional area and programming language, each with a symbol such as `@python-developer`; and agent groups, hierarchical teams of templates such as `#python-team`. 74 starter templates (developer, tech lead and QA for 13 languages, plus managers, designers, DevOps, security and more) and 23 starter groups are installed. In the template editor, every field suggests candidates that depend on the fields above it.
- **Files**: browse the whole file system or a project's repository. Create, open, edit, rename, move (drag and drop) and delete files and folders.
- **Developer**: an agent that develops and modifies this website's own code and restarts it. Code snapshots and automatic rollback keep a broken change from taking the site down.

## Quick start

```bash
./start.sh                # runs in the foreground (Ctrl+C to stop)
./start.sh --background   # detached; stop it with ./stop.sh
```

Open <http://127.0.0.1:8765>. By default the agents run on **Claude Code** with the Claude login on this
machine, so they use your Claude subscription and need no API key. If Claude Code isn't logged in yet, run
`claude` in a terminal on the server and log in. The first run creates `.venv/` and installs the dependencies.

## Engines

Choose the engine under **Settings → Engine**:

| Engine | Billing | How it works |
|---|---|---|
| **Claude Code** (default) | your Claude plan's usage limits | `app/claude_code.py` drives Claude Code through the Claude Agent SDK, using the login in `~/.claude` of the user running the server. Agents use Claude Code's tools (Read, Write, Edit, Bash, …) plus Agent Group's own tools, served in-process over MCP |
| **Anthropic API key** | per token, to the key in Settings or `ANTHROPIC_API_KEY` | `app/runs.py` runs its own streaming tool-use loop on the Messages API |

With Claude Code, the agents share your plan's limits with your own Claude Code sessions. A long task can use
up the current window, and the conversation then shows a notice when you are close to the limit or have hit it.
A subscription is for your personal use: if other people will use the site, switch to an API key.

You can switch engines at any time, including for existing conversations. Turns the other engine produced
are carried over as text.

If you work through VS Code Remote or SSH, forward port 8765 to your machine. VS Code usually does this automatically.

## How the requirements map to the app 

| Requirement | Where |
|---|---|
| Launch a website | `./start.sh` serves the app on port 8765 under a supervisor process |
| Agent that develops, modifies and restarts the website | **Developer** page: file tools on the app's source, `run_command`, `restart_server` (preflight-checked), `read_server_log` |
| Agent that converses with Claude | **Chat** page, streaming replies with visible reasoning summaries |
| Register topics, converse by topic, store or delete history | **Chat**: topics → conversations. History is stored in SQLite. Delete one conversation, clear a topic's history, delete a topic, or export a conversation as Markdown |
| Register projects | **Projects → Register project** |
| Every project has an agent that is assigned tasks and carries them out | Each project has a team (from an agent group, or a single agent). Tasks go to the lead (or an agent you choose); **Run agent** starts the work |
| Per task: total estimated time, progress, emergent ideas/insights, upcoming action items | Task panel: estimated total time, status and progress %, agent time spent, insights, next actions and an activity log. The agent updates them through its `update_task` tool, and you can edit them too |
| Specify the repository a project works with | Repository folder field when registering (with a folder picker and an option to create the folder). The agent's tools are confined to it |
| View and manage the repository's file structure | Project → **Repository files** tab |
| Browse the entire file structure with full file operations | **Files** page (rooted at `/`) |

## The agents

Each agent's model and effort level can be changed in **Settings** (project agents: on the project's
*Agent & settings* tab). The default model is Claude Opus 5.5.

| Agent | Workspace | Tools |
|---|---|---|
| Chat | none | optional web search (Settings) |
| Developer | this app's directory (`data/`, `.venv/`, `.git/` are off-limits) | file tools, search, shell, restart server, read server log |
| Project agent | the project's repository | file tools, search, shell, `update_task` |

A project agent can be based on an agent template (`@slug`): the template's rules, expertise, skills and
instructions are added to the agent's system prompt, and its default model and effort are taken over when you
pick it. Templates listed as the agent's *internal agents* (its own plus those of its template) become Claude
Code sub-agents that the agent can delegate to through the Agent tool. The API engine has no sub-agents and
ignores them. Renaming a template's symbol updates every reference to it, and deleting a template removes them.

The starter library (`app/template_presets.py`) covers planning and research, engineering, experience and art,
quality and safety, delivery and operations, and data and analytics. It is installed once, the first time the
server starts with a database. Deleted starter templates stay deleted; **＋ Starter templates** on the Templates
page adds back any that are missing. In the template editor, the server ranks candidates for each field
(`POST /api/templates/suggest`) from the fields above it, in the order category → name → description → role →
expertise → skills → rules → internal agents → instructions. The ranking draws on the starter library and on your
own templates. **✨ Fill empty fields** copies the best-matching profile into the fields that are still empty.

On both engines, file writes are confined to the agent's workspace. On the Claude Code engine, Claude Code
may still run read-only shell commands (such as `ls`) while shell commands are switched off.

Runs continue in the background if you close the browser. Reopening the conversation resumes the live view.
The **Stop** button cancels a run.

The refusal fallback is on by default: if a safety classifier declines a request, the API retries it on a
recommended fallback model. It doesn't apply to Haiku, and you can turn it off in Settings.

## Teams

A project's agents form a hierarchy. The agent at the top, the lead, receives the tasks you add (you can also
assign a task to any agent directly). An agent with reports gets extra tools:

| Tool | What it does |
|---|---|
| `create_subtasks` | splits its task into sub-tasks, each assigned to an agent below it |
| `delegate` | starts the assignees on sub-tasks (several in parallel, up to *Agents working in parallel per lead* in Settings) and waits for their reports |
| `review_subtask` | accepts a result, sends it back for changes, blocks or cancels it |

Every project agent also has:

| Tool | What it does |
|---|---|
| `ask_agent` | consults a teammate, who answers in a read-only session |
| `ask_user` | asks you a question; the agent waits until you answer on the task page or the dashboard (the sidebar shows open questions) |
| `record_learning` | stores a lesson for itself, for the project team, or for every agent based on its template in all projects |

Lessons are added to each agent's briefings, and the project's **Knowledge** tab lists them. Delegations,
reports, reviews, consultations, questions and lessons appear in the task's **Team discussion**. Each sub-task
has its own work log. Stopping a lead's run also stops the runs it is waiting for. If you answer a question
after the asking run has ended (for example after a restart), the lead of the top-level task is started with
your answer.

## Restarts and recovery

- `supervisor.py` runs the web server as a child process. A restart (from the Developer agent, the
  Developer page, or after restoring a snapshot) makes the server exit with code 3, and the supervisor starts it again.
- Before every restart, a preflight check byte-compiles the app and imports it in a fresh interpreter. If
  that fails, the restart is refused and the error is shown.
- A snapshot of the source (`data/snapshots/*.tar.gz`) is taken before the Developer agent's first change
  in each run. You can also create, restore and delete snapshots by hand on the Developer page.
- If a restarted server crashes three times within 20 seconds of starting, the supervisor restores the most recent
  snapshot by itself.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_PORT` | `8765` | HTTP port |
| `AGENT_HOST` | `127.0.0.1` | Bind address |
| `AGENT_TOKEN` | *(empty)* | Shared secret required for every API call; **mandatory** if `AGENT_HOST` is not loopback |
| `AGENT_DATA_DIR` | `./data` | Database, logs and snapshots |
| `ANTHROPIC_API_KEY` | | API engine: used when no key is saved in Settings |
| `AGENT_CLAUDE_CLI` | bundled | Claude Code engine: path of the `claude` executable to use |

Example of exposing the app on the LAN: `AGENT_HOST=0.0.0.0 AGENT_TOKEN=$(openssl rand -hex 16) ./start.sh`.
Open the site, and enter the token when prompted.

## Security notes

This app can read, write and execute anything the account running it can. That's how the file browser
and the agents' shell tools work. Keep it on localhost (the default) or behind `AGENT_TOKEN`, and
preferably run it as an unprivileged user rather than root. The file APIs refuse to delete or move system
top-level directories (`/etc`, `/usr`, …) and the Agent Group installation itself. Agents can run shell
commands only while **Allow agents to run shell commands** is on in Settings.

## Layout

```
start.sh / stop.sh        start (creates the venv) / stop a background instance
supervisor.py             keeps the server running, handles restarts and rollback (stdlib only)
app/server.py             FastAPI app: REST API, SSE event streams, static files
app/runs.py               run manager and the API engine's streaming tool-use loop
app/claude_code.py        the Claude Code engine (Claude Agent SDK)
app/agents.py             system prompts and tool sets of the three agent types
app/templates.py          agent templates (@symbols), context-dependent editor suggestions, internal agents
app/template_presets.py   the built-in templates (areas, languages), categories and agent groups
app/groups.py             agent groups (#symbols): hierarchical teams of templates
app/team.py               team work: sub-tasks, delegation, consultations, questions, lessons, discussion
app/tools.py              tool definitions and executors
app/fsops.py              filesystem operations (file browser and file tools)
app/maintenance.py        snapshots, preflight, restart, server log
app/db.py                 SQLite schema and helpers
static/                   frontend: plain ES modules, no build step
data/                     database, logs, snapshots (created at runtime; not in git)
```

Conversation history is stored append-only, exactly as the API returned it. That includes thinking blocks,
which must be sent back unchanged. Each conversation's system prompt is frozen when the conversation is created.
Later edits to a topic or an agent reach existing conversations through the next message.
