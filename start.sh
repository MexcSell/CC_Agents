#!/usr/bin/env bash
# Start Agent Group under the supervisor (creates the virtualenv on first run).
#   AGENT_PORT=8765 AGENT_HOST=127.0.0.1 ./start.sh
#   ./start.sh --background    # detach; logs go to data/logs/server.log
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
# Install dependencies only when requirements.txt changed (works offline afterwards).
if [ ! -f .venv/.requirements.stamp ] || [ requirements.txt -nt .venv/.requirements.stamp ]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.requirements.stamp
fi

if [ "${1:-}" = "--background" ]; then
  mkdir -p data/logs
  nohup .venv/bin/python supervisor.py >> data/logs/supervisor.out 2>&1 &
  echo $! > data/supervisor.pid
  echo "Agent Group started in the background (supervisor pid $(cat data/supervisor.pid))."
  echo "Open http://${AGENT_HOST:-127.0.0.1}:${AGENT_PORT:-8765}"
else
  exec .venv/bin/python supervisor.py
fi
