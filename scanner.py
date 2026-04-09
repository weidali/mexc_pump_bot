"""
Scanner — главный цикл сканирования.

1. Каждые SCAN_INTERVAL_SEC секунд получает топ-N монет по объёму
2. Для каждой монеты параллельно (с throttling) запрашивает свечи + трейды
3. Прогоняет через Analyzer
4. Если сигнал — проверяет cooldown и отправляет алерт
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiogram import Bot

from analyzer import Analyzer, SignalResult
from config import Config
from db import Database
from mexc_client import MEXCClient

logger = logging.getLogger(__name__)

# Параллельных запросов одновременно (не злоупотреблять MEXC rate limits)
CONCURRENCY = 10


class Scanner:
    def __init__(self, config: Config, bot: Bot, db: Database):
        self.cfg = config
        self.bot = bot
        self.db = db
        self.client = MEXCClient(api_key=config.MEXC_API_KEY, secret=config.MEXC_SECRET)
        self.analyzer = Analyzer(config)

        self._monitored: List[str] = []
        self._last_scan: Optional[datetime] = None
        self._running = True
        self._semaphore = asyncio.Semaphore(CONCURRENCY)

    # ─────────────────────────────────────────────────────────
    # Публичный интерфейс для команд бота
    # ─────────────────────────────────────────────────────────

    def pause(self):
        self._running = False

    def resume(self):
        self._running = True

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
        """Для команды /top — возвращает список монет с ценой и объёмом."""
        tickers = await self.client.get_ticker_24h_all()
        return self._filter_and_sort(tickers)[: self.cfg.TOP_N_SYMBOLS]

    # ─────────────────────────────────────────────────────────
    # Главный цикл
    # ─────────────────────────────────────────────────────────

    async def run_forever(self):
        logger.info("Scanner started")
        while True:
            try:
                if self._running:
                    await self._scan_cycle()
            except Exception as e:
                logger.exception(f"Scan cycle error: {e}")
            await asyncio.sleep(self.cfg.SCAN_INTERVAL_SEC)

    async def _scan_cycle(self):
        # 1. Обновить список монет
        tickers = await self.client.get_ticker_24h_all()
        top = self._filter_and_sort(tickers)[: self.cfg.TOP_N_SYMBOLS]
        self._monitored = [t["symbol"] for t in top]

        logger.info(f"Scanning {len(self._monitored)} symbols...")

        # 2. Параллельный анализ
        tasks = [self._analyze_symbol(sym) for sym in self._monitored]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = [r for r in results if isinstance(r, SignalResult)]
        logger.info(f"Signals found: {len(signals)}")

        # 3. Отправка алертов
        for sig in signals:
            await self._maybe_send_alert(sig)

        self._last_scan = datetime.now(timezone.utc)

    async def _analyze_symbol(self, symbol: str) -> Optional[SignalResult]:
        async with self._semaphore:
            try:
                klines, trades = await asyncio.gather(
                    self.client.get_klines(
                        symbol,
                        interval=self.cfg.KLINE_INTERVAL,
                        limit=self.cfg.KLINE_LIMIT,
                    ),
                    self.client.get_recent_trades(symbol, limit=200),
                )
                if not klines:
                    return None
                return self.analyzer.analyze(symbol, klines, trades)
            except Exception as e:
                logger.debug(f"Error analyzing {symbol}: {e}")
                return None

    # ─────────────────────────────────────────────────────────
    # Алерты
    # ─────────────────────────────────────────────────────────

    async def _maybe_send_alert(self, sig: SignalResult):
        # Проверяем cooldown
        last = await self.db.get_last_signal_time(sig.symbol)
        if last is not None:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed < self.cfg.SIGNAL_COOLDOWN_MINUTES:
                logger.debug(
                    f"{sig.symbol}: cooldown {elapsed:.0f}/{self.cfg.SIGNAL_COOLDOWN_MINUTES}m"
                )
                return

        # Сохраняем в БД
        await self.db.save_signal(sig)

        # Формируем текст алерта
        text = self._format_alert(sig)

        # Рассылаем всем подписчикам
        subscribers = await self.db.get_subscribers()
        for chat_id in subscribers:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")

    def _format_alert(self, sig: SignalResult) -> str:
        # Определяем уровень опасности
        if sig.score >= 4.0:
            icon = "🔴🔴🔴"
            level = "СИЛЬНЫЙ СИГНАЛ"
        elif sig.score >= 2.5:
            icon = "🟠🟠"
            level = "СИГНАЛ"
        else:
            icon = "🟡"
            level = "слабый сигнал"

        lines = [
            f"{icon} <b>PUMP &amp; DUMP — {level}</b>",
            f"",
            f"📌 <b>{sig.symbol}</b>",
            f"💵 Цена: <code>{sig.price:.6g}</code> USDT",
            f"📊 Score: <b>{sig.score:.1f}</b>",
            f"",
            f"🔍 <b>Признаки:</b>",
        ]

        if sig.volume_spike:
            lines.append(f"  • Объём ×{sig.volume_spike_x:.1f} от нормы 📦")
        if sig.price_pump:
            lines.append(f"  • Рост цены +{sig.price_pump_pct:.1f}% 🚀")
        if sig.cvd_divergence:
            lines.append(
                f"  • CVD дивергенция {sig.cvd_delta_norm:+.2f} "
                f"(продают в рост) 📉"
            )

        lines += [
            f"",
            f"⚡ <i>Возможный шорт при развороте</i>",
            f"⚠️ <i>Не финансовый совет. DYOR.</i>",
        ]

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────
    # Фильтрация монет
    # ─────────────────────────────────────────────────────────

    def _filter_and_sort(self, tickers: List[Dict]) -> List[Dict]:
        result = []
        for t in tickers:
            symbol = t.get("symbol", "")
            # Только спот USDT пары
            if not symbol.endswith("USDT"):
                continue
            # Исключаем стейблы и крупные монеты
            base = symbol.replace("USDT", "")
            if base in ("BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "BUSD", "DAI"):
                continue

            try:
                volume_usdt = float(t.get("quoteVolume", 0) or 0)
                price = float(t.get("lastPrice", 0) or 0)
            except (ValueError, TypeError):
                continue

            if volume_usdt < self.cfg.MIN_VOLUME_USDT_24H:
                continue
            if price == 0:
                continue

            result.append({
                "symbol": symbol,
                "price": price,
                "volume_usdt": volume_usdt,
                "change_pct": float(t.get("priceChangePercent", 0) or 0),
            })

        # Сортировка по объёму убыванию
        result.sort(key=lambda x: x["volume_usdt"], reverse=True)
        return result
