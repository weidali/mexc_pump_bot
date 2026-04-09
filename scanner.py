"""
Scanner — главный цикл сканирования.
Включает все детекторы:
  - Spot: volume spike, price pump, CVD
  - Futures: OI spike, funding rate
  - Accumulation: паттерн "ступенька"
  - Correlation: декорреляция с BTC/ETH/alts
  - PumpHistory: повторные пампы
  - Follow-up: 15 и 30 минут
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiogram import Bot

from analyzer import Analyzer, SignalResult, FuturesAnalyzer, FuturesSignal
from accumulation_detector import AccumulationDetector
from correlation_filter import CorrelationFilter, ALT_INDEX_SYMBOLS
from pump_history import PumpHistory
from config import Config
from db import Database
from mexc_client import MEXCClient
from mexc_futures_client import MEXCFuturesClient
from signal_tracker import SignalTracker

logger = logging.getLogger(__name__)

CONCURRENCY = 8

# Символы рынка — всегда грузим для корреляции
MARKET_SYMBOLS = {"BTCUSDT", "ETHUSDT"} | set(ALT_INDEX_SYMBOLS)


class Scanner:
    def __init__(self, config: Config, bot: Bot, db: Database):
        self.cfg = config
        self.bot = bot
        self.db = db
        self.client = MEXCClient(api_key=config.MEXC_API_KEY, secret=config.MEXC_SECRET)
        self.futures_client = MEXCFuturesClient()
        self.analyzer = Analyzer(config)
        self.futures_analyzer = FuturesAnalyzer()
        self.accumulation = AccumulationDetector()
        self.correlation = CorrelationFilter()
        self.pump_history = PumpHistory(db=db)
        self.tracker = SignalTracker(bot=bot, mexc_client=self.client)

        self._monitored: List[str] = []
        self._last_scan: Optional[datetime] = None
        self._running = True
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._cycle_count = 0
        self._oi_cache: Dict[str, float] = {}
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

        # Добавляем рыночные символы для корреляции (если не в топе)
        all_symbols = list(set(self._monitored) | MARKET_SYMBOLS)

        logger.info(f"Scanning {len(self._monitored)} symbols (+{len(MARKET_SYMBOLS)} market)...")

        # Batch OI
        oi_batch = await self._load_oi_batch()

        # Сначала грузим рыночные данные для корреляции
        await self._load_market_data()

        # Анализируем только топ монеты (не рыночные индексы)
        tasks = [self._analyze_symbol(sym, oi_batch) for sym in self._monitored]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        spot_signals = [r for r in results if isinstance(r, SignalResult)]
        fut_signals  = [r for r in results if isinstance(r, FuturesSignal)]

        logger.info(f"Spot: {len(spot_signals)}, Futures: {len(fut_signals)}")

        for sig in spot_signals:
            await self._maybe_send_spot_alert(sig)
        for sig in fut_signals:
            await self._maybe_send_futures_alert(sig)

        self._last_scan = datetime.now(timezone.utc)

    async def _load_market_data(self):
        """Грузим свечи BTC, ETH и топ алтов для корреляционного фильтра."""
        tasks = {
            sym: self.client.get_klines(sym, interval=self.cfg.KLINE_INTERVAL, limit=20)
            for sym in MARKET_SYMBOLS
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for sym, klines in zip(tasks.keys(), results):
            if isinstance(klines, list) and klines:
                closes = [float(k[4]) for k in klines]
                self.correlation.update_cache(sym, closes)

    async def _load_oi_batch(self) -> Dict[str, float]:
        try:
            tickers = await self.futures_client.get_all_tickers()
            result = {}
            await self.futures_client._load_symbol_map()
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

    async def _analyze_symbol(self, symbol: str, oi_batch: Dict[str, float]) -> Optional[Any]:
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

                # ── 1. Spot анализ ────────────────────────────
                spot_result = self.analyzer.analyze(symbol, klines, trades)

                # ── 2. Accumulation (паттерн "ступенька") ─────
                accum = self.accumulation.detect(symbol, klines, trades)

                # ── 3. Корреляция с рынком ────────────────────
                corr = None
                if self.correlation.has_market_data:
                    corr = self.correlation.analyze(symbol, closes)

                # ── 4. Futures анализ ─────────────────────────
                oi_current = oi_batch.get(symbol)
                oi_prev    = self._oi_cache.get(symbol)
                funding_rate = None

                should_check_futures = (
                    spot_result is not None or accum is not None or corr is not None or
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

                # ── 5. Сборка итогового сигнала ───────────────
                if spot_result:
                    # Усиливаем spot сигнал подтверждениями
                    if fut_result:
                        spot_result.score += fut_result.score * 0.5
                        spot_result.details.extend(fut_result.details)
                        spot_result.oi_usdt = fut_result.oi_usdt
                        spot_result.oi_change_pct = fut_result.oi_change_pct
                        spot_result.funding_rate = fut_result.funding_rate
                        spot_result.has_futures_confirm = True

                    if accum:
                        spot_result.score += accum.score * 0.5
                        spot_result.accum_result = accum

                    if corr:
                        spot_result.score += corr.score * 0.8
                        spot_result.corr_result = corr

                    spot_result.triggered = spot_result.score >= self.cfg.MIN_SIGNAL_SCORE
                    return spot_result if spot_result.triggered else None

                # Только накопление без спот-сигнала — ранний сигнал
                if accum and accum.score >= 1.5:
                    early = self._make_early_signal(symbol, current_price, accum, corr)
                    return early

                # Только futures
                if fut_result and fut_result.score >= 2.0:
                    return fut_result

                return None

            except Exception as e:
                logger.debug(f"Error analyzing {symbol}: {e}")
                return None

    def _make_early_signal(self, symbol, price, accum, corr) -> SignalResult:
        """Создаёт ранний сигнал на основе паттерна накопления."""
        from analyzer import SignalResult
        sig = SignalResult(
            symbol=symbol,
            price=price,
            score=accum.score + (corr.score if corr else 0),
            triggered=True,
        )
        sig.details = accum.details[:]
        sig.accum_result = accum
        sig.corr_result = corr
        sig.is_early_signal = True
        return sig

    # ── Алерты ───────────────────────────────────────────────

    async def _maybe_send_spot_alert(self, sig: SignalResult):
        last = await self.db.get_last_signal_time(sig.symbol)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed < self.cfg.SIGNAL_COOLDOWN_MINUTES:
                return

        await self.db.save_signal(sig)

        # Загружаем историю пампов
        history = await self.pump_history.analyze(sig.symbol)

        text = self._format_spot_alert(sig, history)
        subscribers = await self.db.get_subscribers()

        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")

        self.tracker.track(sig.symbol, sig.price, subscribers)

    async def _maybe_send_futures_alert(self, sig: FuturesSignal):
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

    # ── Форматирование ────────────────────────────────────────

    def _format_spot_alert(self, sig: SignalResult, history=None) -> str:
        is_early = getattr(sig, "is_early_signal", False)
        accum    = getattr(sig, "accum_result", None)
        corr     = getattr(sig, "corr_result", None)

        if is_early:
            icon, level = "👁", "РАННИЙ СИГНАЛ (накопление)"
        elif sig.score >= 6.0:
            icon, level = "🔴🔴🔴", "ОЧЕНЬ СИЛЬНЫЙ СИГНАЛ"
        elif sig.score >= 4.0:
            icon, level = "🔴🔴", "СИЛЬНЫЙ СИГНАЛ"
        elif sig.score >= 2.5:
            icon, level = "🟠", "СИГНАЛ"
        else:
            icon, level = "🟡", "слабый сигнал"

        lines = [
            f"{icon} <b>PUMP &amp; DUMP — {level}</b>",
            f"",
            f"📌 <b>{sig.symbol}</b>",
            f"💵 Цена: <code>{sig.price:.6g}</code> USDT",
            f"📊 Score: <b>{sig.score:.1f}</b>",
        ]

        # Спот признаки
        has_spot = sig.volume_spike or sig.price_pump or sig.cvd_divergence
        if has_spot:
            lines.append(f"\n🔍 <b>Спот:</b>")
            if sig.volume_spike:
                lines.append(f"  • Объём ×{sig.volume_spike_x:.1f} от нормы 📦")
            if sig.price_pump:
                lines.append(f"  • Рост цены +{sig.price_pump_pct:.1f}% 🚀")
            if sig.cvd_divergence:
                lines.append(f"  • CVD {sig.cvd_delta_norm:+.2f} 📉")

        # Накопление
        if accum:
            lines.append(f"\n👁 <b>Накопление ({accum.duration_candles} свечей):</b>")
            lines.append(f"  • Канал: {accum.price_range_pct:.2f}% | Vol ×{accum.avg_volume_ratio:.1f}")
            lines.append(f"  • CVD тренд: {accum.cvd_trend:+.3f}")

        # Корреляция
        if corr:
            lines.append(f"\n📡 <b>Декорреляция с рынком:</b>")
            lines.append(f"  • Монета {corr.coin_change_pct:+.1f}% / BTC {corr.btc_change_pct:+.1f}% / ETH {corr.eth_change_pct:+.1f}%")
            lines.append(f"  • Рынок: {corr.market_direction} | Разрыв: {corr.coin_change_pct - (corr.btc_change_pct + corr.eth_change_pct)/2:+.1f}%")

        # Фьючерсы
        if getattr(sig, "has_futures_confirm", False):
            lines.append(f"\n🔮 <b>Подтверждено фьючерсами:</b>")
            if getattr(sig, "oi_change_pct", 0):
                lines.append(f"  • OI +{sig.oi_change_pct:.1f}%")
            if getattr(sig, "funding_rate", None) is not None:
                lines.append(f"  • Funding: {sig.funding_rate:.4f}")

        # История пампов
        if history:
            hist_text = self.pump_history.format_for_alert(history)
            if hist_text:
                lines.append(hist_text)

        lines += [
            f"",
            f"⚡ <i>{'Возможный вход до пампа' if is_early else 'Возможный шорт при развороте'}</i>",
        ]
        if not is_early:
            lines.append(f"⏱ <i>Follow-up через 15 и 30 минут</i>")
        lines.append(f"⚠️ <i>Не финансовый совет. DYOR.</i>")

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
            lines.append(f"  • OI +{sig.oi_change_pct:.1f}%{oi_usdt} 📈")
        if sig.funding_anomaly:
            lines.append(f"  • Funding: <b>{sig.funding_rate:.4f}</b> 📉")
        lines += [
            f"",
            f"⚡ <i>Крупные готовятся к развороту</i>",
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
