"""
MEXC Pump & Dump Detection Bot
Main entry point
"""
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message

from config import Config
from scanner import Scanner
from db import Database
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    config = Config()
    db = Database()
    await db.init()

    bot = Bot(token=config.TELEGRAM_TOKEN)
    dp = Dispatcher()

    scanner = Scanner(config=config, bot=bot, db=db)

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        await message.answer(
            "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n"
            "Бот отслеживает манипуляции на шиткоинах MEXC.\n\n"
            "Команды:\n"
            "/start — это сообщение\n"
            "/status — статус сканера\n"
            "/top — топ-20 монет по объёму (сейчас)\n"
            "/stats — статистика сигналов за 24ч\n"
            "/pause — приостановить алерты\n"
            "/resume — возобновить алерты\n\n"
            "⚡ Сканирование запущено автоматически.",
            parse_mode="HTML"
        )
        await db.add_subscriber(message.chat.id)

    @dp.message(Command("status"))
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

    @dp.message(Command("top"))
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

    @dp.message(Command("stats"))
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

    @dp.message(Command("pause"))
    async def cmd_pause(message: Message):
        scanner.pause()
        await message.answer("⏸ Алерты приостановлены. /resume — возобновить.")

    @dp.message(Command("resume"))
    async def cmd_resume(message: Message):
        scanner.resume()
        await message.answer("▶️ Алерты возобновлены.")

    # Запускаем сканер фоном
    asyncio.create_task(scanner.run_forever())

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
