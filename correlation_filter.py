"""
CorrelationFilter — декорреляция с рынком.

Если BTC/ETH падают а шиткоин растёт — это аномалия,
почти всегда искусственная манипуляция.

Индекс топ-10 алтов = среднее изменение 10 крупнейших монет
(ADA, SOL, AVAX, DOT, LINK, UNI, ATOM, NEAR, FTM, MATIC)
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Топ алты для индекса (исключая BTC/ETH)
ALT_INDEX_SYMBOLS = [
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "DOTUSDT",
    "LINKUSDT", "UNIUSDT", "ATOMUSDT", "NEARUSDT",
    "MATICUSDT", "LTCUSDT",
]

# Окно для расчёта корреляции (свечей)
CORRELATION_WINDOW = 5


@dataclass
class CorrelationResult:
    symbol: str
    is_decorrelated: bool
    score: float

    coin_change_pct: float      # изменение монеты за окно
    btc_change_pct: float       # изменение BTC
    eth_change_pct: float       # изменение ETH
    alt_index_pct: float        # изменение индекса алтов

    market_direction: str       # "up" / "down" / "sideways"
    details: List[str]


class CorrelationFilter:
    # Монета растёт на X% пока рынок падает/стоит — аномалия
    DECORRELATION_THRESHOLD = 5.0   # монета выросла на 5%+ против рынка

    # Рынок считается падающим если среднее BTC+ETH ниже порога
    MARKET_FALLING_THRESHOLD = -1.5
    MARKET_SIDEWAYS_RANGE    = 1.5  # рынок в боковике ±1.5%

    def __init__(self):
        # Кэш последних цен для BTC, ETH и алт-индекса
        # symbol → [close_prices]
        self._price_cache: Dict[str, List[float]] = {}

    def update_cache(self, symbol: str, closes: List[float]):
        """Обновляет кэш цен. Вызывается из сканера для каждой монеты."""
        self._price_cache[symbol] = closes[-20:]  # храним последние 20 свечей

    def get_market_change(self) -> Tuple[float, float, float]:
        """
        Возвращает (btc_change, eth_change, alt_index_change) за CORRELATION_WINDOW свечей.
        """
        def pct_change(symbol: str) -> Optional[float]:
            closes = self._price_cache.get(symbol)
            if not closes or len(closes) < CORRELATION_WINDOW + 1:
                return None
            start = closes[-(CORRELATION_WINDOW + 1)]
            end   = closes[-1]
            if start == 0:
                return None
            return (end - start) / start * 100

        btc = pct_change("BTCUSDT") or 0.0
        eth = pct_change("ETHUSDT") or 0.0

        # Индекс алтов — среднее по доступным монетам
        alt_changes = []
        for sym in ALT_INDEX_SYMBOLS:
            chg = pct_change(sym)
            if chg is not None:
                alt_changes.append(chg)
        alt_index = sum(alt_changes) / len(alt_changes) if alt_changes else 0.0

        return btc, eth, alt_index

    def analyze(
        self,
        symbol: str,
        coin_closes: List[float],
    ) -> Optional[CorrelationResult]:
        """
        Проверяет декорреляцию монеты с рынком.
        Возвращает CorrelationResult если обнаружена аномалия.
        """
        if len(coin_closes) < CORRELATION_WINDOW + 1:
            return None

        # Изменение монеты
        start = coin_closes[-(CORRELATION_WINDOW + 1)]
        end   = coin_closes[-1]
        if start == 0:
            return None
        coin_change = (end - start) / start * 100

        # Изменение рынка
        btc_chg, eth_chg, alt_chg = self.get_market_change()

        # Направление рынка (среднее BTC + ETH)
        market_avg = (btc_chg + eth_chg) / 2

        if market_avg < self.MARKET_FALLING_THRESHOLD:
            market_direction = "down"
        elif market_avg > self.MARKET_SIDEWAYS_RANGE:
            market_direction = "up"
        else:
            market_direction = "sideways"

        # Декорреляция: монета растёт когда рынок падает или стоит
        decorrelation_gap = coin_change - market_avg
        is_decorrelated = (
            coin_change >= self.DECORRELATION_THRESHOLD and
            market_direction in ("down", "sideways") and
            decorrelation_gap >= self.DECORRELATION_THRESHOLD
        )

        if not is_decorrelated:
            return None

        # Score: чем сильнее расхождение — тем выше
        score = min(decorrelation_gap / 10, 2.0)

        details = [
            f"Монета +{coin_change:.1f}% пока рынок {market_avg:+.1f}%",
            f"Разрыв: {decorrelation_gap:+.1f}%",
        ]
        if btc_chg != 0:
            details.append(f"BTC {btc_chg:+.1f}% / ETH {eth_chg:+.1f}% / Alts {alt_chg:+.1f}%")

        return CorrelationResult(
            symbol=symbol,
            is_decorrelated=True,
            score=round(score, 2),
            coin_change_pct=round(coin_change, 2),
            btc_change_pct=round(btc_chg, 2),
            eth_change_pct=round(eth_chg, 2),
            alt_index_pct=round(alt_chg, 2),
            market_direction=market_direction,
            details=details,
        )

    @property
    def has_market_data(self) -> bool:
        """Есть ли данные по BTC/ETH в кэше."""
        return (
            "BTCUSDT" in self._price_cache and
            "ETHUSDT" in self._price_cache and
            len(self._price_cache.get("BTCUSDT", [])) >= CORRELATION_WINDOW + 1
        )
