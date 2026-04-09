#!/bin/bash
DEPLOY_PATH="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DEPLOY_PATH/bot.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" && rm "$PID_FILE"
        echo "Бот остановлен (PID=$PID)"
    else
        rm -f "$PID_FILE"
        echo "Процесс уже не запущен"
    fi
else
    echo "Бот не запущен"
fi
