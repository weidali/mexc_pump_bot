"""
Analyzer — ядро детектирования манипуляций.

Три сигнала:
  1. Volume Spike       — резкий всплеск объёма
  2. Price Pump         — быстрый рост цены
  3. CVD Divergence     — цена растёт, а net buy volume падает (дамп в толпу)

Каждый сигнал возвращает (triggered: bool, score: float, detail: str).
Итоговый сигнал = сумма score >= MIN_SIGNAL_SCORE.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    symbol: str
    price: float
    score: float
    triggered: bool

    # Детали по каждому признаку
    volume_spike: bool = False
    volume_spike_x: float = 0.0       # во сколько раз объём выше нормы

    price_pump: bool = False
    price_pump_pct: float = 0.0       # % роста за окно

    cvd_divergence: bool = False
    cvd_delta_norm: float = 0.0       # нормализованный CVD delta

    details: List[str] = field(default_factory=list)

    # Futures confirmation (заполняется из scanner)
    has_futures_confirm: bool = False
    oi_change_pct: float = 0.0
    oi_usdt: float = 0.0
    funding_rate: Optional[float] = None

    def short_summary(self) -> str:
        parts = []
        if self.volume_spike:
            parts.append(f"📦 Vol ×{self.volume_spike_x:.1f}")
        if self.price_pump:
            parts.append(f"🚀 +{self.price_pump_pct:.1f}%")
        if self.cvd_divergence:
            parts.append(f"📉 CVD {self.cvd_delta_norm:+.2f}")
        return " | ".join(parts) if parts else "—"


class Analyzer:
    def __init__(self, config):
        self.cfg = config

    # ─────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────

    def analyze(
        self,
        symbol: str,
        klines: List[List],
        trades: List[Dict],
    ) -> Optional[SignalResult]:
        """
        Возвращает SignalResult если обнаружена манипуляция, иначе None.
        klines: [[open_time, open, high, low, close, volume, ...], ...]
        trades: [{price, qty, isBuyerMaker, ...}, ...]
        """
        if len(klines) < 10:
            return None

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        current_price = closes[-1]

        result = SignalResult(
            symbol=symbol,
            price=current_price,
            score=0.0,
            triggered=False,
        )

        # ── 1. Volume Spike ───────────────────────────────────
        vs_triggered, vs_score, vs_x = self._check_volume_spike(volumes)
        result.volume_spike = vs_triggered
        result.volume_spike_x = vs_x
        if vs_triggered:
            result.score += vs_score * self.cfg.WEIGHT_VOLUME_SPIKE
            result.details.append(f"Volume spike ×{vs_x:.1f}")

        # ── 2. Price Pump ─────────────────────────────────────
        pp_triggered, pp_score, pp_pct = self._check_price_pump(closes)
        result.price_pump = pp_triggered
        result.price_pump_pct = pp_pct
        if pp_triggered:
            result.score += pp_score * self.cfg.WEIGHT_PRICE_PUMP
            result.details.append(f"Price pump +{pp_pct:.1f}%")

        # ── 3. CVD Divergence ─────────────────────────────────
        cvd_triggered, cvd_score, cvd_norm = self._check_cvd_divergence(
            closes, trades
        )
        result.cvd_divergence = cvd_triggered
        result.cvd_delta_norm = cvd_norm
        if cvd_triggered:
            result.score += cvd_score * self.cfg.WEIGHT_CVD_DIVERGENCE
            result.details.append(f"CVD divergence {cvd_norm:+.2f}")

        result.triggered = result.score >= self.cfg.MIN_SIGNAL_SCORE
        return result if result.triggered else None

    # ─────────────────────────────────────────────────────────
    # PRIVATE — детекторы
    # ─────────────────────────────────────────────────────────

    def _check_volume_spike(
        self, volumes: List[float]
    ) -> Tuple[bool, float, float]:
        """
        Текущий объём (последняя свеча) vs среднее за предыдущие N-1 свечей.
        Возвращает (triggered, score, multiplier).
        """
        if len(volumes) < 5:
            return False, 0.0, 0.0

        current_vol = volumes[-1]
        baseline_vols = volumes[:-1]
        avg_vol = sum(baseline_vols) / len(baseline_vols)

        if avg_vol == 0:
            return False, 0.0, 0.0

        multiplier = current_vol / avg_vol

        if multiplier >= self.cfg.VOLUME_SPIKE_MULTIPLIER:
            # Нормализуем score: 5x = 1.0, 10x = 1.5, 20x = 2.0
            score = min(1.0 + (multiplier - self.cfg.VOLUME_SPIKE_MULTIPLIER) / 10, 2.0)
            return True, score, multiplier

        return False, 0.0, multiplier

    def _check_price_pump(
        self, closes: List[float]
    ) -> Tuple[bool, float, float]:
        """
        Рост цены за последние PRICE_PUMP_CANDLES свечей.
        Возвращает (triggered, score, pct_change).
        """
        n = self.cfg.PRICE_PUMP_CANDLES
        if len(closes) < n + 1:
            return False, 0.0, 0.0

        price_start = closes[-(n + 1)]
        price_now = closes[-1]

        if price_start == 0:
            return False, 0.0, 0.0

        pct = (price_now - price_start) / price_start * 100

        if pct >= self.cfg.PRICE_PUMP_THRESHOLD_PCT:
            # Нормализуем: 8% = 1.0, 20% = 1.5, 50%+ = 2.0
            score = min(1.0 + (pct - self.cfg.PRICE_PUMP_THRESHOLD_PCT) / 30, 2.0)
            return True, score, pct

        return False, 0.0, pct

    def _check_cvd_divergence(
        self,
        closes: List[float],
        trades: List[Dict],
    ) -> Tuple[bool, float, float]:
        """
        CVD Divergence:
          - Цена выросла на CVD_PRICE_RISE_PCT за последние N свечей
          - При этом нормализованный CVD delta < CVD_DIVERGENCE_THRESHOLD

        CVD delta = (buy_vol - sell_vol) / total_vol
        Если цена растёт, но продавцы доминируют → крупные разгружаются.

        Возвращает (triggered, score, cvd_delta_norm).
        """
        if not trades or len(closes) < 3:
            return False, 0.0, 0.0

        # Проверяем рост цены
        n = self.cfg.PRICE_PUMP_CANDLES
        price_start = closes[-(min(n + 1, len(closes)))]
        price_now = closes[-1]
        if price_start == 0:
            return False, 0.0, 0.0

        price_rise_pct = (price_now - price_start) / price_start * 100

        if price_rise_pct < self.cfg.CVD_PRICE_RISE_PCT:
            return False, 0.0, 0.0

        # Считаем CVD по недавним трейдам
        buy_vol = 0.0
        sell_vol = 0.0
        for t in trades:
            qty = float(t.get("qty", 0))
            is_buyer_maker = t.get("isBuyerMaker", False)
            # isBuyerMaker=True → тейкер продаёт (агрессивная продажа)
            if is_buyer_maker:
                sell_vol += qty
            else:
                buy_vol += qty

        total_vol = buy_vol + sell_vol
        if total_vol == 0:
            return False, 0.0, 0.0

        cvd_norm = (buy_vol - sell_vol) / total_vol  # от -1 до +1

        if cvd_norm <= self.cfg.CVD_DIVERGENCE_THRESHOLD:
            # Чем сильнее дивергенция — тем выше score
            score = min(1.0 + abs(cvd_norm) * 2, 2.0)
            return True, score, cvd_norm

        return False, 0.0, cvd_norm


# ─────────────────────────────────────────────────────────────
# FuturesAnalyzer — OI и Funding Rate детекторы
# ─────────────────────────────────────────────────────────────

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class FuturesSignal:
    symbol: str

    # OI Spike
    oi_spike: bool = False
    oi_change_pct: float = 0.0       # % изменения OI
    oi_usdt: float = 0.0

    # Funding Rate
    funding_anomaly: bool = False
    funding_rate: float = 0.0        # текущий funding rate

    # Итог
    score: float = 0.0
    details: list = None

    def __post_init__(self):
        if self.details is None:
            self.details = []

    def short_summary(self) -> str:
        parts = []
        if self.oi_spike:
            parts.append(f"OI +{self.oi_change_pct:.1f}%")
        if self.funding_anomaly:
            parts.append(f"FR {self.funding_rate:.4f}")
        return " | ".join(parts) if parts else "—"


class FuturesAnalyzer:
    """
    Анализирует OI и Funding Rate для подтверждения манипуляции.

    OI Spike при пампе:
      Если спотовая цена растёт + OI на фьюче резко растёт →
      крупные открывают позиции (скорее всего шорт против толпы).

    Funding Rate аномалия:
      Если funding уходит в сильный минус при росте цены →
      шортеры платят лонгерам, но всё равно держат шорт →
      они уверены в развороте.
    """

    # OI вырос на X% за последние данные
    OI_SPIKE_THRESHOLD_PCT = 15.0

    # Funding rate ниже этого значения при растущей цене = аномалия
    FUNDING_NEGATIVE_THRESHOLD = -0.0005   # -0.05%

    # Очень сильный негативный funding
    FUNDING_STRONG_THRESHOLD = -0.002      # -0.2%

    WEIGHT_OI = 1.2
    WEIGHT_FUNDING = 1.5

    def analyze_futures(
        self,
        symbol: str,
        current_price: float,
        prev_price: float,
        oi_current: Optional[float],
        oi_prev: Optional[float],
        funding_rate: Optional[float],
    ) -> Optional[FuturesSignal]:
        """
        current_price / prev_price — цена сейчас и N минут назад
        oi_current / oi_prev       — OI сейчас и в предыдущем цикле (из кэша)
        funding_rate               — текущий funding rate
        """
        sig = FuturesSignal(symbol=symbol)
        price_rising = current_price > prev_price * 1.02  # цена выросла хотя бы на 2%

        # ── OI Spike ──────────────────────────────────────────
        if oi_current and oi_prev and oi_prev > 0:
            oi_change = (oi_current - oi_prev) / oi_prev * 100
            if oi_change >= self.OI_SPIKE_THRESHOLD_PCT:
                sig.oi_spike = True
                sig.oi_change_pct = oi_change
                sig.oi_usdt = oi_current
                score = min(1.0 + (oi_change - self.OI_SPIKE_THRESHOLD_PCT) / 30, 2.0)
                sig.score += score * self.WEIGHT_OI
                sig.details.append(f"OI spike +{oi_change:.1f}%")

        # ── Funding Rate аномалия ─────────────────────────────
        if funding_rate is not None and price_rising:
            if funding_rate <= self.FUNDING_STRONG_THRESHOLD:
                sig.funding_anomaly = True
                sig.funding_rate = funding_rate
                sig.score += 2.0 * self.WEIGHT_FUNDING
                sig.details.append(f"Strong negative funding {funding_rate:.4f}")
            elif funding_rate <= self.FUNDING_NEGATIVE_THRESHOLD:
                sig.funding_anomaly = True
                sig.funding_rate = funding_rate
                sig.score += 1.0 * self.WEIGHT_FUNDING
                sig.details.append(f"Negative funding {funding_rate:.4f}")

        if sig.score > 0:
            return sig
        return None
