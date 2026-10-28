"""Paths and process-level constants shared by the server and the supervisor."""

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR") or APP_ROOT / "data").resolve()
DB_PATH = DATA_DIR / "agent_group.db"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LOG_DIR = DATA_DIR / "logs"
SERVER_LOG = LOG_DIR / "server.log"
STATIC_DIR = APP_ROOT / "static"

HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "8765"))
# Optional shared secret. When set, every /api request must present it
# (cookie, bearer header or ?token=). Required if HOST is not loopback.
ACCESS_TOKEN = os.environ.get("AGENT_TOKEN", "")

# The server exits with this code to ask the supervisor for a restart.
RESTART_EXIT_CODE = 3

# Directories the developer agent may not touch with its file tools.
DEV_PROTECTED_DIRS = (".venv", "data", ".git")

MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"},
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Models that accept the server-side `fallbacks: "default"` refusal fallback.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}

for d in (DATA_DIR, SNAPSHOT_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)
