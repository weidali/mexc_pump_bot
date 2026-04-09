"""
Configuration — все настройки в одном месте.
Переменные среды имеют приоритет над дефолтами.
"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # ── Telegram ──────────────────────────────────────────────
    TELEGRAM_TOKEN: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
    )

    # ── MEXC API ──────────────────────────────────────────────
    MEXC_API_KEY: str = field(
        default_factory=lambda: os.environ.get("MEXC_API_KEY", "YOUR_MEXC_API_KEY")
    )
    MEXC_SECRET: str = field(
        default_factory=lambda: os.environ.get("MEXC_SECRET", "YOUR_MEXC_SECRET")
    )
    MEXC_BASE_URL: str = "https://api.mexc.com"

    # ── Сканирование ──────────────────────────────────────────
    # Сколько топ-монет по объёму сканировать
    TOP_N_SYMBOLS: int = 80

    # Интервал между полными циклами сканирования (секунды)
    SCAN_INTERVAL_SEC: int = 60

    # Таймфрейм свечей для анализа
    KLINE_INTERVAL: str = "1m"   # 1m, 5m, 15m
    KLINE_LIMIT: int = 60        # сколько свечей брать

    # ── Пороги детектирования ─────────────────────────────────

    # 1. Volume Spike: объём текущей свечи > AVG * этот коэффициент
    VOLUME_SPIKE_MULTIPLIER: float = 5.0

    # 2. Price Pump: рост цены за последние N свечей (%)
    PRICE_PUMP_CANDLES: int = 5       # окно свечей
    PRICE_PUMP_THRESHOLD_PCT: float = 8.0  # % роста

    # 3. CVD Divergence:
    #    цена выросла на X%, а CVD delta за тот же период < 0
    CVD_PRICE_RISE_PCT: float = 5.0
    CVD_DIVERGENCE_THRESHOLD: float = -0.1  # нормализованный CVD

    # ── Итоговый скоринг ──────────────────────────────────────
    # Минимальный суммарный балл для отправки сигнала
    MIN_SIGNAL_SCORE: float = 2.0

    # Веса каждого признака
    WEIGHT_VOLUME_SPIKE: float = 1.0
    WEIGHT_PRICE_PUMP: float = 1.0
    WEIGHT_CVD_DIVERGENCE: float = 1.5   # CVD — самый значимый признак

    # Cooldown: не слать сигнал по одной монете чаще, чем раз в N минут
    SIGNAL_COOLDOWN_MINUTES: int = 30

    # Минимальный объём монеты в USDT за 24ч (фильтр совсем мелких)
    MIN_VOLUME_USDT_24H: float = 500_000

    # ── База данных — очистка ────────────────────────────────
    # Хранить сигналы не дольше N дней
    DB_KEEP_DAYS: int = field(
        default_factory=lambda: int(os.environ.get("DB_KEEP_DAYS", "30"))
    )

    # ── Авторизация
    ADMIN_CHAT_ID: int = field(
        default_factory=lambda: int(os.environ.get("ADMIN_CHAT_ID", "0"))
    )

    # ── БД ────────────────────────────────────────────────────
    DB_PATH: str = "signals.db"