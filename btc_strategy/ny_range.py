"""
NYRange — формирует диапазон первой 4-часовой свечи Нью-Йорка.

Нью-Йорк торговая сессия:
  NY midnight = 00:00 EST = 05:00 UTC (зима) / 04:00 UTC (лето, DST)
  Первая 4ч свеча: 05:00–09:00 UTC (зима) / 04:00–08:00 UTC (лето)

Бот автоматически определяет DST по текущей дате.

Логика:
  1. Определяем время открытия первой 4ч свечи NY (с учётом DST)
  2. Ждём закрытия свечи (через 4 часа)
  3. Фиксируем High/Low свечи как диапазон
  4. Публикуем алерт с диапазоном
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def is_dst(dt: datetime) -> bool:
    """
    Определяет действует ли летнее время в США (EDT, UTC-4).
    DST начинается: второе воскресенье марта в 02:00
    DST заканчивается: первое воскресенье ноября в 02:00
    """
    year = dt.year

    # Второе воскресенье марта
    march1 = date(year, 3, 1)
    days_to_sunday = (6 - march1.weekday()) % 7
    dst_start = datetime(year, 3, march1.day + days_to_sunday + 7, 2, 0, tzinfo=timezone.utc)
    # Приводим к UTC: EST = UTC-5, поэтому 02:00 EST = 07:00 UTC
    dst_start_utc = datetime(year, 3, march1.day + days_to_sunday + 7, 7, 0, tzinfo=timezone.utc)

    # Первое воскресенье ноября
    nov1 = date(year, 11, 1)
    days_to_sunday_nov = (6 - nov1.weekday()) % 7
    # 02:00 EDT = 06:00 UTC
    dst_end_utc = datetime(year, 11, nov1.day + days_to_sunday_nov, 6, 0, tzinfo=timezone.utc)

    return dst_start_utc <= dt < dst_end_utc


def get_ny_first_4h_times(for_date: datetime) -> Tuple[datetime, datetime]:
    """
    Возвращает (open_utc, close_utc) первой 4ч свечи NY для заданной даты.

    Зима (EST, UTC-5): NY 00:00 = UTC 05:00, свеча 05:00–09:00
    Лето  (EDT, UTC-4): NY 00:00 = UTC 04:00, свеча 04:00–08:00
    """
    if is_dst(for_date):
        offset_hours = 4   # EDT = UTC-4
    else:
        offset_hours = 5   # EST = UTC-5

    # NY 00:00 в UTC
    day = for_date.date()
    open_utc = datetime(day.year, day.month, day.day,
                        offset_hours, 0, 0, tzinfo=timezone.utc)
    close_utc = open_utc + timedelta(hours=4)
    return open_utc, close_utc


def get_ny_session_end(for_date: datetime) -> datetime:
    """NY сессия заканчивается в 21:00 UTC."""
    day = for_date.date()
    return datetime(day.year, day.month, day.day, 21, 0, 0, tzinfo=timezone.utc)


@dataclass
class NYRange:
    date: str             # дата торгового дня
    high: float           # хай первой 4ч свечи
    low: float            # лой первой 4ч свечи
    open: float           # открытие
    close: float          # закрытие
    range_size: float     # размер диапазона в $
    range_pct: float      # размер в %
    candle_open_utc: str  # время открытия свечи UTC
    candle_close_utc: str # время закрытия свечи UTC
    is_bullish: bool      # бычья или медвежья свеча

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def is_above(self, price: float) -> bool:
        return price > self.high

    def is_below(self, price: float) -> bool:
        return price < self.low

    def format_alert(self) -> str:
        direction = "🟢 Бычья" if self.is_bullish else "🔴 Медвежья"
        return (
            f"📐 <b>Диапазон NY сформирован</b>\n\n"
            f"📅 {self.date} | {self.candle_open_utc}–{self.candle_close_utc} UTC\n\n"
            f"🔼 Хай: <code>${self.high:,.2f}</code>\n"
            f"🔽 Лой: <code>${self.low:,.2f}</code>\n"
            f"📏 Размер: <code>${self.range_size:,.2f}</code> ({self.range_pct:.2f}%)\n"
            f"📍 Середина: <code>${self.midpoint:,.2f}</code>\n"
            f"Свеча: {direction}\n\n"
            f"👁 Слежу за выходом из диапазона на 5м ТФ\n"
            f"⏰ Сетапы ищу до 21:00 UTC"
        )
