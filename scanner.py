"""
Scanner — главный цикл сканирования.
Теперь включает:
  - Spot анализ (volume spike, price pump, CVD)
  - Futures анализ (OI spike, funding rate)
  - Follow-up через 15 и 30 минут
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiogram import Bot

from analyzer import Analyzer, SignalResult, FuturesAnalyzer, FuturesSignal
from config import Config
from db import Database
from mexc_client import MEXCClient
from mexc_futures_client import MEXCFuturesClient
from signal_tracker import SignalTracker

logger = logging.getLogger(__name__)

CONCURRENCY = 8


class Scanner:
    def __init__(self, config: Config, bot: Bot, db: Database):
        self.cfg = config
        self.bot = bot
        self.db = db
        self.client = MEXCClient(api_key=config.MEXC_API_KEY, secret=config.MEXC_SECRET)
        self.futures_client = MEXCFuturesClient()
        self.analyzer = Analyzer(config)
        self.futures_analyzer = FuturesAnalyzer()
        self.tracker = SignalTracker(bot=bot, mexc_client=self.client)

        self._monitored: List[str] = []
        self._last_scan: Optional[datetime] = None
        self._running = True
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._cycle_count = 0

        # Кэш OI предыдущего цикла: symbol → oi_value
        self._oi_cache: Dict[str, float] = {}
        # Кэш цены предыдущего цикла
        self._price_cache: Dict[str, float] = {}

    def pause(self):  self._running = False
    def resume(self): self._running = True

    @property
    def is_running(self) -> bool:
        return self._running

    def get_monitored_count(self) -> int:
        return len(self._monitored)

    def get_last_scan_time(self) -> str:
        if self._last_scan is None:
            return "ещё не было"
        return self._last_scan.strftime("%H:%M:%S UTC")

    async def get_top_symbols(self) -> List[Dict]:
        tickers = await self.client.get_ticker_24h_all()
        valid = await self.client.get_default_symbols()
        return self._filter_and_sort(tickers, valid)[: self.cfg.TOP_N_SYMBOLS]

    # ── Главный цикл ─────────────────────────────────────────

    async def run_forever(self):
        logger.info("Scanner started")
        await self.client.get_default_symbols()
        # Прогреваем futures symbol map
        await self.futures_client._load_symbol_map()

        while True:
            try:
                if self._running:
                    await self._scan_cycle()
                    await self.tracker.check_followups()

                if self._cycle_count % 1440 == 0 and self._cycle_count > 0:
                    deleted = await self.db.cleanup_old_signals(keep_days=self.cfg.DB_KEEP_DAYS)
                    if deleted:
                        logger.info(f"DB cleanup: removed {deleted} old signals")
            except Exception as e:
                logger.exception(f"Scan cycle error: {e}")
            await asyncio.sleep(self.cfg.SCAN_INTERVAL_SEC)

    async def _scan_cycle(self):
        self._cycle_count += 1

        if self._cycle_count % 10 == 1:
            await self.client.refresh_valid_symbols()

        tickers = await self.client.get_ticker_24h_all()
        valid = await self.client.get_default_symbols()
        top = self._filter_and_sort(tickers, valid)[: self.cfg.TOP_N_SYMBOLS]
        self._monitored = [t["symbol"] for t in top]

        logger.info(f"Scanning {len(self._monitored)} symbols...")

        # Batch-загрузка всех OI из futures одним запросом
        oi_batch = await self._load_oi_batch()

        tasks = [self._analyze_symbol(sym, oi_batch) for sym in self._monitored]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = [r for r in results if isinstance(r, (SignalResult, FuturesSignal))]
        spot_signals = [r for r in signals if isinstance(r, SignalResult)]
        fut_signals  = [r for r in signals if isinstance(r, FuturesSignal)]

        logger.info(f"Spot signals: {len(spot_signals)}, Futures signals: {len(fut_signals)}")

        for sig in spot_signals:
            await self._maybe_send_spot_alert(sig)
        for sig in fut_signals:
            await self._maybe_send_futures_alert(sig)

        self._last_scan = datetime.now(timezone.utc)

    async def _load_oi_batch(self) -> Dict[str, float]:
        """Загружает OI для всех фьючерсных тикеров одним запросом."""
        try:
            tickers = await self.futures_client.get_all_tickers()
            result = {}
            await self.futures_client._load_symbol_map()
            # Инвертируем маппинг: BTC_USDT → BTCUSDT
            inv_map = {v: k for k, v in self.futures_client._symbol_map.items()}
            for t in tickers:
                fut_sym = t.get("symbol", "")
                spot_sym = inv_map.get(fut_sym)
                if spot_sym:
                    try:
                        result[spot_sym] = float(t.get("holdVol", 0))
                    except (TypeError, ValueError):
                        pass
            return result
        except Exception as e:
            logger.debug(f"OI batch load failed: {e}")
            return {}

    async def _analyze_symbol(
        self, symbol: str, oi_batch: Dict[str, float]
    ) -> Optional[Any]:
        async with self._semaphore:
            try:
                klines, trades = await asyncio.gather(
                    self.client.get_klines(symbol, interval=self.cfg.KLINE_INTERVAL, limit=self.cfg.KLINE_LIMIT),
                    self.client.get_recent_trades(symbol, limit=200),
                )
                if not klines:
                    return None

                closes = [float(k[4]) for k in klines]
                current_price = closes[-1]
                prev_price = self._price_cache.get(symbol, current_price)

                # ── Spot анализ ───────────────────────────────
                spot_result = self.analyzer.analyze(symbol, klines, trades)

                # ── Futures анализ ────────────────────────────
                oi_current = oi_batch.get(symbol)
                oi_prev = self._oi_cache.get(symbol)
                funding_rate = None

                # Funding грузим только если есть спот-сигнал или OI изменился заметно
                should_check_futures = (
                    spot_result is not None or
                    (oi_current and oi_prev and abs(oi_current - oi_prev) / max(oi_prev, 1) > 0.05)
                )

                if should_check_futures:
                    fr_data = await self.futures_client.get_funding_rate(symbol)
                    if fr_data:
                        funding_rate = fr_data.get("funding_rate")

                fut_result = self.futures_analyzer.analyze_futures(
                    symbol=symbol,
                    current_price=current_price,
                    prev_price=prev_price,
                    oi_current=oi_current,
                    oi_prev=oi_prev,
                    funding_rate=funding_rate,
                )

                # Обновляем кэш
                self._price_cache[symbol] = current_price
                if oi_current is not None:
                    self._oi_cache[symbol] = oi_current

                # Если оба сигнала — усиливаем spot сигнал данными futures
                if spot_result and fut_result:
                    spot_result.score += fut_result.score * 0.5
                    spot_result.details.extend(fut_result.details)
                    spot_result.oi_usdt = fut_result.oi_usdt
                    spot_result.oi_change_pct = fut_result.oi_change_pct
                    spot_result.funding_rate = fut_result.funding_rate
                    spot_result.has_futures_confirm = True
                    return spot_result

                if spot_result:
                    spot_result.has_futures_confirm = False
                    return spot_result

                if fut_result and fut_result.score >= 2.0:
                    return fut_result

                return None

            except Exception as e:
                logger.debug(f"Error analyzing {symbol}: {e}")
                return None

    # ── Алерты ───────────────────────────────────────────────

    async def _maybe_send_spot_alert(self, sig: SignalResult):
        last = await self.db.get_last_signal_time(sig.symbol)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed < self.cfg.SIGNAL_COOLDOWN_MINUTES:
                return

        await self.db.save_signal(sig)
        text = self._format_spot_alert(sig)
        subscribers = await self.db.get_subscribers()

        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")

        # Запускаем follow-up трекер
        self.tracker.track(sig.symbol, sig.price, subscribers)

    async def _maybe_send_futures_alert(self, sig: FuturesSignal):
        """Отдельный алерт только по futures сигналу (без спота)."""
        last = await self.db.get_last_signal_time(sig.symbol)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed < self.cfg.SIGNAL_COOLDOWN_MINUTES:
                return

        text = self._format_futures_alert(sig)
        subscribers = await self.db.get_subscribers()
        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")

    def _format_spot_alert(self, sig: SignalResult) -> str:
        if sig.score >= 5.0:
            icon, level = "🔴🔴🔴", "СИЛЬНЫЙ СИГНАЛ"
        elif sig.score >= 3.0:
            icon, level = "🟠🟠", "СИГНАЛ"
        else:
            icon, level = "🟡", "слабый сигнал"

        # Подтверждение фьючерсами
        confirm = ""
        if getattr(sig, "has_futures_confirm", False):
            confirm = "\n🔮 <b>Подтверждён фьючерсами</b>"
            oi = getattr(sig, "oi_change_pct", 0)
            fr = getattr(sig, "funding_rate", None)
            if oi:
                confirm += f"\n  • OI вырос на +{oi:.1f}%"
            if fr is not None:
                confirm += f"\n  • Funding rate: {fr:.4f}"

        lines = [
            f"{icon} <b>PUMP &amp; DUMP — {level}</b>",
            f"",
            f"📌 <b>{sig.symbol}</b>",
            f"💵 Цена: <code>{sig.price:.6g}</code> USDT",
            f"📊 Score: <b>{sig.score:.1f}</b>",
            f"",
            f"🔍 <b>Спот признаки:</b>",
        ]
        if sig.volume_spike:
            lines.append(f"  • Объём ×{sig.volume_spike_x:.1f} от нормы 📦")
        if sig.price_pump:
            lines.append(f"  • Рост цены +{sig.price_pump_pct:.1f}% 🚀")
        if sig.cvd_divergence:
            lines.append(f"  • CVD дивергенция {sig.cvd_delta_norm:+.2f} 📉")
        if confirm:
            lines.append(confirm)
        lines += [
            f"",
            f"⚡ <i>Возможный шорт при развороте</i>",
            f"⏱ <i>Follow-up через 15 и 30 минут</i>",
            f"⚠️ <i>Не финансовый совет. DYOR.</i>",
        ]
        return "\n".join(lines)

    def _format_futures_alert(self, sig: FuturesSignal) -> str:
        lines = [
            f"🔮 <b>FUTURES СИГНАЛ</b>",
            f"",
            f"📌 <b>{sig.symbol}</b>",
            f"📊 Score: <b>{sig.score:.1f}</b>",
            f"",
            f"🔍 <b>Признаки:</b>",
        ]
        if sig.oi_spike:
            oi_usdt = f" (${sig.oi_usdt:,.0f})" if sig.oi_usdt else ""
            lines.append(f"  • OI вырос +{sig.oi_change_pct:.1f}%{oi_usdt} 📈")
        if sig.funding_anomaly:
            lines.append(f"  • Funding rate: <b>{sig.funding_rate:.4f}</b> 📉")
            lines.append(f"    Шортеры платят, но держат позицию")
        lines += [
            f"",
            f"⚡ <i>Крупные игроки готовятся к развороту</i>",
            f"⚠️ <i>Не финансовый совет. DYOR.</i>",
        ]
        return "\n".join(lines)

    def _filter_and_sort(self, tickers: List[Dict], valid_symbols: set) -> List[Dict]:
        result = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            if valid_symbols and symbol not in valid_symbols:
                continue
            base = symbol.replace("USDT", "")
            if base in ("BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "BUSD", "DAI"):
                continue
            try:
                volume_usdt = float(t.get("quoteVolume", 0) or 0)
                price = float(t.get("lastPrice", 0) or 0)
            except (ValueError, TypeError):
                continue
            if volume_usdt < self.cfg.MIN_VOLUME_USDT_24H or price == 0:
                continue
            result.append({
                "symbol": symbol,
                "price": price,
                "volume_usdt": volume_usdt,
                "change_pct": float(t.get("priceChangePercent", 0) or 0),
            })
        result.sort(key=lambda x: x["volume_usdt"], reverse=True)
        return result
