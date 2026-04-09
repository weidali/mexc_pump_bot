"""
PumpHistory — статистика повторных пампов.

Для каждой монеты смотрим историю сигналов за 5 дней:
  - Сколько раз пампила
  - Средний интервал между пампами
  - Самый частый день недели
  - Предсказание следующего пампа

Если монета пампит регулярно — это говорит о том что
манипулятор работает по расписанию (типичное поведение
маркет-мейкеров на мелких шиткоинах).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HISTORY_DAYS = 5

DAYS_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт",
    4: "Пт", 5: "Сб", 6: "Вс"
}


@dataclass
class PumpHistoryResult:
    symbol: str
    pump_count: int                  # пампов за период
    period_days: int
    avg_interval_hours: float        # средний интервал между пампами
    most_common_day: Optional[str]   # самый частый день
    last_pump_hours_ago: float       # часов с последнего пампа
    is_regular: bool                 # регулярный паттерн?
    predicted_next: Optional[str]    # когда ожидать следующий
    details: List[str] = field(default_factory=list)


class PumpHistory:
    def __init__(self, db):
        self.db = db

    async def analyze(self, symbol: str) -> Optional[PumpHistoryResult]:
        """
        Анализирует историю сигналов по монете за последние HISTORY_DAYS дней.
        Возвращает None если пампов меньше 2.
        """
        since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
        signals = await self.db.get_signals_for_symbol(symbol, since=since)

        if len(signals) < 2:
            return None

        # Парсим временны́е метки
        times = []
        for s in signals:
            try:
                dt = datetime.fromisoformat(s["created_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                times.append(dt)
            except Exception:
                continue

        if len(times) < 2:
            return None

        times.sort()
        now = datetime.now(timezone.utc)

        # ── Интервалы между пампами ───────────────────────────
        intervals_h = [
            (times[i] - times[i-1]).total_seconds() / 3600
            for i in range(1, len(times))
        ]
        avg_interval = sum(intervals_h) / len(intervals_h)

        # ── Самый частый день ─────────────────────────────────
        day_counts: Dict[int, int] = {}
        for t in times:
            d = t.weekday()
            day_counts[d] = day_counts.get(d, 0) + 1
        most_common_day_num = max(day_counts, key=day_counts.get)
        most_common_day = DAYS_RU[most_common_day_num]

        # ── Часов с последнего пампа ──────────────────────────
        last_pump_hours_ago = (now - times[-1]).total_seconds() / 3600

        # ── Регулярность ──────────────────────────────────────
        # Паттерн регулярный если:
        # - 3+ пампов за период
        # - средний интервал < 48ч
        # - отклонение интервалов небольшое
        is_regular = False
        if len(times) >= 3 and avg_interval < 48:
            if len(intervals_h) > 1:
                mean = avg_interval
                variance = sum((x - mean) ** 2 for x in intervals_h) / len(intervals_h)
                cv = (variance ** 0.5) / mean if mean > 0 else 999
                is_regular = cv < 0.5  # коэффициент вариации < 50%
            else:
                is_regular = True

        # ── Предсказание следующего ───────────────────────────
        predicted_next = None
        if is_regular and avg_interval > 0:
            next_dt = times[-1] + timedelta(hours=avg_interval)
            hours_until = (next_dt - now).total_seconds() / 3600
            if -2 <= hours_until <= avg_interval * 1.5:
                if hours_until < 0:
                    predicted_next = "уже должен был быть (перегрет?)"
                elif hours_until < 1:
                    predicted_next = "менее чем через час ⚡"
                elif hours_until < 6:
                    predicted_next = f"через ~{hours_until:.1f}ч"
                else:
                    predicted_next = f"через ~{hours_until:.0f}ч ({next_dt.strftime('%d.%m %H:%M')} UTC)"

        # ── Детали ────────────────────────────────────────────
        details = [
            f"Пампов за {HISTORY_DAYS}д: {len(times)}",
            f"Средний интервал: {avg_interval:.1f}ч",
            f"Чаще всего: {most_common_day}",
        ]
        if predicted_next:
            details.append(f"Следующий: {predicted_next}")

        return PumpHistoryResult(
            symbol=symbol,
            pump_count=len(times),
            period_days=HISTORY_DAYS,
            avg_interval_hours=round(avg_interval, 1),
            most_common_day=most_common_day,
            last_pump_hours_ago=round(last_pump_hours_ago, 1),
            is_regular=is_regular,
            predicted_next=predicted_next,
            details=details,
        )

    def format_for_alert(self, history: "PumpHistoryResult") -> str:
        """Форматирует блок истории для добавления в алерт."""
        if history is None:
            return ""

        icon = "🔄" if history.is_regular else "📋"
        lines = [f"\n{icon} <b>История пампов ({history.period_days}д):</b>"]
        lines.append(f"  • Повторений: <b>{history.pump_count}x</b> / интервал ~{history.avg_interval_hours}ч")
        lines.append(f"  • Чаще всего в: <b>{history.most_common_day}</b>")

        if history.is_regular:
            lines.append(f"  ⚡ Паттерн регулярный!")
        if history.predicted_next:
            lines.append(f"  🎯 Следующий: {history.predicted_next}")

        return "\n".join(lines)
