#!/bin/bash
DEPLOY_PATH="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DEPLOY_PATH/bot.pid"
LOG_FILE="$DEPLOY_PATH/bot.log"

# Останавливаем старый процесс
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Останавливаем старый процесс PID=$OLD_PID..."
        kill "$OLD_PID"
        sleep 2
    fi
fi

# Загружаем .env
set -a
source "$DEPLOY_PATH/.env"
set +a

# Запускаем
nohup "$DEPLOY_PATH/venv/bin/python" "$DEPLOY_PATH/bot.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Бот запущен, PID=$(cat $PID_FILE)"
echo "Логи: tail -f $LOG_FILE"
