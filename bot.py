"""
MEXC Pump & Dump Detection Bot
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import Config
from scanner import Scanner
from db import Database
from auth import Auth, require_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    config = Config()
    db = Database(path=config.DB_PATH)
    await db.init()

    bot = Bot(token=config.TELEGRAM_TOKEN)
    dp = Dispatcher()
    auth = Auth(db=db, admin_chat_id=config.ADMIN_CHAT_ID)
    scanner = Scanner(config=config, bot=bot, db=db)

    # ── /start — запрос доступа ───────────────────────────────
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        chat_id = message.chat.id
        username = message.from_user.username or "нет username"
        full_name = message.from_user.full_name or ""

        if await auth.is_allowed(chat_id):
            await message.answer(
                "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
                "Команды:\n"
                "/status — статус сканера\n"
                "/top — топ-20 монет по объёму\n"
                "/stats — сигналы за 24ч\n"
                "/pause — приостановить алерты\n"
                "/resume — возобновить алерты",
                parse_mode="HTML"
            )
            return

        # Неавторизованный — уведомляем админа
        await message.answer(
            "🔒 <b>Доступ закрыт</b>\n\n"
            "Запрос на доступ отправлен администратору.\n"
            "Ожидайте подтверждения.",
            parse_mode="HTML"
        )

        if config.ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    config.ADMIN_CHAT_ID,
                    f"🔔 <b>Запрос доступа</b>\n\n"
                    f"👤 Имя: <b>{full_name}</b>\n"
                    f"📛 Username: @{username}\n"
                    f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
                    f"Чтобы одобрить:\n"
                    f"<code>/adduser {chat_id}</code>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to notify admin: {e}")

    # ── /status ───────────────────────────────────────────────
    @dp.message(Command("status"))
    @require_auth(auth)
    async def cmd_status(message: Message):
        monitored = scanner.get_monitored_count()
        signals_24h = await db.get_signals_count_24h()
        last_scan = scanner.get_last_scan_time()
        await message.answer(
            f"📊 <b>Статус сканера</b>\n\n"
            f"🔍 Монет под наблюдением: <b>{monitored}</b>\n"
            f"⚡ Сигналов за 24ч: <b>{signals_24h}</b>\n"
            f"🕐 Последнее сканирование: <b>{last_scan}</b>\n"
            f"🟢 Статус: {'активен' if scanner.is_running else '⏸ приостановлен'}",
            parse_mode="HTML"
        )

    # ── /top ──────────────────────────────────────────────────
    @dp.message(Command("top"))
    @require_auth(auth)
    async def cmd_top(message: Message):
        await message.answer("⏳ Загружаю топ монет...")
        top = await scanner.get_top_symbols()
        if not top:
            await message.answer("❌ Не удалось получить данные с MEXC")
            return
        lines = ["📈 <b>Топ-20 шиткоинов по объёму (MEXC)</b>\n"]
        for i, t in enumerate(top[:20], 1):
            lines.append(
                f"{i}. <b>{t['symbol']}</b> — ${t['price']:.6g} "
                f"| Vol: ${t['volume_usdt']:,.0f}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    # ── /stats ────────────────────────────────────────────────
    @dp.message(Command("stats"))
    @require_auth(auth)
    async def cmd_stats(message: Message):
        rows = await db.get_recent_signals(24)
        if not rows:
            await message.answer("📭 Сигналов за последние 24ч не было.")
            return
        lines = [f"🗂 <b>Сигналы за 24ч ({len(rows)} шт)</b>\n"]
        for r in rows[-15:]:
            lines.append(
                f"• <b>{r['symbol']}</b> ${r['price']:.6g} "
                f"| score={r['score']:.1f} | {r['created_at'][:16]}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    # ── /pause и /resume ──────────────────────────────────────
    @dp.message(Command("pause"))
    @require_auth(auth)
    async def cmd_pause(message: Message):
        scanner.pause()
        await message.answer("⏸ Алерты приостановлены. /resume — возобновить.")

    @dp.message(Command("resume"))
    @require_auth(auth)
    async def cmd_resume(message: Message):
        scanner.resume()
        await message.answer("▶️ Алерты возобновлены.")

    # ── Админские команды ─────────────────────────────────────

    @dp.message(Command("adduser"))
    async def cmd_adduser(message: Message):
        if not auth.is_admin(message.chat.id):
            await message.answer("🚫 Только для администратора.")
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: <code>/adduser 123456789</code>", parse_mode="HTML")
            return
        target_id = int(parts[1])
        added = await auth.add_user(target_id, message.chat.id)
        if added:
            await message.answer(f"✅ Пользователь <code>{target_id}</code> добавлен.", parse_mode="HTML")
            try:
                await bot.send_message(
                    target_id,
                    "✅ <b>Доступ одобрен!</b>\n\nТеперь вы можете пользоваться ботом.\nОтправьте /start",
                    parse_mode="HTML"
                )
            except Exception:
                await message.answer("⚠️ Не удалось уведомить пользователя (он не начинал чат с ботом).")
        else:
            await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> уже есть в списке.", parse_mode="HTML")

    @dp.message(Command("removeuser"))
    async def cmd_removeuser(message: Message):
        if not auth.is_admin(message.chat.id):
            await message.answer("🚫 Только для администратора.")
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: <code>/removeuser 123456789</code>", parse_mode="HTML")
            return
        target_id = int(parts[1])
        removed = await auth.remove_user(target_id)
        if removed:
            await message.answer(f"🗑 Пользователь <code>{target_id}</code> удалён.", parse_mode="HTML")
        else:
            await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> не найден.", parse_mode="HTML")

    @dp.message(Command("users"))
    async def cmd_users(message: Message):
        if not auth.is_admin(message.chat.id):
            await message.answer("🚫 Только для администратора.")
            return
        users = await auth.get_all_users()
        if not users:
            await message.answer("📭 Список пользователей пуст.")
            return
        lines = [f"👥 <b>Пользователи ({len(users)})</b>\n"]
        for uid in users:
            lines.append(f"• <code>{uid}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")

    asyncio.create_task(scanner.run_forever())
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
