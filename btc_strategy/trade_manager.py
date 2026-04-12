"""
TradeManager — управляет открытой сделкой.

Отслеживает:
  - Достижение TP1 (1:2) → частичная фиксация 50%, SL переносим в безубыток
  - Достижение TP2 (1:3) → закрытие оставшихся 50%
  - Срабатывание SL → фиксация убытка
  - Принудительное закрытие в 21:00 UTC

Отправляет уведомления на каждом шаге.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .setup_detector import TradeSetup

logger = logging.getLogger(__name__)


class TradeStatus(Enum):
    OPEN        = "open"
    TP1_HIT     = "tp1_hit"       # TP1 достигнут, идём к TP2
    WIN_FULL    = "win_full"      # оба TP закрыты
    WIN_PARTIAL = "win_partial"   # TP1 + SL по безубытку
    LOSS        = "loss"          # стоп
    CLOSED_EOD  = "closed_eod"   # закрыт по времени сессии


@dataclass
class TradeResult:
    setup: TradeSetup
    status: TradeStatus
    exit_price: float
    exit_price_tp2: Optional[float]
    pnl_tp1: float         # P&L от первой половины
    pnl_tp2: float         # P&L от второй половины
    pnl_total: float       # суммарный P&L на 1 BTC
    open_time: str
    close_time: str
    duration_min: int


class TradeManager:
    def __init__(self):
        self.active_setup: Optional[TradeSetup] = None
        self.tp1_hit: bool = False
        self.breakeven_sl: Optional[float] = None
        self.open_time: Optional[datetime] = None
        self.last_price: float = 0.0
        self.last_result: Optional[TradeResult] = None

    @property
    def is_active(self) -> bool:
        return self.active_setup is not None

    def open_trade(self, setup: TradeSetup):
        self.active_setup = setup
        self.tp1_hit = False
        self.breakeven_sl = None
        self.open_time = datetime.now(timezone.utc)
        logger.info(
            f"Trade opened: {setup.direction.upper()} {setup.entry_price:.2f} "
            f"SL={setup.stop_loss:.2f} TP1={setup.tp1:.2f} TP2={setup.tp2:.2f}"
        )

    def check_price(self, high: float, low: float, close: float, candle_time: str):
        """
        Проверяет достигнуты ли уровни на новой 5м свече.
        Возвращает (event_type, message) или None.
        """
        if not self.active_setup:
            return None

        s = self.active_setup
        self.last_price = close

        # Текущий SL (может быть передвинут в безубыток)
        current_sl = self.breakeven_sl if self.breakeven_sl else s.stop_loss

        if s.direction == "long":
            # ── LONG ─────────────────────────────────────────
            if not self.tp1_hit:
                if high >= s.tp1:
                    self.tp1_hit = True
                    self.breakeven_sl = s.entry_price + (s.entry_price - s.stop_loss) * 0.1
                    return ("tp1", self._fmt_tp1(s, candle_time))
                if low <= current_sl:
                    result = self._close_trade(current_sl, None, "sl", candle_time)
                    return ("sl", self._fmt_sl(result, candle_time))
            else:
                if high >= s.tp2:
                    result = self._close_trade(s.tp1, s.tp2, "tp2", candle_time)
                    return ("tp2", self._fmt_tp2(result, candle_time))
                if low <= current_sl:
                    result = self._close_trade(s.tp1, current_sl, "breakeven", candle_time)
                    return ("breakeven", self._fmt_breakeven(result, candle_time))

        else:
            # ── SHORT ─────────────────────────────────────────
            if not self.tp1_hit:
                if low <= s.tp1:
                    self.tp1_hit = True
                    self.breakeven_sl = s.entry_price - (s.stop_loss - s.entry_price) * 0.1
                    return ("tp1", self._fmt_tp1(s, candle_time))
                if high >= current_sl:
                    result = self._close_trade(current_sl, None, "sl", candle_time)
                    return ("sl", self._fmt_sl(result, candle_time))
            else:
                if low <= s.tp2:
                    result = self._close_trade(s.tp1, s.tp2, "tp2", candle_time)
                    return ("tp2", self._fmt_tp2(result, candle_time))
                if high >= current_sl:
                    result = self._close_trade(s.tp1, current_sl, "breakeven", candle_time)
                    return ("breakeven", self._fmt_breakeven(result, candle_time))

        return None

    def close_eod(self, current_price: float, candle_time: str):
        """Принудительное закрытие в конце сессии (21:00 UTC)."""
        if not self.active_setup:
            return None
        result = self._close_trade(
            current_price,
            None,
            "eod",
            candle_time
        )
        return ("eod", self._fmt_eod(result, current_price, candle_time))

    def _close_trade(
        self,
        exit1: float,
        exit2: Optional[float],
        reason: str,
        candle_time: str,
    ) -> TradeResult:
        s = self.active_setup
        sign = 1 if s.direction == "long" else -1

        pnl1 = (exit1 - s.entry_price) * sign
        pnl2 = ((exit2 - s.entry_price) * sign) if exit2 else 0.0
        pnl_total = pnl1 * 0.5 + pnl2 * 0.5  # каждая половина = 50%

        duration = 0
        if self.open_time:
            duration = int((datetime.now(timezone.utc) - self.open_time).total_seconds() / 60)

        status_map = {
            "sl":         TradeStatus.LOSS,
            "tp2":        TradeStatus.WIN_FULL,
            "breakeven":  TradeStatus.WIN_PARTIAL,
            "eod":        TradeStatus.CLOSED_EOD,
        }

        result = TradeResult(
            setup=s,
            status=status_map.get(reason, TradeStatus.CLOSED_EOD),
            exit_price=exit1,
            exit_price_tp2=exit2,
            pnl_tp1=pnl1,
            pnl_tp2=pnl2,
            pnl_total=pnl_total,
            open_time=self.open_time.strftime("%H:%M") if self.open_time else "—",
            close_time=candle_time,
            duration_min=duration,
        )

        self.active_setup = None
        self.tp1_hit = False
        self.breakeven_sl = None
        self.open_time = None
        self.last_result = result
        return result

    # ── Форматирование уведомлений ────────────────────────────

    def _fmt_tp1(self, s: TradeSetup, t: str) -> str:
        sign = 1 if s.direction == "long" else -1
        pnl1 = (s.tp1 - s.entry_price) * sign
        be = self.breakeven_sl
        return (
            f"🎯 <b>TP1 достигнут! — {'LONG' if s.direction=='long' else 'SHORT'} BTCUSDT</b>\n\n"
            f"💵 Зафиксировано 50% по <code>${s.tp1:,.2f}</code>\n"
            f"💰 P&L (50%): <b>+${pnl1/2:,.2f}</b> на 1 BTC\n\n"
            f"🔒 SL перенесён в безубыток: <code>${be:,.2f}</code>\n"
            f"🎯 Цель TP2: <code>${s.tp2:,.2f}</code>\n"
            f"⏰ {t} UTC"
        )

    def _fmt_tp2(self, r: TradeResult, t: str) -> str:
        return (
            f"🏆 <b>TP2 достигнут! Полная цель — {'LONG' if r.setup.direction=='long' else 'SHORT'} BTCUSDT</b>\n\n"
            f"💵 Закрыто 50% по <code>${r.setup.tp2:,.2f}</code>\n"
            f"📊 P&L итого: <b>+${r.pnl_total:,.2f}</b> на 1 BTC\n"
            f"  • Первая половина: +${r.pnl_tp1/2:,.2f}\n"
            f"  • Вторая половина: +${r.pnl_tp2/2:,.2f}\n\n"
            f"⏱ Длительность: {r.duration_min} мин\n"
            f"⏰ {t} UTC"
        )

    def _fmt_sl(self, r: TradeResult, t: str) -> str:
        return (
            f"🛑 <b>Стоп — {'LONG' if r.setup.direction=='long' else 'SHORT'} BTCUSDT</b>\n\n"
            f"💵 Закрыто по <code>${r.exit_price:,.2f}</code>\n"
            f"📊 P&L: <b>-${abs(r.pnl_total):,.2f}</b> на 1 BTC\n\n"
            f"⏱ Длительность: {r.duration_min} мин\n"
            f"⏰ {t} UTC"
        )

    def _fmt_breakeven(self, r: TradeResult, t: str) -> str:
        return (
            f"🔒 <b>Безубыток — {'LONG' if r.setup.direction=='long' else 'SHORT'} BTCUSDT</b>\n\n"
            f"✅ TP1 был зафиксирован: +${r.pnl_tp1/2:,.2f}\n"
            f"〰️ Вторая половина закрыта в б/у по <code>${r.exit_price:,.2f}</code>\n"
            f"📊 Итого: <b>+${r.pnl_total:,.2f}</b> на 1 BTC\n\n"
            f"⏱ Длительность: {r.duration_min} мин\n"
            f"⏰ {t} UTC"
        )

    def _fmt_eod(self, r: TradeResult, price: float, t: str) -> str:
        pnl_sign = "+" if r.pnl_total >= 0 else ""
        return (
            f"🕘 <b>Закрыто по времени сессии (21:00 UTC)</b>\n\n"
            f"💵 Цена закрытия: <code>${price:,.2f}</code>\n"
            f"📊 P&L: <b>{pnl_sign}${r.pnl_total:,.2f}</b> на 1 BTC\n\n"
            f"⏰ {t} UTC"
        )
