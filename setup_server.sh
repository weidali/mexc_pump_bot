#!/bin/bash
# =============================================================
# setup_server.sh — запускается ОДИН РАЗ на свежем сервере
# Определяет ОС автоматически и устанавливает всё необходимое
# =============================================================
set -e

# ── Цвета для вывода ─────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── Переменные — ИЗМЕНИ ПОД СЕБЯ ─────────────────────────────
DEPLOY_USER="${SUDO_USER:-$(whoami)}"
# DEPLOY_PATH="/var/www/${DEPLOY_USER}/data/bin-tmp/pump-bot"
DEPLOY_PATH="/bin-tmp/pump-bot"
SERVICE_NAME="pump-bot"

info "Deploying as user: ${DEPLOY_USER}"
info "Deploy path: ${DEPLOY_PATH}"

# ── Определяем ОС ────────────────────────────────────────────
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    error "Не удалось определить ОС"
fi

info "Detected OS: ${OS}"

# ── Устанавливаем Python 3 ────────────────────────────────────
case "$OS" in
    ubuntu|debian)
        info "Installing Python3 (apt)..."
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv git
        ;;
    centos|rhel|fedora|rocky|almalinux)
        info "Installing Python3 (yum/dnf)..."
        if command -v dnf &>/dev/null; then
            dnf install -y python3 python3-pip git
        else
            yum install -y python3 python3-pip git
        fi
        ;;
    *)
        warning "Неизвестная ОС: ${OS}. Убедись что python3, pip, git установлены вручную."
        ;;
esac

# ── Проверяем Python ─────────────────────────────────────────
python3 --version || error "Python3 не найден"
info "Python OK: $(python3 --version)"

# ── Создаём папку деплоя ─────────────────────────────────────
mkdir -p "${DEPLOY_PATH}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_PATH}"

# ── Настраиваем SSH ключ для GitHub Actions ───────────────────
info "Генерируем SSH ключ для CI/CD..."
SSH_DIR="/home/${DEPLOY_USER}/.ssh"
KEY_FILE="${SSH_DIR}/github_actions"

mkdir -p "${SSH_DIR}"
chmod 700 "${SSH_DIR}"

if [ ! -f "${KEY_FILE}" ]; then
    ssh-keygen -t ed25519 -C "github-actions-deploy" -f "${KEY_FILE}" -N ""
    info "SSH ключ создан: ${KEY_FILE}"
else
    info "SSH ключ уже существует: ${KEY_FILE}"
fi

# Добавляем публичный ключ в authorized_keys
cat "${KEY_FILE}.pub" >> "${SSH_DIR}/authorized_keys"
sort -u "${SSH_DIR}/authorized_keys" -o "${SSH_DIR}/authorized_keys"
chmod 600 "${SSH_DIR}/authorized_keys"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${SSH_DIR}"

# ── Настраиваем sudoers для systemctl без пароля ─────────────
SUDOERS_FILE="/etc/sudoers.d/pump-bot"
info "Настраиваем sudo для systemctl..."
echo "${DEPLOY_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl status ${SERVICE_NAME}, /bin/systemctl start ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}" > "${SUDOERS_FILE}"
chmod 440 "${SUDOERS_FILE}"
info "sudoers настроен: ${SUDOERS_FILE}"

# ── Устанавливаем systemd сервис ─────────────────────────────
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Создаём systemd сервис..."
cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=MEXC Pump & Dump Scanner Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${DEPLOY_USER}
WorkingDirectory=${DEPLOY_PATH}
EnvironmentFile=${DEPLOY_PATH}/.env
ExecStart=${DEPLOY_PATH}/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
info "Systemd сервис создан и включён"

# ── Итог ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Сервер настроен успешно!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Теперь скопируй ПРИВАТНЫЙ ключ в GitHub Secrets:${NC}"
echo ""
echo "  Команда для просмотра приватного ключа:"
echo -e "  ${GREEN}cat ${KEY_FILE}${NC}"
echo ""
echo "  Добавь в GitHub → Settings → Secrets → Actions:"
echo "  ┌─────────────────────────────────────────────────┐"
echo "  │ SERVER_SSH_KEY  = (содержимое приватного ключа) │"
echo -e "  │ SERVER_HOST     = $(curl -s ifconfig.me 2>/dev/null || echo 'твой_ip')               │"
echo "  │ SERVER_USER     = ${DEPLOY_USER}                        │"
echo "  │ SERVER_PORT     = 22                            │"
echo "  │ DEPLOY_PATH     = ${DEPLOY_PATH}     │"
echo "  │ TELEGRAM_TOKEN  = твой_токен_бота               │"
echo "  │ MEXC_API_KEY    = твой_mexc_api_key             │"
echo "  │ MEXC_SECRET     = твой_mexc_secret              │"
echo "  └─────────────────────────────────────────────────┘"
echo ""
