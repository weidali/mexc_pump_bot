# 🤖 MEXC Pump & Dump Scanner Bot

Телеграм-бот для обнаружения манипуляций (pump & dump) на шиткоинах.

## Как работает

Бот каждую минуту сканирует топ-80 монет по объёму и ищет три признака манипуляции:

| Сигнал | Описание |
|--------|----------|
| 📦 **Volume Spike** | Объём текущей свечи в 5x+ раз выше среднего |
| 🚀 **Price Pump** | Рост цены на 8%+ за последние 5 минут |
| 📉 **CVD Divergence** | Цена растёт, но тейкеры продают — крупный holder разгружается |

Сигнал отправляется только при наборе минимального скора (≥2.0) — это фильтрует ложные срабатывания.

---

## Установка

### Требования
- Python 3.10+
- Аккаунт MEXC с API ключом
- Telegram бот (создать через [@BotFather](https://t.me/BotFather))

### 1. Клонируй и установи зависимости

```bash
git clone <repo>
cd mexc_pump_bot
pip install -r requirements.txt
```

### 2. Настрой переменные среды

```bash
cp .env.example .env
# Отредактируй .env своим редактором
```

Заполни в `.env`:
```
TELEGRAM_TOKEN=твой_токен_бота
MEXC_API_KEY=твой_mexc_api_key
MEXC_SECRET=твой_mexc_secret
```

### 3. Запуск

```bash
# Загрузить .env и запустить
export $(cat .env | xargs) && python bot.py
```

Или через Docker:
```bash
docker build -t pump-bot .
docker run --env-file .env pump-bot
```

---

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Подписаться на алерты |
| `/status` | Статус сканера, кол-во монет |
| `/top` | Топ-20 монет по объёму прямо сейчас |
| `/stats` | История сигналов за 24ч |
| `/pause` | Приостановить алерты |
| `/resume` | Возобновить алерты |

---

## Пример алерта

```
🔴🔴🔴 PUMP & DUMP — СИЛЬНЫЙ СИГНАЛ

📌 XYZUSDT
💵 Цена: 0.00412 USDT
📊 Score: 4.2

🔍 Признаки:
  • Объём ×12.3 от нормы 📦
  • Рост цены +14.5% 🚀
  • CVD дивергенция -0.68 (продают в рост) 📉

⚡ Возможный шорт при развороте
⚠️ Не финансовый совет. DYOR.
```

---

## Настройка порогов (config.py)

```python
VOLUME_SPIKE_MULTIPLIER = 5.0     # объём в 5x от нормы
PRICE_PUMP_THRESHOLD_PCT = 8.0    # рост на 8% за 5 свечей
CVD_DIVERGENCE_THRESHOLD = -0.1   # CVD < -0.1 при росте цены
MIN_SIGNAL_SCORE = 2.0            # минимальный скор для алерта
SIGNAL_COOLDOWN_MINUTES = 30      # не спамить по одной монете
TOP_N_SYMBOLS = 80                # сколько топ монет сканировать
```

**Если слишком много сигналов** — увеличь пороги или `MIN_SIGNAL_SCORE`.  
**Если сигналов нет** — уменьши `PRICE_PUMP_THRESHOLD_PCT` или `VOLUME_SPIKE_MULTIPLIER`.

Если сигналов долго нет — можно временно снизить пороги в config.py чтобы убедиться что детектор вообще срабатывает:
```py
VOLUME_SPIKE_MULTIPLIER = 2.0     # было 5.0
PRICE_PUMP_THRESHOLD_PCT = 3.0    # было 8.0
MIN_SIGNAL_SCORE = 1.5            # было 2.0
```

---

## Архитектура

```
bot.py          — Telegram handlers, точка входа
config.py       — все настройки
mexc_client.py  — async REST клиент MEXC API
analyzer.py     — логика детектирования (3 сигнала)
scanner.py      — главный цикл сканирования
db.py           — SQLite: история сигналов, подписчики
```

---

## Installing and using

```shell
# 1. Зайди в папку проекта
cd mexc_pump_bot

# 2. Создай виртуальное окружение
ls /opt/homebrew/bin/python3*
# или
brew list | grep python

/opt/homebrew/bin/python3.12 -m venv venv
source venv/bin/activate
python --version  # → Python 3.12.x ✓
pip install -r requirements.txt

# 3. activate/deactivate
source venv/bin/activate
deactivate

# 4. Установи зависимости
pip install -r requirements.txt

# 5. Создай .env файл
cp .env.example .env
# Открой .env и вставь свои ключи

# 6. Запусти бота
python bot.py
```

добавь секреты в GitHub


  SERVER_HOST     = ***
  SERVER_USER     = ***
  SERVER_PORT     = ***
  DEPLOY_PATH     = /var/www/***/data/tmp/pump-bot

SERVER_SSH_KEY — скопируй вывод этой команды целиком:
  cat ~/.ssh/github_actions

  TELEGRAM_TOKEN  = токен бота
  MEXC_API_KEY    = ключ mexc
  MEXC_SECRET     = секрет mexc
  ADMIN_CHAT_ID=${{ secrets.ADMIN_CHAT_ID }}


## Логика авторизации

**1. Узнай свой chat_id** — напиши боту [@userinfobot](https://t.me/userinfobot) в Telegram.

**Незнакомый пользователь** пишет `/start` → получает "доступ закрыт" → **тебе** (админу) прилетает уведомление с его chat_id и готовой командой для одобрения.

**Ты** пишешь `/adduser 123456789` → пользователь получает уведомление "доступ одобрен" и начинает получать сигналы.

**Команды для тебя (только для админа):**
```
/adduser 123456789    — добавить пользователя
/removeuser 123456789 — убрать пользователя  
/users                — список всех
```

### versioning
```shell
# version.py
__version__ = "1.1.0"
__release_notes__ = "Добавлена авторизация, inline кнопки, очистка БД"

git add version.py
git commit -m "bump: 1.1.0"

# Правила
1.0.0
│ │ └── patch (1.0.1) — мелкий баг фикс
│ └──── minor (1.1.0) — новая фича → релиз
└────── major (2.0.0) — большие изменения → релиз
```

## Testing

```shell
# 1. Статус — жив ли процесс
~/tmp/pump-bot/stop.sh && sleep 1 && ~/tmp/pump-bot/start.sh

# 2. Смотрим логи в реальном времени
tail -f ~/tmp/pump-bot/bot.log
tail -30 ~/tmp/pump-bot/bot.log
```

Проверка через Telegram
```
/start   — должен ответить приветствием
/status  — покажет сколько монет сканируется
/top     — топ-20 монет по объёму прямо сейчас
```

---
⚠️ **Дисклеймер**: Бот не является финансовым советником. Все сигналы носят информационный характер. Торговля криптовалютой сопряжена с высокими рисками. Как сказала Хабибуля - "Деньги в банке - это уже не ваши деньги"
