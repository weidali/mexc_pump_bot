#!/bin/bash
DEPLOY_PATH="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DEPLOY_PATH/bot.pid"
LOG_FILE="$DEPLOY_PATH/bot.log"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill -0 "$PID" 2>/dev/null && exit 0
fi

echo "$(date): watchdog restarting bot..." >> "$DEPLOY_PATH/watchdog.log"
set -a; source "$DEPLOY_PATH/.env"; set +a
nohup "$DEPLOY_PATH/venv/bin/python" "$DEPLOY_PATH/bot.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
