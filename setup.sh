#!/usr/bin/env bash
# Arena → Odoo Sync — one-shot setup & run
# Usage: bash setup.sh
#   Installs Python deps, then runs the dashboard on port 5000 in the background.
#   Logs go to sync.log. PID is saved to app.pid for stopping later.
#   To stop:  bash setup.sh stop

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT="${PORT:-5000}"
PIDFILE="$DIR/app.pid"
LOGFILE="$DIR/sync.log"

# ── Stop ────────────────────────────────────────────────
if [ "${1:-}" = "stop" ]; then
  if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      echo "Stopped (pid $PID)"
    else
      echo "Process $PID not running"
    fi
    rm -f "$PIDFILE"
  else
    echo "No pidfile found"
  fi
  exit 0
fi

# ── Check Python ────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null && "$cmd" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
    PYTHON="$cmd"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "ERROR: Python 3.10+ not found. Install it first."
  exit 1
fi

echo "Using $($PYTHON --version) at $(command -v $PYTHON)"

# ── Virtual environment ─────────────────────────────────
if [ ! -d "$DIR/venv" ]; then
  echo "Creating virtual environment..."
  $PYTHON -m venv "$DIR/venv"
fi

# Activate
if [ -f "$DIR/venv/bin/activate" ]; then
  source "$DIR/venv/bin/activate"
elif [ -f "$DIR/venv/Scripts/activate" ]; then
  source "$DIR/venv/Scripts/activate"
fi

# ── Install dependencies ────────────────────────────────
echo "Installing dependencies..."
pip install -q -r requirements.txt

# ── Kill old instance if running ────────────────────────
if [ -f "$PIDFILE" ]; then
  OLD_PID=$(cat "$PIDFILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping old instance (pid $OLD_PID)..."
    kill "$OLD_PID"
    sleep 1
  fi
  rm -f "$PIDFILE"
fi

# ── Start in background ─────────────────────────────────
export PRODUCTION=1
echo "Starting Arena-Odoo Sync on port $PORT (production mode)..."
nohup python main.py --port "$PORT" >> "$LOGFILE" 2>&1 &
APP_PID=$!
echo "$APP_PID" > "$PIDFILE"

sleep 2
if kill -0 "$APP_PID" 2>/dev/null; then
  echo ""
  echo "  Running on http://$(hostname -f 2>/dev/null || echo localhost):$PORT"
  echo "  PID: $APP_PID"
  echo "  Log: $LOGFILE"
  echo ""
  echo "  Stop with: bash setup.sh stop"
else
  echo "ERROR: App failed to start. Check $LOGFILE"
  exit 1
fi
