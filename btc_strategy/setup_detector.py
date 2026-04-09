"""
SetupDetector — ищет сетапы на 5м ТФ по стратегии первой 4ч свечи NY.

Логика (строго по правилам стратегии):

Шаг 1 — Выход из диапазона:
  Ищем 5м свечу, которая ЗАКРЫЛАСЬ за пределами диапазона.
  (именно закрытие, не тень/фитиль)

Шаг 2 — Возврат в диапазон:
  После выхода ждём 5м свечу, которая ЗАКРЫЛАСЬ ВНУТРИ диапазона.
  - Закрылась внутри после выхода снизу → LONG сетап
  - Закрылась внутри после выхода сверху → SHORT сетап

Шаг 3 — Параметры сделки:
  LONG:  SL = лой пробойной свечи (та что вышла вниз)
         TP1 = вход + (вход - SL) * 2   (1:2 R:R)
         TP2 = вход + (вход - SL) * 3   (1:3 R:R)

  SHORT: SL = хай пробойной свечи (та что вышла вверх)
         TP1 = вход - (SL - вход) * 2
         TP2 = вход - (SL - вход) * 3
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from .ny_range import NYRange

logger = logging.getLogger(__name__)


class SetupState(Enum):
    WAITING_RANGE     = "waiting_range"      # ждём формирования диапазона
    WATCHING          = "watching"           # диапазон есть, ищем выход
    BREAKOUT_DOWN     = "breakout_down"      # вышли вниз, ждём возврат
    BREAKOUT_UP       = "breakout_up"        # вышли вверх, ждём возврат
    SETUP_LONG        = "setup_long"         # сигнал на лонг
    SETUP_SHORT       = "setup_short"        # сигнал на шорт
    IN_TRADE          = "in_trade"           # в сделке
    SESSION_OVER      = "session_over"       # сессия закончена


@dataclass
class Breakout:
    direction: str          # "down" или "up"
    candle_open: float
    candle_high: float
    candle_low: float
    candle_close: float
    candle_time: str

    @property
    def sl_level(self) -> float:
        """SL ставим за лой/хай пробойной свечи."""
        return self.candle_low if self.direction == "down" else self.candle_high


@dataclass
class TradeSetup:
    direction: str          # "long" или "short"
    entry_price: float      # цена входа (close возвратной свечи)
    stop_loss: float        # SL
    tp1: float              # TP1 (1:2)
    tp2: float              # TP2 (1:3)
    risk: float             # размер стопа в $
    setup_time: str
    breakout: Breakout
    ny_range: NYRange

    @property
    def risk_reward_1(self) -> float:
        return abs(self.tp1 - self.entry_price) / abs(self.entry_price - self.stop_loss)

    @property
    def risk_reward_2(self) -> float:
        return abs(self.tp2 - self.entry_price) / abs(self.entry_price - self.stop_loss)

    def format_alert(self) -> str:
        emoji = "🟢" if self.direction == "long" else "🔴"
        action = "LONG" if self.direction == "long" else "SHORT"
        sl_pct = abs(self.entry_price - self.stop_loss) / self.entry_price * 100

        return (
            f"{emoji} <b>СЕТАП {action} — BTCUSDT</b>\n\n"
            f"📍 Вход: <code>${self.entry_price:,.2f}</code>\n"
            f"🛑 SL: <code>${self.stop_loss:,.2f}</code> (-{sl_pct:.2f}%)\n"
            f"🎯 TP1: <code>${self.tp1:,.2f}</code> (1:2)\n"
            f"🎯 TP2: <code>${self.tp2:,.2f}</code> (1:3)\n\n"
            f"📐 Диапазон NY: ${self.ny_range.low:,.2f} — ${self.ny_range.high:,.2f}\n"
            f"💰 Риск: ${self.risk:,.2f} на 1 BTC\n\n"
            f"⏰ {self.setup_time} UTC\n"
            f"⚠️ <i>Не финансовый совет. DYOR.</i>"
        )


class SetupDetector:
    def __init__(self):
        self.state: SetupState = SetupState.WAITING_RANGE
        self.ny_range: Optional[NYRange] = None
        self.current_breakout: Optional[Breakout] = None
        self.setups_today: List[TradeSetup] = []
        self.date_str: str = ""

    def reset_day(self):
        """Сброс состояния в начале нового торгового дня."""
        self.state = SetupState.WAITING_RANGE
        self.ny_range = None
        self.current_breakout = None
        self.setups_today = []
        logger.info("SetupDetector: new day reset")

    def set_range(self, ny_range: NYRange, date_str: str):
        """Устанавливаем диапазон после закрытия первой 4ч свечи."""
        self.ny_range = ny_range
        self.date_str = date_str
        self.state = SetupState.WATCHING
        logger.info(f"Range set: {ny_range.low:.2f} — {ny_range.high:.2f}")

    def set_session_over(self):
        self.state = SetupState.SESSION_OVER

    def set_in_trade(self):
        self.state = SetupState.IN_TRADE

    def set_watching(self):
        """Возврат в режим поиска после закрытия сделки."""
        if self.state != SetupState.SESSION_OVER:
            self.state = SetupState.WATCHING
            self.current_breakout = None

    def process_candle(
        self,
        open_: float,
        high: float,
        low: float,
        close: float,
        candle_time: str,
    ) -> Optional[object]:
        """
        Обрабатывает новую закрытую 5м свечу.
        Возвращает:
          - ("breakout", direction, breakout_obj) — выход из диапазона
          - ("setup", TradeSetup) — найден сетап
          - None — ничего интересного
        """
        if self.state not in (SetupState.WATCHING, SetupState.BREAKOUT_DOWN, SetupState.BREAKOUT_UP):
            return None
        if self.ny_range is None:
            return None

        rng = self.ny_range

        # ── Шаг 1: Ищем выход из диапазона ───────────────────
        if self.state == SetupState.WATCHING:
            if close < rng.low:
                # Свеча закрылась НИЖЕ диапазона
                self.current_breakout = Breakout(
                    direction="down",
                    candle_open=open_, candle_high=high,
                    candle_low=low, candle_close=close,
                    candle_time=candle_time,
                )
                self.state = SetupState.BREAKOUT_DOWN
                logger.info(f"Breakout DOWN at {close:.2f}, time={candle_time}")
                return ("breakout", "down", self.current_breakout)

            elif close > rng.high:
                # Свеча закрылась ВЫШЕ диапазона
                self.current_breakout = Breakout(
                    direction="up",
                    candle_open=open_, candle_high=high,
                    candle_low=low, candle_close=close,
                    candle_time=candle_time,
                )
                self.state = SetupState.BREAKOUT_UP
                logger.info(f"Breakout UP at {close:.2f}, time={candle_time}")
                return ("breakout", "up", self.current_breakout)

        # ── Шаг 2: Ждём возврат в диапазон ───────────────────
        elif self.state in (SetupState.BREAKOUT_DOWN, SetupState.BREAKOUT_UP):
            if rng.contains(close):
                # Свеча закрылась ВНУТРИ диапазона — сетап!
                setup = self._build_setup(close, candle_time)
                if setup:
                    self.setups_today.append(setup)
                    self.state = SetupState.IN_TRADE
                    logger.info(
                        f"Setup {setup.direction.upper()} at {close:.2f}, "
                        f"SL={setup.stop_loss:.2f}, TP1={setup.tp1:.2f}"
                    )
                    return ("setup", setup)

            # Если цена продолжила движение против диапазона — сбрасываем
            # (ждём новый выход)
            elif (self.state == SetupState.BREAKOUT_DOWN and close < rng.low * 0.995) or \
                 (self.state == SetupState.BREAKOUT_UP and close > rng.high * 1.005):
                # Цена уходит дальше — обновляем пробойную свечу
                if self.current_breakout:
                    if self.state == SetupState.BREAKOUT_DOWN:
                        self.current_breakout.candle_low = min(self.current_breakout.candle_low, low)
                    else:
                        self.current_breakout.candle_high = max(self.current_breakout.candle_high, high)

        return None

    def _build_setup(self, entry_price: float, setup_time: str) -> Optional[TradeSetup]:
        if not self.current_breakout or not self.ny_range:
            return None

        bo = self.current_breakout

        if bo.direction == "down":
            # Вышли вниз → вернулись → LONG
            sl = bo.sl_level  # лой пробойной свечи
            risk = entry_price - sl
            if risk <= 0:
                return None
            tp1 = entry_price + risk * 2
            tp2 = entry_price + risk * 3
            direction = "long"
        else:
            # Вышли вверх → вернулись → SHORT
            sl = bo.sl_level  # хай пробойной свечи
            risk = sl - entry_price
            if risk <= 0:
                return None
            tp1 = entry_price - risk * 2
            tp2 = entry_price - risk * 3
            direction = "short"

        return TradeSetup(
            direction=direction,
            entry_price=entry_price,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            risk=abs(risk),
            setup_time=setup_time,
            breakout=bo,
            ny_range=self.ny_range,
        )
