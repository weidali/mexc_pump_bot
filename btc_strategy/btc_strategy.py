"""
BTCStrategy — главный оркестратор стратегии первой 4ч свечи NY.

Цикл работы (каждые ~30 секунд):
  1. Проверяем — сформировался ли NY диапазон (закрылась 4ч свеча)
  2. Если диапазон есть — мониторим 5м свечи
  3. Передаём свечи в SetupDetector
  4. При сигнале — открываем сделку в TradeManager
  5. Следим за SL/TP по каждой новой свече
  6. В 21:00 UTC — принудительно закрываем и сбрасываем день
"""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from .ny_range import NYRange, get_ny_first_4h_times, get_ny_session_end
from .setup_detector import SetupDetector, SetupState
from .trade_manager import TradeManager
from .trade_journal import TradeJournal

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SEC = 30   # проверяем каждые 30 секунд


class BTCStrategy:
    def __init__(self, bot: Bot, mexc_client, db_path: str, subscribers_fn):
        self.bot = bot
        self.client = mexc_client
        self.journal = TradeJournal(db_path)
        self.get_subscribers = subscribers_fn  # async fn → List[int]

        self.detector = SetupDetector()
        self.trade_mgr = TradeManager()

        self._last_4h_candle_time: str = ""
        self._last_5m_candle_time: str = ""
        self._running = True
        self._current_day: str = ""

    async def init(self):
        await self.journal.init()

    def pause(self): self._running = False
    def resume(self): self._running = True

    # ── Главный цикл ─────────────────────────────────────────

    async def run_forever(self):
        logger.info("BTCStrategy started")
        while True:
            try:
                if self._running:
                    await self._tick()
            except Exception as e:
                logger.exception(f"BTCStrategy tick error: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def _tick(self):
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # ── Новый торговый день ───────────────────────────────
        if today != self._current_day:
            self._current_day = today
            self.detector.reset_day()
            self.trade_mgr.active_setup and logger.warning("New day but trade still open!")
            logger.info(f"New trading day: {today}")

        # ── Конец сессии 21:00 UTC ────────────────────────────
        session_end = get_ny_session_end(now)
        if now >= session_end:
            if self.trade_mgr.is_active:
                # Закрываем открытую сделку
                klines = await self._get_5m_klines(1)
                if klines:
                    close = float(klines[-1][4])
                    t = datetime.utcfromtimestamp(klines[-1][0]/1000).strftime("%H:%M")
                    event = self.trade_mgr.close_eod(close, t)
                    if event:
                        await self.journal.save_trade(event[1].setup if hasattr(event[1], 'setup') else None)
                        await self._broadcast(event[1])
            if self.detector.state != SetupState.SESSION_OVER:
                self.detector.set_session_over()
                await self._broadcast(
                    "🕘 <b>NY сессия завершена (21:00 UTC)</b>\n"
                    f"Сетапов сегодня: {len(self.detector.setups_today)}"
                )
            return

        # ── Шаг 1: Ждём формирования NY диапазона ────────────
        if self.detector.state == SetupState.WAITING_RANGE:
            await self._check_range_formed(now, today)
            return

        # ── Шаг 2–4: Диапазон есть, мониторим 5м ─────────────
        await self._monitor_5m(now)

    async def _check_range_formed(self, now: datetime, today: str):
        """
        Проверяем закрылась ли первая 4ч свеча NY.
        Работает корректно при перезапуске в любое время дня.
        """
        open_utc, close_utc = get_ny_first_4h_times(now)

        if now < close_utc:
            # Свеча ещё не закрылась — ждём
            remaining = int((close_utc - now).total_seconds() / 60)
            if remaining % 30 == 0:
                logger.info(f"Waiting for NY 4h candle close in {remaining} min")
            return

        # Грузим 4h свечи напрямую — публичный клиент без API ключа
        klines_4h = await self.client.get_klines("BTCUSDT", interval="4h", limit=48)
        if not klines_4h:
            logger.warning("BTCUSDT 4h klines returned empty!")
            return

        from datetime import datetime as _dt
        first_dt = _dt.utcfromtimestamp(int(klines_4h[0][0])/1000).strftime("%d.%m %H:%M")
        last_dt  = _dt.utcfromtimestamp(int(klines_4h[-1][0])/1000).strftime("%d.%m %H:%M")
        logger.info(f"Got {len(klines_4h)} 4h candles: {first_dt} — {last_dt} UTC, target={open_utc.strftime('%H:%M')}")


        # Ищем свечу с точным временем открытия NY сессии
        target_ts = int(open_utc.timestamp() * 1000)
        ny_candle = None

        for k in klines_4h:
            k_ts = int(k[0])
            if abs(k_ts - target_ts) < 5 * 60_000:  # ±5 минут
                ny_candle = k
                logger.info(f"Found NY candle at {datetime.utcfromtimestamp(k_ts/1000).strftime('%H:%M')} UTC")
                break

        if not ny_candle:
            # Fallback: ищем последнюю ЗАКРЫТУЮ 4ч свечу (не текущую открытую)
            # Последняя в списке — текущая (возможно не закрытая), берём предпоследнюю
            closed_candles = [k for k in klines_4h if int(k[0]) <= target_ts]
            if closed_candles:
                ny_candle = closed_candles[-1]
                k_ts = int(ny_candle[0])
                logger.info(
                    f"NY candle not found by exact time, using closest: "
                    f"{datetime.utcfromtimestamp(k_ts/1000).strftime('%H:%M')} UTC"
                )
            else:
                logger.warning(f"Could not find NY 4h candle, target={open_utc}")
                return

        o, h, l, c = (float(ny_candle[i]) for i in (1, 2, 3, 4))
        candle_open_dt = datetime.utcfromtimestamp(int(ny_candle[0]) / 1000)

        ny_range = NYRange(
            date=today,
            high=h, low=l, open=o, close=c,
            range_size=round(h - l, 2),
            range_pct=round((h - l) / l * 100, 3),
            candle_open_utc=candle_open_dt.strftime("%H:%M"),
            candle_close_utc=(candle_open_dt.replace(hour=(candle_open_dt.hour + 4) % 24)).strftime("%H:%M"),
            is_bullish=c >= o,
        )

        self.detector.set_range(ny_range, today)
        logger.info(f"NY range set: {ny_range.low:.2f} — {ny_range.high:.2f}")
        await self._broadcast(ny_range.format_alert())

    async def _monitor_5m(self, now: datetime):
        """Получаем последние 5м свечи и обрабатываем закрытые."""
        klines = await self._get_5m_klines(3)
        if not klines or len(klines) < 2:
            return

        # Берём последнюю закрытую свечу (не текущую открытую)
        candle = klines[-2]
        candle_ts = str(candle[0])

        if candle_ts == self._last_5m_candle_time:
            return  # уже обрабатывали
        self._last_5m_candle_time = candle_ts

        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])
        t = datetime.utcfromtimestamp(int(candle[0]) / 1000).strftime("%H:%M")

        # ── Проверяем активную сделку ─────────────────────────
        if self.trade_mgr.is_active:
            event = self.trade_mgr.check_price(h, l, c, t)
            if event:
                event_type, message = event
                await self._broadcast(message)
                if event_type in ("sl", "tp2", "breakeven", "eod"):
                    # Сделка закрыта — сохраняем и продолжаем искать
                    self.detector.set_watching()
                return

        # ── Ищем новый сетап ──────────────────────────────────
        if self.detector.state in (
            SetupState.WATCHING,
            SetupState.BREAKOUT_DOWN,
            SetupState.BREAKOUT_UP,
        ):
            result = self.detector.process_candle(o, h, l, c, t)
            if result:
                event_type = result[0]

                if event_type == "breakout":
                    _, direction, bo = result
                    dir_text = "вниз 👇" if direction == "down" else "вверх 👆"
                    await self._broadcast(
                        f"⚠️ <b>Выход из диапазона {dir_text} — BTCUSDT</b>\n\n"
                        f"📍 Закрытие 5м свечи: <code>${bo.candle_close:,.2f}</code>\n"
                        f"📐 Диапазон: ${self.detector.ny_range.low:,.2f} — "
                        f"${self.detector.ny_range.high:,.2f}\n\n"
                        f"👁 Жду возврат в диапазон...\n"
                        f"⏰ {t} UTC"
                    )

                elif event_type == "setup":
                    _, setup = result
                    await self._broadcast(setup.format_alert())
                    self.trade_mgr.open_trade(setup)

    async def _get_5m_klines(self, limit: int = 3):
        return await self.client.get_klines("BTCUSDT", interval="5m", limit=limit)

    async def _broadcast(self, text: str):
        subscribers = await self.get_subscribers()
        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Broadcast failed to {chat_id}: {e}")
