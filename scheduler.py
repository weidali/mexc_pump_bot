"""
Scheduler — автоматическая пауза/возобновление по расписанию UTC.

Логика:
  Pump сканер:   07:45–21:15 UTC  (London open → NY close + буфер)
  BTC стратегия: 04:45–21:15 UTC  (за 15 мин до формирования 4ч свечи)

Вне этих окон компоненты ставятся на паузу и HTTP запросов нет.
Уведомление в Telegram при переходах.
"""
import logging
from datetime import datetime, timezone, time
from typing import Callable, List

logger = logging.getLogger(__name__)


def _parse_time(t: str) -> time:
    """Парсит "07:45" → time(7, 45)."""
    h, m = map(int, t.split(":"))
    return time(h, m)


def _in_window(now_time: time, start: time, end: time) -> bool:
    """Проверяет попадает ли время в окно (поддерживает переход через полночь)."""
    if start <= end:
        return start <= now_time <= end
    else:
        # Переход через полночь: 22:00–06:00
        return now_time >= start or now_time <= end


class Scheduler:
    def __init__(self, config, bot, get_subscribers: Callable):
        self.cfg = config
        self.bot = bot
        self.get_subscribers = get_subscribers

        self._scanner_active: bool = True
        self._btc_active: bool = True
        self._notified_pause_scanner: bool = False
        self._notified_pause_btc: bool = False

    async def tick(self, scanner, btc_strategy):
        """
        Вызывается каждую минуту из scanner.run_forever().
        Проверяет расписание и управляет паузой/возобновлением.
        """
        if not self.cfg.AUTO_SCHEDULE:
            return

        now_utc = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)

        scan_start = _parse_time(self.cfg.ACTIVE_HOURS_START)
        scan_end   = _parse_time(self.cfg.ACTIVE_HOURS_END)
        btc_start  = _parse_time(self.cfg.BTC_ACTIVE_START)
        btc_end    = _parse_time(self.cfg.BTC_ACTIVE_END)

        should_scan_run = _in_window(now_utc, scan_start, scan_end)
        should_btc_run  = _in_window(now_utc, btc_start, btc_end)

        # ── Pump сканер ───────────────────────────────────────
        if should_scan_run and not scanner.is_running:
            scanner.resume()
            self._notified_pause_scanner = False
            logger.info(f"Scanner resumed at {now_utc}")
            await self._notify(
                f"▶️ <b>Сканер возобновлён</b>\n"
                f"🕐 {now_utc.strftime('%H:%M')} UTC — начало активной сессии\n"
                f"🔍 Мониторю pump & dump сигналы"
            )

        elif not should_scan_run and scanner.is_running:
            scanner.pause()
            logger.info(f"Scanner paused at {now_utc}")
            if not self._notified_pause_scanner:
                self._notified_pause_scanner = True
                resumes_at = self.cfg.ACTIVE_HOURS_START
                await self._notify(
                    f"⏸ <b>Сканер на паузе</b>\n"
                    f"🕐 {now_utc.strftime('%H:%M')} UTC — рынок спит\n"
                    f"▶️ Возобновлю в {resumes_at} UTC"
                )

        # ── BTC стратегия ─────────────────────────────────────
        if should_btc_run and not btc_strategy._running:
            btc_strategy.resume()
            self._notified_pause_btc = False
            logger.info(f"BTC strategy resumed at {now_utc}")
            await self._notify(
                f"▶️ <b>BTC стратегия возобновлена</b>\n"
                f"🕐 {now_utc.strftime('%H:%M')} UTC\n"
                f"👁 Жду формирования NY диапазона в {self.cfg.BTC_ACTIVE_START} UTC"
            )

        elif not should_btc_run and btc_strategy._running:
            btc_strategy.pause()
            logger.info(f"BTC strategy paused at {now_utc}")
            if not self._notified_pause_btc:
                self._notified_pause_btc = True
                await self._notify(
                    f"⏸ <b>BTC стратегия на паузе</b>\n"
                    f"🕐 {now_utc.strftime('%H:%M')} UTC\n"
                    f"▶️ Возобновлю в {self.cfg.BTC_ACTIVE_START} UTC"
                )

    async def _notify(self, text: str):
        subscribers = await self.get_subscribers()
        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.debug(f"Schedule notify failed to {chat_id}: {e}")

    def get_schedule_info(self) -> str:
        """Форматирует текущее расписание для /status."""
        enabled = "✅ включено" if self.cfg.AUTO_SCHEDULE else "❌ выключено"
        return (
            f"\n⏰ <b>Расписание ({enabled}):</b>\n"
            f"  🔀 Сканер: {self.cfg.ACTIVE_HOURS_START}–{self.cfg.ACTIVE_HOURS_END} UTC\n"
            f"  📈 BTC: {self.cfg.BTC_ACTIVE_START}–{self.cfg.BTC_ACTIVE_END} UTC"
        )
