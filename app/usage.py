"""Claude plan usage: the limits Claude Code shows under /usage (read with the login stored on this
machine) and the tokens Agent Group's agents spent per model, counted from the stored messages."""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import MODELS

log = logging.getLogger("agent_group.usage")

API = "https://api.anthropic.com/api/oauth"
HEADERS = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.0 (agent-group)"}
LIMITS_TTL = 60          # seconds between fetches of the plan limits
PROFILE_TTL = 3600
SESSION = timedelta(hours=5)
WEEK = timedelta(days=7)
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- Claude Code login

def _credentials() -> dict:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        return json.loads(path.read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def _get(path: str) -> dict:
    creds = _credentials()
    token = creds.get("accessToken") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise UsageError("Claude Code is not logged in on this machine. Run `claude` and log in.")
    req = urllib.request.Request(f"{API}/{path}", headers={**HEADERS, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise UsageError("The Claude Code login has expired. Run a Claude Code agent (or `claude`) to "
                             "refresh it.") from e
        raise UsageError(f"Claude usage request failed: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise UsageError(f"Claude usage request failed: {e}") from e


def _cached(key: str, ttl: float, fetch, force: bool = False) -> tuple[dict | None, str | None]:
    """Return (data, error). A failed fetch keeps serving the last good data along with the error."""
    with _lock:
        hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < ttl:
        return hit[1], None
    try:
        data = fetch()
    except UsageError as e:
        log.info("usage: %s", e)
        return (hit[1] if hit else None), str(e)
    with _lock:
        _cache[key] = (time.time(), data)
    return data, None


# ---------------------------------------------------------------- plan limits

def _limit(kind: str, label: str, percent, resets_at, model: str | None = None) -> dict:
    percent = max(0.0, min(100.0, float(percent or 0)))
    return {"kind": kind, "label": label, "percent": percent, "remaining": 100.0 - percent,
            "resets_at": resets_at, "model": model}


def _limits(raw: dict) -> list[dict]:
    if raw.get("limits"):
        out = []
        for l in raw["limits"]:
            kind = l.get("kind") or ""
            model = ((l.get("scope") or {}).get("model") or {}).get("display_name")
            label = {"session": "Session (5h)", "weekly_all": "Weekly (7 day)"}.get(kind) or (
                f"Weekly {model}" if model else kind.replace("_", " ").capitalize())
            out.append(_limit(kind, label, l.get("percent"), l.get("resets_at"), model))
        return out
    # Older response shape.
    shapes = [("five_hour", "session", "Session (5h)", None), ("seven_day", "weekly_all", "Weekly (7 day)", None),
              ("seven_day_opus", "weekly_scoped", "Weekly Opus", "Opus"),
              ("seven_day_sonnet", "weekly_scoped", "Weekly Sonnet", "Sonnet")]
    return [_limit(kind, label, raw[key].get("utilization"), raw[key].get("resets_at"), model)
            for key, kind, label, model in shapes if isinstance(raw.get(key), dict)]


def _account() -> dict:
    creds = _credentials()
    profile, _ = _cached("profile", PROFILE_TTL, lambda: _get("profile"))
    profile = profile or {}
    plan = creds.get("subscriptionType")
    return {
        "auth": "Claude AI" if creds or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else None,
        "email": (profile.get("account") or {}).get("email"),
        "organization": (profile.get("organization") or {}).get("name"),
        "plan": f"Claude {plan}" if plan else None,
    }


# ---------------------------------------------------------------- tokens spent by Agent Group's agents

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _model_id(name: str | None) -> str | None:
    """Map a model name reported by the API ("claude-opus-5-5-20261001", "claude-opus-5-5[1m]") to a known id."""
    if not name:
        return None
    for m in sorted(MODELS, key=lambda m: -len(m["id"])):
        if name.startswith(m["id"]):
            return m["id"]
    return name


def _tokens(model: str, session_start: datetime, week_start: datetime) -> dict:
    """Tokens spent on `model` in the current session window, the current weekly window and in total.

    Messages stored before the model was recorded with their usage are attributed to the model their
    agent is configured with now.
    """
    settings = db.get_settings()
    rows = db.query(
        "SELECT m.usage, m.created_at, c.kind, a.model AS agent_model FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id LEFT JOIN tasks t ON t.id = c.task_id "
        "LEFT JOIN agents a ON a.project_id = t.project_id WHERE m.usage IS NOT NULL")
    fallback = {"chat": settings["chat_model"], "dev": settings["dev_model"], "setup": settings["project_model"]}
    zero = lambda: {**{f: 0 for f in TOKEN_FIELDS}, "requests": 0}
    totals = {"session": zero(), "week": zero(), "all": zero()}
    for r in rows:
        try:
            u = json.loads(r["usage"])
        except ValueError:
            continue
        if (_model_id(u.get("model")) or r["agent_model"] or fallback.get(r["kind"])) != model:
            continue
        when = _parse(r["created_at"])
        buckets = ["all"] + (["week"] if when and when >= week_start else []) + (
            ["session"] if when and when >= session_start else [])
        for b in buckets:
            for f in TOKEN_FIELDS:
                totals[b][f] += u.get(f) or 0
            totals[b]["requests"] += 1
    for t in totals.values():
        t["total"] = sum(t[f] for f in TOKEN_FIELDS)
    return totals


# ---------------------------------------------------------------- report

def resolve_model(scope: str | None, project_id: int | None) -> str:
    """The model the agent of a page uses: chat, dev or a project's agent (the default for new
    project agents without a project); chat by default."""
    settings = db.get_settings()
    if scope == "project":
        agent = project_id and db.one("SELECT model FROM agents WHERE project_id = ? ORDER BY parent_id IS NOT NULL, "
                                      "sort, id", (project_id,))
        return agent["model"] if agent else settings["project_model"]
    return settings["dev_model"] if scope == "dev" else settings["chat_model"]


def report(model: str, force: bool = False) -> dict:
    raw, error = _cached("limits", LIMITS_TTL, lambda: _get("usage"), force)
    limits = _limits(raw) if raw else []
    label = next((m["label"] for m in MODELS if m["id"] == model), model)
    applicable = [l for l in limits if not l["model"] or l["model"].lower() in label.lower()]
    binding = min(applicable, key=lambda l: l["remaining"], default=None)

    now = datetime.now(timezone.utc)
    by_kind = {l["kind"]: l for l in limits}
    session_end = _parse((by_kind.get("session") or {}).get("resets_at"))
    week_end = _parse((by_kind.get("weekly_all") or {}).get("resets_at"))
    session_start = session_end - SESSION if session_end and session_end > now else now - SESSION
    week_start = week_end - WEEK if week_end and week_end > now else now - WEEK

    return {
        "account": _account(),
        "engine": db.get_settings()["engine"],
        "limits": [{**l, "applies": l in applicable} for l in limits],
        "error": error,
        "fetched_at": _cache.get("limits", (None,))[0],
        "model": {
            "id": model, "label": label,
            "tokens": _tokens(model, session_start, week_start),
            "session_start": session_start.isoformat(), "week_start": week_start.isoformat(),
            "remaining": binding["remaining"] if binding else None,
            "percent": binding["percent"] if binding else None,
            "resets_at": binding["resets_at"] if binding else None,
            "binding": binding["label"] if binding else None,
        },
    }
