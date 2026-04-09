"""
Database — SQLite через aiosqlite.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict

import aiosqlite

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
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id     INTEGER PRIMARY KEY,
                    approved    INTEGER DEFAULT 0,
                    full_name   TEXT    DEFAULT '',
                    username    TEXT    DEFAULT '',
                    added_at    TEXT    NOT NULL
                )
            """)
            # Миграции для старых БД
            for col, definition in [
                ("approved",  "INTEGER DEFAULT 0"),
                ("full_name", "TEXT DEFAULT ''"),
                ("username",  "TEXT DEFAULT ''"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE subscribers ADD COLUMN {col} {definition}")
                except Exception:
                    pass
            await db.commit()
        logger.info("Database initialized")

    # ── Signals ───────────────────────────────────────────────

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
        return datetime.fromisoformat(row[0]) if row else None

    async def get_signals_count_24h(self) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM signals WHERE created_at >= ?", (since,)
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_signals_for_symbol(self, symbol: str, since: "datetime") -> List[dict]:
        """История сигналов по монете за период — для PumpHistory."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT symbol, price, score, created_at
                   FROM signals WHERE symbol = ? AND created_at >= ?
                   ORDER BY created_at ASC""",
                (symbol, since.isoformat())
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def cleanup_old_signals(self, keep_days: int = 30) -> int:
        """Удаляет сигналы старше keep_days дней. Возвращает кол-во удалённых."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM signals WHERE created_at < ?", (cutoff,)
            )
            await db.execute("VACUUM")
            await db.commit()
            return cursor.rowcount

    async def get_db_stats(self) -> Dict:
        """Статистика БД для админа."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM signals") as c:
                total_signals = (await c.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM signals WHERE created_at >= ?",
                ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)
            ) as c:
                week_signals = (await c.fetchone())[0]
            async with db.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM signals"
            ) as c:
                row = await c.fetchone()
                oldest = row[0][:10] if row[0] else "—"
                newest = row[1][:10] if row[1] else "—"

        import os
        size_kb = os.path.getsize(self.path) // 1024 if os.path.exists(self.path) else 0

        return {
            "total_signals": total_signals,
            "week_signals": week_signals,
            "oldest": oldest,
            "newest": newest,
            "size_kb": size_kb,
        }

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

    # ── Subscribers / Auth ────────────────────────────────────

    async def add_subscriber(
        self,
        chat_id: int,
        approved: bool = False,
        full_name: str = "",
        username: str = "",
    ):
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO subscribers (chat_id, approved, full_name, username, added_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       approved  = excluded.approved,
                       full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END,
                       username  = CASE WHEN excluded.username  != '' THEN excluded.username  ELSE username  END""",
                (chat_id, int(approved), full_name, username, now)
            )
            await db.commit()

    async def remove_subscriber(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM subscribers WHERE chat_id = ?", (chat_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_subscriber(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT approved FROM subscribers WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return bool(row and row[0])

    async def get_subscribers(self) -> List[int]:
        """Только одобренные — для рассылки алертов."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT chat_id FROM subscribers WHERE approved = 1"
            ) as cursor:
                rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_all_subscribers_info(self) -> List[Dict]:
        """Все пользователи с именами и статусом — для /users."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT chat_id, approved, full_name, username, added_at
                   FROM subscribers
                   ORDER BY approved DESC, added_at ASC"""
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]
