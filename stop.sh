#!/usr/bin/env bash
# Stop a background Agent Group started with ./start.sh --background.
cd "$(dirname "$0")"
if [ -f data/supervisor.pid ] && kill -0 "$(cat data/supervisor.pid)" 2>/dev/null; then
  kill "$(cat data/supervisor.pid)" && rm -f data/supervisor.pid && echo "Stopped."
else
  echo "Agent Group is not running in the background."
fi
