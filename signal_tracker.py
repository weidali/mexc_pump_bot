"""
SignalTracker — отслеживает follow-up по выпущенным сигналам.

После отправки алерта планирует проверку через 15 и 30 минут:
  - Цена выросла ещё → памп продолжается, осторожно
  - Цена упала      → сигнал отработал, шорт в плюсе
  - Цена на месте   → боковик
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

FOLLOWUP_MINUTES = [15, 30]


@dataclass
class TrackedSignal:
    symbol:       str
    entry_price:  float
    signal_time:  datetime
    chat_ids:     List[int]
    checked_at:   List[int] = field(default_factory=list)  # какие интервалы уже проверены


class SignalTracker:
    def __init__(self, bot, mexc_client):
        self.bot = bot
        self.client = mexc_client
        self._tracked: Dict[str, TrackedSignal] = {}

    def track(self, symbol: str, entry_price: float, chat_ids: List[int]):
        """Начать отслеживание сигнала."""
        self._tracked[symbol] = TrackedSignal(
            symbol=symbol,
            entry_price=entry_price,
            signal_time=datetime.now(timezone.utc),
            chat_ids=chat_ids,
        )
        logger.info(f"Tracking signal: {symbol} @ {entry_price}")

    async def check_followups(self):
        """
        Вызывается из основного цикла сканера каждую минуту.
        Проверяет не пора ли слать follow-up по отслеживаемым сигналам.
        """
        now = datetime.now(timezone.utc)
        to_remove = []

        for symbol, tracked in list(self._tracked.items()):
            elapsed_min = (now - tracked.signal_time).total_seconds() / 60

            for interval in FOLLOWUP_MINUTES:
                if interval in tracked.checked_at:
                    continue
                if elapsed_min >= interval:
                    await self._send_followup(tracked, interval)
                    tracked.checked_at.append(interval)

            # Убираем из трекера после последней проверки
            if all(m in tracked.checked_at for m in FOLLOWUP_MINUTES):
                to_remove.append(symbol)

        for sym in to_remove:
            del self._tracked[sym]
            logger.info(f"Signal tracking completed: {sym}")

    async def _send_followup(self, tracked: TrackedSignal, minutes: int):
        """Получает текущую цену и отправляет follow-up."""
        try:
            klines = await self.client.get_klines(tracked.symbol, interval="1m", limit=2)
            if not klines:
                return
            current_price = float(klines[-1][4])
        except Exception as e:
            logger.debug(f"Follow-up price fetch failed {tracked.symbol}: {e}")
            return

        entry   = tracked.entry_price
        change  = (current_price - entry) / entry * 100

        # Определяем итог
        if change <= -5:
            icon   = "✅"
            result = f"упала на {abs(change):.1f}% — сигнал отработал"
        elif change <= -2:
            icon   = "🟢"
            result = f"снизилась на {abs(change):.1f}%"
        elif change < 2:
            icon   = "➡️"
            result = f"боковик ({change:+.1f}%)"
        elif change < 5:
            icon   = "🟡"
            result = f"всё ещё растёт +{change:.1f}% — осторожно"
        else:
            icon   = "🔴"
            result = f"памп продолжается +{change:.1f}% — не шортить!"

        text = (
            f"{icon} <b>Follow-up {minutes}м · {tracked.symbol}</b>\n\n"
            f"📍 Вход: <code>{entry:.6g}</code> USDT\n"
            f"💵 Сейчас: <code>{current_price:.6g}</code> USDT\n"
            f"📊 Изменение: <b>{change:+.1f}%</b>\n\n"
            f"💬 {result}"
        )

        for chat_id in tracked.chat_ids:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.debug(f"Follow-up send failed to {chat_id}: {e}")

        logger.info(f"Follow-up {minutes}m sent for {tracked.symbol}: {change:+.1f}%")
