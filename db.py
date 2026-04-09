"""
Database — SQLite через aiosqlite.
Таблицы: signals, subscribers
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiosqlite

from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str = "signals.db"):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT    NOT NULL,
                    price       REAL    NOT NULL,
                    score       REAL    NOT NULL,
                    vol_spike   INTEGER DEFAULT 0,
                    vol_spike_x REAL    DEFAULT 0,
                    price_pump  INTEGER DEFAULT 0,
                    pump_pct    REAL    DEFAULT 0,
                    cvd_div     INTEGER DEFAULT 0,
                    cvd_norm    REAL    DEFAULT 0,
                    created_at  TEXT    NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_symbol
                ON signals(symbol)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    added_at TEXT NOT NULL
                )
            """)
            await db.commit()
        logger.info("Database initialized")

    async def save_signal(self, sig) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                INSERT INTO signals
                (symbol, price, score, vol_spike, vol_spike_x,
                 price_pump, pump_pct, cvd_div, cvd_norm, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sig.symbol, sig.price, sig.score,
                int(sig.volume_spike), sig.volume_spike_x,
                int(sig.price_pump), sig.price_pump_pct,
                int(sig.cvd_divergence), sig.cvd_delta_norm,
                now,
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_last_signal_time(self, symbol: str) -> Optional[datetime]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT created_at FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol,)
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            return datetime.fromisoformat(row[0])
        return None

    async def get_signals_count_24h(self) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM signals WHERE created_at >= ?", (since,)
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_recent_signals(self, hours: int = 24) -> List[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT symbol, price, score, created_at
                   FROM signals WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT 50""",
                (since,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def add_subscriber(self, chat_id: int):
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id, added_at) VALUES (?, ?)",
                (chat_id, now)
            )
            await db.commit()

    async def get_subscribers(self) -> List[int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT chat_id FROM subscribers") as cursor:
                rows = await cursor.fetchall()
        return [r[0] for r in rows]
