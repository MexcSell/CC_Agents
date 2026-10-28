# Agent Group

A web workspace where Claude agents work for you:

- **Chat**: create topics, hold conversations with Claude about them, keep or delete the history.
- **Projects**: register a project with its repository folder. Each project gets its own agent. Assign tasks to the agent and it carries them out, keeping an estimate, progress, insights and next actions up to date.
- **Files**: browse the whole file system or a project's repository. Create, open, edit, rename, move (drag and drop) and delete files and folders.
- **Developer**: an agent that develops and modifies this website's own code and restarts it. Code snapshots and automatic rollback keep a broken change from taking the site down.

## Quick start

```bash
./start.sh                # runs in the foreground (Ctrl+C to stop)
./start.sh --background   # detached; stop it with ./stop.sh
```

Open <http://127.0.0.1:8765>, go to **Settings** and paste an Anthropic API key. Alternatively, export
`ANTHROPIC_API_KEY` before starting. The first run creates `.venv/` and installs the dependencies.

If you work through VS Code Remote or SSH, forward port 8765 to your machine. VS Code usually does this automatically.

## How the requirements map to the app

| Requirement | Where |
|---|---|
| Launch a website | `./start.sh` serves the app on port 8765 under a supervisor process |
| Agent that develops, modifies and restarts the website | **Developer** page: file tools on the app's source, `run_command`, `restart_server` (preflight-checked), `read_server_log` |
| Agent that converses with Claude | **Chat** page, streaming replies with visible reasoning summaries |
| Register topics, converse by topic, store or delete history | **Chat**: topics → conversations. History is stored in SQLite. Delete one conversation, clear a topic's history, delete a topic, or export a conversation as Markdown |
| Register projects | **Projects → Register project** |
| Every project has an agent that is assigned tasks and carries them out | Each project has one agent (name, role, instructions, model, effort). Every task in the project is assigned to it; **Run agent** starts the work |
| Per task: total estimated time, progress, emergent ideas/insights, upcoming action items | Task panel: estimated total time, status and progress %, agent time spent, insights, next actions and an activity log. The agent updates them through its `update_task` tool, and you can edit them too |
| Specify the repository a project works with | Repository folder field when registering (with a folder picker and an option to create the folder). The agent's tools are confined to it |
| View and manage the repository's file structure | Project → **Repository files** tab |
| Browse the entire file structure with full file operations | **Files** page (rooted at `/`) |

## The agents

All agents use the Anthropic Messages API with streaming, adaptive thinking and a tool-use loop
(`app/runs.py`). Each agent's model and effort level can be changed in **Settings** (project agents: on the
project's *Agent & settings* tab). The default model is Claude Opus 5.5.

| Agent | Workspace | Tools |
|---|---|---|
| Chat | none | optional web search (Settings) |
| Developer | this app's directory (`data/`, `.venv/`, `.git/` are off-limits) | list/read/write/edit/move/delete files, search, shell, restart server, read server log |
| Project agent | the project's repository | list/read/write/edit/move/delete files, search, shell, `update_task` |

Runs continue in the background if you close the browser. Reopening the conversation resumes the live view.
The **Stop** button cancels a run.

The refusal fallback is on by default: if a safety classifier declines a request, the API retries it on a
recommended fallback model. It doesn't apply to Haiku, and you can turn it off in Settings.

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
| `ANTHROPIC_API_KEY` | | Used when no key is saved in Settings |

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
app/runs.py               run manager and streaming Claude tool-use loop
app/agents.py             system prompts and tool sets of the three agent types
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
