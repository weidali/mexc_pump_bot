"""
RiskIndicator — индикатор Risk-On / Risk-Off.

Считает составной score из нескольких источников:

1. SPY/TLT ratio    — акции vs облигации (Yahoo Finance / yfinance)
2. VIX              — индекс страха (Yahoo Finance)
3. DXY              — индекс доллара (Yahoo Finance)
4. Fear & Greed     — крипто-настроения (alternative.me API)
5. BTC Dominance    — доминация BTC (CoinGecko)
6. BTC Funding Rate — агрессивность лонгов (MEXC, уже есть)

Итоговый score: от -100 (Extreme Risk-OFF) до +100 (Extreme Risk-ON)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


# ── Зоны интерпретации ────────────────────────────────────────
ZONES = [
    (75,  100, "🟢🟢 EXTREME RISK-ON",  "Рынок в эйфории. Высокий аппетит к риску. Альты могут расти агрессивно."),
    (40,   74, "🟢 RISK-ON",             "Позитивный фон. Инвесторы берут риск. Благоприятно для крипты."),
    (15,   39, "🟡 NEUTRAL-ON",          "Умеренный оптимизм. Рынок осторожно берёт риск."),
    (-14,  14, "⚪ NEUTRAL",             "Неопределённость. Рынок ищет направление."),
    (-39, -15, "🟠 NEUTRAL-OFF",         "Осторожность. Инвесторы снижают риск."),
    (-74, -40, "🔴 RISK-OFF",            "Бегство от риска. Давление на крипту и альты."),
    (-100,-75, "🔴🔴 EXTREME RISK-OFF",  "Паника. Деньги уходят в облигации и доллар. Крипта под сильным давлением."),
]


@dataclass
class ComponentScore:
    name: str
    value: float           # сырое значение
    score: float           # нормализованный вклад (-1 до +1)
    weight: float          # вес компонента
    description: str       # что показывает
    available: bool = True
    error: str = ""


@dataclass
class RiskResult:
    score: float                          # итоговый -100 до +100
    zone: str                             # название зоны
    comment: str                          # интерпретация
    components: List[ComponentScore] = field(default_factory=list)
    timestamp: str = ""
    sources_used: int = 0
    sources_total: int = 0

    def format_message(self) -> str:
        bar = self._score_bar(self.score)
        lines = [
            f"📊 <b>Risk-On / Risk-Off Индикатор</b>",
            f"",
            f"{self.zone}",
            f"<b>Score: {self.score:+.0f}</b>  {bar}",
            f"",
            f"💬 {self.comment}",
            f"",
            f"<b>Компоненты ({self.sources_used}/{self.sources_total}):</b>",
        ]
        for c in self.components:
            if c.available:
                sign = "+" if c.score >= 0 else ""
                lines.append(
                    f"  {'🟢' if c.score > 0.2 else '🔴' if c.score < -0.2 else '⚪'} "
                    f"<b>{c.name}</b>: {c.description}"
                )
            else:
                lines.append(f"  ⚫ <b>{c.name}</b>: недоступен")

        lines += [
            f"",
            f"🕐 {self.timestamp} UTC",
        ]
        return "\n".join(lines)

    @staticmethod
    def _score_bar(score: float) -> str:
        """Визуальная полоска от -100 до +100."""
        filled = int((score + 100) / 200 * 10)
        filled = max(0, min(10, filled))
        return "🟥" * max(0, 5 - filled) + "⬛" * (filled == 5) + "🟩" * max(0, filled - 5) if filled != 5 else "⬛" * 1
        # Упрощённый вариант
        bar = ""
        for i in range(10):
            threshold = (i + 1) * 20 - 100
            if score >= threshold:
                bar += "🟩" if threshold >= 0 else "🟥"
            else:
                bar += "⬜"
        return bar


class RiskIndicator:
    """Составной Risk-On/Risk-Off индикатор."""

    WEIGHTS = {
        "SPY/TLT":    0.25,
        "VIX":        0.20,
        "DXY":        0.15,
        "Fear&Greed": 0.25,
        "BTC Dom":    0.10,
        "Funding":    0.05,
    }

    def __init__(self, futures_client=None):
        self.futures_client = futures_client
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Optional[RiskResult] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl_min = 30  # кэш 30 минут

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            import socket
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(family=socket.AF_INET6),
            )
        return self._session

    async def get(self, force: bool = False) -> RiskResult:
        """Возвращает результат (из кэша или свежий)."""
        now = datetime.now(timezone.utc)
        if (
            not force and
            self._cache and
            self._cache_time and
            (now - self._cache_time).total_seconds() < self._cache_ttl_min * 60
        ):
            return self._cache

        result = await self._calculate()
        self._cache = result
        self._cache_time = now
        return result

    async def _calculate(self) -> RiskResult:
        """Параллельно собираем все компоненты."""
        tasks = await asyncio.gather(
            self._get_spy_tlt(),
            self._get_vix(),
            self._get_dxy(),
            self._get_fear_greed(),
            self._get_btc_dominance(),
            self._get_funding_rate(),
            return_exceptions=True,
        )

        components = []
        for t in tasks:
            if isinstance(t, ComponentScore):
                components.append(t)
            elif isinstance(t, Exception):
                logger.debug(f"Risk component error: {t}")

        # Считаем взвешенный score
        total_weight = 0.0
        weighted_sum = 0.0
        sources_used = 0

        for c in components:
            if c.available and c.name in self.WEIGHTS:
                w = self.WEIGHTS[c.name]
                weighted_sum += c.score * w
                total_weight += w
                sources_used += 1

        if total_weight > 0:
            raw_score = weighted_sum / total_weight  # от -1 до +1
            final_score = round(raw_score * 100, 1)
        else:
            final_score = 0.0

        # Находим зону
        zone, comment = "⚪ NEUTRAL", "Недостаточно данных для анализа."
        for lo, hi, z, c in ZONES:
            if lo <= final_score <= hi:
                zone, comment = z, c
                break

        return RiskResult(
            score=final_score,
            zone=zone,
            comment=comment,
            components=components,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sources_used=sources_used,
            sources_total=len(self.WEIGHTS),
        )

    # ── Источник 1: SPY/TLT через Yahoo Finance ───────────────

    async def _get_spy_tlt(self) -> ComponentScore:
        """
        Соотношение SPY (акции) / TLT (облигации) за 5 торговых дней.
        Рост ratio → risk-on, падение → risk-off.
        """
        try:
            spy = await self._yahoo_change("SPY", days=5)
            tlt = await self._yahoo_change("TLT", days=5)

            if spy is None or tlt is None:
                return ComponentScore("SPY/TLT", 0, 0, 0, "нет данных", available=False)

            # Относительная сила: SPY outperforms TLT
            diff = spy - tlt  # % разница
            score = max(-1.0, min(1.0, diff / 10))  # нормализуем: ±10% = ±1.0

            desc = f"SPY {spy:+.1f}% / TLT {tlt:+.1f}% за 5д → {'risk-on ↑' if score > 0 else 'risk-off ↓'}"
            return ComponentScore("SPY/TLT", diff, score, self.WEIGHTS["SPY/TLT"], desc)

        except Exception as e:
            return ComponentScore("SPY/TLT", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Источник 2: VIX ───────────────────────────────────────

    async def _get_vix(self) -> ComponentScore:
        """
        VIX < 15 → спокойствие (risk-on)
        VIX 15-25 → нейтрально
        VIX > 25 → страх (risk-off)
        VIX > 35 → паника (extreme risk-off)
        """
        try:
            vix = await self._yahoo_price("^VIX")
            if vix is None:
                return ComponentScore("VIX", 0, 0, 0, "нет данных", available=False)

            if vix < 12:   score = 1.0
            elif vix < 15: score = 0.6
            elif vix < 20: score = 0.2
            elif vix < 25: score = -0.2
            elif vix < 30: score = -0.6
            elif vix < 35: score = -0.8
            else:          score = -1.0

            level = "спокойно" if vix < 15 else "нейтрально" if vix < 25 else "страх" if vix < 35 else "паника"
            desc = f"VIX = {vix:.1f} ({level})"
            return ComponentScore("VIX", vix, score, self.WEIGHTS["VIX"], desc)

        except Exception as e:
            return ComponentScore("VIX", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Источник 3: DXY ───────────────────────────────────────

    async def _get_dxy(self) -> ComponentScore:
        """
        Сильный доллар = risk-off для крипты (деньги уходят в доллар).
        Изменение DXY за 5 дней, инвертировано.
        """
        try:
            dxy_change = await self._yahoo_change("DX-Y.NYB", days=5)
            if dxy_change is None:
                # Пробуем альтернативный тикер
                dxy_change = await self._yahoo_change("UUP", days=5)

            if dxy_change is None:
                return ComponentScore("DXY", 0, 0, 0, "нет данных", available=False)

            # Инвертируем: рост доллара = risk-off
            score = max(-1.0, min(1.0, -dxy_change / 3))  # ±3% = ±1.0

            desc = f"DXY {dxy_change:+.2f}% за 5д → {'давление на крипту ↓' if dxy_change > 0 else 'слабый доллар ↑'}"
            return ComponentScore("DXY", dxy_change, score, self.WEIGHTS["DXY"], desc)

        except Exception as e:
            return ComponentScore("DXY", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Источник 4: Crypto Fear & Greed ──────────────────────

    async def _get_fear_greed(self) -> ComponentScore:
        """
        Crypto Fear & Greed Index от alternative.me.
        0 = Extreme Fear, 100 = Extreme Greed.
        """
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return ComponentScore("Fear&Greed", 0, 0, 0, f"HTTP {resp.status}", available=False)
                data = await resp.json()

            value = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]

            # Нормализуем 0-100 → -1 до +1
            score = (value - 50) / 50

            desc = f"F&G = {value}/100 ({label})"
            return ComponentScore("Fear&Greed", value, score, self.WEIGHTS["Fear&Greed"], desc)

        except Exception as e:
            return ComponentScore("Fear&Greed", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Источник 5: BTC Dominance ─────────────────────────────

    async def _get_btc_dominance(self) -> ComponentScore:
        """
        Растущая доминация BTC = альты под давлением (risk-off для альтов).
        Падающая доминация = альт-сезон (risk-on для шиткоинов).
        """
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.coingecko.com/api/v3/global",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return ComponentScore("BTC Dom", 0, 0, 0, f"HTTP {resp.status}", available=False)
                data = await resp.json()

            dom = data["data"]["market_cap_percentage"]["btc"]
            dom_change = data["data"].get("market_cap_change_percentage_24h_usd", 0)

            # Высокая доминация (>55%) = risk-off для альтов
            # Низкая (<40%) = alt-season = risk-on
            if dom > 60:   score = -0.8
            elif dom > 55: score = -0.4
            elif dom > 50: score = -0.1
            elif dom > 45: score = 0.2
            elif dom > 40: score = 0.5
            else:          score = 0.8

            trend = "↑ риск-офф для альтов" if dom > 55 else "↓ альт-сезон" if dom < 45 else "нейтрально"
            desc = f"BTC Dom = {dom:.1f}% ({trend})"
            return ComponentScore("BTC Dom", dom, score, self.WEIGHTS["BTC Dom"], desc)

        except Exception as e:
            return ComponentScore("BTC Dom", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Источник 6: BTC Funding Rate ─────────────────────────

    async def _get_funding_rate(self) -> ComponentScore:
        """
        Высокий положительный funding = толпа в лонгах = перегрев (risk-off сигнал).
        Отрицательный funding = страх = возможный разворот вверх.
        """
        try:
            if not self.futures_client:
                return ComponentScore("Funding", 0, 0, 0, "клиент не подключён", available=False)

            fr_data = await self.futures_client.get_funding_rate("BTCUSDT")
            if not fr_data:
                return ComponentScore("Funding", 0, 0, 0, "нет данных", available=False)

            fr = fr_data.get("funding_rate", 0)

            # Высокий фандинг = перегрев = risk-off
            if fr > 0.003:   score = -0.8   # экстремальный лонг перегрев
            elif fr > 0.001: score = -0.3
            elif fr > 0:     score = 0.1
            elif fr > -0.001:score = 0.3
            else:            score = 0.7    # негативный фандинг = потенциал роста

            desc = f"BTC Funding = {fr:.4f} ({'лонг перегрев' if fr > 0.001 else 'шорт давление' if fr < -0.001 else 'нейтрально'})"
            return ComponentScore("Funding", fr, score, self.WEIGHTS["Funding"], desc)

        except Exception as e:
            return ComponentScore("Funding", 0, 0, 0, str(e)[:50], available=False, error=str(e))

    # ── Yahoo Finance helper ──────────────────────────────────

    async def _yahoo_price(self, ticker: str) -> Optional[float]:
        """Текущая цена через Yahoo Finance JSON API."""
        try:
            session = await self._get_session()
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            meta = data["chart"]["result"][0]["meta"]
            return float(meta.get("regularMarketPrice") or meta.get("previousClose"))
        except Exception as e:
            logger.debug(f"Yahoo price {ticker}: {e}")
            return None

    async def _yahoo_change(self, ticker: str, days: int = 5) -> Optional[float]:
        """Изменение цены в % за N дней через Yahoo Finance."""
        try:
            session = await self._get_session()
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={days+2}d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 2:
                return None

            price_now  = closes[-1]
            price_then = closes[max(0, len(closes) - days - 1)]
            if price_then == 0:
                return None
            return (price_now - price_then) / price_then * 100

        except Exception as e:
            logger.debug(f"Yahoo change {ticker}: {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
