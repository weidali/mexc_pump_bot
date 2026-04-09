"""
MEXC Pump & Dump Detection Bot
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import Config
from scanner import Scanner
from db import Database
from auth import Auth, require_auth
from version import __version__, __release_notes__

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

    # ── /start — лендинг бота ────────────────────────────────
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        chat_id = message.chat.id

        if await auth.is_allowed(chat_id):
            if auth.is_admin(chat_id):
                await message.answer(
                    "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
                    f"👑 <b>Режим администратора</b> · v{__version__}\n\n"
                    "📊 <b>Сканер:</b>\n"
                    "/status — статус и статистика\n"
                    "/top — топ-20 монет по объёму\n"
                    "/stats — сигналы за 24ч\n"
                    "/pause — приостановить алерты\n"
                    "/resume — возобновить алерты\n\n"
                    "👥 <b>Пользователи:</b>\n"
                    "/users — список всех пользователей\n"
                    "/adduser &lt;id&gt; — одобрить пользователя\n"
                    "/removeuser &lt;id&gt; — удалить пользователя\n\n"
                    "🗄 <b>База данных:</b>\n"
                    "/dbstats — размер и статистика БД\n"
                    "/dbclean — очистить старые записи",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
                    "Бот отслеживает манипуляции на шиткоинах MEXC "
                    "и даёт сигналы на шорт.\n\n"
                    "📋 <b>Команды:</b>\n"
                    "/status — статус сканера\n"
                    "/top — топ-20 монет по объёму\n"
                    "/stats — сигналы за 24ч\n"
                    "/pause — приостановить алерты\n"
                    "/resume — возобновить алерты",
                    parse_mode="HTML"
                )
        else:
            # Незнакомый пользователь — красивый лендинг
            await message.answer(
                "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
                "Бот в реальном времени сканирует топ шиткоины на MEXC "
                "и выявляет признаки pump &amp; dump манипуляций.\n\n"
                "⚡ <b>Что умеет бот:</b>\n"
                "• Отслеживает 80+ монет каждую минуту\n"
                "• Детектирует аномальный всплеск объёма\n"
                "• Ловит резкий памп цены\n"
                "• Анализирует CVD-дивергенцию (продают в рост)\n"
                "• Отправляет алерт с оценкой силы сигнала\n\n"
                "🔒 <b>Доступ закрыт</b>\n"
                "Бот работает только для авторизованных пользователей.\n\n"
                "👉 Отправьте /request чтобы запросить доступ.",
                parse_mode="HTML"
            )

    # ── /request — запрос доступа ────────────────────────────
    @dp.message(Command("request"))
    async def cmd_request(message: Message):
        chat_id = message.chat.id
        username = message.from_user.username or "—"
        full_name = message.from_user.full_name or "—"

        if await auth.is_allowed(chat_id):
            await message.answer("✅ У вас уже есть доступ. Отправьте /start")
            return

        # Сохраняем в БД со статусом pending (approved=False)
        await db.add_subscriber(
            chat_id,
            approved=False,
            full_name=full_name,
            username=username,
        )

        await message.answer(
            "📨 <b>Запрос отправлен!</b>\n\n"
            "Администратор рассмотрит вашу заявку.\n"
            "Как только доступ будет одобрен — вы получите уведомление.",
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
                    f"Одобрить: <code>/adduser {chat_id}</code>\n"
                    f"Отклонить: просто проигнорировать",
                    parse_mode="HTML"
                )
                logger.info(f"Access request from {chat_id} (@{username}) sent to admin")
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
        rows = await db.get_all_subscribers_info()
        if not rows:
            await message.answer("📭 Список пользователей пуст.")
            return

        approved = [r for r in rows if r["approved"]]
        pending  = [r for r in rows if not r["approved"]]

        # ── Активные пользователи ─────────────────────────────
        if approved:
            lines = [f"✅ <b>Активные ({len(approved)})</b>\n"]
            for r in approved:
                name = r["full_name"] or "—"
                uname = f" @{r['username']}" if r["username"] else ""
                date = r["added_at"][:10]
                lines.append(f"• {name}{uname} · <code>{r['chat_id']}</code> · с {date}")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"🗑 Удалить {r['full_name'] or r['chat_id']}",
                    callback_data=f"remove_{r['chat_id']}"
                )]
                for r in approved
            ])
            await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

        # ── Ожидают одобрения ─────────────────────────────────
        if pending:
            lines = [f"⏳ <b>Ожидают одобрения ({len(pending)})</b>\n"]
            for r in pending:
                name = r["full_name"] or "—"
                uname = f" @{r['username']}" if r["username"] else ""
                date = r["added_at"][:10]
                lines.append(f"• {name}{uname} · <code>{r['chat_id']}</code> · {date}")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ {r['full_name'] or r['chat_id']}",
                        callback_data=f"approve_{r['chat_id']}"
                    ),
                    InlineKeyboardButton(
                        text="❌",
                        callback_data=f"decline_{r['chat_id']}"
                    ),
                ]
                for r in pending
            ])
            await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

        if not approved and not pending:
            await message.answer("📭 Список пользователей пуст.")

    # ── Callback кнопок одобрения/удаления ───────────────────
    @dp.callback_query(lambda c: c.data and c.data.startswith(("approve_", "remove_", "decline_")))
    async def handle_user_action(callback: CallbackQuery):
        if not auth.is_admin(callback.from_user.id):
            await callback.answer("🚫 Нет доступа", show_alert=True)
            return

        action, target_id = callback.data.split("_", 1)
        target_id = int(target_id)

        if action == "approve":
            added = await auth.add_user(target_id, callback.from_user.id)
            if added:
                await callback.answer("✅ Пользователь одобрен")
                await callback.message.edit_text(
                    callback.message.text + f"\n\n✅ <code>{target_id}</code> одобрен",
                    parse_mode="HTML"
                )
                try:
                    await callback.bot.send_message(
                        target_id,
                        "✅ <b>Доступ одобрен!</b>\n\nТеперь вы можете пользоваться ботом.\nОтправьте /start",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            else:
                await callback.answer("ℹ️ Уже одобрен")

        elif action == "remove":
            removed = await auth.remove_user(target_id)
            if removed:
                await callback.answer("🗑 Удалён")
                await callback.message.edit_text(
                    callback.message.text + f"\n\n🗑 <code>{target_id}</code> удалён",
                    parse_mode="HTML",
                    reply_markup=None
                )
            else:
                await callback.answer("ℹ️ Пользователь не найден")

        elif action == "decline":
            removed = await db.remove_subscriber(target_id)
            if removed:
                await callback.answer("❌ Отклонён и удалён из списка")
                await callback.message.edit_text(
                    callback.message.text + f"\n\n❌ <code>{target_id}</code> отклонён",
                    parse_mode="HTML",
                    reply_markup=None
                )
            else:
                await callback.answer("ℹ️ Не найден")

    # ── /dbstats — статистика и очистка БД (только админ) ───
    @dp.message(Command("dbstats"))
    async def cmd_dbstats(message: Message):
        if not auth.is_admin(message.chat.id):
            await message.answer("🚫 Только для администратора.")
            return
        stats = await db.get_db_stats()
        await message.answer(
            f"🗄 <b>Статистика базы данных</b>\n\n"
            f"📦 Размер файла: <b>{stats['size_kb']} КБ</b>\n"
            f"📊 Всего сигналов: <b>{stats['total_signals']}</b>\n"
            f"📅 За последние 7 дней: <b>{stats['week_signals']}</b>\n"
            f"🕐 Самый старый: <b>{stats['oldest']}</b>\n"
            f"🕐 Самый новый: <b>{stats['newest']}</b>\n\n"
            f"⚙️ Хранение: <b>{config.DB_KEEP_DAYS} дней</b>\n\n"
            f"Для ручной очистки: /dbclean",
            parse_mode="HTML"
        )

    @dp.message(Command("dbclean"))
    async def cmd_dbclean(message: Message):
        if not auth.is_admin(message.chat.id):
            await message.answer("🚫 Только для администратора.")
            return
        await message.answer("🧹 Запускаю очистку...")
        deleted = await db.cleanup_old_signals(keep_days=config.DB_KEEP_DAYS)
        stats = await db.get_db_stats()
        await message.answer(
            f"✅ <b>Очистка завершена</b>\n\n"
            f"🗑 Удалено записей: <b>{deleted}</b>\n"
            f"📦 Размер после: <b>{stats['size_kb']} КБ</b>\n"
            f"📊 Осталось сигналов: <b>{stats['total_signals']}</b>",
            parse_mode="HTML"
        )

    # ── /version ──────────────────────────────────────────────
    @dp.message(Command("version"))
    @require_auth(auth)
    async def cmd_version(message: Message):
        await message.answer(
            f"🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
            f"📦 Версия: <b>v{__version__}</b>\n"
            f"📝 {__release_notes__}",
            parse_mode="HTML"
        )

    asyncio.create_task(scanner.run_forever())
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())