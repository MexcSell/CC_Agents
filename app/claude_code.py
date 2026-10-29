"""Claude Code engine: runs agents through the Claude Agent SDK, which drives the Claude Code CLI with
the login stored on this machine (~/.claude). Usage counts against the user's Claude plan; no API key
is needed.

Claude Code keeps the model-facing history in its own session, resumed by id on every run. This module
mirrors each message into the conversation's stored history (so the UI renders it like an API-engine
run) and forwards stream events to the browser in the same format as app/runs.py.
"""

import asyncio
import logging
import os
from collections import deque
from datetime import datetime
from pathlib import Path

import claude_agent_sdk as sdk
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, RateLimitEvent, ResultMessage, StreamEvent, SystemMessage,
                              TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage,
                              create_sdk_mcp_server, tool)

from . import db, fsops, images, maintenance, tools as T
from .agents import CLAUDE_CODE_NOTES, AgentSpec
from .config import DATA_DIR

log = logging.getLogger("agent_group.claude_code")

ENGINE = "claude_code"
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
INTERACTIVE_TOOLS = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]
TRANSCRIPT_LIMIT = 60_000
CHAT_DIR = DATA_DIR / "chat-workspace"

# Variables that tie a process to the Claude Code session it was started from. The server inherits
# them when it is launched from inside Claude Code; the agents' Claude Code processes must not.
for _var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
             "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
             "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "AI_AGENT"):
    os.environ.pop(_var, None)


class EngineError(Exception):
    """A failure whose message is shown to the user as-is."""
    user_facing = True


class _ResumeFailed(Exception):
    pass


def cli_path() -> str | None:
    """The CLI to run: AGENT_CLAUDE_CLI if set, otherwise the one bundled with the SDK."""
    return os.environ.get("AGENT_CLAUDE_CLI") or None


def status() -> dict:
    bundled = Path(sdk.__file__).parent / "_bundled" / "claude"
    cli = cli_path() or (str(bundled) if bundled.exists() else None)
    logged_in = (Path.home() / ".claude" / ".credentials.json").exists() or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    return {"available": bool(cli), "cli": cli, "logged_in": logged_in, "sdk_version": sdk.__version__}


def run(run, spec: AgentSpec) -> None:
    """Entry point, called on the run's worker thread."""
    asyncio.run(_Session(run, spec).execute())


# ---------------------------------------------------------------- prompt and options

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text", "").strip())


def _prompt(messages: list[dict], options: dict) -> tuple[str | list[dict], str | None]:
    """The prompt for this run and the session to resume.

    Messages the Claude Code session has not seen (an earlier stretch on the API engine, or a
    session that could not be resumed) are prepended as a plain-text transcript. A message with
    attached images becomes a list of content blocks: the images, then the text.
    """
    session = options.get("cc_session_id")
    seen = options.get("cc_last_message_id", 0) if session else 0
    last = messages[-1]["content"]
    text = _text_of(last)
    lines = [f"[{'User' if m['role'] == 'user' else 'Assistant'}]\n{t}"
             for m in messages[:-1] if m["id"] > seen and (t := _text_of(m["content"]).strip())]
    if lines:
        transcript = "\n\n".join(lines)[-TRANSCRIPT_LIMIT:]
        text = ("Earlier messages of this conversation, for context (they were exchanged outside this session):"
                f"\n\n{transcript}\n\n---\n\n{text}")
    pictures = [images.api_block(b) for b in last if b.get("type") == "image"] if isinstance(last, list) else []
    if pictures:
        return pictures + ([{"type": "text", "text": text}] if text.strip() else []), session
    return text, session


async def _user_message(content: list[dict]):
    """A prompt with content blocks, as the stream of one user message that ClaudeSDKClient.query accepts."""
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def _tool_text(text: str, is_error: bool) -> dict:
    result = {"content": [{"type": "text", "text": T.clip(text or "(no output)")}]}
    if is_error:
        result["is_error"] = True
    return result


def _mcp_tools(run, spec: AgentSpec) -> list:
    """Agent Group's own tools, served in-process to Claude Code as the MCP server "agent"."""
    from . import runs  # imported here: runs imports this module lazily

    def wrap(definition: dict, fn):
        @tool(definition["name"], definition["description"], definition["input_schema"])
        async def handler(args):
            problem = T.validate(definition, args)
            if problem:
                return _tool_text(f"Invalid input: {problem}", True)
            try:
                return _tool_text(await asyncio.to_thread(fn, args), False)
            except T.ToolError as e:
                return _tool_text(str(e), True)
            except Exception as e:
                log.exception("tool %s failed", definition["name"])
                return _tool_text(f"{type(e).__name__}: {e}", True)
        return handler

    defs = {d["name"]: d for d in T.TASK_TOOLS + T.DEV_TOOLS}
    if spec.kind == "task":
        return [wrap(defs["update_task"], lambda a: runs._update_task(run, spec.task_id, a))]
    if spec.kind == "dev":
        return [wrap(defs["restart_server"], lambda a: runs._restart(run, a)),
                wrap(defs["read_server_log"], lambda a: maintenance.tail_log(max(1, min(a.get("lines", 120), 2000))))]
    return []


def _gate(spec: AgentSpec):
    """Permission callback for tool calls Claude Code would otherwise ask the user about."""
    ws = spec.workspace

    async def gate(name, args, _context):
        if name in INTERACTIVE_TOOLS:
            return PermissionResultDeny(message="This session is not interactive. Ask the user in your reply instead.")
        if ws is None:
            return PermissionResultAllow()
        if name == "Bash" and not ws.allow_commands:
            return PermissionResultDeny(message="Shell commands are disabled in Agent Group's settings.")
        if name in WRITE_TOOLS:
            try:
                target = fsops.confine(ws.root, args.get(WRITE_TOOLS[name]) or "")
            except fsops.FsError:
                return PermissionResultDeny(message=f"Only files inside {ws.root} may be changed.")
            parts = target.resolve().relative_to(ws.root).parts
            if parts and parts[0] in ws.protected:
                return PermissionResultDeny(message=f"'{parts[0]}/' is protected and cannot be modified.")
        if ws.before_write and (name in WRITE_TOOLS or name == "Bash"):
            await asyncio.to_thread(ws.before_write)
        return PermissionResultAllow()

    return gate


def _options(run, spec: AgentSpec, conv: dict, resume: str | None, stderr: deque) -> ClaudeAgentOptions:
    def on_stderr(line: str):
        stderr.append(line.rstrip())
        log.info("claude-code: %s", line.rstrip())

    common = dict(model=spec.model, effort=spec.effort, include_partial_messages=True, can_use_tool=_gate(spec),
                  permission_mode="default", max_turns=spec.max_turns, resume=resume, cli_path=cli_path(),
                  stderr=on_stderr, disallowed_tools=INTERACTIVE_TOOLS)
    if spec.kind == "chat":
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(**common, cwd=str(CHAT_DIR), setting_sources=[],
                                  system_prompt=conv["system_prompt"],
                                  tools=["WebSearch", "WebFetch"] if spec.web_search else [])
    return ClaudeAgentOptions(
        **common, cwd=str(spec.workspace.root), setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": conv["system_prompt"] + CLAUDE_CODE_NOTES[spec.kind]},
        tools={"type": "preset", "preset": "claude_code"},
        agents={name: sdk.AgentDefinition(**d) for name, d in spec.subagents.items()} or None,
        mcp_servers={"agent": create_sdk_mcp_server("agent", tools=_mcp_tools(run, spec))})


# ---------------------------------------------------------------- content conversion

def _result_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Keep text; replace images and other payloads with a marker so history stays small.
    return [c if c.get("type") == "text" else {"type": "text", "text": f"[{c.get('type', 'content')}]"} for c in content]


def _block(b) -> dict | None:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "thinking": b.thinking}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        out = {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": _result_content(b.content)}
        if b.is_error:
            out["is_error"] = True
        return out
    if isinstance(b, sdk.ServerToolUseBlock):
        return {"type": "server_tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, sdk.ServerToolResultBlock):
        return {"type": "server_tool_result", "tool_use_id": b.tool_use_id, "content": b.content}
    return None


def _missing_session(text: str) -> bool:
    t = text.lower()
    return "conversation found" in t or ("session" in t and ("not found" in t or "no such" in t or "invalid" in t))


# ---------------------------------------------------------------- one run

class _Session:
    def __init__(self, run, spec: AgentSpec):
        from . import runs
        self.runs = runs
        self.run = run
        self.spec = spec
        self.conv = db.get_conversation(run.conv_id)
        self.notified: set[str] = set()

    async def execute(self) -> None:
        messages = db.list_messages(self.run.conv_id)
        prompt, resume = _prompt(messages, self.conv["options"])
        try:
            await self.attempt(prompt, resume)
        except _ResumeFailed:
            log.info("run %s: could not resume Claude Code session %s; starting a new one", self.run.id, resume)
            self.run.emit("notice", level="info", text="The previous Claude Code session could not be resumed. "
                          "Continuing in a new session that includes the conversation so far.")
            prompt, _ = _prompt(messages, {})
            await self.attempt(prompt, None)

    async def attempt(self, prompt: str | list[dict], resume: str | None) -> None:
        self.output = False        # stored any assistant output in this attempt
        self.started = False       # Claude Code accepted the prompt (init message seen)
        self.streamed = False      # stream events seen for the current assistant message
        self.json_bytes: dict[int, int] = {}
        self.usage_key = None      # id of the model response whose token usage is being tracked
        self.usage_row = None      # stored message that carries that usage
        self.usage: dict = {}
        stderr: deque = deque(maxlen=30)
        final: ResultMessage | None = None
        try:
            async with ClaudeSDKClient(options=_options(self.run, self.spec, self.conv, resume, stderr)) as client:
                await client.query(prompt if isinstance(prompt, str) else _user_message(prompt))
                consume = asyncio.create_task(self.consume(client))
                watcher = asyncio.create_task(self.watch_cancel(client, consume))
                try:
                    final = await consume
                except asyncio.CancelledError:
                    final = None
                finally:
                    watcher.cancel()
        except sdk.CLINotFoundError as e:
            raise EngineError("The Claude Code CLI was not found. Reinstall the dependencies (./start.sh) or set "
                              "AGENT_CLAUDE_CLI to the path of `claude`.") from e
        except sdk.ClaudeSDKError as e:
            detail = f"{e} {' '.join(stderr)}".strip()
            if resume and not self.output and _missing_session(detail):
                raise _ResumeFailed() from e
            if self.run.cancel.is_set():
                return
            raise EngineError(f"Claude Code failed: {detail[-1500:]}") from e
        finally:
            if self.started:
                last = db.one("SELECT MAX(id) AS id FROM messages WHERE conversation_id = ?", (self.run.conv_id,))
                db.update_conversation_options(self.run.conv_id, {"cc_last_message_id": last["id"] or 0})
        if self.run.cancel.is_set():
            return
        if final is None:
            raise EngineError("Claude Code ended without a result. " + " ".join(stderr)[-800:])
        if final.subtype == "error_max_turns":
            self.run.emit("notice", level="warn", text=f"Stopped after {final.num_turns} turns. "
                          "Send a message to let the agent continue.")
        elif final.is_error:
            text = final.result or "; ".join(final.errors or []) or final.subtype
            if resume and not self.output and _missing_session(text):
                raise _ResumeFailed()
            raise EngineError(f"Claude Code: {text}")

    async def consume(self, client) -> ResultMessage | None:
        final = None
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                final = msg
            else:
                self.handle(msg)
        return final

    async def watch_cancel(self, client, consume: asyncio.Task) -> None:
        while not self.run.cancel.is_set():
            await asyncio.sleep(0.2)
        try:
            await client.interrupt()
        except Exception:
            log.exception("interrupt failed")
        await asyncio.sleep(10)          # give Claude Code time to wind down, then stop waiting
        consume.cancel()

    # ------------------------------------------------------------ message handling

    def handle(self, msg) -> None:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self.started = True
                session = msg.data.get("session_id")
                if session:
                    db.update_conversation_options(self.run.conv_id, {"cc_session_id": session})
            return
        if isinstance(msg, RateLimitEvent):
            self.rate_limit(msg.rate_limit_info)
            return
        if getattr(msg, "parent_tool_use_id", None):
            return  # a sub-agent's internal messages; its final result arrives as a tool result
        if isinstance(msg, StreamEvent):
            self.relay(msg.event)
        elif isinstance(msg, AssistantMessage):
            self.assistant(msg)
        elif isinstance(msg, UserMessage):
            self.tool_results(msg)

    def relay(self, e: dict) -> None:
        """Forward a raw API stream event to the browser (same events as runs._relay)."""
        run, kind = self.run, e.get("type")
        if kind == "message_start":
            self.streamed = False
            self.json_bytes = {}
            message = e.get("message", {})
            self.track_usage(message.get("id"), message.get("usage"), model=message.get("model"))
            run.emit("turn_start", model=message.get("model", self.spec.model))
        elif kind == "message_delta":
            self.track_usage(self.usage_key, e.get("usage"))
        elif kind == "content_block_start":
            self.streamed = True
            block = e["content_block"]
            data = {"index": e["index"], "block_type": block["type"]}
            if block["type"] in ("tool_use", "server_tool_use"):
                data.update(id=block["id"], name=block["name"])
            run.emit("block_start", **data)
        elif kind == "content_block_delta":
            delta, index = e["delta"], e["index"]
            if delta["type"] == "text_delta":
                run.emit("text", index=index, text=delta["text"])
            elif delta["type"] == "thinking_delta":
                run.emit("thinking", index=index, text=delta["thinking"])
            elif delta["type"] == "input_json_delta":
                before = self.json_bytes.get(index, 0)
                after = before + len(delta["partial_json"])
                self.json_bytes[index] = after
                if after // 2048 != before // 2048:
                    run.emit("tool_input_progress", index=index, bytes=after)
        elif kind == "content_block_stop":
            run.emit("block_stop", index=e["index"])

    def assistant(self, msg: AssistantMessage) -> None:
        blocks = [b for b in map(_block, msg.content) if b]
        if not blocks:
            if msg.error:
                self.run.emit("notice", level="error", text=f"Claude Code reported an error: {msg.error}")
            return
        if not self.streamed:
            self.synthesize(blocks)
        with self.run.lock:
            mid = db.add_message(self.run.conv_id, "assistant", blocks, ENGINE)
            self.run.persisted()
        self.output = True
        self.track_usage(msg.message_id, msg.usage, mid, msg.model)
        for b in blocks:
            if b["type"] == "tool_use":
                self.run.emit("tool_start", id=b["id"], name=b["name"], input=self.runs._preview_input(b["input"]))

    def track_usage(self, key, raw, row: int | None = None, model: str | None = None) -> None:
        """Record a model response's token usage (and model) on the first stored message it produced.

        Claude Code stores one response as several assistant messages (one per content block) and
        reports usage on each of them and in the stream's message_start/message_delta events; all of
        these are snapshots of the same counts, merged here.
        """
        if key is None or key != self.usage_key:
            self.usage_key, self.usage_row, self.usage = key, None, {}
        self.runs.merge_usage(self.usage, raw)
        if model and self.usage:
            self.usage["model"] = model
        if self.usage_row is None:
            self.usage_row = row
        if self.usage_row is not None and self.usage:
            db.set_message_usage(self.usage_row, self.usage)
            self.run.emit("usage", message_id=self.usage_row, usage=dict(self.usage))

    def synthesize(self, blocks: list[dict]) -> None:
        """Render a message for which no stream events arrived."""
        self.run.emit("turn_start", model=self.spec.model)
        for i, b in enumerate(blocks):
            data = {"index": i, "block_type": b["type"]}
            if b["type"] in ("tool_use", "server_tool_use"):
                data.update(id=b["id"], name=b["name"])
            self.run.emit("block_start", **data)
            if b["type"] == "text":
                self.run.emit("text", index=i, text=b["text"])
            elif b["type"] == "thinking":
                self.run.emit("thinking", index=i, text=b["thinking"])
            self.run.emit("block_stop", index=i)
        self.streamed = True

    def tool_results(self, msg: UserMessage) -> None:
        if not isinstance(msg.content, list):
            return
        blocks = [b for b in map(_block, msg.content) if b and b["type"] == "tool_result"]
        if not blocks:
            return
        with self.run.lock:
            db.add_message(self.run.conv_id, "user", blocks, ENGINE)
            self.run.persisted()
        for b in blocks:
            text = _text_of(b["content"]) if isinstance(b["content"], list) else b["content"]
            self.run.emit("tool_result", tool_use_id=b["tool_use_id"], name="", is_error=bool(b.get("is_error")),
                          content=T.clip(text, self.runs.UI_PREVIEW_CHARS))

    def rate_limit(self, info) -> None:
        if info.status == "allowed" or info.status in self.notified:
            return
        self.notified.add(info.status)
        kind = (info.rate_limit_type or "usage").replace("_", " ")
        when = f" It resets {datetime.fromtimestamp(info.resets_at):%a %H:%M}." if info.resets_at else ""
        if info.status == "allowed_warning":
            used = ""
            if info.utilization is not None:
                pct = info.utilization if info.utilization > 1 else info.utilization * 100
                used = f" ({pct:.0f}% used)"
            self.run.emit("notice", level="warn", text=f"You are close to your Claude plan's {kind} limit{used}.{when}")
        else:
            self.run.emit("notice", level="error", text=f"Your Claude plan's {kind} limit has been reached.{when}")
