"""Agent runs: a background thread per run drives the streaming Claude tool-use loop and
records events that browsers read over Server-Sent Events.

History is append-only. Every assistant turn is stored exactly as returned (including
thinking blocks), and every turn that contains tool calls is followed by a stored user
turn with one tool_result per call, so the history is always valid to send back.
"""

import json
import logging
import threading
import time
import traceback
import uuid

import anthropic

from . import db, images, maintenance, project_setup, team, tools as T
from .agents import AgentSpec
from .config import FALLBACK_MODELS

log = logging.getLogger("agent_group.runs")

MAX_TOKENS = 64000
UI_PREVIEW_CHARS = 4000
ACTIVITY_CHARS = 300          # per string in the dashboard's activity
ACTIVITY_INPUT_CHARS = 1500   # whole tool input in the dashboard's activity (about)
ACTIVITY_LIST_ITEMS = 10      # per list in the dashboard's activity
TIMED_EVENTS = {"turn_start", "message_start", "block_start", "tool_start"}   # these carry a `ts`


class RunBusy(Exception):
    pass


class Run:
    def __init__(self, conv_id: int, kind: str, title: str, task_id: int | None):
        self.id = uuid.uuid4().hex[:12]
        self.conv_id = conv_id
        self.kind = kind
        self.title = title
        self.task_id = task_id
        self.events: list[dict] = []
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.status = "running"
        self.started = time.time()
        self.finished: float | None = None
        # Index of the first event not yet reflected in stored messages. A browser that loads
        # the conversation mid-run renders stored messages and replays events from here.
        self.resume_from = 0
        self.restart_after = False

    @property
    def done(self) -> bool:
        return self.status != "running"

    def emit(self, type_: str, **data) -> None:
        with self.lock:
            data["type"] = type_
            data["seq"] = len(self.events)
            if type_ in TIMED_EVENTS:
                data["ts"] = time.time()
            self.events.append(data)

    def persisted(self) -> None:
        with self.lock:
            self.resume_from = len(self.events)

    def info(self) -> dict:
        with self.lock:
            return {"id": self.id, "conversation_id": self.conv_id, "kind": self.kind, "title": self.title,
                    "task_id": self.task_id, "status": self.status, "started": self.started,
                    "finished": self.finished, "resume_from": self.resume_from, "event_count": len(self.events)}

    def activity(self) -> dict | None:
        """What the agent is doing right now (for the dashboard): the latest open tool call, else the
        block being written in the current turn, else None.

        Scans back to the current turn's start only: both engines run tools between a response and
        the next one, so a tool still open before the latest turn_start is stale.
        """
        with self.lock:
            closed, started, texts, block, turn_over = set(), set(), [], None, False
            for e in reversed(self.events):
                t = e["type"]
                if t == "tool_result":
                    closed.add(e["tool_use_id"])
                elif t == "tool_start":
                    if e["id"] not in closed:
                        return {"type": "tool", "name": e["name"], "input": _activity_input(e.get("input")),
                                "since": e.get("ts")}
                    started.add(e["id"])
                elif t in ("turn_start", "message_start"):
                    break
                elif block or turn_over:
                    continue
                elif t in ("turn_end", "turn_discarded"):
                    turn_over = True
                elif t == "block_start":
                    block = e
                elif t == "text":
                    texts.append(e)
        if not block:
            return None
        kind, since = block["block_type"], block.get("ts")
        if kind in ("tool_use", "server_tool_use"):
            if block.get("id") in started:
                return None
            return {"type": "tool", "name": block.get("name", ""), "input": None, "since": since}
        if kind == "text":
            text = "".join(e["text"] for e in reversed(texts) if e["index"] == block["index"])
            return {"type": "text", "text": text.strip()[-ACTIVITY_CHARS:].strip(), "since": since}
        if kind in ("thinking", "redacted_thinking"):
            return {"type": "thinking", "since": since}
        return None


def _activity_input(args) -> dict | None:
    """A tool input trimmed for the dashboard, keeping its types: strings to ACTIVITY_CHARS + '…',
    lists to ACTIVITY_LIST_ITEMS items (then a string item '… N more'), and once about
    ACTIVITY_INPUT_CHARS are used, further list items / dict fields dropped the same way."""
    return _trim(args, [ACTIVITY_INPUT_CHARS]) if isinstance(args, dict) else None


def _trim(v, budget: list[int]):
    """Trim v recursively; budget[0] is the number of characters left for the whole input."""
    if isinstance(v, str):
        v = v[:ACTIVITY_CHARS] + "…" if len(v) > ACTIVITY_CHARS else v
        budget[0] -= len(v) + 2
        return v
    if isinstance(v, list):
        out = []
        for item in v[:ACTIVITY_LIST_ITEMS]:
            if budget[0] <= 0:
                break
            out.append(_trim(item, budget))
        if len(v) > len(out):
            out.append(f"… {len(v) - len(out)} more")
        return out
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if budget[0] <= 0:
                out["…"] = f"{len(v) - len(out)} more fields"
                break
            k = str(k)[:ACTIVITY_CHARS]
            budget[0] -= len(k) + 4
            out[k] = _trim(x, budget)
        return out
    budget[0] -= len(json.dumps(v, default=str))
    return v


_runs: dict[str, Run] = {}
_active: dict[int, str] = {}   # conversation id -> run id
_registry = threading.Lock()


def get(run_id: str) -> Run | None:
    return _runs.get(run_id)


def active_for(conv_id: int) -> Run | None:
    run = _runs.get(_active.get(conv_id, ""))
    return run if run and not run.done else None


def active_runs(activity: bool = False) -> list[dict]:
    """Running runs' info; with activity=True each also gets `activity` (see Run.activity)."""
    return [{**r.info(), "activity": _safe_activity(r)} if activity else r.info()
            for r in list(_runs.values()) if not r.done]


def _safe_activity(run: Run) -> dict | None:
    """Run.activity(), but odd tool input (e.g. nesting deep enough for RecursionError) must not break the dashboard."""
    try:
        return run.activity()
    except Exception:
        log.exception("activity of run %s", run.id)
        return None


def recent_runs() -> list[dict]:
    return [r.info() for r in sorted(_runs.values(), key=lambda r: r.started, reverse=True)[:30]]


def start(conv_id: int, kind: str, title: str, user_content, spec: AgentSpec, task_id: int | None = None) -> Run:
    """Store the user message and launch the agent loop in a background thread."""
    _gc()
    with _registry:
        if active_for(conv_id):
            raise RunBusy("This conversation already has a running agent. Stop it or wait for it to finish.")
        run = Run(conv_id, kind, title, task_id)
        _runs[run.id] = run
        _active[conv_id] = run.id
    with run.lock:
        mid = db.add_message(conv_id, "user", user_content)
        run.emit("user_message", message_id=mid)
        run.persisted()
    threading.Thread(target=_execute, args=(run, spec), name=f"run-{run.id}", daemon=True).start()
    return run


def cancel_all() -> None:
    for run in list(_runs.values()):
        run.cancel.set()


def _gc() -> None:
    cutoff = time.time() - 1800
    for rid, run in list(_runs.items()):
        if run.done and run.finished and run.finished < cutoff:
            _runs.pop(rid, None)


# ---------------------------------------------------------------- client

_client_lock = threading.Lock()
_client: tuple[str, anthropic.Anthropic] | None = None


def get_client() -> anthropic.Anthropic:
    """Client for the API key in Settings; falls back to ANTHROPIC_API_KEY / `ant auth login`."""
    global _client
    key = db.get_settings()["api_key"].strip()
    with _client_lock:
        if _client is None or _client[0] != key:
            _client = (key, anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic())
        return _client[1]


def describe_error(e: Exception) -> str:
    if getattr(e, "user_facing", False):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "Authentication failed. Add a valid Anthropic API key in Settings."
    if isinstance(e, anthropic.PermissionDeniedError):
        return f"Permission denied by the API: {e.message}"
    if isinstance(e, anthropic.NotFoundError):
        return f"Model or endpoint not found: {e.message}"
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the API. Wait a moment and try again."
    if isinstance(e, anthropic.BadRequestError):
        return f"The API rejected the request: {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the network connection."
    text = str(e)
    if "api_key" in text or "authentication" in text.lower():
        return "No Anthropic credentials found. Add an API key in Settings."
    return f"{type(e).__name__}: {text}"


# ---------------------------------------------------------------- the loop

def _execute(run: Run, spec: AgentSpec) -> None:
    status = "completed"
    try:
        if spec.engine == "claude_code":
            from . import claude_code
            claude_code.run(run, spec)
        else:
            _loop(run, spec)
        if run.cancel.is_set():
            status = "cancelled"
    except Exception as e:  # report every failure to the browser, keep the server alive
        status = "error"
        log.error("run %s failed:\n%s", run.id, traceback.format_exc())
        run.emit("error", text=describe_error(e))
    finally:
        if run.task_id:
            task = db.get_task(run.task_id)
            if task:
                db.update_task(run.task_id, {"time_spent_seconds": task["time_spent_seconds"] + time.time() - run.started})
        with run.lock:
            run.status = status
            run.finished = time.time()
            run.emit("done", status=status, restart=run.restart_after)
        if run.restart_after:
            # Give open event streams a moment to deliver "done" before the server stops.
            threading.Timer(1.0, maintenance.request_restart).start()


def _params(spec: AgentSpec, system: str, history: list) -> dict:
    params = {
        "model": spec.model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": history,
        # drop_block: if a stored thinking block no longer matches its prefix (e.g. the developer
        # agent changed a tool definition), drop that block instead of failing the request.
        "thinking": {"type": "adaptive", "display": "summarized",
                     "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        "output_config": {"effort": spec.effort},
        "cache_control": {"type": "ephemeral"},
        "betas": ["thinking-binding-controls-2026-08-01"],
    }
    if spec.tools:
        params["tools"] = spec.tools
    if db.get_settings()["use_fallback"] and spec.model in FALLBACK_MODELS:
        params["betas"].append("server-side-fallback-2026-07-01")
        params["fallbacks"] = "default"
    return params


def _loop(run: Run, spec: AgentSpec) -> None:
    conv = db.get_conversation(run.conv_id)
    history = api_history(db.list_messages(run.conv_id))
    client = get_client()
    json_retries = 0

    for _ in range(spec.max_turns):
        if run.cancel.is_set():
            return
        run.emit("turn_start", model=spec.model)
        try:
            with client.beta.messages.stream(**_params(spec, conv["system_prompt"], history)) as stream:
                _relay(run, stream)
                if run.cancel.is_set():
                    run.emit("turn_discarded")
                    return
                final = stream.get_final_message()
            json_retries = 0
        except ValueError:
            # The SDK raises ValueError (only) for tool-input JSON it cannot parse at all. There is no
            # complete tool_use block to answer, so re-issue the turn, a bounded number of times.
            json_retries += 1
            run.emit("turn_discarded")
            if json_retries > 2:
                raise
            run.emit("notice", level="warn", text="The model produced malformed tool input; retrying the turn.")
            continue

        content = serialize_content(final.content)
        usage = {**merge_usage({}, final.usage), "model": final.model}
        with run.lock:
            mid = db.add_message(run.conv_id, "assistant", content, usage=usage)
            history.append({"role": "assistant", "content": content})
            run.emit("turn_end", message_id=mid, stop_reason=final.stop_reason, model=final.model)
            run.emit("usage", message_id=mid, usage=usage)
            run.persisted()

        for t in getattr(final, "input_transformations", None) or []:
            log.info("run %s input transformation: %s", run.id, t)

        stop = final.stop_reason
        if stop == "pause_turn":
            continue
        tool_uses = executable_tool_uses(content)

        if stop == "refusal":
            details = final.stop_details
            category = getattr(details, "category", None) if details else None
            run.emit("notice", level="error",
                     text="Claude declined this request" + (f" (category: {category})." if category else "."))
            if tool_uses:
                _store_results(run, history, [_result(b["id"], "Not executed: the turn ended in a refusal.", True)
                                              for b in tool_uses])
            return
        if not tool_uses:
            if stop == "max_tokens":
                run.emit("notice", level="warn", text="The reply hit the output token limit and was cut off.")
            return
        if stop == "max_tokens":
            # A truncated tool input still parses as a (partial) object; never run it.
            _store_results(run, history, [_result(
                b["id"], "Not executed: your tool input was cut off by the output token limit. "
                "Send large content in smaller pieces (e.g. write_file a skeleton, then edit_file).", True)
                for b in tool_uses])
            continue

        results = []
        for block in tool_uses:
            if run.cancel.is_set():
                results.append(_result(block["id"], "Not executed: the user stopped the run.", True))
                continue
            run.emit("tool_start", id=block["id"], name=block["name"], input=_preview_input(block["input"]))
            text, is_error = _run_tool(run, spec, block)
            results.append(_result(block["id"], text, is_error))
            run.emit("tool_result", tool_use_id=block["id"], name=block["name"], is_error=is_error,
                     content=T.clip(text, UI_PREVIEW_CHARS))
        _store_results(run, history, results)
        if run.cancel.is_set():
            return

    run.emit("notice", level="warn",
             text=f"Stopped after {spec.max_turns} agent turns. Send a message to let the agent continue.")


def api_history(messages: list[dict]) -> list[dict]:
    """Stored messages as Messages API input.

    Turns produced by the Claude Code engine used Claude Code's own tools and session, so only
    their text is replayed; API-engine turns are sent back exactly as they were returned. Attached
    images are read from disk.
    """
    history = []
    for m in messages:
        content = m["content"]
        if m.get("engine") == "claude_code":
            text = content if isinstance(content, str) else "\n\n".join(
                b["text"] for b in content if b.get("type") == "text" and b.get("text", "").strip())
            if not text.strip():
                continue
            content = [{"type": "text", "text": text}]
        else:
            content = images.api_content(content)
        history.append({"role": m["role"], "content": content})
    return history


def _relay(run: Run, stream) -> None:
    """Forward raw stream events to the browser as compact UI events."""
    json_bytes: dict[int, int] = {}
    for event in stream:
        if run.cancel.is_set():
            return
        et = event.type
        if et == "content_block_start":
            block = event.content_block
            data = {"index": event.index, "block_type": block.type}
            if block.type in ("tool_use", "server_tool_use"):
                data.update(id=block.id, name=block.name)
            elif block.type == "fallback":
                data.update(from_model=block.from_.model, to_model=block.to.model)
            elif block.type.endswith("_tool_result"):
                data["block"] = block.to_dict(mode="json")
            run.emit("block_start", **data)
        elif et == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                run.emit("text", index=event.index, text=delta.text)
            elif delta.type == "thinking_delta":
                run.emit("thinking", index=event.index, text=delta.thinking)
            elif delta.type == "input_json_delta":
                before = json_bytes.get(event.index, 0)
                after = before + len(delta.partial_json)
                json_bytes[event.index] = after
                if after // 2048 != before // 2048:
                    run.emit("tool_input_progress", index=event.index, bytes=after)
        elif et == "content_block_stop":
            run.emit("block_stop", index=event.index)
        elif et == "message_start":
            run.emit("message_start", model=event.message.model)


def serialize_content(blocks) -> list[dict]:
    """Assistant content as plain dicts, ready to store and send back unchanged.

    After a mid-output refusal fallback, the declined attempt's thinking, tool_use and
    unpaired server_tool_use blocks that precede the last `fallback` block must not be
    echoed back; everything else is kept as returned.
    """
    out = [b.to_dict(mode="json") for b in blocks]
    last_fb = max((i for i, b in enumerate(out) if b["type"] == "fallback"), default=-1)
    if last_fb < 0:
        return out
    paired = {b.get("tool_use_id") for b in out if b["type"].endswith("_tool_result")}
    keep = []
    for i, b in enumerate(out):
        if i < last_fb and (b["type"] in ("thinking", "redacted_thinking", "tool_use")
                            or (b["type"] == "server_tool_use" and b.get("id") not in paired)):
            continue
        keep.append(b)
    return keep


USAGE_FIELDS = {"input_tokens": "input", "output_tokens": "output",
                "cache_read_input_tokens": "cache_read", "cache_creation_input_tokens": "cache_write"}


def merge_usage(usage: dict, raw) -> dict:
    """Fold an API usage object or dict into {input, output, cache_read, cache_write}.

    Counts only grow while a response streams, so taking the maximum makes the result independent of
    the order in which snapshots arrive.
    """
    if raw is None:
        return usage
    get = raw.get if isinstance(raw, dict) else lambda k: getattr(raw, k, None)
    for src, dst in USAGE_FIELDS.items():
        value = get(src)
        if isinstance(value, int):
            usage[dst] = max(usage.get(dst, 0), value)
    return usage


def executable_tool_uses(content: list[dict]) -> list[dict]:
    return [b for b in content if b["type"] == "tool_use"]


def _result(tool_use_id: str, text: str, is_error: bool) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text or "(no output)"}
    if is_error:
        block["is_error"] = True
    return block


def _store_results(run: Run, history: list, results: list[dict]) -> None:
    with run.lock:
        db.add_message(run.conv_id, "user", results)
        history.append({"role": "user", "content": results})
        run.persisted()


def _preview_input(args) -> dict:
    if not isinstance(args, dict):
        return {"_raw": str(args)[:UI_PREVIEW_CHARS]}
    return {k: (v[:UI_PREVIEW_CHARS] + f"… ({len(v)} chars)" if isinstance(v, str) and len(v) > UI_PREVIEW_CHARS else v)
            for k, v in args.items()}


def _run_tool(run: Run, spec: AgentSpec, block: dict) -> tuple[str, bool]:
    name, args = block["name"], block["input"]
    tool_def = spec.tool_index.get(name)
    if tool_def is None:
        return f"Unknown tool: {name}", True
    problem = T.validate(tool_def, args)
    if problem:
        return json.dumps({"INVALID_INPUT": problem, "received": T.format_args(args)}), True
    try:
        if name == "update_task":
            return _update_task(run, spec.task_id, args), False
        if name in team.HANDLERS:
            return T.clip(team.HANDLERS[name](run, spec, args)), False
        if name in project_setup.HANDLERS:
            return T.clip(project_setup.HANDLERS[name](run, spec, args)), False
        if name == "restart_server":
            return _restart(run, args), False
        if name == "read_server_log":
            return maintenance.tail_log(max(1, min(args.get("lines", 120), 2000))), False
        return T.clip(spec.workspace.run(name, args)), False
    except T.ToolError as e:
        return str(e), True
    except Exception as e:
        log.error("tool %s failed:\n%s", name, traceback.format_exc())
        return f"{type(e).__name__}: {e}", True


def _update_task(run: Run, task_id: int | None, args: dict) -> str:
    task = db.get_task(task_id) if task_id else None
    if task is None:
        raise T.ToolError("No task is attached to this conversation.")
    fields, changes = {}, []
    if "status" in args and args["status"] != task["status"]:
        fields["status"] = args["status"]
        changes.append(f"status → {args['status']}")
    if "progress" in args:
        fields["progress"] = max(0, min(100, args["progress"]))
        changes.append(f"progress → {fields['progress']}%")
    if "estimated_hours" in args:
        fields["estimated_hours"] = max(0.0, float(args["estimated_hours"]))
        changes.append(f"estimate → {fields['estimated_hours']:g} h")
    if args.get("add_insights"):
        stamp = db.now()
        fields["insights"] = task["insights"] + [{"text": t, "at": stamp, "source": "agent"}
                                                 for t in args["add_insights"] if t.strip()]
        changes.append(f"+{len(args['add_insights'])} insight(s)")
    if "next_actions" in args:
        fields["next_actions"] = [{"text": t, "done": False} for t in args["next_actions"] if t.strip()]
        changes.append(f"{len(fields['next_actions'])} next action(s)")
    if "summary" in args:
        fields["summary"] = args["summary"]
        changes.append("summary updated")
    task = db.update_task(task_id, fields)
    if args.get("log"):
        db.add_task_event(task_id, "agent", args["log"])
    if changes:
        db.add_task_event(task_id, "system", "Agent updated the task: " + ", ".join(changes))
    run.emit("task_update", task=task)
    return "Task updated: " + (", ".join(changes) if changes else "no changes") + "."


def _restart(run: Run, args: dict) -> str:
    ok, output = maintenance.preflight()
    if not ok:
        raise T.ToolError("Preflight failed, so the restart was refused. Fix the error and try again:\n"
                          + T.clip(output, 8000))
    if not maintenance.under_supervisor():
        return ("Preflight passed, but this server was not started through supervisor.py, so it cannot "
                "restart itself. Ask the user to restart it (./start.sh).")
    run.restart_after = True
    others = [r for r in active_runs() if r["id"] != run.id]
    note = f" {len(others)} other running agent(s) will be interrupted." if others else ""
    run.emit("notice", level="info", text=f"Server restart scheduled: {args['reason']}")
    return f"Preflight passed. The server will restart as soon as this turn ends.{note}"
