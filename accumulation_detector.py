"""
AccumulationDetector — паттерн "ступенька".

Признаки фазы накопления (15 свечей до пампа):
  - Объём чуть выше нормы (1.5–3x) но не памп
  - Цена стоит в узком диапазоне (±1.5%)
  - CVD потихоньку растёт (покупки накапливаются)
  - Нет резких движений цены

Если накопление обнаружено до пампа — это ранний сигнал,
можно войти раньше толпы.
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACCUMULATION_WINDOW = 15   # свечей для анализа накопления


@dataclass
class AccumulationResult:
    symbol: str
    detected: bool
    score: float
    duration_candles: int       # сколько свечей идёт накопление
    price_range_pct: float      # диапазон цены в % (чем меньше — тем лучше)
    avg_volume_ratio: float     # средний объём / базовый объём
    cvd_trend: float            # нормализованный тренд CVD (+1 накопление, -1 распродажа)
    details: List[str]


class AccumulationDetector:
    # Объём в диапазоне 1.3x–4x (тихое накопление, не памп)
    VOL_MIN_RATIO = 1.3
    VOL_MAX_RATIO = 4.0

    # Цена в узком канале
    PRICE_RANGE_MAX_PCT = 2.0

    # CVD тренд должен быть положительным
    CVD_TREND_MIN = 0.1

    # Минимум свечей с признаками накопления
    MIN_ACCUMULATION_CANDLES = 8

    def detect(
        self,
        symbol: str,
        klines: List[List],
        trades: List[Dict],
    ) -> Optional[AccumulationResult]:
        """
        klines: последние 60 свечей
        trades: последние сделки для CVD
        Анализируем последние ACCUMULATION_WINDOW свечей.
        """
        if len(klines) < ACCUMULATION_WINDOW + 10:
            return None

        # Берём окно накопления (предпоследние 15 свечей, не самую последнюю)
        window = klines[-(ACCUMULATION_WINDOW + 1):-1]
        baseline = klines[:-(ACCUMULATION_WINDOW + 1)]

        closes  = [float(k[4]) for k in window]
        highs   = [float(k[2]) for k in window]
        lows    = [float(k[3]) for k in window]
        volumes = [float(k[5]) for k in window]

        baseline_vols = [float(k[5]) for k in baseline]
        if not baseline_vols:
            return None
        base_vol = sum(baseline_vols) / len(baseline_vols)
        if base_vol == 0:
            return None

        # ── 1. Ценовой диапазон ───────────────────────────────
        price_max = max(highs)
        price_min = min(lows)
        price_mid = (price_max + price_min) / 2
        if price_mid == 0:
            return None
        price_range_pct = (price_max - price_min) / price_mid * 100

        # ── 2. Объём — считаем свечи в нужном диапазоне ──────
        accum_candles = 0
        vol_ratios = []
        for v in volumes:
            ratio = v / base_vol
            vol_ratios.append(ratio)
            if self.VOL_MIN_RATIO <= ratio <= self.VOL_MAX_RATIO:
                accum_candles += 1

        avg_vol_ratio = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 0

        # ── 3. CVD тренд по трейдам ───────────────────────────
        cvd_trend = self._calc_cvd_trend(trades)

        # ── Оценка ────────────────────────────────────────────
        result = AccumulationResult(
            symbol=symbol,
            detected=False,
            score=0.0,
            duration_candles=accum_candles,
            price_range_pct=price_range_pct,
            avg_volume_ratio=avg_vol_ratio,
            cvd_trend=cvd_trend,
            details=[],
        )

        score = 0.0
        detected = True

        if price_range_pct > self.PRICE_RANGE_MAX_PCT:
            detected = False
        else:
            score += (self.PRICE_RANGE_MAX_PCT - price_range_pct) / self.PRICE_RANGE_MAX_PCT
            result.details.append(f"Цена в канале {price_range_pct:.2f}%")

        if accum_candles < self.MIN_ACCUMULATION_CANDLES:
            detected = False
        else:
            score += accum_candles / ACCUMULATION_WINDOW
            result.details.append(f"Накопление {accum_candles}/{ACCUMULATION_WINDOW} свечей")

        if cvd_trend < self.CVD_TREND_MIN:
            detected = False
        else:
            score += cvd_trend
            result.details.append(f"CVD тренд +{cvd_trend:.2f}")

        result.detected = detected
        result.score = round(score, 2)
        return result if detected else None

    def _calc_cvd_trend(self, trades: List[Dict]) -> float:
        """
        Считает нормализованный тренд CVD.
        Делим трейды на две половины и смотрим растёт ли CVD.
        """
        if len(trades) < 20:
            return 0.0

        def net_cvd(batch):
            buy = sum(float(t.get("qty", 0)) for t in batch if not t.get("isBuyerMaker"))
            sell = sum(float(t.get("qty", 0)) for t in batch if t.get("isBuyerMaker"))
            total = buy + sell
            return (buy - sell) / total if total > 0 else 0.0

        mid = len(trades) // 2
        cvd_early = net_cvd(trades[:mid])
        cvd_late  = net_cvd(trades[mid:])

        # Тренд: насколько вырос CVD от первой половины ко второй
        return round(cvd_late - cvd_early, 3)
