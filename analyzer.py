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
